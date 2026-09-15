"""Scrapes current streaming offers for a film from its JustWatch page.

JustWatch server-renders each title page with the normalized Apollo GraphQL
cache it used to build the page. That cache holds the exact data we need
(which services carry the film, at what price, under what monetization type)
without needing a headless browser.

There are two page generations in the wild, and JustWatch is migrating title
by title from the first to the second, so we support both:

* Old Vue frontend: `window.__APOLLO_STATE__={...}`, whose "defaultClient" is
  the cache, with references encoded as {"type":"id","id":...}.
* New Nuxt frontend: the same cache, serialized inside
  `<script id="__NUXT_DATA__">` in devalue's index-referenced array form under
  the "apollo:default" client, with references encoded as {"__ref":...}
  (Apollo Client 3).
"""
import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

JUSTWATCH_BASE_URL = "https://www.justwatch.com/us/movie/"
JUSTWATCH_HOST = urlparse(JUSTWATCH_BASE_URL).hostname
JUSTWATCH_SCHEME = urlparse(JUSTWATCH_BASE_URL).scheme
JUSTWATCH_ICON_BASE_URL = "https://images.justwatch.com"
ICON_PROFILE = "s100"
ICON_FORMAT = "webp"
REQUEST_TIMEOUT = 12
MAX_CONCURRENT_REQUESTS = 8
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# JustWatch's monetizationType -> the bucket we report it under.
MONETIZATION_TYPE_MAP = {
    "FREE": "free",
    "ADS": "free",
    "FAST": "free",
    "FLATRATE": "subscription",
    "FLATRATE_AND_BUY": "subscription",
    "RENT": "rent",
    "BUY": "buy",
    "CINEMA": "cinema",
}

# Presentation types that mean "physical media", not a streaming offer.
PHYSICAL_PRESENTATION_TYPES = {"DVD", "BLURAY", "BLURAY_4K"}


class ScrapeError(Exception):
    """Raised when a title's JustWatch page can't be fetched or parsed."""


def slugify(title):
    """Match JustWatch's URL slug convention for a film title.

    JustWatch drops a trailing disambiguation year (e.g. "The Strangers
    (2008)" lives at /the-strangers, not /the-strangers-2008) and strips
    other punctuation.
    """
    title = re.sub(r"\s*\([^)]*\)\s*$", "", title)
    title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    title = title.lower()
    title = re.sub(r"['’:]", "", title)
    title = re.sub(r"[^a-z0-9]+", "-", title)
    return title.strip("-")


def build_url(title):
    return JUSTWATCH_BASE_URL + slugify(title)


def is_justwatch_url(url):
    """True if `url` is an https URL on JustWatch's own domain.

    Guards the `url`/`justwatch_url` override on scrape_title against
    being used as an open SSRF proxy. Callers already require the admin
    API key, but a leaked key or a mistaken entry shouldn't be able to
    make this server fetch arbitrary internal or third-party URLs.
    """
    if not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == JUSTWATCH_SCHEME and parsed.hostname == JUSTWATCH_HOST


def _extract_json_var(html, var_name):
    marker = "window.{}=".format(var_name)
    start = html.find(marker)
    if start == -1:
        raise ScrapeError("{} not found on page".format(var_name))
    start += len(marker)
    end = html.find("</script>", start)
    if end == -1:
        raise ScrapeError("Could not find end of {} script tag".format(var_name))
    try:
        return json.loads(html[start:end])
    except ValueError as exc:
        raise ScrapeError("Could not parse {}: {}".format(var_name, exc))


