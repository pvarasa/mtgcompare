from pathlib import Path

import pytest

from mtgcompare.scrappers.blackfrog import parse_search_html

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def search_html() -> str:
    # The fixture is the live response, EUC-JP encoded.
    return (FIXTURES / "blackfrog_force_of_will.html").read_bytes().decode("euc-jp", errors="replace")


def test_parse_returns_records_for_matching_card(search_html):
    records = parse_search_html(search_html, "Force of Will", fx_jpy_per_usd=150.0)
    assert records, "expected at least one in-stock NM English Force of Will record"
    for r in records:
        assert r["shop"] == "BLACK FROG"
        assert r["card"] == "Force of Will"
        assert isinstance(r["set"], str) and r["set"]
        assert isinstance(r["price_jpy"], float) and r["price_jpy"] > 0
        assert isinstance(r["price_usd"], float) and r["price_usd"] > 0
        assert r["condition"] == "NM"
        assert r["stock"] is None  # list view doesn't expose stock counts
        assert r["link"].startswith("https://blackfrog.jp/")


def test_parse_case_insensitive_match(search_html):
    upper = parse_search_html(search_html, "FORCE OF WILL", fx_jpy_per_usd=150.0)
    mixed = parse_search_html(search_html, "Force of Will", fx_jpy_per_usd=150.0)
    assert len(upper) == len(mixed) > 0


def test_parse_ignores_non_matching_card(search_html):
    assert parse_search_html(search_html, "Some Other Card", fx_jpy_per_usd=150.0) == []


def test_parse_skips_japanese_listings():
    html = """
    <ul class="innerList clear"><li>
      <p class="name"><a href="/shop/shopdetail.html?brandcode=000000218390">【日】意志の力/Force of Will[青MR]【SOA】</a></p>
      <p class="price">15,000円</p>
      <a href="basket.html?brandcode=000000218390&amount=1">cart</a>
    </li></ul>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_skips_foil_listings():
    html = """
    <ul class="innerList clear"><li>
      <p class="name"><a href="/shop/shopdetail.html?brandcode=1">【FOIL】【英】意志の力/Force of Will[青MR]【SOA】</a></p>
      <p class="price">32,000円</p>
      <a href="basket.html?brandcode=1&amount=1">cart</a>
    </li></ul>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_skips_below_nm_listings():
    """Condition stamp 状態EX / 状態VG → not NM."""
    html = """
    <ul class="innerList clear"><li>
      <p class="name"><a href="/shop/shopdetail.html?brandcode=2">★特価品　状態EX★【英】意志の力/Force of Will[青MR]【SOA】</a></p>
      <p class="price">14,500円</p>
      <a href="basket.html?brandcode=2&amount=1">cart</a>
    </li></ul>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_skips_borderless_and_old_frame_variants():
    html = """
    <ul class="innerList clear">
      <li>
        <p class="name"><a href="/p/3">【英】意志の力/Force of Will[青MR]【DMR】[ボーダーレス]</a></p>
        <p class="price">22,000円</p>
        <a href="basket.html?brandcode=3&amount=1">cart</a>
      </li>
      <li>
        <p class="name"><a href="/p/4">【英】意志の力/Force of Will[青MR]【DMR】[旧枠]</a></p>
        <p class="price">25,000円</p>
        <a href="basket.html?brandcode=4&amount=1">cart</a>
      </li>
    </ul>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_skips_out_of_stock_listings():
    html = """
    <ul class="innerList clear"><li>
      <p class="name"><a href="/p/5">【英】意志の力/Force of Will[青MR]【SOA】</a></p>
      <p class="price">15,800円</p>
      <!-- no basket button -->
    </li></ul>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_extracts_set_code_from_brackets():
    html = """
    <ul class="innerList clear"><li>
      <p class="name"><a href="/p/6">【英】意志の力/Force of Will[青MR]【ALL】</a></p>
      <p class="price">19,000円</p>
      <a href="basket.html?brandcode=6&amount=1">cart</a>
    </li></ul>
    """
    records = parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0)
    assert len(records) == 1
    assert records[0]["set"] == "ALL"
    assert records[0]["price_jpy"] == 19000.0


def test_parse_price_jpy_to_usd_conversion():
    html = """
    <ul class="innerList clear"><li>
      <p class="name"><a href="/p/7">【英】意志の力/Force of Will[青MR]【SOA】</a></p>
      <p class="price">15,000円</p>
      <a href="basket.html?brandcode=7&amount=1">cart</a>
    </li></ul>
    """
    records = parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0)
    assert records[0]["price_jpy"] == 15000.0
    assert records[0]["price_usd"] == pytest.approx(100.0)
