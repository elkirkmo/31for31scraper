import pytest
import requests
import responses

from helpers import load_fixture
from scraper import (
    ScrapeError,
    _extract_json_var,
    _find_offer_refs,
    _package_icon_url,
    _resolve_ref,
    build_url,
    is_justwatch_url,
    scrape_title,
    scrape_titles,
    slugify,
)

JUSTWATCH_BASE = "https://www.justwatch.com/us/movie/"


class TestSlugify:
    def test_lowercases_and_hyphenates_spaces(self):
        assert slugify("Blood Feast") == "blood-feast"

    def test_strips_trailing_parenthetical_year(self):
        # JustWatch itself drops these -- "the-strangers-2008" 404s,
        # "the-strangers" is the real slug.
        assert slugify("The Strangers (2008)") == "the-strangers"

    def test_strips_colon(self):
        assert slugify("Tales from the Crypt: Demon Knight") == "tales-from-the-crypt-demon-knight"

    def test_strips_straight_and_curly_apostrophes(self):
        assert slugify("Wes Craven's New Nightmare") == "wes-cravens-new-nightmare"
        assert slugify("Wes Craven’s New Nightmare") == "wes-cravens-new-nightmare"

    def test_collapses_runs_of_punctuation_to_a_single_hyphen(self):
        assert slugify("V/H/S") == "v-h-s"
        assert slugify("Halloween:  The Curse!!") == "halloween-the-curse"

    def test_transliterates_accented_characters(self):
        assert slugify("Amélie") == "amelie"

    def test_all_caps_title(self):
        assert slugify("EVIL DEAD 2") == "evil-dead-2"

    def test_no_leading_or_trailing_hyphens(self):
        assert slugify("  The Movie! (2020)  ") == "the-movie"


def test_build_url_prefixes_base_and_slug():
    assert build_url("Blood Feast") == JUSTWATCH_BASE + "blood-feast"


class TestIsJustwatchUrl:
    def test_accepts_real_justwatch_movie_url(self):
        assert is_justwatch_url("https://www.justwatch.com/us/movie/blood-feast") is True

    def test_rejects_http_even_on_the_right_host(self):
        assert is_justwatch_url("http://www.justwatch.com/us/movie/blood-feast") is False

    def test_rejects_a_different_host_entirely(self):
        # This is the SSRF case: an admin-supplied url= override or
        # justwatch_url pointing anywhere else, e.g. an internal service
        # or cloud metadata endpoint.
        assert is_justwatch_url("https://example.com") is False
        assert is_justwatch_url("https://169.254.169.254/latest/meta-data/") is False

    def test_rejects_lookalike_host(self):
        # A host that merely contains "justwatch.com" isn't the same as
        # being justwatch.com itself.
        assert is_justwatch_url("https://justwatch.com.evil.example/") is False
        assert is_justwatch_url("https://notjustwatch.com/") is False

    def test_rejects_non_string_input(self):
        assert is_justwatch_url(None) is False
        assert is_justwatch_url(12345) is False

    def test_rejects_malformed_url(self):
        assert is_justwatch_url("not a url at all") is False


class TestExtractJsonVar:
    def test_parses_embedded_json(self):
        html = '<script>window.__X__={"a": 1}</script>'
        assert _extract_json_var(html, "__X__") == {"a": 1}

    def test_missing_marker_raises_scrape_error(self):
        with pytest.raises(ScrapeError, match="not found on page"):
            _extract_json_var("<html></html>", "__APOLLO_STATE__")

    def test_missing_closing_script_tag_raises_scrape_error(self):
        with pytest.raises(ScrapeError, match="Could not find end"):
            _extract_json_var('<script>window.__X__={"a": 1}', "__X__")

    def test_malformed_json_raises_scrape_error(self):
        with pytest.raises(ScrapeError, match="Could not parse"):
            _extract_json_var('<script>window.__X__={"a": 1</script>', "__X__")


class TestResolveRef:
    def test_follows_id_ref_into_cache(self):
        cache = {"Movie:1": {"title": "X"}}
        assert _resolve_ref(cache, {"type": "id", "id": "Movie:1"}) == {"title": "X"}

    def test_id_ref_missing_from_cache_returns_empty_dict(self):
        assert _resolve_ref({}, {"type": "id", "id": "Movie:missing"}) == {}

    def test_none_ref_returns_empty_dict(self):
        assert _resolve_ref({}, None) == {}


