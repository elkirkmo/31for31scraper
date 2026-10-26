import json

import pytest
import responses

BASE = "https://sb.test"
AUTH_USER = BASE + "/auth/v1/user"
PROGRESS = BASE + "/rest/v1/progress"
FILMS = BASE + "/rest/v1/films"

TOKEN = "user-access-token"
USER = {"Authorization": "Bearer " + TOKEN}

CATALOGUE = [
    {"year": 2024, "title": "Trick R Treat"},
    {"year": 2024, "title": "Hatchet"},
    {"year": 2025, "title": "Hatchet"},
    {"year": 2025, "title": "The Thing From Another World"},
]


@pytest.fixture
def user_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", BASE)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-role-key")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "publishable-key")


def signed_in(user_id="user-1"):
    responses.add(responses.GET, AUTH_USER, json={"id": user_id}, status=200)


def stored(liked):
    rows = [] if liked is None else [{"liked": liked}]
    responses.add(responses.GET, PROGRESS, json=rows, status=200)


def catalogue():
    responses.add(responses.GET, FILMS, json=CATALOGUE, status=200)


def accept_upsert():
    responses.add(responses.POST, PROGRESS, status=201)


def upserted():
    call = next(c for c in responses.calls
                if c.request.method == "POST" and c.request.url.startswith(PROGRESS))
    return call.request, json.loads(call.request.body)


def put(client, body, headers=USER):
    return client.put("/me/liked", json=body, headers=headers)


def delete(client, body, headers=USER):
    return client.delete("/me/liked", json=body, headers=headers)


class TestAuth:
    def test_no_token_is_401_and_never_reaches_supabase(self, client, user_env):
        with responses.RequestsMock() as rsps:  # fails on any unregistered call
            resp = client.get("/me/liked")
        assert resp.status_code == 401
        assert "Bearer" in resp.get_json()["error"]
        assert len(rsps.calls) == 0

    def test_api_key_is_not_a_user_credential(self, client, user_env, auth_headers):
        # The admin key can rewrite the catalogue, but it doesn't say whose
        # likes these are -- it must not be accepted in place of a user token.
        resp = put(client, {"title": "Hatchet", "year": 2024}, headers=auth_headers)
        assert resp.status_code == 401

    @responses.activate
    def test_token_supabase_rejects_is_401(self, client, user_env):
        responses.add(responses.GET, AUTH_USER, json={"msg": "bad jwt"}, status=401)
        resp = client.get("/me/liked", headers=USER)
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "invalid or expired access token"

    @responses.activate
    def test_token_is_forwarded_with_the_publishable_key_not_the_service_role(self, client, user_env):
        signed_in()
        stored({})
        client.get("/me/liked", headers=USER)

        for call in responses.calls:
            assert call.request.headers["Authorization"] == "Bearer " + TOKEN
            assert call.request.headers["apikey"] == "publishable-key"
            assert "svc-role-key" not in str(call.request.headers)

    def test_missing_publishable_key_is_500(self, client, user_env, monkeypatch):
        monkeypatch.delenv("SUPABASE_PUBLISHABLE_KEY")
        monkeypatch.delenv("PUBLIC_SUPABASE_PUBLISHABLE_KEY", raising=False)
        resp = client.get("/me/liked", headers=USER)
        assert resp.status_code == 500
        assert "SUPABASE_PUBLISHABLE_KEY" in resp.get_json()["error"]


class TestGetLiked:
    @responses.activate
    def test_returns_the_users_likes(self, client, user_env):
        signed_in()
        stored({"2024": ["Hatchet"]})
        resp = client.get("/me/liked", headers=USER)
        assert resp.status_code == 200
        assert resp.get_json() == {"liked": {"2024": ["Hatchet"]}}
        assert "user_id=eq.user-1" in responses.calls[1].request.url

    @responses.activate
    def test_no_progress_row_is_an_empty_map(self, client, user_env):
        signed_in()
        stored(None)
        assert client.get("/me/liked", headers=USER).get_json() == {"liked": {}}


