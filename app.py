import hmac
import json
import os
import random
import re

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file

from scraper import ScrapeError, is_justwatch_url, scrape_title, scrape_titles

load_dotenv()

DATA_FILE = os.path.join(os.path.dirname(__file__), "data.json")
OPENAPI_FILE = os.path.join(os.path.dirname(__file__), "openapi.yaml")

YEAR_PATTERN = re.compile(r"^\d{4}$")
DATE_PATTERN = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")

app = Flask(__name__)


def _load_data():
    with open(DATA_FILE) as f:
        return json.load(f)


def _save_data(data):
    """Persist data.json.

    Only works where the filesystem is writable -- local dev, not Vercel's
    read-only production filesystem. Raises OSError there; callers must
    turn that into a clean error response rather than let it crash. This
    is the one function a future Supabase migration needs to replace.
    """
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)
        f.write("\n")


def _validate_and_build_entry(film, year, used_days):
    """Validate one film dict and assign it a unique id for `year`.

    `used_days` is the set of day-of-month values (real or overflow)
    already spoken for in this year -- mutated in place as ids are
    assigned, so callers can validate/build a whole list of films in
    sequence without colliding with each other.

    Returns (entry, None) on success, or (None, error_message) on failure.
    """
    if not isinstance(film, dict):
        return None, "each film must be a JSON object"

    title = film.get("title")
    if not isinstance(title, str) or not title.strip():
        return None, 'each film needs a non-empty "title"'
    title = title.strip()

    date = film.get("date") or ""
    if date:
        m = DATE_PATTERN.match(date)
        if not m or int(m.group(3)) != year:
            return None, 'invalid "date" for {!r}: expected M/D/{} (or omit date entirely)'.format(title, year)
        day = int(m.group(2))
        if not 1 <= day <= 31:
            return None, 'invalid "date" for {!r}: day must be 1-31'.format(title)
        if day in used_days:
            return None, "duplicate date {} for {!r}".format(date, title)
        used_days.add(day)
        film_id = year * 100 + day
    else:
        # No real date -- assign an id that can't be mistaken for a
        # calendar day, same as the one existing dateless entry
        # (Terrifier 3, 2024) got when ids were first added.
        day = random.randint(32, 99)
        while day in used_days:
            day = random.randint(32, 99)
        used_days.add(day)
        film_id = year * 100 + day

    entry = {"id": film_id, "date": date, "title": title}
    justwatch_url = film.get("justwatch_url")
    if justwatch_url:
        if not is_justwatch_url(justwatch_url):
            return None, 'invalid "justwatch_url" for {!r}: must be an https URL on justwatch.com'.format(title)
        entry["justwatch_url"] = justwatch_url
    entry["service"] = []
    return entry, None


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
    if title_param:
        try:
            return jsonify(scrape_title(title_param, url=request.args.get("url")))
        except ScrapeError as exc:
            return jsonify({"title": title_param, "service": [], "error": str(exc)}), 404
        except requests.RequestException as exc:
            return jsonify({"title": title_param, "service": [], "error": str(exc)}), 502

    data = _load_data()

    output = {}
    for year, entries in data.items():
        if not isinstance(entries, list) or not entries or "title" not in entries[0]:
            output[year] = entries
            continue

        scraped_by_title = {
            result["title"]: result
            for result in scrape_titles(
                [(entry["title"], entry.get("justwatch_url")) for entry in entries]
            )
        }

        year_output = []
        for entry in entries:
            result = scraped_by_title[entry["title"]]
            updated_entry = dict(entry, service=result["service"])
            if "error" in result:
                updated_entry["error"] = result["error"]
            year_output.append(updated_entry)
        output[year] = year_output

    return jsonify(output)


def _write_or_500(data):
    """Attempt to persist data.json, returning a Flask error response on failure."""
    try:
        _save_data(data)
        return None
    except OSError as exc:
        return jsonify({
            "error": (
                "Could not write data.json: {}. This endpoint needs a writable "
                "filesystem -- it works in local dev, but not on Vercel's "
                "read-only production filesystem (this is exactly the gap the "
                "eventual Supabase migration closes)."
            ).format(exc)
        }), 500


@app.route("/api/years/<year>", methods=["PUT"])
def replace_year(year):
    """Replace a year's entire film list. Idempotent: PUT the same body twice, same result."""
    auth_error = _check_auth()
    if auth_error:
        return auth_error
    if not YEAR_PATTERN.match(year):
        return jsonify({"error": "year must be a 4-digit number"}), 400

    films = request.get_json(silent=True)
    if not isinstance(films, list):
        return jsonify({"error": "request body must be a JSON array of films"}), 400

    used_days = set()
    entries = []
    for film in films:
        entry, err = _validate_and_build_entry(film, int(year), used_days)
        if err:
            return jsonify({"error": err}), 400
        entries.append(entry)

    data = _load_data()
    data[year] = entries
    write_error = _write_or_500(data)
    if write_error:
        return write_error

    return jsonify(entries)


@app.route("/api/years/<year>", methods=["POST"])
def add_film(year):
    """Append a single new film to a year's list (creating the year if needed)."""
    auth_error = _check_auth()
    if auth_error:
        return auth_error
    if not YEAR_PATTERN.match(year):
        return jsonify({"error": "year must be a 4-digit number"}), 400

    film = request.get_json(silent=True)
    if not isinstance(film, dict):
        return jsonify({"error": "request body must be a JSON film object"}), 400

    data = _load_data()
    existing = data.get(year, [])
    used_days = {entry["id"] % 100 for entry in existing if isinstance(entry.get("id"), int)}

    entry, err = _validate_and_build_entry(film, int(year), used_days)
    if err:
        return jsonify({"error": err}), 400

    existing.append(entry)
    data[year] = existing
    write_error = _write_or_500(data)
    if write_error:
        return write_error

    return jsonify(entry), 201


if __name__ == "__main__":
    app.run(debug=True)
