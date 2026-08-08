import json
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from scraper import ScrapeError, scrape_title, scrape_titles

load_dotenv()

DATA_FILE = os.path.join(os.path.dirname(__file__), "data.json")

app = Flask(__name__)


def _check_auth():
    api_key = os.environ.get("ADMIN_API_KEY")
    if not api_key:
        return jsonify({"error": "ADMIN_API_KEY is not configured on the server"}), 500
    if request.headers.get("X-API-Key") != api_key:
        return jsonify({"error": "Unauthorized"}), 401
    return None


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

    with open(DATA_FILE) as f:
        data = json.load(f)

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


if __name__ == "__main__":
    app.run(debug=True)
