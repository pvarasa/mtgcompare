import json
from pathlib import Path

import pytest

from mtgcompare.scrappers.scryfall import parse_search_response

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fow_page() -> dict:
    return json.loads(
        (FIXTURES / "scryfall_force_of_will.json").read_text(encoding="utf-8")
    )


def test_parse_returns_records_for_matching_card(fow_page):
    records = parse_search_response([fow_page], "Force of Will", fx_jpy_per_usd=150.0)
    assert records, "expected at least one Force of Will record in the fixture"
    for r in records:
        assert r["shop"] == "TCGPlayer (Scryfall)"
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
    records = parse_search_response([fow_page], "Force of Will", fx_jpy_per_usd=150.0)
    assert len(records) == len(printings_with_usd)


def test_parse_case_insensitive_match(fow_page):
    upper = parse_search_response([fow_page], "FORCE OF WILL", fx_jpy_per_usd=150.0)
    mixed = parse_search_response([fow_page], "Force of Will", fx_jpy_per_usd=150.0)
    assert len(upper) == len(mixed) > 0


def test_parse_ignores_non_matching_card(fow_page):
    assert parse_search_response([fow_page], "Some Other Card", fx_jpy_per_usd=150.0) == []


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
    records = parse_search_response([page], "Foo", fx_jpy_per_usd=150.0)
    assert len(records) == 1
    r = records[0]
    assert r == {
        "shop": "TCGPlayer (Scryfall)",
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
    records = parse_search_response([page1, page2], "Foo", fx_jpy_per_usd=100.0)
    assert [r["set"] for r in records] == ["A", "B"]
