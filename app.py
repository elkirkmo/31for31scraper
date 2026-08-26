import hmac
import os

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file

from scraper import ScrapeError, scrape_title, scrape_titles

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


if __name__ == "__main__":
    app.run(debug=True)