def _extract_nuxt_apollo_cache(html):
    """Reconstruct the Apollo cache from a Nuxt page's __NUXT_DATA__ payload.

    Nuxt serializes its payload with `devalue`: a flat JSON array where every
    value is hoisted into its own slot and referenced by integer index, with a
    handful of string-tagged tuples (e.g. ["ShallowReactive", 1]) for reactive
    wrappers. We don't need to understand the whole tree -- the Apollo cache is
    the one hoisted object keyed by entity ids, which is the only dict carrying
    a "ROOT_QUERY" entry. Resolving that slot back yields the same
    {entityId: entity} shape as the old __APOLLO_STATE__ cache.
    """
    match = re.search(r'<script[^>]*\bid="__NUXT_DATA__"[^>]*>', html)
    if not match:
        raise ScrapeError("__APOLLO_STATE__ not found on page")
    start = match.end()
    end = html.find("</script>", start)
    if end == -1:
        raise ScrapeError("Could not find end of __NUXT_DATA__ script tag")
    try:
        flat = json.loads(html[start:end])
    except ValueError as exc:
        raise ScrapeError("Could not parse __NUXT_DATA__: {}".format(exc))
    if not isinstance(flat, list):
        raise ScrapeError("Unexpected __NUXT_DATA__ shape")

    cache_index = next(
        (i for i, v in enumerate(flat) if isinstance(v, dict) and "ROOT_QUERY" in v),
        None,
    )
    if cache_index is None:
        raise ScrapeError("__APOLLO_STATE__ not found on page")
    return _resolve_nuxt_index(flat, cache_index)


def _resolve_nuxt_index(flat, root_index):
    """Walk a devalue-flattened array back into ordinary nested dicts/lists.

    Each slot is a scalar, an object/array whose members are indices into
    `flat`, or a string-tagged tuple for a special type (we unwrap those to
    their payload -- the only ones on the path into the Apollo cache are
    Nuxt's reactive/ref wrappers). Memoized so repeated indices resolve to the
    same object and any cycle resolves to a single shared instance.
    """
    memo = {}

    def resolve(index):
        if not isinstance(index, int) or isinstance(index, bool):
            return index
        # devalue's negative sentinels (undefined/NaN/Infinity) never appear in
        # the JSON-shaped Apollo cache; collapse them to None rather than guess.
        if index < 0:
            return None
        if index in memo:
            return memo[index]
        value = flat[index]
        if isinstance(value, dict):
            out = {}
            memo[index] = out  # seed before recursing so cycles land back here
            for key, ref in value.items():
                out[key] = resolve(ref)
            return out
        if isinstance(value, list):
            if value and isinstance(value[0], str):
                # String-tagged tuple, not a plain array (whose members would
                # all be integer indices). Unwrap to its payload.
                inner = resolve(value[1]) if len(value) > 1 else None
                memo[index] = inner
                return inner
            out = []
            memo[index] = out
            for ref in value:
                out.append(resolve(ref))
            return out
        memo[index] = value
        return value

    return resolve(root_index)


def _extract_apollo_cache(html):
    """Return JustWatch's Apollo cache from a title page, old or new format."""
    if "window.__APOLLO_STATE__=" in html:
        return _extract_json_var(html, "__APOLLO_STATE__").get("defaultClient", {})
    return _extract_nuxt_apollo_cache(html)


def _resolve_ref(cache, ref):
    if isinstance(ref, dict):
        if "__ref" in ref:  # Apollo Client 3, on the Nuxt pages
            return cache.get(ref["__ref"], {})
        if ref.get("type") == "id":  # older Apollo cache, on the Vue pages
            return cache.get(ref["id"], {})
    return ref or {}


def _package_icon_url(package):
    """Build a full icon URL from a Package entity's icon path.

    JustWatch exposes this two different ways depending on the package,
    and most packages only have the first: a size-parameterized field
    with the profile already baked into the value, e.g.
    icon({"profile":"S100"}) -> "/icon/241588643/s100/kanopy.{format}"
    (only {format} left to fill in). A few packages additionally have a
    plain "icon" field instead, with both {profile} and {format} open,
    e.g. "/icon/76972041/{profile}/rokuchannel.{format}". Same fixed
    size/format either way, at least for now.
    """
    icon_path = (
        package.get('icon({"profile":"S100"})')
        or package.get("icon")
        or next(
            (v for k, v in package.items() if k.startswith("icon(") and isinstance(v, str)),
            None,
        )
    )
    if not icon_path:
        return None
    try:
        return JUSTWATCH_ICON_BASE_URL + icon_path.format(profile=ICON_PROFILE, format=ICON_FORMAT)
    except (KeyError, IndexError):
        return None


