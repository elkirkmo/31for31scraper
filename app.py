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


def _check_auth(allow_read_key=False):
    """Authorise a request by its X-API-Key header. Returns an error response,
    or None if the caller may proceed.

    Two tiers, so that read access can eventually be granted to someone
    without also handing them the ability to delete the catalogue:

      ADMIN_API_KEY  -- everything, including writes and refreshes.
      READ_API_KEY   -- the read endpoints only (allow_read_key=True), and
                        only if the server has one configured.

    READ_API_KEY is deliberately optional and unset by default: with no read
    key, reads accept the admin key alone, which is today's behaviour and
    means no third party can read anything. The tier exists so that opening
    that door later is a config change rather than a redesign. Note that a
    browser-based third-party app would also need CORS headers, which this
    service deliberately does not send.
    """
    admin_key = os.environ.get("ADMIN_API_KEY")
    if not admin_key:
        return jsonify({"error": "ADMIN_API_KEY is not configured on the server"}), 500

    presented = request.headers.get("X-API-Key", "")
    if hmac.compare_digest(presented, admin_key):
        return None

    if allow_read_key:
        read_key = os.environ.get("READ_API_KEY")
        # compare_digest only on a configured key -- an unset READ_API_KEY
        # must never match an absent header.
        if read_key and hmac.compare_digest(presented, read_key):
            return None

    return jsonify({"error": "Unauthorized"}), 401


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
    """The film catalogue, each film with its nested offers (services).

    Keyed, but at the read tier: accepts READ_API_KEY as well as the admin
    key, so a consumer can be given the catalogue without being given the
    ability to change it. See _check_auth.
    """
    auth_error = _check_auth(allow_read_key=True)
    if auth_error:
        return auth_error

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
    """One film with its nested offers. Read tier, as GET /films is."""
    auth_error = _check_auth(allow_read_key=True)
    if auth_error:
        return auth_error

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


def _scrape_failure_entry(film, result):
    """One film's inline failure record in a batch refresh summary."""
    return {"film_id": film["id"], "title": film["title"],
            "error": result["error"], "refreshed": False}


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

        # Scrape everything and decide whether the run is trustworthy BEFORE
        # writing anything -- the guard below is worthless if half the
        # catalogue has already been overwritten by the time it runs.
        scraped = [(film, result["service"])
                   for film, result in zip(films, results) if "error" not in result]

        # A film here and there genuinely streams nowhere. Every single film
        # streaming nowhere is a broken scrape, not a catalogue that emptied
        # overnight -- and applying it would erase the one thing the site is
        # for. Refuse the whole run rather than write a single row.
        if scraped and not any(offers for _, offers in scraped):
            return jsonify({
                "films": len(films),
                "refreshed": 0,
                "error": "Refused: the scrape found no streaming offers for any of the {} "
                         "films it could read, which means the scrape failed rather than "
                         "every film leaving every service. Nothing was changed.".format(len(scraped)),
                # The films that failed outright are the diagnostic an admin
                # needs at exactly this moment, so don't swallow them.
                "results": [_scrape_failure_entry(f, r)
                            for f, r in zip(films, results) if "error" in r],
            }), 502

        summary = []
        for film, result in zip(films, results):
            if "error" in result:
                summary.append(_scrape_failure_entry(film, result))
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
