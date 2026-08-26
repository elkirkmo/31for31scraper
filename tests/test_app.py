import json

import requests
import responses

import app as app_module
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

    def test_header_name_is_case_insensitive(self, client, api_key, data_file):
        data_file({})
        resp = client.get("/api/scrape", headers={"x-api-key": api_key})
        assert resp.status_code == 200

    def test_no_admin_api_key_configured_on_server_returns_500(self, client, monkeypatch):
        monkeypatch.delenv("ADMIN_API_KEY", raising=False)
        resp = client.get("/api/scrape", headers={"X-API-Key": "anything"})
        assert resp.status_code == 500
        assert "ADMIN_API_KEY" in resp.get_json()["error"]

    def test_delete_method_not_allowed_returns_405(self, client, api_key):
        # GET (batch/single-title) and POST (caller-supplied list) are both
        # valid on /api/scrape; DELETE isn't a thing this endpoint does.
        resp = client.delete("/api/scrape", headers={"X-API-Key": api_key})
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

    def test_does_not_touch_data_json(self, client, auth_headers, data_file):
        # Stateless: this endpoint never reads or writes data.json at all.
        path = data_file({"2025": [{"id": 1, "title": "Untouched", "service": []}]})
        before = path.read_text()

        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(responses.GET, JUSTWATCH_BASE + "not-a-real-movie", status=404)
            client.post("/api/scrape", json=[{"title": "Not A Real Movie"}], headers=auth_headers)

        assert path.read_text() == before


