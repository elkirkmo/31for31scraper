import hmac
import os

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file

import supabase_store as store
from scraper import ScrapeError, is_justwatch_url, scrape_title, scrape_titles
from supabase_store import StoreError

load_dotenv()

OPENAPI_FILE = os.path.join(os.path.dirname(__file__), "openapi.yaml")

app = Flask(__name__)


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/openapi.yaml", methods=["GET"])
def openapi_spec():
    return send_file(OPENAPI_FILE, mimetype="application/yaml")


def _check_auth():
    api_key = os.environ.get("ADMIN_API_KEY")
    if not api_key:
        return jsonify({"error": "ADMIN_API_KEY is not configured on the server"}), 500
    if not hmac.compare_digest(request.headers.get("X-API-Key", ""), api_key):
        return jsonify({"error": "Unauthorized"}), 401
    return None


def _store_error_response(exc):
    # No status_code -> local problem (missing config): our fault, 500.
    # 409 -> a real conflict (e.g. the films unique(year, title)), surface it.
    # Anything else from Supabase -> treat as an upstream failure, 502.
    if exc.status_code is None:
        return jsonify({"error": str(exc)}), 500
    if exc.status_code == 409:
        return jsonify({"error": str(exc)}), 409
    return jsonify({"error": str(exc)}), 502


def _validate_film_fields(body, *, require_required):
    """Validate a films payload. With require_required, year/date/title must be
    present (create); without, only the fields that are present are checked
    (patch). Returns an error message string, or None if valid."""
    if require_required:
        if not isinstance(body.get("year"), int) or isinstance(body.get("year"), bool):
            return '"year" is required and must be an integer'
        if not isinstance(body.get("title"), str) or not body["title"].strip():
            return '"title" is required'
        if not isinstance(body.get("date"), str) or not body["date"].strip():
            return '"date" is required'
    else:
        if "year" in body and (not isinstance(body["year"], int) or isinstance(body["year"], bool)):
            return '"year" must be an integer'
        if "title" in body and (not isinstance(body["title"], str) or not body["title"].strip()):
            return '"title" must be a non-empty string'
        if "date" in body and (not isinstance(body["date"], str) or not body["date"].strip()):
            return '"date" must be a non-empty string'
    if "sort_order" in body and (not isinstance(body["sort_order"], int) or isinstance(body["sort_order"], bool)):
        return '"sort_order" must be an integer'
    # justwatch_url may be explicitly null (to clear an override); if set, it
    # must be a real JustWatch URL -- same SSRF guard scrape_title() enforces.
    jw = body.get("justwatch_url")
    if jw is not None and not is_justwatch_url(jw):
        return "justwatch_url must be an https URL on justwatch.com"
    return None


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/api/scrape", methods=["GET"])
def scrape():
    auth_error = _check_auth()
    if auth_error:
        return auth_error

    title_param = request.args.get("title")
    if not title_param:
        return jsonify({"error": '"title" query param is required'}), 400

    try:
        return jsonify(scrape_title(title_param, url=request.args.get("url")))
    except ScrapeError as exc:
        return jsonify({"title": title_param, "service": [], "error": str(exc)}), 404
    except requests.RequestException as exc:
        return jsonify({"title": title_param, "service": [], "error": str(exc)}), 502


@app.route("/api/scrape", methods=["POST"])
def scrape_batch():
    """Scrape a caller-supplied list of films, statelessly -- no film list stored here.

    Body: a JSON array of {"title": ..., "justwatch_url": ...} (justwatch_url
    optional). Returns one FilmResult per input film, in order. A single
    film failing (404, bad justwatch_url, network error, ...) doesn't fail
    the request -- scrape_titles() already reports that inline via the
    result's "error" field.
    """
    auth_error = _check_auth()
    if auth_error:
        return auth_error

    films = request.get_json(silent=True)
    if not isinstance(films, list) or not films:
        return jsonify({"error": "request body must be a non-empty JSON array of films"}), 400

    titles = []
    for film in films:
        if not isinstance(film, dict):
            return jsonify({"error": "each film must be a JSON object"}), 400
        title = film.get("title")
        if not isinstance(title, str) or not title.strip():
            return jsonify({"error": 'each film needs a non-empty "title"'}), 400
        titles.append((title.strip(), film.get("justwatch_url")))

    return jsonify(scrape_titles(titles))


@app.route("/films", methods=["GET"])
def list_films():
    """Public: the film catalogue, each film with its nested offers (services)."""
    year = request.args.get("year")
    if year is not None:
        try:
            year = int(year)
        except ValueError:
            return jsonify({"error": '"year" must be an integer'}), 400
    try:
        return jsonify(store.list_films(year=year))
    except StoreError as exc:
        return _store_error_response(exc)


