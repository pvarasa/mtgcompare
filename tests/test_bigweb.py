from pathlib import Path

import pytest

from mtgcompare.scrapers.bigweb import (
    BigwebScrapper,
    _comment_number,
    parse_search_json,
)
from mtgcompare.scrapers.html_base import ScraperFetchError

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FX = 150.0


@pytest.fixture
def search_body() -> bytes:
    return (FIXTURES / "bigweb_force_of_will.json").read_bytes()


def _item(name="Force of Will", slip="2XM", comment="51", **overrides) -> dict:
    """One /products item, trimmed to the fields the parser reads."""
    item = {
        "id": 3116757,
        "name": name,
        "fname": "意志の力",
        "stock_count": 4,
        "is_sold_out": False,
        "is_hidden_price": False,
        "is_box": False,
        "is_supply": False,
        "is_preorder_item": False,
        "price": 14000,
        "comment": comment,
        "cardset": {"slip": slip, "web": "ダブルマスターズ"},
        "language": {"id": 1, "slip_en": "EN"},
        "condition": {"id": 1, "web": "ニアミント", "name": "NM"},
    }
    item.update(overrides)
    return item


def _parse(*items, card="Force of Will"):
    return parse_search_json({"items": list(items)}, card, FX)


def test_fixture_yields_in_stock_english_nm_records(search_body):
    records = parse_search_json(search_body, "Force of Will", FX)
    assert records, "expected in-stock Force of Will rows in the fixture"
    for r in records:
        assert r["shop"] == "BIGWEB"
        assert r["card"] == "Force of Will"
        assert r["set"] and r["set"] == r["set"].strip()
        assert r["price_jpy"] > 0 and r["price_usd"] == round(r["price_jpy"] / FX, 2)
        assert r["stock"] > 0
        assert r["condition"] == "NM"
        assert r["link"].startswith("https://www.bigweb.co.jp/ja/products/mtg/cardViewer/")


def test_fixture_other_card_yields_nothing(search_body):
    assert parse_search_json(search_body, "Some Other Card", FX) == []


def test_record_shape():
    (r,) = _parse(_item())
    assert r == {
        "shop": "BIGWEB",
        "card": "Force of Will",
        "set": "2XM",
        "number": "51",
        "variant": "",
        "price_jpy": 14000.0,
        "price_usd": 93.33,
        "stock": 4,
        "condition": "NM",
        "link": "https://www.bigweb.co.jp/ja/products/mtg/cardViewer/3116757",
    }


@pytest.mark.parametrize("overrides", [
    {"is_sold_out": True, "stock_count": 0},
    {"stock_count": 0},
    {"is_hidden_price": True},
    {"price": 0},
    {"is_preorder_item": True},
    {"language": {"id": 2, "slip_en": "JP"}},
    {"condition": {"id": 4, "web": "FOILニアミント", "name": "NM"}},
    {"condition": {"id": 2, "web": "プレイド", "name": "PLD"}},
])
def test_skips_rows_that_are_not_an_in_stock_english_nm_offer(overrides):
    assert _parse(_item(**overrides)) == []


@pytest.mark.parametrize("name", [
    "Swamp(10枚セット）",                 # a lot, priced per bundle
    "(Art)(箔押し)Force of Will",         # art card
    "Vivi's Thunder Magic",              # alt-name Secret Lair printing
])
def test_skips_lots_art_cards_and_unnamed_printings(name):
    card = "Swamp" if name.startswith("Swamp") else "Force of Will"
    assert _parse(_item(name=name), card=card) == []


def test_number_glued_to_the_name():
    (r,) = _parse(_item(name="Sol Ring(2672", slip="SLD", comment="2672<br>Garfield"), card="Sol Ring")
    assert (r["card"], r["set"], r["number"]) == ("Sol Ring", "SLD", "2672")
    (r,) = _parse(_item(name="Elspeth, Knight-Errant（701", slip="SLD", comment=""),
                  card="Elspeth, Knight-Errant")
    assert r["number"] == "701"


def test_leading_markers_become_variant_tags():
    (r,) = _parse(_item(name="【旧枠】Force of Will", slip="DMR", comment="284"))
    assert (r["card"], r["variant"]) == ("Force of Will", "oldframe")
    (r,) = _parse(_item(name="【拡張】Fable of the Mirror-Breaker", slip="NEO_BF", comment="357"),
                  card="Fable of the Mirror-Breaker // Reflection of Kiki-Jiki")
    assert (r["set"], r["variant"]) == ("NEO", "extended")


def test_slip_suffixes():
    # Booster fun sub-sheet: a variant printing, treatment for enrichment.
    (r,) = _parse(_item(slip="MH2_BF", comment="315"))
    assert (r["set"], r["variant"]) == ("MH2", "other")
    # Commander-deck sub-sheet says nothing about the frame.
    (r,) = _parse(_item(slip="CMR_CMD", comment="472"))
    assert (r["set"], r["variant"]) == ("CMR", None)
    # Stray whitespace / trailing dot; a sub-sheet that is its own set.
    assert _parse(_item(slip=" 2X2_BF"))[0]["set"] == "2X2"
    assert _parse(_item(slip="FDN."))[0]["set"] == "FDN"
    assert _parse(_item(slip="TSR_TSB"))[0]["set"] == "TSB"
    # A set *name* keeps its spelling for the enrichment's name lookup.
    assert _parse(_item(slip="The List"))[0]["set"] == "The List"


@pytest.mark.parametrize("comment,expected", [
    ("138", "138"),
    ("なんば店で展示中 138", "138"),
    ("なんば店で展示中<br>1988<br>Secret Lair x Marvel's Spider-Man", "1988"),
    ("#0405 2539<br>My Little Pony", "2539"),
    ("37 Secret Lair Prize<br> \r\nNon-Foil", "37"),
    ("99(PWマークあり）<br>Angels", "99"),
    ("214<br>【管理コード:MTG-S】", "214"),
    ("秋葉原店で展示中 管理番号26030701<br>※画像は実物", None),
    ("C18 222", None),
    ("A25版 The List", None),
    ("NON FOIL", None),
    ("EVG9", None),
    ("", None),
    (None, None),
])
def test_comment_number(comment, expected):
    assert _comment_number(comment) == expected


def test_split_card_is_queried_with_a_plus():
    s = BigwebScrapper(fx=FX)
    assert s.query_variants("Fire // Ice") == ["Fire // Ice", "Fire+Ice", "Fire"]
    assert s.query_variants("Force of Will") == ["Force of Will"]


def test_search_params_filter_english_nm_server_side():
    params = BigwebScrapper(fx=FX).search_params("Force of Will")
    assert params == {"game_id": 1, "name": "Force of Will", "lang": "[1]", "condition": "[1]"}


def test_next_page_follows_the_pagenate_flag():
    s = BigwebScrapper(fx=FX)
    assert s._has_next_page(b'{"pagenate":{"page":1,"nextPage":true}}', 1)
    assert not s._has_next_page(b'{"pagenate":{"page":1,"nextPage":false}}', 1)


def test_bad_json_is_a_fetch_error_not_an_empty_result():
    with pytest.raises(ScraperFetchError):
        parse_search_json(b"<html>Just a moment...</html>", "Force of Will", FX)
