"""Thin wrapper over the Supabase PostgREST API for the films/services tables.

This service is the REST layer in front of Supabase (the source of truth); all
reads and writes to the film catalogue go through here using the service-role
key, which bypasses row-level security -- appropriate for a trusted server-side
API, and never exposed to browsers.

Kept deliberately small: it speaks PostgREST over plain HTTPS with `requests`
(already a dependency) rather than pulling in supabase-py for what is a handful
of calls. Schema (see the frontend repo's supabase/migrations):

    films    (id, year, sort_order, date, title, justwatch_url, created_at, updated_at)
    services (id, film_id -> films.id, name, type, price, currency, link, icon, ...)
"""
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

REQUEST_TIMEOUT = 12

# Columns a client may set on a film. id/created_at/updated_at are
# server-managed; everything a scrape produces lives in `services`.
FILM_WRITABLE_FIELDS = ("year", "date", "title", "justwatch_url", "sort_order")

# The offer fields scrape_title() emits, which map 1:1 onto services columns.
SERVICE_FIELDS = ("name", "type", "price", "currency", "link", "icon")

# Of those, the ones rendered as URLs in the browser -- sanitised on write.
SERVICE_URL_FIELDS = ("link", "icon")


class StoreError(Exception):
    """Raised when Supabase can't be reached or returns an error status.

    `status_code` is the PostgREST HTTP status when there was one, or None for
    a local problem (missing config, connection failure) -- callers use it to
    pick their own response code.
    """

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _config():
    # Read at call time (like app.py's ADMIN_API_KEY) so tests can set env and
    # a misconfigured server fails per-request with a clear message rather than
    # at import. PUBLIC_SUPABASE_URL is accepted because the frontend's .env
    # (which this borrows) names it that way.
    url = os.environ.get("SUPABASE_URL") or os.environ.get("PUBLIC_SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise StoreError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be configured on the server"
        )
    return url.rstrip("/"), key


def _request(method, path, *, params=None, json=None, prefer=None):
    base, key = _config()
    headers = {
        "apikey": key,
        "Authorization": "Bearer {}".format(key),
        "Accept": "application/json",
    }
    if json is not None:
        headers["Content-Type"] = "application/json"
    if prefer:
        headers["Prefer"] = prefer
    try:
        resp = requests.request(
            method, "{}/rest/v1/{}".format(base, path),
            headers=headers, params=params, json=json, timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise StoreError("Supabase request failed: {}".format(exc))
    if not resp.ok:
        raise StoreError(
            "Supabase {} {} -> {}: {}".format(method, path, resp.status_code, resp.text[:300]),
            status_code=resp.status_code,
        )
    if resp.status_code == 204 or not resp.content:
        return None
    return resp.json()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _https_url_or_none(value):
    """Keep a plain https URL, drop anything else.

    A service's `link` is rendered as an <a href> and `icon` as an <img src>
    for every visitor to the site, so neither can be trusted verbatim just
    because it came off a JustWatch page -- a `javascript:` URI there would be
    clickable XSS on the homepage. https-only rather than http too: every real
    offer already uses https, so there's nothing legitimate to accommodate and
    it avoids mixed content on an https site.

    This guard used to live in the frontend (applyOffers.ts) and moved here
    when offer writes did; it is now the only thing enforcing it.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        return value if urlparse(value).scheme == "https" else None
    except ValueError:
        return None


def list_films(year=None):
    """All films, each with its nested `services`, ordered as the site shows them."""
    params = {"select": "*,services(*)", "order": "year.asc,sort_order.asc"}
    if year is not None:
        params["year"] = "eq.{}".format(year)
    return _request("GET", "films", params=params) or []


def get_film(film_id):
    """One film with its nested `services`, or None if no such id."""
    rows = _request(
        "GET", "films",
        params={"select": "*,services(*)", "id": "eq.{}".format(film_id)},
    )
    return rows[0] if rows else None


def _next_sort_order(year):
    """One past the highest sort_order in `year` (0 if the year is empty).

    Films are displayed ordered by (year, sort_order), so a new film has to
    land at the end of its year. The column defaults to 0 in the schema, which
    would file every new film alongside whatever opened that year instead.
    Computed here rather than by the caller so clients stay dumb -- they POST
    a film and get the right position without a read-modify-write of their own.
    """
    rows = _request(
        "GET", "films",
        params={"select": "sort_order", "year": "eq.{}".format(year),
                "order": "sort_order.desc", "limit": "1"},
    )
    return (rows[0]["sort_order"] + 1) if rows else 0


def create_film(fields):
    payload = {k: fields[k] for k in FILM_WRITABLE_FIELDS if k in fields}
    # An explicit sort_order still wins -- this only fills in the common case.
    if "sort_order" not in payload:
        payload["sort_order"] = _next_sort_order(payload["year"])
    rows = _request("POST", "films", json=payload, prefer="return=representation")
    return rows[0] if rows else None


def update_film(film_id, fields):
    """Patch a film's writable fields; returns the updated row, or None if no
    film has that id. Always bumps updated_at (the table has no such trigger),
    so an empty `fields` acts as a 'touch' -- used after a refresh."""
    payload = {k: fields[k] for k in FILM_WRITABLE_FIELDS if k in fields}
    payload["updated_at"] = _now()
    rows = _request(
        "PATCH", "films",
        params={"id": "eq.{}".format(film_id)},
        json=payload, prefer="return=representation",
    )
    return rows[0] if rows else None


def delete_film(film_id):
    """Delete a film (services cascade). Returns True if a row was deleted."""
    rows = _request(
        "DELETE", "films",
        params={"id": "eq.{}".format(film_id)},
        prefer="return=representation",
    )
    return bool(rows)


def replace_services(film_id, services):
    """Replace all of a film's offers with a freshly scraped set.

    Insert-first, then delete only the rows that existed before this call
    (captured by id). Done this way -- rather than the simpler delete-then-
    insert -- because it's spread across two non-transactional PostgREST calls:
    if the insert fails, the old offers are still there (never a film with zero
    offers on a transient upstream error); if the delete fails, we're left with
    harmless duplicates that the next refresh clears. PostgREST runs each
    insert/delete as a single all-or-nothing statement, so neither call leaves
    a partial set.
    """
    existing = _request(
        "GET", "services",
        params={"select": "id", "film_id": "eq.{}".format(film_id)},
    ) or []
    old_ids = [row["id"] for row in existing]

    rows = [
        dict({"film_id": film_id},
             **{k: (_https_url_or_none(s.get(k)) if k in SERVICE_URL_FIELDS else s.get(k))
                for k in SERVICE_FIELDS})
        for s in services
    ]
    if rows:
        _request("POST", "services", json=rows)
    if old_ids:
        id_list = ",".join(str(i) for i in old_ids)
        _request("DELETE", "services", params={"id": "in.({})".format(id_list)})
    return len(rows)
