from pathlib import Path

import pytest

from mtgcompare.scrapers.hareruya import parse_lazy_html

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


def _item(name: str, price: str = "¥ 1,200", stock: str = "【NM Stock:19】") -> str:
    return f"""
    <div class="itemData">
      <a href=" /en/products/detail/1?lang=EN " class="itemName">{name}</a>
      <div class="itemDetail">
        <p class="itemDetail__price">{price}</p>
        <p class="itemDetail__stock">{stock}</p>
      </div>
    </div>
    """


def test_parse_matches_alternate_name_printing():
    html = _item("【EN】(1871)■Borderless■《Vivi's Thunder Magic》//《Lightning Bolt》[SLD]")
    [record] = parse_lazy_html(html, "Lightning Bolt", fx_jpy_per_usd=150.0)
    assert (record["card"], record["set"]) == ("Lightning Bolt", "SLD")


@pytest.mark.parametrize("name,card", [
    ("【EN】《Delver of Secrets》/《Insectile Aberration》[ISD]", "Delver of Secrets // Insectile Aberration"),
    ("【EN】《Fire+Ice》[[APC]", "Fire // Ice"),
    ("【EN】《Bonecrusher Giant》[ELD]", "Bonecrusher Giant // Stomp"),
])
def test_parse_matches_multi_face_names(name, card):
    assert len(parse_lazy_html(_item(name), card, fx_jpy_per_usd=150.0)) == 1


def test_get_prices_pages_until_num_found_is_covered(monkeypatch):
    from mtgcompare.scrapers import hareruya

    scraper = hareruya.HareruyaScrapper(fx=150.0)
    seen = []

    def fake_docs(query, page=1):
        seen.append(page)
        return [{"p": page}], 130  # 130 matches -> 3 pages of 60

    monkeypatch.setattr(scraper, "_fetch_docs", fake_docs)
    monkeypatch.setattr(scraper, "_fetch_lazy_html", lambda docs: _item("【EN】《Sol Ring》[C21]"))
    assert len(scraper.get_prices("Sol Ring")) == 3
    assert seen == [1, 2, 3]
