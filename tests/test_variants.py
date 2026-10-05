"""Printing variants: shop markers → tags, Scryfall enrichment, the
search-box filter syntax, and the shop_listings round trip."""
import json
from pathlib import Path

import pytest

from mtgcompare import search
from mtgcompare.scrapers import (
    blackfrog,
    cache,
    cardrush,
    hareruya,
    mintmall,
    scryfall,
    serra,
    singlestar,
    tokyomtg,
)
from mtgcompare.scrapers.enrich import EnrichingScrapper
from mtgcompare.scrapers.variants import (
    enrich_records,
    label,
    normalize_number,
    tags_from_markers,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(("markers", "expected"), [
    (["全面アート版"], {"fullart"}),
    (["フルアート"], {"fullart"}),
    (["Full Art"], {"fullart"}),
    (["ボーダーレス"], {"borderless"}),
    (["ショーケース・海外産ブースター版"], {"showcase"}),
    (["拡張アート版"], {"extended"}),
    (["★拡張枠★"], {"extended"}),
    (["旧枠仕様"], {"oldframe"}),
    (["RetroF"], {"oldframe"}),
    (["日本画"], {"other"}),
    (["Alternate Frame", "Borderless"], {"borderless"}),  # specific beats other
    (["青MR", "1604", "アーカイブ", "海外産ブースター版"], set()),
])
def test_tags_from_markers(markers, expected):
    assert tags_from_markers(markers) == expected


@pytest.mark.parametrize(("raw", "expected"), [
    ("050", "50"), ("0019", "19"), ("418", "418"), ("#292", "292"),
    ("123a", "123a"), ("0", "0"), ("", None), (None, None), ("2XM-340", "2xm-340"),
])
def test_normalize_number(raw, expected):
    assert normalize_number(raw) == expected


def test_label():
    assert label(None) == "?"
    assert label("") == "Regular"
    assert label("fullart,borderless") == "Full art · Borderless"


def test_scryfall_variant_tags_spot_old_frame_reprints_only():
    tags = scryfall.variant_tags
    assert tags({"frame": "1997", "released_at": "2023-01-13"}) == {"oldframe"}
    # Printed before the modern frame existed: that *is* the regular card.
    assert tags({"frame": "1993", "released_at": "1996-06-10"}) == set()
    assert tags({"full_art": True, "border_color": "borderless",
                 "frame_effects": ["inverted"]}) == {"fullart", "borderless"}
    assert tags({"frame_effects": ["showcase", "extendedart"]}) == {"showcase", "extended"}


@pytest.fixture
def fow_printings():
    page = json.loads((FIXTURES / "scryfall_force_of_will.json").read_text())
    return scryfall.summarize_page(page, "force of will")


def _shop_records(fx=150.0):
    def html(name):
        return (FIXTURES / name).read_bytes()
    out = []
    out += singlestar.parse_search_html(html("singlestar_force_of_will.html"), "Force of Will", fx)
    out += cardrush.parse_search_html(html("cardrush_force_of_will.html"), "Force of Will", fx)
    out += serra.parse_search_html(html("serra_force_of_will.html"), "Force of Will", fx)
    out += tokyomtg.parse_search_html(html("tokyomtg_force_of_will.html"), "Force of Will", fx)
    out += blackfrog.parse_search_html(
        html("blackfrog_force_of_will.html").decode("euc-jp", errors="replace"), "Force of Will", fx)
    out += hareruya.parse_lazy_html(html("hareruya_force_of_will_lazy.html"), "Force of Will", fx)
    out += mintmall.parse_search_html(html("mintmall_force_of_will.html"), "Force of Will", fx)
    return out


def test_enrichment_makes_shops_agree_on_the_same_printing(fow_printings):
    """Dominaria Remastered's borderless Force of Will (#418) is "[ボーダーレス]"
    at BLACK FROG, "★拡張枠★ No.418" at Serra and "■Borderless■" at Hareruya;
    after enrichment every one reads as Scryfall's full-art borderless #418."""
    records = enrich_records(_shop_records(), fow_printings)
    dmr_418 = {r["shop"] for r in records if r["set"] == "DMR" and r["number"] == "418"}
    assert dmr_418 == {"BLACK FROG", "Cardshop Serra", "Hareruya"}
    assert {r["variant"] for r in records if r["number"] == "418"} == {"fullart,borderless"}

    # Hareruya's box topper: set suffix "2XM-BT", no tag of its own.
    [topper] = [r for r in records if r["shop"] == "Hareruya" and r["number"] == "340"]
    assert (topper["set"], topper["variant"]) == ("2XM", "fullart,borderless")

    # Retro-frame copies, marked "■RetroF■" / "(旧枠仕様)", resolve to #284.
    retro = {r["shop"] for r in records if r["set"] == "DMR" and r["variant"] == "oldframe"}
    assert retro == {"Hareruya", "Card Rush"}
    assert {r["number"] for r in records if r["variant"] == "oldframe"} == {"284"}


def test_enrichment_maps_set_names_and_leaves_ambiguity_unknown(fow_printings):
    records = enrich_records(_shop_records(), fow_printings)
    tokyo = {(r["set"], r["variant"]) for r in records if r["shop"] == "TokyoMTG"}
    # "Secrets of Strixhaven: Mystical Archive" vs Scryfall's colon-less name.
    assert ("SOA", None) in tokyo
    # A plain set name covers DMR's regular *and* retro-frame card: unknown.
    assert ("DMR", None) in tokyo
    # Only one printing exists in Alliances, so it's settled.
    assert ("ALL", "") in tokyo


def test_unmarked_listing_in_an_all_variant_set_takes_the_sets_treatment(fow_printings):
    """Every Mystical Archive card is a borderless showcase, so shops don't
    mark it; "regular" there means the set's base treatment."""
    records = enrich_records(_shop_records(), fow_printings)
    [frog] = [r for r in records if r["shop"] == "BLACK FROG" and r["set"] == "SOA"]
    assert frog["variant"] == "borderless,showcase"
    assert frog["number"] is None  # inferred treatment, not an identified printing


def test_card_rush_sale_flag_no_longer_hides_the_variant_tags():
    html = """
    <ul><li class="list_item_cell">
      <span class="goods_name">☆SALE☆(旧枠仕様)意志の力/Force of Will《英語》【DMR】</span>
      <span class="figure">14,800円</span><p class="stock">在庫数 4枚</p>
      <a class="item_data_link" href="/product/1"></a>
    </li><li class="list_item_cell">
      <span class="goods_name">☆SALE☆(FOIL)意志の力/Force of Will《英語》【DMR】</span>
      <span class="figure">9,800円</span><p class="stock">在庫数 4枚</p>
      <a class="item_data_link" href="/product/2"></a>
    </li></ul>
    """
    [r] = cardrush.parse_search_html(html, "Force of Will", 150.0)
    assert r["variant"] == "oldframe"  # and the sale foil is dropped as a foil


def test_hareruya_marker_number_and_set_suffix():
    html = """
    <div class="itemData">
      <a class="itemName" href="/en/products/detail/1">【EN】(017)■Showcase■《Overwhelming Forces》[OTP]</a>
      <div class="itemDetail__price">¥ 3,000</div><div class="itemDetail__stock">【NM Stock:2】</div>
    </div>
    <div class="itemData">
      <a class="itemName" href="/en/products/detail/2">【EN】《Overwhelming Forces》[2XM-BT]</a>
      <div class="itemDetail__price">¥ 5,000</div><div class="itemDetail__stock">【NM Stock:1】</div>
    </div>
    """
    a, b = hareruya.parse_lazy_html(html, "Overwhelming Forces", 150.0)
    assert (a["set"], a["number"], a["variant"]) == ("OTP", "17", "showcase")
    assert (b["set"], b["number"], b["variant"]) == ("2XM", None, "other")


class _FakeShop:
    def __init__(self, records):
        self.records = records

    def get_prices(self, card_name):
        return [dict(r) for r in self.records]


def test_enriching_scrapper_survives_a_scryfall_failure(monkeypatch):
    row = {"shop": "X", "card": "Foo", "set": "DMR", "number": None, "variant": ""}

    def boom(name):
        raise scryfall.ScraperFetchError("down")

    monkeypatch.setattr("mtgcompare.scrapers.enrich.fetch_card_summaries", boom)
    assert EnrichingScrapper(_FakeShop([row]), "X").get_prices("Foo") == [row]


def test_enriching_scrapper_skips_scryfall_when_rows_are_complete(monkeypatch):
    row = {"shop": "X", "card": "Foo", "set": "DMR", "number": "1", "variant": ""}
    monkeypatch.setattr("mtgcompare.scrapers.enrich.fetch_card_summaries",
                        lambda name: pytest.fail("should not be called"))
    assert EnrichingScrapper(_FakeShop([row]), "X").get_prices("Foo") == [row]


# --- shop_listings round trip ---------------------------------------------

def _row(**kw):
    base = {"card": "Foo", "set": "DMR", "price_jpy": 100.0, "price_usd": 1.0,
            "stock": 1, "condition": "NM", "link": "u"}
    return {**base, **kw}


def test_variant_and_number_round_trip_through_the_cache(test_db):
    from mtgcompare import db
    rows = [_row(variant="", number="50"), _row(variant=None, number=None),
            _row(variant="fullart,borderless", number="418")]
    with db.get_conn() as conn:
        cache.replace_listings(conn, "S", "foo", rows)
        back = cache.read_listings(conn, "S", "foo")
    assert [(r["variant"], r["number"]) for r in back] == [
        ("", "50"), (None, None), ("fullart,borderless", "418"),
    ]


def test_rows_written_before_the_variant_column_are_refetched(test_db):
    """stg and prod share shop_listings: rows from a release that doesn't
    know the column come back NULL and must not be served as unknown."""
    from sqlalchemy import text

    from mtgcompare import db
    with db.get_conn() as conn:
        cache.replace_listings(conn, "S", "foo", [_row(variant="")])
        cache.upsert_log(conn, "S", "foo", 1)
        conn.execute(text("UPDATE shop_listings SET variant = NULL"))

    fresh = [_row(variant="showcase", number="19")]
    shop = cache.CachedScrapper(_FakeShop(fresh), shop_name="S")
    assert [r["variant"] for r in shop.get_prices("Foo")] == ["showcase"]


# --- search-box syntax ------------------------------------------------------

@pytest.mark.parametrize(("q", "name", "set_code", "number", "wanted"), [
    ("Force of Will", "Force of Will", None, None, []),
    ("Force of Will set:dmr is:borderless", "Force of Will", "DMR", None, ["borderless"]),
    ("Sol Ring #2807", "Sol Ring", None, "2807", []),
    ("Force of Will (DMR) 418", "Force of Will", "DMR", "418", []),
    ("島/Island (全面アート版)", "Island", None, None, ["fullart"]),
    ("Force of Will is:extendedart e:2xm", "Force of Will", "2XM", None, ["extended"]),
    ("Island is:fullart is:regular", "Island", None, None, ["regular"]),
    ("Fire // Ice", "Fire // Ice", None, None, []),
])
def test_parse_query(q, name, set_code, number, wanted):
    parsed = search.parse_query(q)
    assert (parsed.name, parsed.set_code, parsed.number, parsed.variants) == (
        name, set_code, number, wanted)


def test_parse_query_reports_unknown_is_tokens():
    assert search.parse_query("Bolt is:foil").unknown_tokens == ["is:foil"]


def test_filter_options_lists_present_sets_and_treatments():
    results = [
        {"set": "DMR", "variant": ""}, {"set": "DMR", "variant": "fullart,borderless"},
        {"set": "2XM", "variant": None},
    ]
    opts = search.filter_options(results)
    assert opts["sets"] == [("2XM", 1), ("DMR", 2)]
    assert [v for v, _ in opts["variant_chips"]] == ["regular", "fullart", "borderless"]
    assert opts["has_unknown"] is True


def test_collapse_keeps_a_sets_distinct_printings_apart():
    a = {"shop": "TCGPlayer → JP", "card": "Foo", "set": "DMR", "number": "50",
         "price_jpy": 500.0, "ship_jpy": 0.0}
    b = {**a, "number": "418", "price_jpy": 900.0}
    assert len(search.collapse_marketplace_offers([a, b], False)) == 2


# --- decklist printing matching ---------------------------------------------

def test_decklist_lines_with_export_suffixes_keep_a_clean_name():
    """Moxfield's "*F*" and non-numeric collector numbers used to fail the
    optional suffix and leave the whole tail in the card name."""
    from mtgcompare import decklist
    text = """
    1 Sol Ring (C21) 263 *F*
    1 Sol Ring (PLST) LTR-123
    1 Sol Ring (SLD) 1234★
    1 Rhystic Study (C21) 79
    """
    assert [n for _, n in decklist.parse_decklist(text)] == ["Sol Ring"] * 3 + ["Rhystic Study"]


def test_parse_printings_collects_sets_and_drops_unconstrained_cards():
    from mtgcompare import decklist
    text = """
    1 Force of Will (DMR) 418
    1 Force of Will (2XM)
    1 Sol Ring (C21) 263
    1 Sol Ring
    1 Counterspell
    """
    assert decklist.parse_printings(text) == {
        "force of will": {("DMR", "418"), ("2XM", None)},
    }


def test_match_printings_narrows_rows_only_when_opted_in():
    from mtgcompare import decklist
    rows = [
        {"shop": "A", "set": "DMR", "number": "50", "price_jpy": 100},
        {"shop": "B", "set": "DMR", "number": "418", "price_jpy": 300},
        {"shop": "C", "set": "DMR", "number": None, "price_jpy": 200},  # number unknown: kept
        {"shop": "D", "set": "2XM", "number": "51", "price_jpy": 50},
    ]

    def fake_collect(name, fx, *, enabled, logger, timeouts_out):
        return [dict(r) for r in rows]

    def shops(printings):
        out = dict(decklist.iter_decklist_prices(
            ["force of will"], {"force of will": "Force of Will"}, fx=150.0,
            enabled_shops=None, collect=fake_collect, printings=printings,
        ))
        return [r["shop"] for r in out["force of will"]]

    assert shops(None) == ["D", "A", "C", "B"]
    assert shops({"force of will": {("DMR", "418")}}) == ["C", "B"]
    assert shops({"force of will": {("DMR", None)}}) == ["A", "C", "B"]


def test_prepare_decklist_search_parses_printings_only_when_asked():
    from mtgcompare import decklist

    def prep(match):
        return decklist.prepare_decklist_search(
            decklist.DecklistFormBasics(
                decklist_text="1 Force of Will (DMR) 418",
                shipping_overrides_jpy={}, use_inventory=False,
                enabled_shops=None, match_printings=match,
            ),
            load_inv_map=dict, get_fx=lambda: 150.0,
        )

    off, on = prep(False), prep(True)
    assert isinstance(off, decklist.DecklistPrep) and off.name_printings == {}
    assert isinstance(on, decklist.DecklistPrep)
    assert on.name_printings == {"force of will": {("DMR", "418")}}


def test_full_art_borderless_and_extended_are_interchangeable_but_old_frame_is_not():
    """Card Rush sells DMR #418 as "(フルアート)"; Scryfall calls it borderless."""
    printings = [
        {"set": "DMR", "set_name": "Dominaria Remastered", "number": n, "variant": v}
        for n, v in (("50", ""), ("284", "oldframe"), ("418", "borderless"))
    ]
    rec = {"shop": "Card Rush", "set": "DMR", "number": None, "variant": "fullart"}
    [out] = enrich_records([rec], printings)
    assert (out["number"], out["variant"]) == ("418", "borderless")
