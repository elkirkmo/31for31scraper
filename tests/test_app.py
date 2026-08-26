import requests
import responses

from helpers import load_fixture

JUSTWATCH_BASE = "https://www.justwatch.com/us/movie/"


class TestHealth:
    def test_returns_200_without_auth(self, client):
        # Deliberately no api_key/auth_headers fixture -- health checks
        # need to work for monitoring tools that don't have the admin key.
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.get_json() == {"status": "ok"}


class TestSwaggerUI:
    def test_index_serves_swagger_ui_without_auth(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/html")
        assert b"swagger-ui" in resp.data

    def test_index_points_swagger_ui_at_the_spec_route(self, client):
        resp = client.get("/")
        assert b'"/openapi.yaml"' in resp.data

    def test_openapi_yaml_served_without_auth(self, client):
        resp = client.get("/openapi.yaml")
        assert resp.status_code == 200
        assert resp.content_type == "application/yaml"
        assert resp.data.startswith(b"openapi: 3.0.3")


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

    def test_header_name_is_case_insensitive(self, client, api_key):
        # No title param -- expecting 400 (not 401) proves the
        # lowercase header was accepted and auth passed.
        resp = client.get("/api/scrape", headers={"x-api-key": api_key})
        assert resp.status_code == 400

    def test_no_admin_api_key_configured_on_server_returns_500(self, client, monkeypatch):
        monkeypatch.delenv("ADMIN_API_KEY", raising=False)
        resp = client.get("/api/scrape", headers={"X-API-Key": "anything"})
        assert resp.status_code == 500
        assert "ADMIN_API_KEY" in resp.get_json()["error"]

    def test_delete_method_not_allowed_returns_405(self, client, api_key):
        # GET (single-title) and POST (caller-supplied list) are both
        # valid on /api/scrape; DELETE isn't a thing this endpoint does.
        resp = client.delete("/api/scrape", headers={"X-API-Key": api_key})
        assert resp.status_code == 405


class TestSingleTitle:
    def test_missing_title_returns_400(self, client, auth_headers):
        # No batch mode anymore -- title is required.
        resp = client.get("/api/scrape", headers=auth_headers)
        assert resp.status_code == 400
        assert "title" in resp.get_json()["error"]

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

    def test_url_param_pointing_off_justwatch_is_rejected(self, client, auth_headers):
        # SSRF guard: no responses.activate here on purpose -- if this
        # weren't rejected before the request went out, the test would
        # either hang/fail on a real connection attempt to example.com.
        resp = client.get(
            "/api/scrape",
            query_string={"title": "Anything", "url": "https://example.com"},
            headers=auth_headers,
        )
        assert resp.status_code == 404
        assert "Refusing to scrape non-JustWatch URL" in resp.get_json()["error"]

    @responses.activate
    def test_upstream_network_error_returns_502(self, client, auth_headers):
        url = JUSTWATCH_BASE + "blood-feast"
        responses.add(responses.GET, url, body=requests.exceptions.ConnectTimeout("boom"))

        resp = client.get("/api/scrape?title=Blood+Feast", headers=auth_headers)

        assert resp.status_code == 502
        body = resp.get_json()
        assert body["service"] == []
        assert "error" in body


class TestScrapeBatchPost:
    def test_missing_auth_returns_401(self, client, api_key):
        resp = client.post("/api/scrape", json=[{"title": "X"}])
        assert resp.status_code == 401

    def test_non_list_body_returns_400(self, client, auth_headers):
        resp = client.post("/api/scrape", json={"title": "X"}, headers=auth_headers)
        assert resp.status_code == 400

    def test_empty_list_returns_400(self, client, auth_headers):
        resp = client.post("/api/scrape", json=[], headers=auth_headers)
        assert resp.status_code == 400

    def test_malformed_json_body_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/scrape",
            data="{not valid json",
            headers={**auth_headers, "Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_array_item_that_is_not_an_object_returns_400(self, client, auth_headers):
        resp = client.post("/api/scrape", json=["not an object"], headers=auth_headers)
        assert resp.status_code == 400
        assert "must be a JSON object" in resp.get_json()["error"]

    def test_item_missing_title_returns_400(self, client, auth_headers):
        resp = client.post("/api/scrape", json=[{"justwatch_url": "https://www.justwatch.com/us/movie/x"}], headers=auth_headers)
        assert resp.status_code == 400
        assert "title" in resp.get_json()["error"]

    @responses.activate
    def test_success_returns_one_film_result_per_input_in_order(self, client, auth_headers):
        responses.add(
            responses.GET, JUSTWATCH_BASE + "the-thing-from-another-world",
            body=load_fixture("the_thing_from_another_world.html"), status=200,
        )
        responses.add(responses.GET, JUSTWATCH_BASE + "not-a-real-movie", status=404)

        resp = client.post(
            "/api/scrape",
            json=[
                {"title": "The Thing From Another World"},
                {"title": "Not A Real Movie"},
            ],
            headers=auth_headers,
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert [f["title"] for f in body] == ["The Thing From Another World", "Not A Real Movie"]
        assert len(body[0]["service"]) == 11
        assert "error" not in body[0]
        assert body[1]["service"] == []
        assert "error" in body[1]

    @responses.activate
    def test_justwatch_url_override_is_used_and_validated_like_the_url_param(self, client, auth_headers):
        # Points somewhere other than what the title would guess -- proves
        # the override is honored, same mechanic as ?title=&url=.
        responses.add(
            responses.GET, JUSTWATCH_BASE + "the-thing-from-another-world",
            body=load_fixture("the_thing_from_another_world.html"), status=200,
        )

        resp = client.post(
            "/api/scrape",
            json=[{"title": "Some Other Title", "justwatch_url": JUSTWATCH_BASE + "the-thing-from-another-world"}],
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert len(resp.get_json()[0]["service"]) == 11

    def test_justwatch_url_pointing_off_justwatch_becomes_a_per_film_error_not_a_400(self, client, auth_headers):
        # SSRF guard reused from scrape_title() -- a bad justwatch_url
        # here fails that one film, not the whole batch, consistent with
        # every other per-film scrape failure.
        resp = client.post(
            "/api/scrape",
            json=[{"title": "Evil", "justwatch_url": "https://example.com"}],
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.get_json()[0]
        assert body["service"] == []
        assert "Refusing to scrape non-JustWatch URL" in body["error"]