class TestPutYear:
    def test_missing_auth_returns_401(self, client, api_key):
        resp = client.put("/api/years/2026", json=[])
        assert resp.status_code == 401

    def test_non_four_digit_year_returns_400(self, client, auth_headers):
        resp = client.put("/api/years/26", json=[], headers=auth_headers)
        assert resp.status_code == 400

    def test_non_list_body_returns_400(self, client, auth_headers, data_file):
        data_file({})
        resp = client.put("/api/years/2026", json={"not": "a list"}, headers=auth_headers)
        assert resp.status_code == 400

    def test_item_missing_title_returns_400(self, client, auth_headers, data_file):
        data_file({})
        resp = client.put("/api/years/2026", json=[{"date": "10/1/2026"}], headers=auth_headers)
        assert resp.status_code == 400
        assert "title" in resp.get_json()["error"]

    def test_array_item_that_is_not_an_object_returns_400(self, client, auth_headers, data_file):
        path = data_file({})
        resp = client.put(
            "/api/years/2026",
            json=["not an object", {"title": "Valid Film", "date": "10/1/2026"}],
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "must be a JSON object" in resp.get_json()["error"]
        # Nothing written -- the whole request is validated before any write.
        assert json.loads(path.read_text()) == {}

    def test_title_with_wrong_type_returns_400(self, client, auth_headers, data_file):
        data_file({})
        resp = client.put("/api/years/2026", json=[{"title": 123}], headers=auth_headers)
        assert resp.status_code == 400

    def test_invalid_date_format_returns_400(self, client, auth_headers, data_file):
        data_file({})
        resp = client.put(
            "/api/years/2026",
            json=[{"title": "X", "date": "October 1st"}],
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_justwatch_url_pointing_off_justwatch_returns_400(self, client, auth_headers, data_file):
        # SSRF guard, enforced at write time too so a bad justwatch_url
        # is caught immediately rather than only failing later when
        # /api/scrape happens to run.
        path = data_file({})
        resp = client.put(
            "/api/years/2026",
            json=[{"title": "X", "date": "10/1/2026", "justwatch_url": "https://example.com"}],
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "justwatch_url" in resp.get_json()["error"]
        assert json.loads(path.read_text()) == {}

    def test_justwatch_url_on_the_right_host_is_accepted(self, client, auth_headers, data_file):
        data_file({})
        resp = client.put(
            "/api/years/2026",
            json=[{
                "title": "X",
                "date": "10/1/2026",
                "justwatch_url": "https://www.justwatch.com/us/movie/x",
            }],
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()[0]["justwatch_url"] == "https://www.justwatch.com/us/movie/x"

    def test_date_year_mismatch_returns_400(self, client, auth_headers, data_file):
        data_file({})
        resp = client.put(
            "/api/years/2026",
            json=[{"title": "X", "date": "10/1/2025"}],
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_day_out_of_range_returns_400(self, client, auth_headers, data_file):
        data_file({})
        resp = client.put(
            "/api/years/2026",
            json=[{"title": "X", "date": "10/35/2026"}],
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_duplicate_date_within_payload_returns_400(self, client, auth_headers, data_file):
        path = data_file({})
        resp = client.put(
            "/api/years/2026",
            json=[
                {"title": "First", "date": "10/1/2026"},
                {"title": "Second", "date": "10/1/2026"},
            ],
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "duplicate" in resp.get_json()["error"].lower()
        # Nothing should have been written on a validation failure.
        assert json.loads(path.read_text()) == {}

    def test_success_computes_ids_and_persists(self, client, auth_headers, data_file):
        path = data_file({"2025": [{"id": 202501, "date": "10/1/2025", "title": "Old", "service": []}]})

        resp = client.put(
            "/api/years/2026",
            json=[
                {"title": "Film One", "date": "10/1/2026"},
                {"title": "Film Two", "date": "10/2/2026", "justwatch_url": "https://www.justwatch.com/us/movie/film-two"},
            ],
            headers=auth_headers,
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body[0] == {"id": 202601, "date": "10/1/2026", "title": "Film One", "service": []}
        assert body[1]["id"] == 202602
        assert body[1]["justwatch_url"] == "https://www.justwatch.com/us/movie/film-two"

        on_disk = json.loads(path.read_text())
        assert on_disk["2026"] == body
        # Existing years are untouched.
        assert on_disk["2025"][0]["title"] == "Old"

    def test_missing_date_gets_overflow_id(self, client, auth_headers, data_file):
        data_file({})
        resp = client.put("/api/years/2026", json=[{"title": "No Date Yet"}], headers=auth_headers)
        assert resp.status_code == 200
        film_id = resp.get_json()[0]["id"]
        assert 202632 <= film_id <= 202699

    def test_fully_replaces_existing_year(self, client, auth_headers, data_file):
        path = data_file({"2026": [{"id": 202601, "date": "10/1/2026", "title": "Stale", "service": []}]})
        resp = client.put(
            "/api/years/2026",
            json=[{"title": "Fresh", "date": "10/1/2026"}],
            headers=auth_headers,
        )
        assert resp.status_code == 200
        on_disk = json.loads(path.read_text())
        titles = [f["title"] for f in on_disk["2026"]]
        assert titles == ["Fresh"]

    def test_empty_list_is_allowed(self, client, auth_headers, data_file):
        data_file({})
        resp = client.put("/api/years/2026", json=[], headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_caller_supplied_id_and_service_are_ignored(self, client, auth_headers, data_file):
        data_file({})
        resp = client.put(
            "/api/years/2026",
            json=[{"title": "X", "date": "10/1/2026", "id": 999999, "service": [{"name": "fake"}]}],
            headers=auth_headers,
        )
        assert resp.status_code == 200
        entry = resp.get_json()[0]
        assert entry["id"] == 202601
        assert entry["service"] == []

    def test_write_failure_returns_500(self, client, auth_headers, data_file, monkeypatch):
        data_file({})

        def _boom(_data):
            raise OSError("Read-only file system")

        monkeypatch.setattr(app_module, "_save_data", _boom)

        resp = client.put("/api/years/2026", json=[{"title": "X", "date": "10/1/2026"}], headers=auth_headers)

        assert resp.status_code == 500
        assert "Could not write data.json" in resp.get_json()["error"]


class TestPostYear:
    def test_missing_auth_returns_401(self, client, api_key):
        resp = client.post("/api/years/2026", json={"title": "X"})
        assert resp.status_code == 401

    def test_non_four_digit_year_returns_400(self, client, auth_headers):
        resp = client.post("/api/years/26", json={"title": "X"}, headers=auth_headers)
        assert resp.status_code == 400

    def test_non_dict_body_returns_400(self, client, auth_headers, data_file):
        data_file({})
        resp = client.post("/api/years/2026", json=[{"title": "X"}], headers=auth_headers)
        assert resp.status_code == 400

    def test_missing_title_returns_400(self, client, auth_headers, data_file):
        data_file({})
        resp = client.post("/api/years/2026", json={"date": "10/1/2026"}, headers=auth_headers)
        assert resp.status_code == 400

    def test_appends_to_existing_year_without_disturbing_other_entries(self, client, auth_headers, data_file):
        path = data_file({
            "2026": [{"id": 202601, "date": "10/1/2026", "title": "Existing", "service": []}],
        })

        resp = client.post(
            "/api/years/2026",
            json={"title": "New Film", "date": "10/2/2026"},
            headers=auth_headers,
        )

        assert resp.status_code == 201
        assert resp.get_json() == {"id": 202602, "date": "10/2/2026", "title": "New Film", "service": []}

        on_disk = json.loads(path.read_text())
        titles = [f["title"] for f in on_disk["2026"]]
        assert titles == ["Existing", "New Film"]

    def test_creates_year_if_it_does_not_exist_yet(self, client, auth_headers, data_file):
        path = data_file({})
        resp = client.post(
            "/api/years/2027",
            json={"title": "First Of 2027", "date": "10/1/2027"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        on_disk = json.loads(path.read_text())
        assert len(on_disk["2027"]) == 1

    def test_duplicate_date_against_existing_entry_returns_400(self, client, auth_headers, data_file):
        path = data_file({
            "2026": [{"id": 202601, "date": "10/1/2026", "title": "Existing", "service": []}],
        })
        resp = client.post(
            "/api/years/2026",
            json={"title": "Collides", "date": "10/1/2026"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        # Nothing should have been appended on a validation failure.
        on_disk = json.loads(path.read_text())
        assert len(on_disk["2026"]) == 1

    def test_missing_date_gets_overflow_id_not_colliding_with_existing_overflow_ids(self, client, auth_headers, data_file):
        data_file({"2026": [{"id": 202645, "date": "", "title": "Already Overflow", "service": []}]})
        resp = client.post("/api/years/2026", json={"title": "Another No-Date"}, headers=auth_headers)
        assert resp.status_code == 201
        assert resp.get_json()["id"] != 202645

    def test_write_failure_returns_500(self, client, auth_headers, data_file, monkeypatch):
        data_file({})

        def _boom(_data):
            raise OSError("Read-only file system")

        monkeypatch.setattr(app_module, "_save_data", _boom)

        resp = client.post("/api/years/2026", json={"title": "X", "date": "10/1/2026"}, headers=auth_headers)

        assert resp.status_code == 500
        assert "Could not write data.json" in resp.get_json()["error"]