class TestPackageIconUrl:
    def test_prefers_the_s100_parameterized_field(self):
        # This is the field most packages actually have (profile already
        # baked into the value, only {format} left) -- e.g. Kanopy,
        # FlixHouse, Amazon Video on a real page only ever have this one,
        # not the plain "icon" field below.
        package = {'icon({"profile":"S100"})': "/icon/241588643/s100/kanopy.{format}"}
        assert _package_icon_url(package) == "https://images.justwatch.com/icon/241588643/s100/kanopy.webp"

    def test_falls_back_to_plain_templated_icon_field(self):
        package = {"icon": "/icon/76972041/{profile}/rokuchannel.{format}"}
        assert _package_icon_url(package) == "https://images.justwatch.com/icon/76972041/s100/rokuchannel.webp"

    def test_s100_field_takes_priority_over_plain_icon_field(self):
        # Real packages that have both (e.g. Philo) point at the same
        # underlying image either way, but the S100 field should still
        # win if they ever disagreed.
        package = {
            'icon({"profile":"S100"})': "/icon/111/s100/a.{format}",
            "icon": "/icon/222/{profile}/b.{format}",
        }
        assert _package_icon_url(package) == "https://images.justwatch.com/icon/111/s100/a.webp"

    def test_falls_back_to_any_other_icon_parameterized_field(self):
        # Defensive last resort, in case a package only ever exposes some
        # other profile size.
        package = {'icon({"profile":"S780"})': "/icon/333/s780/c.{format}"}
        assert _package_icon_url(package) == "https://images.justwatch.com/icon/333/s780/c.webp"

    def test_missing_icon_field_returns_none(self):
        assert _package_icon_url({}) is None

    def test_icon_field_none_returns_none(self):
        assert _package_icon_url({"icon": None}) is None

    def test_malformed_template_returns_none_instead_of_raising(self):
        assert _package_icon_url({"icon": "/icon/{unexpected_placeholder}"}) is None


class TestFindOfferRefs:
    def test_prefers_the_largest_real_streaming_list_over_curated_and_physical(self):
        # Mirrors what a real title page actually carries: a curated "jwt"
        # selection, a physical media (DVD/Blu-ray) list, and the real
        # streaming offers -- we want the last one.
        movie = {
            'offers({"filter":{"packages":["jwt"]}})': [{"id": "Offer:jwt1"}],
            'offers({"filter":{"presentationTypes":["DVD"]}})': [
                {"id": "Offer:dvd1"},
                {"id": "Offer:dvd2"},
            ],
            'offers({"filter":{"monetizationTypes":["FLATRATE","RENT"]}})': [
                {"id": "Offer:real1"},
                {"id": "Offer:real2"},
                {"id": "Offer:real3"},
            ],
        }
        refs = _find_offer_refs(movie)
        assert refs == movie['offers({"filter":{"monetizationTypes":["FLATRATE","RENT"]}})']

    def test_no_offers_keys_returns_empty_list(self):
        assert _find_offer_refs({"title": "X"}) == []

    def test_non_list_offers_value_is_ignored(self):
        assert _find_offer_refs({'offers({"filter":{}})': None}) == []

    def test_unparsable_key_args_are_ignored(self):
        movie = {"offers(not valid json)": [{"id": "Offer:1"}]}
        assert _find_offer_refs(movie) == []


class TestScrapeTitle:
    def test_rejects_url_override_pointing_off_justwatch(self):
        # SSRF guard: this must be rejected before any network request is
        # made at all -- no responses.activate here on purpose, so the
        # test would fail with a real connection attempt if the check
        # didn't run first.
        with pytest.raises(ScrapeError, match="Refusing to scrape non-JustWatch URL"):
            scrape_title("Anything", url="https://example.com")

    @responses.activate
    def test_success_returns_all_offer_types_with_correct_values(self):
        url = JUSTWATCH_BASE + "the-thing-from-another-world"
        responses.add(
            responses.GET, url,
            body=load_fixture("the_thing_from_another_world.html"), status=200,
        )

        result = scrape_title("The Thing From Another World")

        assert result["title"] == "The Thing From Another World"
        assert result["url"] == url
        assert len(result["service"]) == 11
        assert {s["type"] for s in result["service"]} == {"free", "subscription", "rent", "buy"}

        roku = next(s for s in result["service"] if s["name"] == "The Roku Channel")
        assert roku == {
            "name": "The Roku Channel",
            "type": "free",
            "price": None,
            "currency": "USD",
            "link": "https://therokuchannel.roku.com/details/369cb0c11bbb5a41829a580de8af302d/the-thing?source=bing",
            "icon": "https://images.justwatch.com/icon/76972041/s100/rokuchannel.webp",
        }

        amazon_rent = next(s for s in result["service"] if s["name"] == "Amazon Video" and s["type"] == "rent")
        assert amazon_rent["price"] == 2.99
        assert amazon_rent["currency"] == "USD"

    @responses.activate
    def test_404_raises_scrape_error(self):
        url = JUSTWATCH_BASE + "not-a-real-movie"
        responses.add(responses.GET, url, status=404)

        with pytest.raises(ScrapeError, match="No JustWatch page found"):
            scrape_title("Not A Real Movie")

    @responses.activate
    def test_follows_redirect_and_returns_the_canonical_url(self):
        # JustWatch 301s some guessed slugs to their canonical one (this
        # actually happened for "Terrifier 2" -> "Terrifier 2 2022").
        guessed = JUSTWATCH_BASE + "terrifier-2"
        canonical = JUSTWATCH_BASE + "terrifier-2-2022"
        responses.add(responses.GET, guessed, status=301, headers={"Location": canonical})
        responses.add(
            responses.GET, canonical,
            body=load_fixture("terrifier_2_redirect_target.html"), status=200,
        )

        result = scrape_title("Terrifier 2")

        assert result["url"] == canonical
        assert len(result["service"]) == 19

    @responses.activate
    def test_explicit_url_override_bypasses_slug_guessing(self):
        override_url = JUSTWATCH_BASE + "the-thing-from-another-world"
        responses.add(
            responses.GET, override_url,
            body=load_fixture("the_thing_from_another_world.html"), status=200,
        )

        result = scrape_title("A Totally Different Title", url=override_url)

        assert result["url"] == override_url
        assert len(result["service"]) == 11

    @responses.activate
    def test_title_with_zero_current_offers_returns_empty_service_list(self):
        url = JUSTWATCH_BASE + "no-offers-title"
        responses.add(
            responses.GET, url,
            body=load_fixture("no_offers_available.html"), status=200,
        )

        result = scrape_title("No Offers Title", url=url)

        assert result["service"] == []

    @responses.activate
    def test_missing_apollo_state_raises_scrape_error(self):
        url = JUSTWATCH_BASE + "ghost-title"
        responses.add(responses.GET, url, body=load_fixture("no_apollo_state.html"), status=200)

        with pytest.raises(ScrapeError, match="not found on page"):
            scrape_title("Ghost Title", url=url)

    @responses.activate
    def test_malformed_apollo_state_raises_scrape_error(self):
        url = JUSTWATCH_BASE + "broken-title"
        responses.add(responses.GET, url, body=load_fixture("malformed_apollo_state.html"), status=200)

        with pytest.raises(ScrapeError, match="Could not parse"):
            scrape_title("Broken Title", url=url)

    @responses.activate
    def test_no_matching_url_entity_raises_scrape_error(self):
        # Page loaded fine and has real __APOLLO_STATE__, but nothing in it
        # matches the path we landed on (e.g. served a listing page).
        url = JUSTWATCH_BASE + "some-title"
        responses.add(responses.GET, url, body=load_fixture("no_matching_url_entity.html"), status=200)

        with pytest.raises(ScrapeError, match="Could not locate title data"):
            scrape_title("Some Title", url=url)

    @responses.activate
    def test_movie_entity_missing_from_cache_raises_scrape_error(self):
        url = JUSTWATCH_BASE + "ghost-title"
        responses.add(responses.GET, url, body=load_fixture("missing_movie_node.html"), status=200)

        with pytest.raises(ScrapeError, match="Could not locate movie data"):
            scrape_title("Ghost Title", url=url)

    @responses.activate
    def test_network_error_propagates_uncaught(self):
        # scrape_title itself does NOT swallow network errors -- that's
        # scrape_titles'/app.py's job. Verifies the contract they rely on.
        url = JUSTWATCH_BASE + "blood-feast"
        responses.add(responses.GET, url, body=requests.exceptions.ConnectTimeout("boom"))

        with pytest.raises(requests.exceptions.ConnectTimeout):
            scrape_title("Blood Feast")


class TestScrapeTitles:
    @responses.activate
    def test_continues_past_individual_failures(self):
        # ok_url deliberately doesn't match the guessed slug for "Blood
        # Feast" -- it's passed as an explicit override, same as a
        # justwatch_url entry would be.
        ok_url = JUSTWATCH_BASE + "the-thing-from-another-world"
        bad_url = JUSTWATCH_BASE + "not-a-real-movie"
        responses.add(
            responses.GET, ok_url,
            body=load_fixture("the_thing_from_another_world.html"), status=200,
        )
        responses.add(responses.GET, bad_url, status=404)

        results = scrape_titles([("Blood Feast", ok_url), ("Not A Real Movie", bad_url)])
        by_title = {r["title"]: r for r in results}

        assert "error" not in by_title["Blood Feast"]
        assert len(by_title["Blood Feast"]["service"]) == 11

        assert by_title["Not A Real Movie"]["service"] == []
        assert "No JustWatch page found" in by_title["Not A Real Movie"]["error"]

    @responses.activate
    def test_accepts_plain_title_strings_without_url_override(self):
        # Title's own guessed slug must match the fixture's real page --
        # no url override passed here, unlike the test above.
        url = JUSTWATCH_BASE + "the-thing-from-another-world"
        responses.add(
            responses.GET, url,
            body=load_fixture("the_thing_from_another_world.html"), status=200,
        )

        results = scrape_titles(["The Thing From Another World"])

        assert results[0]["title"] == "The Thing From Another World"
        assert results[0]["url"] == url
        assert len(results[0]["service"]) == 11

    @responses.activate
    def test_catches_network_errors_per_title_instead_of_raising(self):
        url = JUSTWATCH_BASE + "blood-feast"
        responses.add(responses.GET, url, body=requests.exceptions.ConnectionError("boom"))

        results = scrape_titles(["Blood Feast"])

        assert results[0]["service"] == []
        assert "error" in results[0]

    def test_empty_input_returns_empty_list(self):
        assert scrape_titles([]) == []