class TestLike:
    @responses.activate
    def test_likes_a_film_by_title(self, client, user_env):
        signed_in()
        stored({})
        catalogue()
        accept_upsert()

        resp = put(client, {"title": "Trick R Treat"})

        assert resp.status_code == 200
        assert resp.get_json() == {"film": {"year": 2024, "title": "Trick R Treat"},
                                   "liked": {"2024": ["Trick R Treat"]}}
        request, body = upserted()
        assert body == {"user_id": "user-1", "liked": {"2024": ["Trick R Treat"]}}
        assert "on_conflict=user_id" in request.url
        assert "merge-duplicates" in request.headers["Prefer"]

    @responses.activate
    def test_user_id_comes_from_the_token_never_the_body(self, client, user_env):
        signed_in("the-real-user")
        stored({})
        catalogue()
        accept_upsert()

        put(client, {"title": "Trick R Treat", "user_id": "someone-else"})

        _, body = upserted()
        assert body["user_id"] == "the-real-user"

    @responses.activate
    def test_only_writes_liked_so_watched_survives(self, client, user_env):
        signed_in()
        stored({})
        catalogue()
        accept_upsert()

        put(client, {"title": "Trick R Treat"})

        _, body = upserted()
        assert set(body) == {"user_id", "liked"}

    @responses.activate
    def test_matches_case_insensitively_and_stores_the_catalogue_spelling(self, client, user_env):
        signed_in()
        stored({})
        catalogue()
        accept_upsert()

        resp = put(client, {"title": "  trick r treat "})

        assert resp.get_json()["film"] == {"year": 2024, "title": "Trick R Treat"}

    @responses.activate
    def test_liking_twice_is_a_no_op(self, client, user_env):
        signed_in()
        stored({"2024": ["Trick R Treat"]})
        catalogue()
        accept_upsert()

        resp = put(client, {"title": "Trick R Treat"})

        assert resp.get_json()["liked"] == {"2024": ["Trick R Treat"]}

    @responses.activate
    def test_keeps_other_likes(self, client, user_env):
        signed_in()
        stored({"2025": ["The Thing From Another World"]})
        catalogue()
        accept_upsert()

        resp = put(client, {"title": "Trick R Treat"})

        assert resp.get_json()["liked"] == {
            "2025": ["The Thing From Another World"],
            "2024": ["Trick R Treat"],
        }

    @responses.activate
    def test_unknown_title_is_404_and_writes_nothing(self, client, user_env):
        signed_in()
        stored({})
        catalogue()

        resp = put(client, {"title": "Not A Real Film"})

        assert resp.status_code == 404
        assert not any(c.request.method == "POST" for c in responses.calls)

    @responses.activate
    def test_title_in_two_years_needs_a_year(self, client, user_env):
        signed_in()
        stored({})
        catalogue()

        resp = put(client, {"title": "Hatchet"})

        assert resp.status_code == 409
        assert "2024, 2025" in resp.get_json()["error"]
        assert not any(c.request.method == "POST" for c in responses.calls)

    @responses.activate
    def test_year_picks_between_them(self, client, user_env):
        signed_in()
        stored({})
        catalogue()
        accept_upsert()

        resp = put(client, {"title": "Hatchet", "year": 2025})

        assert resp.get_json()["film"] == {"year": 2025, "title": "Hatchet"}

    @responses.activate
    def test_year_that_does_not_have_the_title_is_404(self, client, user_env):
        signed_in()
        stored({})
        catalogue()

        resp = put(client, {"title": "Trick R Treat", "year": 2025})

        assert resp.status_code == 404
        assert "in 2025" in resp.get_json()["error"]

    @pytest.mark.parametrize("body, message", [
        ({}, '"title" is required'),
        ({"title": "  "}, '"title" is required'),
        ({"title": "Hatchet", "year": "2024"}, '"year" must be an integer'),
        ({"title": "Hatchet", "year": True}, '"year" must be an integer'),
    ])
    @responses.activate
    def test_bad_bodies_are_400(self, client, user_env, body, message):
        signed_in()
        stored({})
        catalogue()
        resp = put(client, body)
        assert resp.status_code == 400
        assert resp.get_json()["error"] == message

    @responses.activate
    def test_non_object_body_is_400(self, client, user_env):
        resp = client.put("/me/liked", json=["Hatchet"], headers=USER)
        assert resp.status_code == 400


class TestUnlike:
    @responses.activate
    def test_removes_the_film_and_drops_an_emptied_year(self, client, user_env):
        signed_in()
        stored({"2024": ["Trick R Treat"], "2025": ["Hatchet"]})
        catalogue()
        accept_upsert()

        resp = delete(client, {"title": "Trick R Treat"})

        assert resp.status_code == 200
        assert resp.get_json()["liked"] == {"2025": ["Hatchet"]}

    @responses.activate
    def test_unliking_an_unliked_film_is_a_no_op(self, client, user_env):
        signed_in()
        stored({})
        catalogue()
        accept_upsert()

        resp = delete(client, {"title": "Trick R Treat"})

        assert resp.status_code == 200
        assert resp.get_json()["liked"] == {}

    @responses.activate
    def test_can_remove_a_title_that_left_the_catalogue(self, client, user_env):
        # Renamed or dropped films would otherwise be stuck in the likes.
        signed_in()
        stored({"2024": ["Old Title"]})
        catalogue()
        accept_upsert()

        resp = delete(client, {"title": "old title"})

        assert resp.status_code == 200
        assert resp.get_json() == {"film": {"year": 2024, "title": "Old Title"}, "liked": {}}

    @responses.activate
    def test_cannot_like_a_title_that_left_the_catalogue(self, client, user_env):
        signed_in()
        stored({"2024": ["Old Title"]})
        catalogue()

        assert put(client, {"title": "Old Title"}).status_code == 404