@app.route("/films/<int:film_id>", methods=["GET"])
def get_film(film_id):
    """Public: one film with its nested offers."""
    try:
        film = store.get_film(film_id)
    except StoreError as exc:
        return _store_error_response(exc)
    if film is None:
        return jsonify({"error": "film {} not found".format(film_id)}), 404
    return jsonify(film)


@app.route("/films", methods=["POST"])
def create_film():
    """Admin: add a film. Offers are populated separately via refresh."""
    auth_error = _check_auth()
    if auth_error:
        return auth_error

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400
    error = _validate_film_fields(body, require_required=True)
    if error:
        return jsonify({"error": error}), 400

    try:
        return jsonify(store.create_film(body)), 201
    except StoreError as exc:
        return _store_error_response(exc)


@app.route("/films/<int:film_id>", methods=["PATCH"])
def update_film(film_id):
    """Admin: update a film's fields -- including justwatch_url, the "Override"."""
    auth_error = _check_auth()
    if auth_error:
        return auth_error

    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not body:
        return jsonify({"error": "request body must be a non-empty JSON object"}), 400
    if not any(f in body for f in store.FILM_WRITABLE_FIELDS):
        return jsonify({"error": "no updatable fields in request body"}), 400
    error = _validate_film_fields(body, require_required=False)
    if error:
        return jsonify({"error": error}), 400

    try:
        film = store.update_film(film_id, body)
    except StoreError as exc:
        return _store_error_response(exc)
    if film is None:
        return jsonify({"error": "film {} not found".format(film_id)}), 404
    return jsonify(film)


@app.route("/films/<int:film_id>", methods=["DELETE"])
def delete_film(film_id):
    """Admin: delete a film (its services cascade)."""
    auth_error = _check_auth()
    if auth_error:
        return auth_error

    try:
        deleted = store.delete_film(film_id)
    except StoreError as exc:
        return _store_error_response(exc)
    if not deleted:
        return jsonify({"error": "film {} not found".format(film_id)}), 404
    return "", 204


def _refresh_film(film):
    """Scrape one film and replace its services -- only on a successful scrape.

    Returns (summary_dict, ok). On a scrape/network failure the film's existing
    offers are left untouched (never wiped on a transient upstream failure).
    """
    try:
        result = scrape_title(film["title"], url=film.get("justwatch_url"))
    except (ScrapeError, requests.RequestException) as exc:
        return {"film_id": film["id"], "title": film["title"],
                "error": str(exc), "refreshed": False}, False
    count = store.replace_services(film["id"], result["service"])
    store.update_film(film["id"], {})  # bump updated_at as "last refreshed"
    return {"film_id": film["id"], "title": film["title"], "url": result["url"],
            "services_updated": count, "refreshed": True}, True


@app.route("/films/<int:film_id>/refresh", methods=["POST"])
def refresh_film(film_id):
    """Admin: re-scrape one film's JustWatch page and replace its offers.

    Uses the film's stored justwatch_url override if set, else the guessed
    slug. A scrape failure returns 502 and leaves the existing offers intact.
    """
    auth_error = _check_auth()
    if auth_error:
        return auth_error

    try:
        film = store.get_film(film_id)
        if film is None:
            return jsonify({"error": "film {} not found".format(film_id)}), 404
        summary, ok = _refresh_film(film)
    except StoreError as exc:
        return _store_error_response(exc)
    if not ok:
        summary["note"] = "existing offers left unchanged"
        return jsonify(summary), 502
    return jsonify(summary)


@app.route("/films/refresh", methods=["POST"])
def refresh_all_films():
    """Admin: re-scrape every film and refresh its offers (the periodic job).

    One film's scrape failure doesn't abort the batch or wipe that film's
    offers; it's reported inline with refreshed=false, like the batch scrape.
    """
    auth_error = _check_auth()
    if auth_error:
        return auth_error

    try:
        films = store.list_films()
        pairs = [(f["title"], f.get("justwatch_url")) for f in films]
        results = scrape_titles(pairs)
        summary = []
        for film, result in zip(films, results):
            if "error" in result:
                summary.append({"film_id": film["id"], "title": film["title"],
                                "error": result["error"], "refreshed": False})
                continue
            count = store.replace_services(film["id"], result["service"])
            store.update_film(film["id"], {})
            summary.append({"film_id": film["id"], "title": film["title"],
                            "services_updated": count, "refreshed": True})
    except StoreError as exc:
        return _store_error_response(exc)

    return jsonify({
        "films": len(films),
        "refreshed": sum(1 for s in summary if s["refreshed"]),
        "results": summary,
    })


if __name__ == "__main__":
    app.run(debug=True)
