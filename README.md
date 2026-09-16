# 31for31scraper

A small Flask service behind [31for31](https://31for31.vercel.app/) that is
the REST API in front of the film catalogue. It manages films and their
`justwatch_url` overrides in a [Supabase](https://supabase.com) database (the
source of truth) and refreshes each film's current streaming offers (free /
subscription / rent / buy, with prices and direct links) by scraping
[JustWatch](https://www.justwatch.com). The `/films` endpoints read and write
Supabase; a separate stateless `/api/scrape` endpoint scrapes ad hoc without
persisting anything (handy as a dry run).

## How it works

Each JustWatch movie page server-renders a `window.__APOLLO_STATE__` blob
containing the exact offer data used to build the page — no headless
browser required. [`scraper.py`](scraper.py) fetches the page, extracts that
blob, and resolves it into a clean list of offers per film.

JustWatch URLs are guessed from the film title (lowercased, hyphenated,
punctuation stripped — see `slugify()` in `scraper.py`). This works for most
titles, but not all: JustWatch sometimes disambiguates a title collision
(e.g. a movie vs. a TV series of the same name) with a slug that can't be
derived from the title text at all. For those, the caller passes a
`justwatch_url` override alongside the title (see the API section below).
It's also a good way to catch typos in a title (a scrape failing with "No
JustWatch page found" is often just a misspelled title).

## Setup

Requires Python 3.9+ (Vercel deploys on 3.12, pinned in `.python-version`).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set:

- `ADMIN_API_KEY` — a random secret guarding the write/refresh endpoints,
  e.g. `openssl rand -hex 32`.
- `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` — the Supabase project the
  `/films` endpoints read and write. The service-role key bypasses row-level
  security, so it's server-only; never expose it to a browser.

## Running locally

```bash
python app.py
```

Starts the Flask dev server on `http://127.0.0.1:5000`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Useful variants while iterating:

```bash
pytest -v                    # verbose: one line per test
pytest tests/test_scraper.py # just the scraper unit tests
pytest tests/test_app.py     # just the Flask endpoint tests
pytest -k redirect           # only tests matching a keyword
```

All HTTP calls to JustWatch are mocked (via `responses`) using fixtures in
`tests/fixtures/` — the tests never hit the network. Most of those fixtures
are real JustWatch page data (scraped and trimmed down to just the entities
`scraper.py` reads, so they stay faithful to the real response shape without
carrying megabytes of unrelated recommendation data); a few edge-case
fixtures (malformed/missing data) are hand-written since they simulate page
shapes JustWatch doesn't currently serve. `requirements-dev.txt` is kept
separate from `requirements.txt` so `pytest`/`responses` don't get bundled
into the Vercel deployment.

## API

Full endpoint/schema reference: visit `/` (e.g.
https://31for31scraper.vercel.app/) for an interactive Swagger UI, served
straight from [`openapi.yaml`](openapi.yaml) (OpenAPI 3.0) — no auth
required. The raw spec is also served as-is at `/openapi.yaml` if you'd
rather point another tool (Postman, Redoc, etc.) at it directly.

### Film catalogue: `/films`

The primary API, backed by Supabase. Every endpoint requires the
`X-API-Key` header — reads included. The intended caller is a server-side
consumer holding the key; there are no CORS headers, so a browser can't
call these directly in any case.

- **`GET /films`** (`?year=`) — every film with its stored offers.
- **`GET /films/{id}`** — one film with its offers.
- **`POST /films`** — add a film (`{year, date, title, justwatch_url?}`).
  `sort_order` is optional and defaults to the end of that year.
- **`PATCH /films/{id}`** — update fields, including `justwatch_url` (the
  "Override"); set it to `null` to clear.
- **`DELETE /films/{id}`** — delete a film (its offers cascade).
- **`POST /films/{id}/refresh`** — re-scrape one film and replace its stored
  offers. A failed scrape leaves the existing offers untouched (a transient
  JustWatch failure never wipes good data).
- **`POST /films/refresh`** — re-scrape the whole catalogue (the periodic
  job). One film failing is reported inline, not fatal. If *every* film it
  could read came back with zero offers, the run is refused with `502` and
  nothing is written — that's a broken scrape, not a catalogue that emptied
  overnight, and applying it would erase the one thing the site is for.

```bash
# set The Ring's override, then refresh its offers from the 2002 film's page
curl -X PATCH -H "X-API-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"justwatch_url": "https://www.justwatch.com/us/movie/le-cercle"}' \
  http://127.0.0.1:5000/films/20
curl -X POST -H "X-API-Key: $ADMIN_API_KEY" http://127.0.0.1:5000/films/20/refresh
```

### `GET /api/health`

No auth required. Always returns `200 {"status": "ok"}` if the process is
up — a liveness check for uptime monitoring, not a check that JustWatch is
reachable.

### `GET /api/scrape?title=...`

Requires an `X-API-Key` header matching `ADMIN_API_KEY`. Requests without a
valid key get `401 Unauthorized`; if the server has no `ADMIN_API_KEY`
configured at all, requests get `500`. `title` is required — omitting it
gets `400`.

Scrapes a single film ad hoc and returns a `FilmResult`.

```bash
curl -H "X-API-Key: $ADMIN_API_KEY" \
  "http://127.0.0.1:5000/api/scrape?title=The%20Strangers%20(2008)"
```

**`&url=`** — scrapes the given URL directly instead of guessing the slug
from the title. This is how you'd try out a `justwatch_url` override
before using it in a `POST` request.

```bash
curl -H "X-API-Key: $ADMIN_API_KEY" \
  "http://127.0.0.1:5000/api/scrape?title=Event%20Horizon&url=https://www.justwatch.com/us/movie/event-horizon-1997"
```

### `POST /api/scrape`

Same auth as above. Body is a JSON array of `{title, justwatch_url?}` —
this is the one to use for more than a single film, since it's one
invocation handling the whole list (via `scrape_titles()`'s bounded
concurrency) rather than one round trip per film. Returns one `FilmResult`
per input film, in the same order.

```bash
curl -X POST -H "X-API-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '[{"title": "The Ring", "justwatch_url": "https://www.justwatch.com/us/movie/le-cercle"}, {"title": "Hereditary"}]' \
  http://127.0.0.1:5000/api/scrape
```

**A film that fails to scrape doesn't fail the whole request.** It comes
back with `"service": []` and an `"error"` message explaining why (404,
bad `justwatch_url`, couldn't parse the page, etc.), so one bad title
never blocks the rest of the list. Malformed input (body isn't an array,
empty array, an item missing `title`) is the exception — that's a `400`
for the whole request, since it's a client error, not a per-film scrape
failure.

### Response shape

Every offer in a `service` array looks like:

```json
{
  "name": "Amazon Video",
  "type": "rent",
  "price": 3.99,
  "currency": "USD",
  "link": "https://watch.amazon.com/...",
  "icon": "https://images.justwatch.com/icon/.../s100/amazonvideo.webp"
}
```

- **`type`** — one of `free`, `subscription`, `rent`, `buy`, `cinema`, or
  `unknown` (JustWatch's monetization type, bucketed — see
  `MONETIZATION_TYPE_MAP` in `scraper.py`). `subscription` covers titles
  included with a service like Netflix or a channel add-on; there's no
  price for those.
- **`price`** — `null` for free/subscription offers.
- **`icon`** — the service's icon, hosted on JustWatch's own image CDN
  (`images.justwatch.com`) rather than scraped from each individual
  streaming site — JustWatch already curates one per package, and we get
  it for free from the same page we're already scraping. Fixed at a
  100px-wide profile and webp format (`ICON_PROFILE`/`ICON_FORMAT` in
  `scraper.py`); `null` if JustWatch had no icon path for that package.

## Deployment (Vercel)

Deploys as-is — Vercel auto-detects the Flask app from `requirements.txt`
and `app.py`. Configured in [`vercel.json`](vercel.json) with
`maxDuration: 300` (5 minutes), which is Vercel's default on every plan
tier including Hobby; the concurrent scraping in `scraper.py` keeps a full
run to a few seconds in practice, well under that.

Before deploying: set `ADMIN_API_KEY` in the Vercel project's Environment
Variables (Settings → Environment Variables). `.env` is gitignored and
never gets deployed, so this is the only place the production secret
lives.

## Security

Last audited 2026-08-08 against SQL injection, brute force, SSRF, path
traversal, command injection, CSRF, XSS, mass assignment, unsafe
deserialization, ReDoS, and dependency pinning. The Supabase-backed `/films`
API postdates that audit; the notes below cover its main surface
(service-role key handling, mass-assignment allow-listing, PostgREST instead
of raw SQL), but it hasn't had a full dedicated audit pass.

- **`/api/scrape` requires `ADMIN_API_KEY`** via the `X-API-Key` header,
  compared with `hmac.compare_digest` (constant-time — plain `!=` leaks
  timing information proportional to how many leading characters match).
- **`url` (GET) and `justwatch_url` (POST body) are restricted to
  `https://www.justwatch.com/...`** (`is_justwatch_url()` in
  `scraper.py`). Without this, either one would let an authenticated
  caller make the server fetch arbitrary URLs on its behalf — an open
  SSRF proxy if the API key ever leaked, e.g. to probe Vercel's internal
  network or cloud metadata endpoints.
- **Supabase access goes through PostgREST, not hand-built SQL** — the
  `/films` endpoints talk to Supabase over its REST API (`supabase_store.py`)
  with parameterized filters, so there's no SQL string for injection to
  target. The **service-role key bypasses row-level security and is
  server-only** — read from the environment, never returned to a client.
- **Writable fields are allow-listed** (`FILM_WRITABLE_FIELDS` in
  `supabase_store.py`) — `POST`/`PATCH /films` only forward `year`, `date`,
  `title`, `justwatch_url`, `sort_order`, so a caller can't set `id`,
  timestamps, or any other column (mass-assignment guard). `justwatch_url`
  is validated with the same `is_justwatch_url()` SSRF check as the scrape
  endpoints.
- **Stored offer URLs are sanitised to https on write**
  (`_https_url_or_none()` in `supabase_store.py`). A service's `link` is
  rendered as an `<a href>` and its `icon` as an `<img src>` for every
  visitor to the site, so neither is trusted verbatim just because it came
  off a JustWatch page — a `javascript:` URI there would be clickable XSS on
  the homepage. This guard previously lived in the frontend; it lives here
  now, and here only, so a consumer that renders what this API returns no
  longer has to re-check it.
- **`requirements.txt`/`requirements-dev.txt` are pinned to exact
  versions**, not left open-ended, so deploys are reproducible and don't
  silently pick up a new (possibly broken or vulnerable) release.
- **Known accepted gaps, not fixed:** error bodies on `500`/`502` include
  raw exception text (no secrets in them, and only reachable by an
  authenticated caller already); there's no rate limiting on repeated
  failed-auth attempts (the 256-bit key makes brute force computationally
  infeasible regardless, and a real fix needs shared state like
  Redis/Vercel Edge Config — a naive in-process limiter wouldn't actually
  work across Vercel's stateless serverless instances).

## Notes for whoever touches this next

- This service used to store its own copy of the film list in
  `data.json`, with `PUT`/`POST /api/years/<year>` to manage it. That's
  gone — the frontend's Supabase database is now the single source of
  truth for which films exist, and `POST /api/scrape` takes that list as
  input instead. If you're looking for that code, it's in history before
  the removal (tracked as issue #15 in this repo).
- The scraper reads `robots.txt` on justwatch.com as of writing and it
  allows crawling. Still, keep concurrency modest (`MAX_CONCURRENT_REQUESTS`
  in `scraper.py`) — this only needs to run a handful of times a year, no
  reason to hammer them.
- If JustWatch changes their page structure, the break point is
  `_extract_json_var` / `_find_offer_refs` in `scraper.py` — both depend on
  the shape of `window.__APOLLO_STATE__`, which isn't a public/stable API.
