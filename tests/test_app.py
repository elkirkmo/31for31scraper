import requests
import responses

from helpers import load_fixture

JUSTWATCH_BASE = "https://www.justwatch.com/us/movie/"


class TestAuth:
    def test_missing_api_key_header_returns_401(self, client, api_key):
        resp = client.get("/api/scrape")
        assert resp.status_code == 401
        assert resp.get_json() == {"error": "Unauthorized"}

    def test_wrong_api_key_returns_401(self, client, api_key):
        resp = client.get("/api/scrape", headers={"X-API-Key": "wrong"})
        assert resp.status_code == 401

    def test_empty_api_key_header_returns_401(self, client, api_key):
        resp = client.get("/api/scrape", headers={"X-API-Key": ""})
        assert resp.status_code == 401

    def test_header_name_is_case_insensitive(self, client, api_key, data_file):
        data_file({})
        resp = client.get("/api/scrape", headers={"x-api-key": api_key})
        assert resp.status_code == 200

    def test_no_admin_api_key_configured_on_server_returns_500(self, client, monkeypatch):
        monkeypatch.delenv("ADMIN_API_KEY", raising=False)
        resp = client.get("/api/scrape", headers={"X-API-Key": "anything"})
        assert resp.status_code == 500
        assert "ADMIN_API_KEY" in resp.get_json()["error"]

    def test_post_method_not_allowed_returns_405(self, client, api_key):
        resp = client.post("/api/scrape", headers={"X-API-Key": api_key})
        assert resp.status_code == 405


class TestSingleTitle:
    @responses.activate
    def test_success_returns_200_with_offers(self, client, auth_headers):
        url = JUSTWATCH_BASE + "the-thing-from-another-world"
        responses.add(
            responses.GET, url,
            body=load_fixture("the_thing_from_another_world.html"), status=200,
        )

        resp = client.get("/api/scrape?title=The+Thing+From+Another+World", headers=auth_headers)

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["title"] == "The Thing From Another World"
        assert len(body["service"]) == 11

    @responses.activate
    def test_not_found_returns_404_with_error_body(self, client, auth_headers):
        url = JUSTWATCH_BASE + "not-a-real-movie"
        responses.add(responses.GET, url, status=404)

        resp = client.get("/api/scrape?title=Not+A+Real+Movie", headers=auth_headers)

        assert resp.status_code == 404
        body = resp.get_json()
        assert body["title"] == "Not A Real Movie"
        assert body["service"] == []
        assert "error" in body

    @responses.activate
    def test_explicit_url_param_is_used_instead_of_guessed_slug(self, client, auth_headers):
        url = JUSTWATCH_BASE + "the-thing-from-another-world"
        responses.add(
            responses.GET, url,
            body=load_fixture("the_thing_from_another_world.html"), status=200,
        )

        resp = client.get(
            "/api/scrape",
            query_string={"title": "Event Horizon", "url": url},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert resp.get_json()["url"] == url

    @responses.activate
    def test_upstream_network_error_returns_502(self, client, auth_headers):
        url = JUSTWATCH_BASE + "blood-feast"
        responses.add(responses.GET, url, body=requests.exceptions.ConnectTimeout("boom"))

        resp = client.get("/api/scrape?title=Blood+Feast", headers=auth_headers)

        assert resp.status_code == 502
        body = resp.get_json()
        assert body["service"] == []
        assert "error" in body


class TestFullScrape:
    @responses.activate
    def test_success_replaces_service_and_preserves_other_fields(self, client, auth_headers, data_file):
        data_file({
            "2025": [
                {
                    "date": "10/1/2025",
                    "title": "The Thing From Another World",
                    "service": [{"name": "stale", "link": "stale"}],
                },
            ],
            "textContent": {"heading": "31 for 31"},
        })
        responses.add(
            responses.GET, JUSTWATCH_BASE + "the-thing-from-another-world",
            body=load_fixture("the_thing_from_another_world.html"), status=200,
        )

        resp = client.get("/api/scrape", headers=auth_headers)

        assert resp.status_code == 200
        body = resp.get_json()
        entry = body["2025"][0]
        assert entry["date"] == "10/1/2025"
        assert entry["title"] == "The Thing From Another World"
        assert len(entry["service"]) == 11
        assert entry["service"][0] != {"name": "stale", "link": "stale"}
        assert "error" not in entry
        assert body["textContent"] == {"heading": "31 for 31"}

    @responses.activate
    def test_uses_justwatch_url_override_when_present(self, client, auth_headers, data_file):
        data_file({
            "2025": [
                {
                    "date": "10/26/2025",
                    "title": "Event Horizon",
                    "justwatch_url": JUSTWATCH_BASE + "the-thing-from-another-world",
                    "service": [],
                },
            ],
        })
        responses.add(
            responses.GET, JUSTWATCH_BASE + "the-thing-from-another-world",
            body=load_fixture("the_thing_from_another_world.html"), status=200,
        )
        # Note: no mock registered for the guessed slug ("event-horizon") --
        # if the override weren't honored, that request would hit an
        # unregistered URL and the entry would come back with an "error"
        # instead of 11 offers, failing the assertion below.

        resp = client.get("/api/scrape", headers=auth_headers)

        assert resp.status_code == 200
        entry = resp.get_json()["2025"][0]
        assert len(entry["service"]) == 11

    @responses.activate
    def test_per_title_failure_is_reported_without_failing_the_request(self, client, auth_headers, data_file):
        data_file({
            "2025": [
                {"date": "10/1/2025", "title": "The Thing From Another World", "service": []},
                {"date": "10/2/2025", "title": "Not A Real Movie", "service": []},
            ],
        })
        responses.add(
            responses.GET, JUSTWATCH_BASE + "the-thing-from-another-world",
            body=load_fixture("the_thing_from_another_world.html"), status=200,
        )
        responses.add(responses.GET, JUSTWATCH_BASE + "not-a-real-movie", status=404)

        resp = client.get("/api/scrape", headers=auth_headers)

        assert resp.status_code == 200
        body = resp.get_json()
        assert "error" not in body["2025"][0]
        assert "error" in body["2025"][1]
        assert body["2025"][1]["service"] == []

    def test_empty_year_list_passes_through_unchanged(self, client, auth_headers, data_file):
        data_file({"2026": [], "textContent": {"heading": "x"}})

        resp = client.get("/api/scrape", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.get_json()["2026"] == []

    def test_entries_without_a_title_key_pass_through_unchanged(self, client, auth_headers, data_file):
        # Guards against a KeyError if data.json ever has an unexpected shape.
        data_file({"weird": [{"not_a_title_field": "x"}]})

        resp = client.get("/api/scrape", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.get_json()["weird"] == [{"not_a_title_field": "x"}]

    def test_non_list_non_dict_value_passes_through_unchanged(self, client, auth_headers, data_file):
        data_file({"someFlag": True})

        resp = client.get("/api/scrape", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.get_json()["someFlag"] is True
