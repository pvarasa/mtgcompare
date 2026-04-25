from pathlib import Path

import pytest

from mtgcompare.scrappers.singlestar import parse_search_html

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def search_html() -> str:
    return (FIXTURES / "singlestar_force_of_will.html").read_text(encoding="utf-8")


def test_parse_returns_records_for_matching_card(search_html):
    records = parse_search_html(search_html, "Force of Will", fx_jpy_per_usd=150.0)
    assert records, "expected at least one Force of Will record in the fixture"
    for r in records:
        assert r["shop"] == "SingleStar"
        assert r["card"] == "Force of Will"
        assert isinstance(r["set"], str) and r["set"]
        assert isinstance(r["price_jpy"], float) and r["price_jpy"] > 0
        assert isinstance(r["price_usd"], float) and r["price_usd"] > 0
        assert isinstance(r["stock"], int) and r["stock"] > 0
        assert r["condition"] == "NM"
        assert r["link"].startswith("https://www.singlestar.jp/")


def test_parse_case_insensitive_match(search_html):
    upper = parse_search_html(search_html, "FORCE OF WILL", fx_jpy_per_usd=150.0)
    mixed = parse_search_html(search_html, "Force of Will", fx_jpy_per_usd=150.0)
    assert len(upper) == len(mixed) > 0


def test_parse_ignores_non_matching_card(search_html):
    assert parse_search_html(search_html, "Some Other Card", fx_jpy_per_usd=150.0) == []


def test_parse_skips_foil_and_non_english(search_html):
    records = parse_search_html(search_html, "Force of Will", fx_jpy_per_usd=150.0)
    for r in records:
        # Set codes are uppercase ASCII like SOA/DMR, not JP-only or foil-tagged.
        assert r["set"].isupper()


def test_parse_handcrafted_record_field_math():
    html = """
    <li class="list_item_cell">
      <div class="item_data" data-product-id="1">
        <a href="https://www.singlestar.jp/product/1" class="item_data_link">
          <p class="item_name"><span class="goods_name">意志の力/Force of Will 【英語版】 [DMR-青MR]</span></p>
          <div class="item_info">
            <div class="price"><p class="selling_price"><span class="figure">15,000円</span></p></div>
            <p class="stock">在庫数 4点</p>
          </div>
        </a>
      </div>
    </li>
    """
    records = parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0)
    assert len(records) == 1
    r = records[0]
    assert r == {
        "shop": "SingleStar",
        "card": "Force of Will",
        "set": "DMR",
        "price_jpy": 15000.0,
        "price_usd": 100.0,
        "stock": 4,
        "condition": "NM",
        "link": "https://www.singlestar.jp/product/1",
    }


def test_parse_skips_soldout():
    html = """
    <li class="list_item_cell">
      <div class="item_data" data-product-id="1">
        <a href="https://www.singlestar.jp/product/1" class="item_data_link">
          <p class="item_name"><span class="goods_name">意志の力/Force of Will 【英語版】 [DMR-青MR]</span></p>
          <div class="item_info">
            <div class="price"><p class="selling_price"><span class="figure">9,930円</span></p></div>
            <p class="stock soldout">在庫なし</p>
          </div>
        </a>
      </div>
    </li>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_skips_foil_prefix():
    html = """
    <li class="list_item_cell">
      <div class="item_data" data-product-id="1">
        <a href="https://www.singlestar.jp/product/1" class="item_data_link">
          <p class="item_name"><span class="goods_name">[FOIL] 意志の力/Force of Will 【英語版】 [DMR-青MR]</span></p>
          <div class="item_info">
            <div class="price"><p class="selling_price"><span class="figure">20,000円</span></p></div>
            <p class="stock">在庫数 1点</p>
          </div>
        </a>
      </div>
    </li>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_skips_japanese_edition():
    html = """
    <li class="list_item_cell">
      <div class="item_data" data-product-id="1">
        <a href="https://www.singlestar.jp/product/1" class="item_data_link">
          <p class="item_name"><span class="goods_name">意志の力/Force of Will 【日本語版】 [DMR-青MR]</span></p>
          <div class="item_info">
            <div class="price"><p class="selling_price"><span class="figure">13,830円</span></p></div>
            <p class="stock">在庫数 2点</p>
          </div>
        </a>
      </div>
    </li>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []
