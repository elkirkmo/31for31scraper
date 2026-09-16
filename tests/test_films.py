import json

import responses

from helpers import load_fixture

JUSTWATCH_BASE = "https://www.justwatch.com/us/movie/"
REST = "https://sb.test/rest/v1/"


def _service_calls(paths):
    """URLs among the mocked calls that hit the given rest path (e.g. 'services')."""
    return [c.request.url for c in responses.calls if REST + paths in c.request.url]


def _post_body(path):
    """Body of the POST to the given rest path -- not positional, since some
    calls are preceded by a lookup (e.g. create_film reads max sort_order)."""
    return next(c.request.body for c in responses.calls
                if c.request.method == "POST" and REST + path in c.request.url)


class TestListFilms:
    def test_requires_auth(self, client, api_key, supabase_env):
        # Reads are keyed like the writes: the frontend holds the key and
        # calls this server-side. An unkeyed read must not reach Supabase at
        # all -- no PostgREST response is registered, so a request would raise.
        resp = client.get("/films")
        assert resp.status_code == 401

    @responses.activate
    def test_read_returns_films_with_services(self, client, auth_headers, supabase_env):
        responses.add(responses.GET, REST + "films", json=[
            {"id": 1, "title": "A", "year": 2024, "services": [{"name": "Netflix", "type": "subscription"}]},
        ], status=200)

        resp = client.get("/films", headers=auth_headers)

        assert resp.status_code == 200
        body = resp.get_json()
        assert body[0]["title"] == "A"
        assert body[0]["services"][0]["name"] == "Netflix"

    @responses.activate
    def test_year_filter_is_passed_through(self, client, auth_headers, supabase_env):
        responses.add(responses.GET, REST + "films", json=[], status=200)

        resp = client.get("/films?year=2024", headers=auth_headers)

        assert resp.status_code == 200
        assert "year=eq.2024" in responses.calls[0].request.url

    def test_non_integer_year_returns_400(self, client, auth_headers, supabase_env):
        resp = client.get("/films?year=nope", headers=auth_headers)
        assert resp.status_code == 400

    @responses.activate
    def test_supabase_error_returns_502(self, client, auth_headers, supabase_env):
        responses.add(responses.GET, REST + "films", json={"message": "boom"}, status=500)
        resp = client.get("/films", headers=auth_headers)
        assert resp.status_code == 502

    def test_missing_supabase_config_returns_500(self, client, auth_headers, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("PUBLIC_SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
        resp = client.get("/films", headers=auth_headers)
        assert resp.status_code == 500
        assert "SUPABASE" in resp.get_json()["error"]


class TestGetFilm:
    def test_requires_auth(self, client, api_key, supabase_env):
        resp = client.get("/films/20")
        assert resp.status_code == 401

    @responses.activate
    def test_found_returns_the_film(self, client, auth_headers, supabase_env):
        responses.add(responses.GET, REST + "films", json=[{"id": 20, "title": "The Ring"}], status=200)
        resp = client.get("/films/20", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["title"] == "The Ring"

    @responses.activate
    def test_missing_returns_404(self, client, auth_headers, supabase_env):
        responses.add(responses.GET, REST + "films", json=[], status=200)
        resp = client.get("/films/999", headers=auth_headers)
        assert resp.status_code == 404


class TestCreateFilm:
    def test_requires_auth(self, client, api_key, supabase_env):
        resp = client.post("/films", json={"year": 2024, "date": "10/1/2025", "title": "X"})
        assert resp.status_code == 401

    def test_missing_required_field_returns_400(self, client, auth_headers, supabase_env):
        resp = client.post("/films", json={"title": "X"}, headers=auth_headers)
        assert resp.status_code == 400

    def test_bad_justwatch_url_returns_400(self, client, auth_headers, supabase_env):
        resp = client.post(
            "/films",
            json={"year": 2024, "date": "10/1/2025", "title": "X", "justwatch_url": "https://evil.example"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "justwatch_url" in resp.get_json()["error"]

    @responses.activate
    def test_valid_create_returns_201_and_only_writable_fields(self, client, auth_headers, supabase_env):
        responses.add(responses.GET, REST + "films", json=[{"sort_order": 4}], status=200)
        responses.add(responses.POST, REST + "films",
                      json=[{"id": 5, "year": 2024, "date": "10/1/2025", "title": "X"}], status=201)

        resp = client.post(
            "/films",
            json={"year": 2024, "date": "10/1/2025", "title": "X", "id": 999, "created_at": "hax"},
            headers=auth_headers,
        )

        assert resp.status_code == 201
        assert resp.get_json()["id"] == 5
        sent = json.loads(_post_body("films"))
        # Server-managed fields must be dropped, not forwarded.
        assert "id" not in sent and "created_at" not in sent
        assert sent["title"] == "X"

    @responses.activate
    def test_duplicate_year_title_returns_409(self, client, auth_headers, supabase_env):
        responses.add(responses.GET, REST + "films", json=[], status=200)
        responses.add(responses.POST, REST + "films",
                      json={"message": "duplicate key value violates unique constraint"}, status=409)
        resp = client.post(
            "/films", json={"year": 2024, "date": "10/1/2025", "title": "Dupe"}, headers=auth_headers,
        )
        assert resp.status_code == 409


class TestCreateFilmSortOrder:
    """A new film has to land at the END of its year, not at position 0.

    films are displayed ordered by (year, sort_order) and the column defaults
    to 0, so without this every new film files alongside whatever opened that
    year. Computed server-side so a client can POST a film without doing a
    read-modify-write of its own.
    """

    @responses.activate
    def test_appends_after_the_years_highest_sort_order(self, client, auth_headers, supabase_env):
        responses.add(responses.GET, REST + "films", json=[{"sort_order": 30}], status=200)
        responses.add(responses.POST, REST + "films", json=[{"id": 5}], status=201)

        client.post("/films", json={"year": 2024, "date": "10/31/2024", "title": "Last"},
                    headers=auth_headers)

        assert json.loads(_post_body("films"))["sort_order"] == 31
        # Scoped to the year being added to, not the whole table.
        lookup = next(c.request.url for c in responses.calls if c.request.method == "GET")
        assert "year=eq.2024" in lookup
        assert "order=sort_order.desc" in lookup

    @responses.activate
    def test_first_film_of_a_year_starts_at_zero(self, client, auth_headers, supabase_env):
        responses.add(responses.GET, REST + "films", json=[], status=200)  # empty year
        responses.add(responses.POST, REST + "films", json=[{"id": 1}], status=201)

        client.post("/films", json={"year": 2030, "date": "10/1/2030", "title": "First"},
                    headers=auth_headers)

        assert json.loads(_post_body("films"))["sort_order"] == 0

    @responses.activate
    def test_explicit_sort_order_is_honoured_without_a_lookup(self, client, auth_headers, supabase_env):
        # No GET registered: if the code looked up the max anyway, this raises.
        responses.add(responses.POST, REST + "films", json=[{"id": 9}], status=201)

        client.post("/films", json={"year": 2024, "date": "10/2/2024", "title": "Second", "sort_order": 1},
                    headers=auth_headers)

        assert json.loads(_post_body("films"))["sort_order"] == 1


class TestUpdateFilm:
    def test_requires_auth(self, client, api_key, supabase_env):
        resp = client.patch("/films/20", json={"justwatch_url": JUSTWATCH_BASE + "le-cercle"})
        assert resp.status_code == 401

    def test_empty_body_returns_400(self, client, auth_headers, supabase_env):
        resp = client.patch("/films/20", json={}, headers=auth_headers)
        assert resp.status_code == 400

    def test_only_unknown_fields_returns_400(self, client, auth_headers, supabase_env):
        resp = client.patch("/films/20", json={"id": 1, "created_at": "x"}, headers=auth_headers)
        assert resp.status_code == 400

    @responses.activate
    def test_sets_override_and_bumps_updated_at(self, client, auth_headers, supabase_env):
        override = JUSTWATCH_BASE + "le-cercle"
        responses.add(responses.PATCH, REST + "films",
                      json=[{"id": 20, "title": "The Ring", "justwatch_url": override}], status=200)

        resp = client.patch("/films/20", json={"justwatch_url": override}, headers=auth_headers)

        assert resp.status_code == 200
        assert resp.get_json()["justwatch_url"] == override
        sent = json.loads(responses.calls[0].request.body)
        assert sent["justwatch_url"] == override
        assert "updated_at" in sent  # no DB trigger, so the store sets it
        assert "id=eq.20" in responses.calls[0].request.url

    @responses.activate
    def test_null_justwatch_url_is_allowed_to_clear_override(self, client, auth_headers, supabase_env):
        responses.add(responses.PATCH, REST + "films",
                      json=[{"id": 20, "title": "The Ring", "justwatch_url": None}], status=200)
        resp = client.patch("/films/20", json={"justwatch_url": None}, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["justwatch_url"] is None

    def test_bad_justwatch_url_returns_400(self, client, auth_headers, supabase_env):
        resp = client.patch("/films/20", json={"justwatch_url": "https://evil.example"}, headers=auth_headers)
        assert resp.status_code == 400

    @responses.activate
    def test_missing_film_returns_404(self, client, auth_headers, supabase_env):
        responses.add(responses.PATCH, REST + "films", json=[], status=200)  # no row matched
        resp = client.patch("/films/999", json={"title": "X"}, headers=auth_headers)
        assert resp.status_code == 404


class TestDeleteFilm:
    def test_requires_auth(self, client, api_key, supabase_env):
        resp = client.delete("/films/20")
        assert resp.status_code == 401

    @responses.activate
    def test_delete_existing_returns_204(self, client, auth_headers, supabase_env):
        responses.add(responses.DELETE, REST + "films", json=[{"id": 20}], status=200)
        resp = client.delete("/films/20", headers=auth_headers)
        assert resp.status_code == 204

    @responses.activate
    def test_delete_missing_returns_404(self, client, auth_headers, supabase_env):
        responses.add(responses.DELETE, REST + "films", json=[], status=200)  # nothing deleted
        resp = client.delete("/films/999", headers=auth_headers)
        assert resp.status_code == 404


class TestRefreshFilm:
    def test_requires_auth(self, client, api_key, supabase_env):
        resp = client.post("/films/20/refresh")
        assert resp.status_code == 401

    @responses.activate
    def test_missing_film_returns_404(self, client, auth_headers, supabase_env):
        responses.add(responses.GET, REST + "films", json=[], status=200)
        resp = client.post("/films/999/refresh", headers=auth_headers)
        assert resp.status_code == 404

    @responses.activate
    def test_success_replaces_services(self, client, auth_headers, supabase_env):
        # Film with no override -> scrape the guessed slug; fixture has 11 offers.
        responses.add(responses.GET, REST + "films",
                      json=[{"id": 7, "title": "The Thing From Another World", "justwatch_url": None}], status=200)
        responses.add(responses.GET, JUSTWATCH_BASE + "the-thing-from-another-world",
                      body=load_fixture("the_thing_from_another_world.html"), status=200)
        responses.add(responses.GET, REST + "services", json=[{"id": 900}, {"id": 901}], status=200)
        responses.add(responses.POST, REST + "services", body="", status=201)
        responses.add(responses.DELETE, REST + "services", body="", status=204)
        responses.add(responses.PATCH, REST + "films", json=[{"id": 7}], status=200)

        resp = client.post("/films/7/refresh", headers=auth_headers)

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["refreshed"] is True
        assert body["services_updated"] == 11
        # The fresh set was inserted and the old offers then deleted -- that
        # order matters, and is asserted properly in the insert-failure test.
        assert _service_calls("services")  # both POST and DELETE hit /services
        posted = json.loads(next(c.request.body for c in responses.calls
                                 if c.request.method == "POST" and "services" in c.request.url))
        assert len(posted) == 11
        assert all(r["film_id"] == 7 for r in posted)

    @responses.activate
    def test_scrape_failure_returns_502_and_leaves_services_untouched(self, client, auth_headers, supabase_env):
        # This is the key robustness guarantee: a failed scrape must NOT wipe
        # the film's existing offers. Note: the /services endpoints are NOT
        # registered, so any write to them would raise (test fails) -- plus we
        # assert explicitly that none were called.
        responses.add(responses.GET, REST + "films",
                      json=[{"id": 20, "title": "The Ring", "justwatch_url": None}], status=200)
        responses.add(responses.GET, JUSTWATCH_BASE + "the-ring", status=404)

        resp = client.post("/films/20/refresh", headers=auth_headers)

        assert resp.status_code == 502
        assert resp.get_json()["refreshed"] is False
        assert _service_calls("services") == []  # existing offers left untouched

    @responses.activate
    def test_insert_failure_does_not_delete_old_services(self, client, auth_headers, supabase_env):
        # Regression guard: replace_services inserts the fresh set BEFORE
        # deleting the old rows, so if the insert fails the film keeps its
        # existing offers rather than being left empty. DELETE /services is
        # deliberately NOT registered -- if the code tried it, this test fails.
        responses.add(responses.GET, REST + "films",
                      json=[{"id": 7, "title": "The Thing From Another World", "justwatch_url": None}], status=200)
        responses.add(responses.GET, JUSTWATCH_BASE + "the-thing-from-another-world",
                      body=load_fixture("the_thing_from_another_world.html"), status=200)
        responses.add(responses.GET, REST + "services", json=[{"id": 900}, {"id": 901}], status=200)
        responses.add(responses.POST, REST + "services", json={"message": "boom"}, status=502)

        resp = client.post("/films/7/refresh", headers=auth_headers)

        assert resp.status_code == 502
        deletes = [c for c in responses.calls if c.request.method == "DELETE" and "services" in c.request.url]
        assert deletes == []  # old offers never deleted when the insert fails


class TestOfferUrlSanitisation:
    """service.link becomes an <a href> and service.icon an <img src> on the
    public homepage, so a non-https URL must never reach the database. This
    guard used to live in the frontend (applyOffers.ts); it lives here now,
    and here only.
    """

    @staticmethod
    def _stored_rows():
        return json.loads(_post_body("services"))

    @responses.activate
    def _refresh_with_offers(self, client, auth_headers, offers, monkeypatch):
        monkeypatch.setattr(
            "app.scrape_title",
            lambda title, url=None: {"title": title, "url": "x", "service": offers},
        )
        responses.add(responses.GET, REST + "films",
                      json=[{"id": 7, "title": "T", "justwatch_url": None}], status=200)
        responses.add(responses.GET, REST + "services", json=[], status=200)
        responses.add(responses.POST, REST + "services", body="", status=201)
        responses.add(responses.PATCH, REST + "films", json=[{"id": 7}], status=200)
        client.post("/films/7/refresh", headers=auth_headers)
        return self._stored_rows()

    def test_javascript_uri_is_dropped(self, client, auth_headers, supabase_env, monkeypatch):
        rows = self._refresh_with_offers(client, auth_headers, [{
            "name": "Evil", "type": "rent", "price": 1.0, "currency": "USD",
            "link": "javascript:alert(document.cookie)", "icon": "https://images.justwatch.com/a.webp",
        }], monkeypatch)

        assert rows[0]["link"] is None          # the XSS vector, neutralised
        assert rows[0]["icon"].startswith("https://")
        assert rows[0]["name"] == "Evil"        # the offer itself is still stored

    def test_http_url_is_dropped(self, client, auth_headers, supabase_env, monkeypatch):
        rows = self._refresh_with_offers(client, auth_headers, [{
            "name": "Plain", "type": "rent", "price": None, "currency": None,
            "link": "http://insecure.example/watch", "icon": None,
        }], monkeypatch)

        assert rows[0]["link"] is None

    def test_https_url_survives_untouched(self, client, auth_headers, supabase_env, monkeypatch):
        link = "https://watch.amazon.com/detail?gti=abc123&ref=x"
        rows = self._refresh_with_offers(client, auth_headers, [{
            "name": "Amazon Video", "type": "rent", "price": 3.99, "currency": "USD",
            "link": link, "icon": "https://images.justwatch.com/icon/a/s100/amazonvideo.webp",
        }], monkeypatch)

        assert rows[0]["link"] == link  # byte-for-byte, query string intact


class TestRefreshAllFilms:
    def test_requires_auth(self, client, api_key, supabase_env):
        resp = client.post("/films/refresh")
        assert resp.status_code == 401

    @responses.activate
    def test_refuses_the_run_when_every_film_scraped_empty(self, client, auth_headers,
                                                           supabase_env, monkeypatch):
        # Every film coming back with zero offers is a broken scrape, not a
        # catalogue that emptied overnight. Writing it would erase the only
        # thing the site is for, so the whole run is refused.
        # No /services endpoints are registered: any write would raise here.
        monkeypatch.setattr(
            "app.scrape_titles",
            lambda pairs: [{"title": t, "url": "x", "service": []} for t, _ in pairs],
        )
        responses.add(responses.GET, REST + "films", json=[
            {"id": 7, "title": "A", "justwatch_url": None},
            {"id": 8, "title": "B", "justwatch_url": None},
        ], status=200)

        resp = client.post("/films/refresh", headers=auth_headers)

        assert resp.status_code == 502
        assert "Refused" in resp.get_json()["error"]
        assert resp.get_json()["refreshed"] == 0
        assert _service_calls("services") == []  # not one row written

    @responses.activate
    def test_refusal_still_reports_which_films_failed_to_scrape(self, client, auth_headers,
                                                                supabase_env, monkeypatch):
        # Mixed run: one film unreadable, the rest readable but empty -> still
        # refused, and the unreadable one is named. That list is the diagnostic
        # an admin needs at exactly the moment the run is refused.
        def fake(pairs):
            return [{"title": t, "service": [], "error": "No JustWatch page found"} if t == "B"
                    else {"title": t, "url": "x", "service": []} for t, _ in pairs]

        monkeypatch.setattr("app.scrape_titles", fake)
        responses.add(responses.GET, REST + "films", json=[
            {"id": 7, "title": "A", "justwatch_url": None},
            {"id": 8, "title": "B", "justwatch_url": None},
        ], status=200)

        resp = client.post("/films/refresh", headers=auth_headers)

        assert resp.status_code == 502
        results = resp.get_json()["results"]
        assert [r["film_id"] for r in results] == [8]
        assert "No JustWatch page found" in results[0]["error"]
        assert _service_calls("services") == []

    @responses.activate
    def test_one_film_with_offers_is_enough_to_let_the_run_proceed(self, client, auth_headers,
                                                                   supabase_env, monkeypatch):
        # The inverse of the guard: a film that genuinely streams nowhere is
        # legitimate and must still be applied, clearing its offers.
        offers = {"A": [{"name": "Netflix", "type": "subscription", "price": None,
                         "currency": None, "link": "https://netflix.com/1", "icon": None}],
                  "B": []}
        monkeypatch.setattr(
            "app.scrape_titles",
            lambda pairs: [{"title": t, "url": "x", "service": offers[t]} for t, _ in pairs],
        )
        responses.add(responses.GET, REST + "films", json=[
            {"id": 7, "title": "A", "justwatch_url": None},
            {"id": 8, "title": "B", "justwatch_url": None},
        ], status=200)
        responses.add(responses.GET, REST + "services", json=[{"id": 900}], status=200)
        responses.add(responses.POST, REST + "services", body="", status=201)
        responses.add(responses.DELETE, REST + "services", body="", status=204)
        responses.add(responses.PATCH, REST + "films", json=[{"id": 7}], status=200)

        resp = client.post("/films/refresh", headers=auth_headers)

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["refreshed"] == 2
        by_id = {r["film_id"]: r for r in body["results"]}
        assert by_id[8]["services_updated"] == 0  # B cleared, legitimately

    @responses.activate
    def test_all_films_failing_to_scrape_is_not_a_refusal(self, client, auth_headers,
                                                          supabase_env, monkeypatch):
        # Distinct from the guard above: these films were never read at all,
        # so there is nothing to write and nothing to refuse. Reported inline.
        monkeypatch.setattr(
            "app.scrape_titles",
            lambda pairs: [{"title": t, "service": [], "error": "No JustWatch page found"}
                           for t, _ in pairs],
        )
        responses.add(responses.GET, REST + "films", json=[
            {"id": 7, "title": "A", "justwatch_url": None},
        ], status=200)

        resp = client.post("/films/refresh", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.get_json()["refreshed"] == 0
        assert resp.get_json()["results"][0]["refreshed"] is False
        assert _service_calls("services") == []

    @responses.activate
    def test_refreshes_each_film_and_reports_failures_inline(self, client, auth_headers, supabase_env):
        responses.add(responses.GET, REST + "films", json=[
            {"id": 7, "title": "The Thing From Another World", "justwatch_url": None},
            {"id": 8, "title": "Not A Real Movie", "justwatch_url": None},
        ], status=200)
        responses.add(responses.GET, JUSTWATCH_BASE + "the-thing-from-another-world",
                      body=load_fixture("the_thing_from_another_world.html"), status=200)
        responses.add(responses.GET, JUSTWATCH_BASE + "not-a-real-movie", status=404)
        responses.add(responses.GET, REST + "services", json=[{"id": 900}], status=200)
        responses.add(responses.POST, REST + "services", body="", status=201)
        responses.add(responses.DELETE, REST + "services", body="", status=204)
        responses.add(responses.PATCH, REST + "films", json=[{"id": 7}], status=200)

        resp = client.post("/films/refresh", headers=auth_headers)

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["films"] == 2
        assert body["refreshed"] == 1
        by_id = {r["film_id"]: r for r in body["results"]}
        assert by_id[7]["refreshed"] is True and by_id[7]["services_updated"] == 11
        assert by_id[8]["refreshed"] is False and "error" in by_id[8]
        # The failed film's services were never written.
        posted_ids = [json.loads(c.request.body) for c in responses.calls
                      if c.request.method == "POST" and "services" in c.request.url]
        assert all(row["film_id"] == 7 for batch in posted_ids for row in batch)
