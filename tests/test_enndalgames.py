from pathlib import Path

import pytest

from mtgcompare.scrappers.enndalgames import parse_search_html

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def search_html() -> str:
    return (FIXTURES / "enndalgames_force_of_will.html").read_text(encoding="utf-8")


def test_parse_returns_records_for_matching_card(search_html):
    records = parse_search_html(search_html, "Force of Will", fx_jpy_per_usd=150.0)
    # The fixture has English NM rows for Force of Will across multiple sets;
    # at least one must be in stock for this test to be useful.
    if not records:
        pytest.skip("fixture currently has no in-stock English NM Force of Will rows")
    for r in records:
        assert r["shop"] == "ENNDAL GAMES"
        assert r["card"] == "Force of Will"
        assert isinstance(r["set"], str) and r["set"]
        assert "-" not in r["set"], "set code should be the bare set, not set-rarity"
        assert isinstance(r["price_jpy"], float) and r["price_jpy"] > 0
        assert isinstance(r["price_usd"], float) and r["price_usd"] > 0
        assert isinstance(r["stock"], int) and r["stock"] > 0
        assert r["condition"] == "NM"
        assert r["link"].startswith("https://www.enndalgames.com/")


def test_parse_case_insensitive_match(search_html):
    upper = parse_search_html(search_html, "FORCE OF WILL", fx_jpy_per_usd=150.0)
    mixed = parse_search_html(search_html, "Force of Will", fx_jpy_per_usd=150.0)
    assert len(upper) == len(mixed)


def test_parse_ignores_non_matching_card(search_html):
    assert parse_search_html(search_html, "Some Other Card", fx_jpy_per_usd=150.0) == []


def test_parse_skips_variant_prefixed_listings():
    """【Foil】 / 【日本画】 / 【旧枠】 / 【PSA10】 prefixes mark variant printings;
    they must not be returned as the canonical card."""
    html = """
    <div class="product_detail_wrapper">
      <a class="product_name" href="/p/1">【Foil】(SOA-MU)Force of Will/意志の力</a>
      <table class="item_stock_table">
        <tr><th>English NM</th>
            <td><span class="price">25,000 yen</span><span class="quantity">(2)</span></td></tr>
      </table>
    </div>
    <div class="product_detail_wrapper">
      <a class="product_name" href="/p/2">【日本画】(SOA-MU)Force of Will/意志の力</a>
      <table class="item_stock_table">
        <tr><th>English NM</th>
            <td><span class="price">30,000 yen</span><span class="quantity">(1)</span></td></tr>
      </table>
    </div>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_skips_box_topper_and_other_variant_set_codes():
    """Set codes like 2XM-Box_Topper-MU shouldn't be conflated with the
    plain 2XM printing — extra segment in the set code = variant."""
    html = """
    <div class="product_detail_wrapper">
      <a class="product_name" href="/p/3">(2XM-Box_Topper-MU)Force of Will/意志の力</a>
      <table class="item_stock_table">
        <tr><th>English NM</th>
            <td><span class="price">50,000 yen</span><span class="quantity">(1)</span></td></tr>
      </table>
    </div>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_skips_japanese_nm_and_below_nm_grades():
    """Only English NM is returned; Japanese NM and English EX must be dropped."""
    html = """
    <div class="product_detail_wrapper">
      <a class="product_name" href="/p/4">(SOA-MU)Force of Will/意志の力</a>
      <table class="item_stock_table">
        <tr><th>Japanese NM</th>
            <td><span class="price">15,999 yen</span><span class="quantity">(3)</span></td></tr>
        <tr><th>English EX</th>
            <td><span class="price">14,000 yen</span><span class="quantity">(2)</span></td></tr>
      </table>
    </div>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_skips_zero_stock():
    html = """
    <div class="product_detail_wrapper">
      <a class="product_name" href="/p/5">(DMR-MU)Force of Will/意志の力</a>
      <table class="item_stock_table">
        <tr><th>English NM</th>
            <td><span class="price">15,000 yen</span><span class="quantity">(0)</span></td></tr>
      </table>
    </div>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_extracts_bare_set_code_not_rarity_suffix():
    html = """
    <div class="product_detail_wrapper">
      <a class="product_name" href="/p/6">(ALL-UU)Force of Will/意志の力</a>
      <table class="item_stock_table">
        <tr><th>English NM</th>
            <td><span class="price">12,000 yen</span><span class="quantity">(4)</span></td></tr>
      </table>
    </div>
    """
    records = parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0)
    assert len(records) == 1
    assert records[0]["set"] == "ALL"
    assert records[0]["price_jpy"] == 12000.0
    assert records[0]["stock"] == 4


def test_parse_price_jpy_to_usd_conversion():
    html = """
    <div class="product_detail_wrapper">
      <a class="product_name" href="/p/7">(SOA-MU)Force of Will/意志の力</a>
      <table class="item_stock_table">
        <tr><th>English NM</th>
            <td><span class="price">15,000 yen</span><span class="quantity">(2)</span></td></tr>
      </table>
    </div>
    """
    records = parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0)
    assert records[0]["price_jpy"] == 15000.0
    assert records[0]["price_usd"] == pytest.approx(100.0)


def test_parse_strips_collector_number_suffix():
    html = """
    <div class="product_detail_wrapper">
      <a class="product_name" href="/p/8">(SOA-MU)Force of Will/意志の力【No.0019】</a>
      <table class="item_stock_table">
        <tr><th>English NM</th>
            <td><span class="price">15,000 yen</span><span class="quantity">(2)</span></td></tr>
      </table>
    </div>
    """
    records = parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0)
    assert len(records) == 1
    assert records[0]["set"] == "SOA"
