from pathlib import Path

import pytest

from mtgcompare.scrappers.serra import parse_search_html

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def search_html() -> str:
    return (FIXTURES / "serra_force_of_will.html").read_text(encoding="utf-8")


def test_parse_returns_records_for_matching_card(search_html):
    records = parse_search_html(search_html, "Force of Will", fx_jpy_per_usd=150.0)
    assert records, "expected at least one Force of Will record in the fixture"
    for r in records:
        assert r["shop"] == "Cardshop Serra"
        assert r["card"] == "Force of Will"
        assert isinstance(r["set"], str) and r["set"]
        assert isinstance(r["price_jpy"], float) and r["price_jpy"] > 0
        assert isinstance(r["price_usd"], float) and r["price_usd"] > 0
        assert isinstance(r["stock"], int) and r["stock"] > 0
        assert r["condition"] == "NM"
        assert r["link"].startswith("https://cardshop-serra.com/")


def test_parse_case_insensitive_match(search_html):
    upper = parse_search_html(search_html, "FORCE OF WILL", fx_jpy_per_usd=150.0)
    mixed = parse_search_html(search_html, "Force of Will", fx_jpy_per_usd=150.0)
    assert len(upper) == len(mixed) > 0


def test_parse_ignores_non_matching_card(search_html):
    assert parse_search_html(search_html, "Some Other Card", fx_jpy_per_usd=150.0) == []


def test_parse_skips_japanese_listings():
    html = """
    <div class="product-list__item">
      <h2 class="product-list__item__title">
        <a class="product-list__item__title--name" href="https://cardshop-serra.com/mtg/products/detail/1">(日)意志の力 / Force of Will【2XM】 No.051</a>
      </h2>
      <table class="product-list__item__table">
        <tr>
          <th class="product-list__item__table--type">NM</th>
          <td class="product-list__item__table--price">15,000円</td>
          <td class="product-list__item__table--count">/3</td>
        </tr>
      </table>
    </div>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_skips_below_nm_grades():
    """NM-, EX, EX-, GD rows must be dropped — only NM is exposed."""
    html = """
    <div class="product-list__item">
      <h2 class="product-list__item__title">
        <a class="product-list__item__title--name" href="/mtg/products/detail/1">(英)意志の力 / Force of Will【2XM】 No.051</a>
      </h2>
      <table class="product-list__item__table">
        <tr><th class="product-list__item__table--type">NM-</th>
            <td class="product-list__item__table--price">14,000円</td>
            <td class="product-list__item__table--count">/2</td></tr>
        <tr><th class="product-list__item__table--type">EX</th>
            <td class="product-list__item__table--price">12,000円</td>
            <td class="product-list__item__table--count">/3</td></tr>
        <tr><th class="product-list__item__table--type">GD</th>
            <td class="product-list__item__table--price">9,000円</td>
            <td class="product-list__item__table--count">/1</td></tr>
      </table>
    </div>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_skips_zero_stock_rows():
    html = """
    <div class="product-list__item">
      <h2 class="product-list__item__title">
        <a class="product-list__item__title--name" href="/mtg/products/detail/1">(英)意志の力 / Force of Will【2XM】 No.051</a>
      </h2>
      <table class="product-list__item__table">
        <tr>
          <th class="product-list__item__table--type">NM</th>
          <td class="product-list__item__table--price">15,000円</td>
          <td class="product-list__item__table--count">
            <span class="product-list__item__table--none">-</span>
            <span>/0</span>
          </td>
        </tr>
      </table>
    </div>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_uses_actual_price_not_strikethrough():
    """Serra often shows a strike-through retail price next to the actual price.
    The parser must extract the actual selling price, not the strike-through.
    """
    html = """
    <div class="product-list__item">
      <h2 class="product-list__item__title">
        <a class="product-list__item__title--name" href="/mtg/products/detail/1">(英)意志の力 / Force of Will【2XM】 No.051</a>
      </h2>
      <table class="product-list__item__table">
        <tr>
          <th class="product-list__item__table--type">NM</th>
          <td class="product-list__item__table--price">
            <span class="product-list__item__table--price-original">18,000円</span>
            15,000円
          </td>
          <td class="product-list__item__table--count">/2</td>
        </tr>
      </table>
    </div>
    """
    records = parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0)
    assert len(records) == 1
    assert records[0]["price_jpy"] == 15000.0


def test_parse_handles_extended_frame_flavor_marker():
    """★拡張枠★ between the EN name and the set bracket is part of the listing
    but mustn't end up in the captured card name."""
    html = """
    <div class="product-list__item">
      <h2 class="product-list__item__title">
        <a class="product-list__item__title--name" href="/mtg/products/detail/1">(英)意志の力 / Force of Will ★拡張枠★ 【DMR】 No.418</a>
      </h2>
      <table class="product-list__item__table">
        <tr>
          <th class="product-list__item__table--type">NM</th>
          <td class="product-list__item__table--price">20,000円</td>
          <td class="product-list__item__table--count">/1</td>
        </tr>
      </table>
    </div>
    """
    records = parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0)
    assert len(records) == 1
    assert records[0]["card"] == "Force of Will"
    assert records[0]["set"] == "DMR"


def test_parse_price_jpy_to_usd_conversion():
    html = """
    <div class="product-list__item">
      <h2 class="product-list__item__title">
        <a class="product-list__item__title--name" href="/mtg/products/detail/1">(英)意志の力 / Force of Will【2XM】 No.051</a>
      </h2>
      <table class="product-list__item__table">
        <tr>
          <th class="product-list__item__table--type">NM</th>
          <td class="product-list__item__table--price">15,000円</td>
          <td class="product-list__item__table--count">/4</td>
        </tr>
      </table>
    </div>
    """
    records = parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0)
    assert records[0]["price_jpy"] == 15000.0
    assert records[0]["price_usd"] == pytest.approx(100.0)
