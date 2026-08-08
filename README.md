# 31for31scraper

A small Flask service that keeps [`data.json`](data.json) — the streaming
links behind [31for31](https://31for31.vercel.app/) — up to date. It scrapes
[JustWatch](https://www.justwatch.com) for each film and returns the current
streaming offers (free / subscription / rent / buy, with prices and direct
links), so the site's admin can refresh the whole list without hand-checking
every title.

## How it works

Each JustWatch movie page server-renders a `window.__APOLLO_STATE__` blob
containing the exact offer data used to build the page — no headless
browser required. [`scraper.py`](scraper.py) fetches the page, extracts that
blob, and resolves it into a clean list of offers per film.

JustWatch URLs are guessed from the film title (lowercased, hyphenated,
punctuation stripped — see `slugify()` in `scraper.py`). This works for most
titles, but not all: JustWatch sometimes disambiguates a title collision
(e.g. a movie vs. a TV series of the same name) with a slug that can't be
derived from the title text at all. For those, add a `justwatch_url` field
to the film's entry in `data.json` — see [Data format](#data-format) below.
It's also common to catch typos in `data.json` this way (a scrape failing
with "No JustWatch page found" is often just a misspelled title).

## Setup

Requires Python 3.9+ (Vercel deploys on 3.12, pinned in `.python-version`).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set `ADMIN_API_KEY` to a random secret, e.g.:

```bash
openssl rand -hex 32
```

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

### `GET /api/health`

No auth required. Always returns `200 {"status": "ok"}` if the process is
up — a liveness check for uptime monitoring, not a check that JustWatch or
`data.json` are reachable.

### `GET /api/scrape`

Requires an `X-API-Key` header matching `ADMIN_API_KEY`. Requests without a
valid key get `401 Unauthorized`; if the server has no `ADMIN_API_KEY`
configured at all, requests get `500`.

**No query params** — scrapes every film across every year in `data.json`
and returns the same structure back, with each film's `service` array
replaced by the current offers. Non-film keys (like `textContent`) pass
through unchanged. Runs scrapes concurrently (a few requests at a time), so
the full ~90-film list finishes in a few seconds, not minutes.

```bash
curl -H "X-API-Key: $ADMIN_API_KEY" http://127.0.0.1:5000/api/scrape
```

**`?title=`** — scrapes a single film ad hoc, without touching `data.json`.
Useful for testing a title before adding it to the list, or for checking a
`justwatch_url` override works before committing it.

```bash
curl -H "X-API-Key: $ADMIN_API_KEY" \
  "http://127.0.0.1:5000/api/scrape?title=The%20Strangers%20(2008)"
```

**`?title=&url=`** — same as above, but scrapes the given URL directly
instead of guessing the slug. This is how you'd try out a `justwatch_url`
override before writing it to `data.json`.

```bash
curl -H "X-API-Key: $ADMIN_API_KEY" \
  "http://127.0.0.1:5000/api/scrape?title=Event%20Horizon&url=https://www.justwatch.com/us/movie/event-horizon-1997"
```

**A film that fails to scrape doesn't fail the whole request.** It comes
back with `"service": []` and an `"error"` message explaining why (404,
couldn't parse the page, etc.), so a bad title never blocks the other ~90.
Check the response for `error` fields after a full scrape to see what needs
a manual look.

**This endpoint only returns data — it never writes to `data.json`.**
Review the response and copy the parts you want into `data.json` yourself
(or script that step separately). This is deliberate: a bad scrape should
never be able to silently overwrite real data.

## Data format

`data.json` is a dict keyed by year (`"2024"`, `"2025"`, ...) plus a
`textContent` key used for the site's copy (not a film list — left alone by
the scraper). Each year is a list of film entries:

```json
{
  "date": "10/26/2025",
  "title": "Event Horizon",
  "justwatch_url": "https://www.justwatch.com/us/movie/event-horizon-1997",
  "service": [
    {
      "name": "Kanopy",
      "type": "free",
      "price": null,
      "currency": "USD",
      "link": "https://www.kanopy.com/..."
    },
    {
      "name": "Amazon Video",
      "type": "rent",
      "price": 3.99,
      "currency": "USD",
      "link": "https://watch.amazon.com/..."
    }
  ]
}
```

- **`justwatch_url`** (optional) — set this when the guessed slug is wrong.
  Takes priority over slug-guessing whenever present. Not required for most
  films.
- **`service[].type`** — one of `free`, `subscription`, `rent`, `buy`,
  `cinema`, or `unknown` (JustWatch's monetization type, bucketed — see
  `MONETIZATION_TYPE_MAP` in `scraper.py`). `subscription` covers titles
  included with a service like Netflix or a channel add-on; there's no
  price for those.
- **`price`** — `null` for free/subscription offers.

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

## Notes for whoever touches this next

- The scraper reads `robots.txt` on justwatch.com as of writing and it
  allows crawling. Still, keep concurrency modest (`MAX_CONCURRENT_REQUESTS`
  in `scraper.py`) — this only needs to run a handful of times a year, no
  reason to hammer them.
- If JustWatch changes their page structure, the break point is
  `_extract_json_var` / `_find_offer_refs` in `scraper.py` — both depend on
  the shape of `window.__APOLLO_STATE__`, which isn't a public/stable API.
- The film list grows to 93 titles in October — no code changes needed for
  that, just more `data.json` entries.
