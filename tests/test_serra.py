from pathlib import Path

import pytest

from mtgcompare.scrapers.serra import parse_search_html

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def search_html() -> str:
    return (FIXTURES / "serra_force_of_will.html").read_text(encoding="utf-8")


def _condition_area(grade: str, price: str, stock: int) -> str:
    """One grade sub-row. Serra renders the grade twice (desktop + mobile)
    and shows stock as the "/ N" figure beside the quantity input."""
    qty_input = (
        f'<input type="number" name="count" min="0" max="{stock}" value="0">' if stock else "-"
    )
    return f"""
      <div class="condition_area">
        <div class="condition_name pc">{grade}</div>
        <div class="condition_body">
          <span class="price_area">
            <span class="condition_name sp">{grade}</span>
            <span class="price_wrap"><span class="big">{price}</span>円</span>
          </span>
          <div class="count">
            <span class="input_wrap">{qty_input}</span>
            <span class="delimiter">/</span>
            <span class="parameter">{stock}</span>
          </div>
        </div>
      </div>
    """


def _item(title: str, *areas: str, href: str = "/products/x?_pos=1&_sid=abc&_ss=r") -> str:
    return f"""
    <div class="product_item">
      <div class="content_body">
        <div class="card_title"><a href="{href}">{title}</a></div>
        <div class="card_body"><div class="price_list">{"".join(areas)}</div></div>
      </div>
    </div>
    """


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
        assert r["link"].startswith("https://cardshop-serra.com/products/")


def test_parse_case_insensitive_match(search_html):
    upper = parse_search_html(search_html, "FORCE OF WILL", fx_jpy_per_usd=150.0)
    mixed = parse_search_html(search_html, "Force of Will", fx_jpy_per_usd=150.0)
    assert len(upper) == len(mixed) > 0


def test_parse_ignores_non_matching_card(search_html):
    assert parse_search_html(search_html, "Some Other Card", fx_jpy_per_usd=150.0) == []


def test_parse_skips_japanese_listings():
    html = _item("(日)意志の力 / Force of Will【2XM】 No.051", _condition_area("NM", "15,000", 3))
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_skips_english_foil_listings():
    """The foil marker sits on the JP side of the "/", so it lands in the
    discarded `jp` group — without an explicit check an English foil would
    parse as a plain English listing and report a foil price as non-foil."""
    html = _item("(英)【Foil】意志の力 / Force of Will【DMR】 No.050", _condition_area("NM", "30,000", 2))
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_skips_decorated_foil_marker():
    html = _item(
        "(英)【シルバースクロールFoil】意志の力 / Force of Will【SOA】 No.149",
        _condition_area("NM", "45,000", 1),
    )
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_skips_below_nm_grades():
    """SP and MP rows must be dropped — only NM is exposed."""
    html = _item(
        "(英)意志の力 / Force of Will【2XM】 No.051",
        _condition_area("SP", "11,250", 2),
        _condition_area("MP", "9,000", 3),
    )
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_picks_nm_row_out_of_multiple_grades():
    html = _item(
        "(英)意志の力 / Force of Will【DMR】 No.050",
        _condition_area("NM", "14,000", 4),
        _condition_area("SP", "10,500", 0),
        _condition_area("MP", "8,400", 0),
    )
    records = parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0)
    assert len(records) == 1
    assert records[0]["price_jpy"] == 14000.0
    assert records[0]["stock"] == 4


def test_parse_skips_zero_stock_rows():
    html = _item("(英)意志の力 / Force of Will【2XM】 No.051", _condition_area("NM", "15,000", 0))
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_handles_extended_frame_flavor_marker():
    """★拡張枠★ between the EN name and the set bracket is part of the listing
    but mustn't end up in the captured card name."""
    html = _item(
        "(英)意志の力 / Force of Will ★拡張枠★ 【DMR】 No.418",
        _condition_area("NM", "20,000", 1),
    )
    records = parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0)
    assert len(records) == 1
    assert records[0]["card"] == "Force of Will"
    assert records[0]["set"] == "DMR"


def test_parse_strips_shopify_tracking_params_from_link():
    html = _item(
        "(英)意志の力 / Force of Will【2XM】 No.051",
        _condition_area("NM", "15,000", 4),
        href="/products/2xm-051-e_nm_abcd?_pos=1&_sid=16c93ec61&_ss=r",
    )
    records = parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0)
    assert records[0]["link"] == "https://cardshop-serra.com/products/2xm-051-e_nm_abcd"


def test_parse_price_jpy_to_usd_conversion():
    html = _item("(英)意志の力 / Force of Will【2XM】 No.051", _condition_area("NM", "15,000", 4))
    records = parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0)
    assert records[0]["price_jpy"] == 15000.0
    assert records[0]["price_usd"] == pytest.approx(100.0)
