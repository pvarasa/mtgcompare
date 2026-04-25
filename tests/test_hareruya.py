from pathlib import Path

import pytest

from mtgcompare.scrappers.hareruya import parse_lazy_html

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def lazy_html() -> str:
    return (FIXTURES / "hareruya_force_of_will_lazy.html").read_text(encoding="utf-8")


def test_parse_returns_records_for_matching_card(lazy_html):
    records = parse_lazy_html(lazy_html, "Force of Will", fx_jpy_per_usd=150.0)
    assert records, "expected at least one Force of Will record in the fixture"
    for r in records:
        assert r["shop"] == "Hareruya"
        assert r["card"] == "Force of Will"
        assert isinstance(r["set"], str) and r["set"]
        assert isinstance(r["price_jpy"], float) and r["price_jpy"] > 0
        assert isinstance(r["price_usd"], float) and r["price_usd"] > 0
        assert isinstance(r["stock"], int) and r["stock"] > 0
        assert r["condition"]
        assert r["link"].startswith("https://www.hareruyamtg.com/")


def test_parse_case_insensitive_match(lazy_html):
    upper = parse_lazy_html(lazy_html, "FORCE OF WILL", fx_jpy_per_usd=150.0)
    mixed = parse_lazy_html(lazy_html, "Force of Will", fx_jpy_per_usd=150.0)
    assert len(upper) == len(mixed) > 0


def test_parse_ignores_non_matching_card(lazy_html):
    assert parse_lazy_html(lazy_html, "Some Other Card", fx_jpy_per_usd=150.0) == []


def test_parse_handcrafted_record_field_math():
    html = """
    <div class="itemData">
      <a class="itemName" href="/en/products/detail/1?lang=EN">《Foo》[BAR]</a>
      <div class="itemDetail">
        <p class="itemDetail__price">¥ 15,000</p>
        <p class="itemDetail__stock">【NM Stock:4】</p>
      </div>
    </div>
    """
    records = parse_lazy_html(html, "Foo", fx_jpy_per_usd=150.0)
    assert len(records) == 1
    r = records[0]
    assert r == {
        "shop": "Hareruya",
        "card": "Foo",
        "set": "BAR",
        "price_jpy": 15000.0,
        "price_usd": 100.0,
        "stock": 4,
        "condition": "NM",
        "link": "https://www.hareruyamtg.com/en/products/detail/1?lang=EN",
    }


def test_parse_skips_zero_stock():
    html = """
    <div class="itemData">
      <a class="itemName" href="/en/products/detail/1?lang=EN">《Foo》[BAR]</a>
      <div class="itemDetail">
        <p class="itemDetail__price">¥ 15,000</p>
        <p class="itemDetail__stock">【NM Stock:0】</p>
      </div>
    </div>
    """
    assert parse_lazy_html(html, "Foo", fx_jpy_per_usd=150.0) == []