def _find_offer_refs(movie):
    """Pick the offers(...) field that lists real streaming offers.

    A title page's Movie entity carries several `offers(<json args>)`
    fields for different purposes (a curated "JustWatch selection" list,
    physical media listings, etc). We want the one filtered on real
    monetization types, not packages or physical presentation types.
    """
    best_key, best_value = None, []
    for key, value in movie.items():
        if not key.startswith("offers(") or not isinstance(value, list):
            continue
        try:
            args = json.loads(key[len("offers("):-1])
        except ValueError:
            continue
        filt = args.get("filter", {})
        if "packages" in filt:
            continue
        if set(filt.get("presentationTypes", [])) & PHYSICAL_PRESENTATION_TYPES:
            continue
        if len(value) > len(best_value):
            best_key, best_value = key, value
    return best_value


def scrape_title(title, session=None, url=None):
    """Scrape current JustWatch offers for a single film title.

    Pass `url` for titles whose guessed slug is wrong -- e.g. a collision
    JustWatch resolved with a year suffix it doesn't otherwise expose
    ("Event Horizon" (the movie) vs. the TV series). Callers pass this as
    an optional "justwatch_url" per film.

    Returns {"title": title, "url": ..., "service": [...]}.
    Raises ScrapeError if the page can't be found or parsed.
    """
    session = session or requests.Session()
    url = url or build_url(title)
    if not is_justwatch_url(url):
        raise ScrapeError("Refusing to scrape non-JustWatch URL: {}".format(url))

    resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 404:
        raise ScrapeError("No JustWatch page found at {}".format(url))
    resp.raise_for_status()

    cache = _extract_apollo_cache(resp.text)

    # JustWatch sometimes 301s a guessed slug to its canonical one (e.g.
    # "terrifier-2" -> "terrifier-2-2022"), so match against where we
    # actually landed rather than the slug we guessed.
    expected_path = urlparse(resp.url).path
    url_entity = next(
        (v for k, v in cache.items() if k.startswith("Url:") and v.get("fullPath") == expected_path),
        None,
    )
    if url_entity is None:
        raise ScrapeError("Could not locate title data on {}".format(url))

    movie = _resolve_ref(cache, url_entity.get("node"))
    if not movie:
        raise ScrapeError("Could not locate movie data on {}".format(url))

    services = []
    for ref in _find_offer_refs(movie):
        offer = _resolve_ref(cache, ref)
        if not offer:
            continue
        package = _resolve_ref(cache, offer.get("package"))
        monetization = offer.get("monetizationType")
        services.append({
            "name": package.get("clearName"),
            "type": MONETIZATION_TYPE_MAP.get(monetization, (monetization or "unknown").lower()),
            "price": offer.get("retailPriceValue"),
            "currency": offer.get("currency"),
            "link": offer.get("standardWebURL") or offer.get("preAffiliatedStandardWebURL"),
            "icon": _package_icon_url(package),
        })

    # Report the canonical URL we actually landed on, not the guessed one --
    # matters when JustWatch redirected us to a different slug.
    return {"title": title, "url": resp.url, "service": services}


def _scrape_title_safe(title_and_url):
    title, url = title_and_url
    try:
        return scrape_title(title, session=requests.Session(), url=url)
    except (ScrapeError, requests.RequestException) as exc:
        return {"title": title, "url": url or build_url(title), "service": [], "error": str(exc)}


def scrape_titles(titles, max_workers=MAX_CONCURRENT_REQUESTS):
    """Scrape a list of titles concurrently, a few requests at a time.

    `titles` is an iterable of either plain title strings or (title, url)
    pairs -- pass the latter for titles that need an explicit
    "justwatch_url" override instead of the guessed slug.

    Bounded concurrency keeps this fast enough to fit inside a single
    serverless function invocation (e.g. Vercel's default 300s limit) while
    staying polite to JustWatch. Never raises for an individual title's
    failure -- a bad scrape for one film shouldn't abort the whole batch.
    Failures are reported inline via an "error" key so the caller can see
    which titles need a manual look.
    """
    normalized = [t if isinstance(t, tuple) else (t, None) for t in titles]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return list(executor.map(_scrape_title_safe, normalized))
