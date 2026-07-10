"""The htmx UI: pages render, the query fragment swaps, attacks show a message."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_datasets_page_lists_the_table(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "gdp_annual" in r.text
    assert "read-only" in r.text.lower()


def test_table_detail_shows_download_buttons(client):
    r = client.get("/table/gdp_annual")
    assert r.status_code == 200
    assert "/api/tables/gdp_annual/export.csv" in r.text
    assert "/api/tables/gdp_annual/export.xlsx" in r.text
    assert "Columns" in r.text


def test_table_detail_unknown_is_404(client):
    assert client.get("/table/secrets").status_code == 404


def test_query_page_renders(client):
    r = client.get("/query")
    assert r.status_code == 200
    assert "<textarea" in r.text


def test_query_page_prefills_sql_from_query_string(client):
    r = client.get("/query", params={"sql": "SELECT 1"})
    assert "SELECT 1" in r.text


def test_htmx_query_run_returns_a_result_fragment(client):
    r = client.post(
        "/query/run",
        data={"sql": "SELECT country_iso3, sum(gdp_usd) g FROM lake.gdp_annual GROUP BY 1"},
    )
    assert r.status_code == 200
    # a fragment, not a whole page
    assert "<html" not in r.text.lower()
    assert "table" in r.text.lower()
    # download links carry the query
    assert "/api/query/export.csv" in r.text


def test_htmx_query_run_shows_the_guard_error_not_a_500(client):
    r = client.post("/query/run", data={"sql": "DROP TABLE lake.gdp_annual"})
    assert r.status_code == 200  # the fragment renders; the error is inside it
    assert "not permitted" in r.text
    # rendered as a Basecoat destructive alert, not a raw error div
    assert 'class="alert"' in r.text
    assert 'data-variant="destructive"' in r.text


def test_htmx_query_run_renders_bars_for_a_two_column_result(client):
    r = client.post(
        "/query/run",
        data={"sql": "SELECT country_iso3, sum(gdp_usd) g FROM lake.gdp_annual GROUP BY 1"},
    )
    assert "bar-fill" in r.text


def test_ask_page_renders(client):
    r = client.get("/ask")
    assert r.status_code == 200
    assert "Ask" in r.text


def test_static_htmx_is_served(client):
    r = client.get("/static/htmx.min.js")
    assert r.status_code == 200
    assert "htmx" in r.text[:200].lower()


def test_basecoat_assets_are_vendored_and_served(client):
    """No CDN — the design system ships with the app, so it works air-gapped."""
    css = client.get("/static/basecoat.min.css")
    assert css.status_code == 200
    assert ".btn" in css.text and ".card" in css.text
    assert client.get("/static/basecoat-all.min.js").status_code == 200


def test_fonts_are_vendored_and_served(client):
    """IBM Plex ships with the app too. A CDN font would silently fall back to
    Helvetica air-gapped, losing Carbon's weight-300 display treatment."""
    css = client.get("/static/fonts.css")
    assert css.status_code == 200
    assert "IBM Plex Sans" in css.text
    for face in ("ibm-plex-sans.woff2", "ibm-plex-mono.woff2"):
        r = client.get(f"/static/{face}")
        assert r.status_code == 200, face
        assert r.content[:4] == b"wOF2", f"{face} is not woff2"


