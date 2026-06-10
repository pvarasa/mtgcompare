import json
from pathlib import Path

import pytest

from mtgcompare.scrapers import scryfall
from mtgcompare.scrapers.scryfall import record_from_summary, summarize_page

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def records_from_pages(pages, card_name, fx_jpy_per_usd):
    """Mirror what `ScryfallScrapper.get_prices` does end-to-end: summarize
    each page, project priced summaries into records. Lives here in tests
    because production streams page-by-page inside fetch_card_summaries."""
    target = card_name.strip().lower()
    out = []
    for page in pages:
        for summary in summarize_page(page, target):
            record = record_from_summary(summary, fx_jpy_per_usd)
            if record is not None:
                out.append(record)
    return out


@pytest.fixture
def fow_page() -> dict:
    return json.loads(
        (FIXTURES / "scryfall_force_of_will.json").read_text(encoding="utf-8")
    )


def test_parse_returns_records_for_matching_card(fow_page):
    records = records_from_pages([fow_page], "Force of Will", fx_jpy_per_usd=150.0)
    assert records, "expected at least one Force of Will record in the fixture"
    for r in records:
        assert r["shop"] == "TCGPlayer market"
        assert r["card"] == "Force of Will"
        assert isinstance(r["set"], str) and r["set"]
        assert isinstance(r["price_usd"], float) and r["price_usd"] > 0
        assert isinstance(r["price_jpy"], float) and r["price_jpy"] > 0
        assert r["stock"] is None
        assert r["condition"] == "NM"
        assert r["link"].startswith("http")


def test_parse_skips_printings_without_usd_price(fow_page):
    # The fixture includes digital-only printings (e.g. VMA) with no usd.
    printings_with_usd = [
        c for c in fow_page["data"]
        if (c.get("prices") or {}).get("usd") and c["name"].lower() == "force of will"
    ]
    records = records_from_pages([fow_page], "Force of Will", fx_jpy_per_usd=150.0)
    assert len(records) == len(printings_with_usd)


def test_summaries_include_unpriced_printings_and_product_ids(fow_page):
    # The ships-to-JP scraper consumes summaries directly and needs every
    # printing (priced or not) with its tcgplayer_id surfaced.
    summaries = summarize_page(fow_page, "force of will")
    all_printings = [c for c in fow_page["data"] if c["name"].lower() == "force of will"]
    assert len(summaries) == len(all_printings)
    for s in summaries:
        assert set(s) == {"name", "set", "usd", "tcgplayer_id", "link"}


def test_parse_case_insensitive_match(fow_page):
    upper = records_from_pages([fow_page], "FORCE OF WILL", fx_jpy_per_usd=150.0)
    mixed = records_from_pages([fow_page], "Force of Will", fx_jpy_per_usd=150.0)
    assert len(upper) == len(mixed) > 0


def test_parse_ignores_non_matching_card(fow_page):
    assert records_from_pages([fow_page], "Some Other Card", fx_jpy_per_usd=150.0) == []


def test_record_links_to_filtered_product_page_when_id_known():
    """prices.usd is the non-foil market price — the link must open the
    Normal-finish listing view, not the foil-mixed default page."""
    page = {
        "data": [{
            "name": "Foo",
            "set": "mh3",
            "prices": {"usd": "2.92"},
            "tcgplayer_id": 552336,
            "purchase_uris": {"tcgplayer": "https://partner.tcgplayer.example/affiliate"},
        }],
        "has_more": False,
    }
    [r] = records_from_pages([page], "Foo", fx_jpy_per_usd=150.0)
    assert r["link"] == (
        "https://www.tcgplayer.com/product/552336?Language=English&Printing=Normal"
    )


def test_parse_handcrafted_fx_conversion():
    page = {
        "data": [
            {
                "name": "Foo",
                "set": "bar",
                "prices": {"usd": "50.00", "usd_foil": "100.00"},
                "purchase_uris": {"tcgplayer": "https://tcgplayer.example/foo"},
                "scryfall_uri": "https://scryfall.com/card/bar/1",
            }
        ],
        "has_more": False,
    }
    records = records_from_pages([page], "Foo", fx_jpy_per_usd=150.0)
    assert len(records) == 1
    r = records[0]
    assert r == {
        "shop": "TCGPlayer market",
        "card": "Foo",
        "set": "BAR",
        "price_jpy": 7500.0,
        "price_usd": 50.0,
        "stock": None,
        "condition": "NM",
        "link": "https://tcgplayer.example/foo",
    }


def test_parse_concatenates_multiple_pages():
    page1 = {
        "data": [
            {"name": "Foo", "set": "a", "prices": {"usd": "1.00"}, "purchase_uris": {}}
        ],
        "has_more": True,
    }
    page2 = {
        "data": [
            {"name": "Foo", "set": "b", "prices": {"usd": "2.00"}, "purchase_uris": {}}
        ],
        "has_more": False,
    }
    records = records_from_pages([page1, page2], "Foo", fx_jpy_per_usd=100.0)
    assert [r["set"] for r in records] == ["A", "B"]


@pytest.fixture
def clean_memo():
    scryfall._summaries_cache.clear()
    scryfall._summaries_locks.clear()
    yield
    scryfall._summaries_cache.clear()
    scryfall._summaries_locks.clear()


def test_fetch_card_summaries_memoizes_shared_session(monkeypatch, clean_memo):
    """Both TCGPlayer shops resolve the same card per search; the memo must
    collapse that into one Scryfall walk (keyed on the normalized name)."""
    calls: list[str] = []
    monkeypatch.setattr(
        scryfall, "_walk_summaries",
        lambda name, session: calls.append(name) or [{"name": name}],
    )
    first = scryfall.fetch_card_summaries("Foo Bar")
    second = scryfall.fetch_card_summaries("  foo   bar ")
    assert first == second == [{"name": "Foo Bar"}]
    assert calls == ["Foo Bar"]


def test_fetch_card_summaries_custom_session_bypasses_memo(monkeypatch, clean_memo):
    """Injected sessions (tests, error-path probes) must never read or
    populate the shared memo."""
    calls: list[str] = []
    monkeypatch.setattr(
        scryfall, "_walk_summaries",
        lambda name, session: calls.append(name) or [],
    )
    sentinel_session = object()
    scryfall.fetch_card_summaries("Foo", sentinel_session)
    scryfall.fetch_card_summaries("Foo", sentinel_session)
    assert calls == ["Foo", "Foo"]
    assert scryfall._summaries_cache == {}