def test_no_external_asset_references():
    """Nothing the browser loads may point off-host."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "src" / "lake" / "api"
    watched = [*(root / "templates").glob("*.html"), root / "static" / "app.css",
               root / "static" / "fonts.css"]
    for path in watched:
        text = path.read_text(encoding="utf-8")
        for marker in ("https://fonts.googleapis.com", "https://fonts.gstatic.com", "cdn."):
            assert marker not in text, f"{path.name} references {marker}"


def test_logo_is_vendored_and_served(client):
    """The logo is masked, not drawn, so one file serves light, dark, and the
    charcoal footer. It still has to ship with the app."""
    r = client.get("/static/logo.png")
    assert r.status_code == 200
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_about_and_contact_pages_render(client):
    for path, marker in (("/about", "The pipeline"), ("/contact", "Get in touch")):
        r = client.get(path)
        assert r.status_code == 200, path
        assert marker in r.text, path


def test_dataset_list_is_reachable_from_the_nav(client):
    """The dataset list is the point of the site. It must be its own page, linked
    from the nav — not buried in a scroll position on the landing page."""
    body = client.get("/").text
    assert 'href="/datasets"' in body
    assert client.get("/datasets").status_code == 200


def test_datasets_page_cards_carry_the_required_fields(client):
    """Each card shows title, source, latest update, labels, and a description."""
    body = client.get("/datasets").text
    assert "gdp_annual" in body  # title
    assert "World Bank GDP indicator" in body  # source
    assert "GDP in current US dollars" in body  # description, from sources.yaml
    assert "queryable" in body  # label
    assert "Updated" in body or "Never collected" in body  # latest update


def test_only_queryable_cards_offer_a_query_link(client):
    """A source with no transform must never look queryable. This is the whole
    reason the card carries a `queryable` flag rather than assuming."""
    body = client.get("/datasets").text
    assert "bps_inflation" in body  # it is listed…
    assert 'href="/table/bps_inflation"' not in body  # …but not linkable
    assert "Collected, not yet queryable" in body
    assert 'href="/table/gdp_annual"' in body  # the published one is


def test_datasets_page_links_resolve(client):
    """Every action on a dataset card must reach a real endpoint."""
    assert client.get("/table/gdp_annual").status_code == 200
    assert client.get("/api/tables/gdp_annual/export.csv").status_code == 200
    assert client.get("/api/tables/gdp_annual/export.xlsx").status_code == 200


def test_dataset_detail_breadcrumb_returns_to_datasets(client):
    body = client.get("/table/gdp_annual").text
    assert 'href="/datasets"' in body


def test_datasets_page_declares_provenance_or_nothing(client):
    """A dataset whose source is unknown must show no source, never a guess."""
    from lake.api import catalog

    assert catalog.DATASET_SOURCE["gdp_annual"] == "worldbank_gdp"
    # with no source registry, the published dataset still appears — otherwise it
    # would be queryable but invisible — but claims no provenance it cannot prove
    cards = catalog.dataset_cards([])
    gdp = next(c for c in cards if c["dataset"] == "gdp_annual")
    assert gdp["queryable"] is True
    assert gdp["source_name"] is None
    assert gdp["description"] is None


def _fake_cards():
    return [
        {"title": "gdp_annual", "dataset": "gdp_annual", "queryable": True,
         "source_id": "worldbank_gdp", "source_name": "World Bank GDP indicator",
         "description": "GDP in current US dollars.", "kind": "api", "enabled": True},
        {"title": "bps_inflation", "dataset": None, "queryable": False,
         "source_id": "bps_inflation", "source_name": "BPS monthly inflation",
         "description": "Consumer price inflation, an Excel workbook.",
         "kind": "file", "enabled": True},
        {"title": "census_full", "dataset": None, "queryable": False,
         "source_id": "census_full", "source_name": "Annual census dump",
         "description": "A gzipped CSV.", "kind": "file", "enabled": False},
    ]


def test_search_matches_title_source_and_description():
    from lake.api.catalog import filter_cards

    cards = _fake_cards()
    titles = lambda **kw: [c["title"] for c in filter_cards(cards, **kw)]  # noqa: E731

    assert titles(q="gdp_annual") == ["gdp_annual"]  # title
    assert titles(q="World Bank") == ["gdp_annual"]  # source name
    assert titles(q="Excel workbook") == ["bps_inflation"]  # description
    assert titles(q="census_full") == ["census_full"]  # source_id
    assert titles(q="GDP") == ["gdp_annual"]  # case-insensitive
    assert titles(q="   gdp   ") == ["gdp_annual"]  # trimmed
    assert titles(q="nothing-matches-this") == []


def test_filters_narrow_and_compose():
    from lake.api.catalog import filter_cards

    cards = _fake_cards()
    titles = lambda **kw: [c["title"] for c in filter_cards(cards, **kw)]  # noqa: E731

    assert titles(kind="file") == ["bps_inflation", "census_full"]
    assert titles(status="queryable") == ["gdp_annual"]
    assert titles(status="raw") == ["bps_inflation", "census_full"]
    assert titles(status="paused") == ["census_full"]
    # search and filter compose rather than override each other
    assert titles(q="census", kind="file") == ["census_full"]
    assert titles(q="gdp", kind="file") == []
    # no filters is the identity
    assert len(filter_cards(cards)) == 3


def test_search_works_without_javascript(client):
    """The form is a real GET. A filtered view must be a shareable URL that the
    server renders on its own, so it survives JS being off."""
    r = client.get("/datasets", params={"q": "inflation"})
    assert r.status_code == 200
    assert "bps_inflation" in r.text
    assert "gdp_annual" not in r.text.split('id="cards"')[1]
    # the form posts back to the same route, and carries a submit button
    assert 'action="/datasets"' in r.text
    assert 'method="get"' in r.text
    assert 'type="submit"' in r.text


def test_card_fragment_endpoint_returns_only_cards(client):
    """htmx swaps this fragment in; it must not be a whole page."""
    r = client.get("/datasets/cards", params={"q": "gdp"})
    assert r.status_code == 200
    assert "gdp_annual" in r.text
    assert "<html" not in r.text.lower()
    assert "<nav" not in r.text.lower()


def test_fragment_pushes_the_page_url_not_its_own(client):
    """htmx would otherwise put /datasets/cards?... in the address bar, and
    reloading that gives a bare grid with no nav. The server hands back the page
    URL so a searched view stays a shareable link."""
    r = client.get("/datasets/cards", params={"q": "inflation"})
    assert r.headers["HX-Push-Url"] == "/datasets?q=inflation"
    # empty filters are dropped rather than pushed as `?q=&kind=&status=`
    assert client.get("/datasets/cards").headers["HX-Push-Url"] == "/datasets"
    # and that pushed URL really does render a full page
    full = client.get("/datasets", params={"q": "inflation"})
    assert "<nav" in full.text.lower()
    assert "bps_inflation" in full.text


def _contrast(fg: str, bg: str) -> float:
    """WCAG 2.x relative-luminance contrast ratio for two #rrggbb colours."""

    def luminance(hex_colour: str) -> float:
        channels = (int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_status_label_colours_meet_wcag_aa_in_both_themes():
    """Carbon's green-50 is only 3.35:1 on white — below AA for text — so the
    light theme takes green-60 and the dark theme keeps green-50. Whatever the
    CSS says, the rendered colours must clear 4.5:1 against their own surface.
    """
    import re
    from pathlib import Path

    css = (
        Path(__file__).resolve().parents[3] / "src" / "lake" / "api" / "static" / "app.css"
    ).read_text(encoding="utf-8")

    light = re.search(r"\.label-queryable \{[^}]*color: (#[0-9a-f]{6})", css)
    dark = re.search(r"\.dark \.label-queryable \{[^}]*color: ([^;}]+)", css)
    assert light and dark, "label-queryable must be themed for light and dark"

    assert _contrast(light.group(1), "#ffffff") >= 4.5
    # the dark rule points at --carbon-success, whose value is defined once
    success = re.search(r"--carbon-success: (#[0-9a-f]{6})", css)
    assert success and _contrast(success.group(1), "#161616") >= 4.5


def test_empty_search_result_offers_a_way_back(client):
    r = client.get("/datasets", params={"q": "zzzz-no-such-dataset"})
    assert r.status_code == 200
    assert "No datasets match" in r.text
    assert 'href="/datasets"' in r.text  # clear the filters


def test_contact_form_rejects_bad_input(client):
    r = client.post("/contact/send", data={"name": "", "email": "nope", "message": "hi"})
    assert r.status_code == 422
    assert "Tell us who you are." in r.text
    assert "doesn&#39;t look right" in r.text or "doesn't look right" in r.text


def test_contact_form_accepts_and_escapes(client):
    r = client.post(
        "/contact/send",
        data={
            "name": "<script>alert(1)</script>",
            "email": "a@b.com",
            "message": "A message that is comfortably long enough to pass.",
        },
    )
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text  # never reflected raw
    assert "&lt;script&gt;" in r.text


def test_contact_form_caps_message_length(client):
    r = client.post(
        "/contact/send",
        data={"name": "Bo", "email": "a@b.com", "message": "x" * 4001},
    )
    assert r.status_code == 422
    assert "too long" in r.text


def test_reveal_animations_are_additive(client):
    """Motion must never gate content: `will-reveal` (opacity 0) is applied by
    script, so with JS off, or with reduced motion, everything renders visible.
    Server-rendering that class anywhere would hide content from those readers.
    """
    import re

    body = client.get("/").text
    assert "data-reveal" in body
    # `will-reveal` legitimately appears inside the inline script that adds it.
    # What must never appear is the class on a rendered element.
    rendered = [m for m in re.findall(r'class="([^"]*)"', body) if "will-reveal" in m]
    assert not rendered, f"will-reveal server-rendered on: {rendered}"
    css = client.get("/static/app.css").text
    assert "prefers-reduced-motion" in css


def test_pages_use_basecoat_components(client):
    """Interactive chrome comes from Basecoat, not hand-rolled classes.

    Surfaces (the Carbon `.tile`) are ours: Basecoat's `.card` carries a radius
    and a shadow, and Carbon has neither.
    """
    body = client.get("/").text
    assert "basecoat.min.css" in body
    assert 'class="btn"' in body
    assert 'class="badge"' in body  # read-only badge
