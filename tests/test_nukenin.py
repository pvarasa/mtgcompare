import json
from pathlib import Path

import pytest

from mtgcompare.scrapers.nukenin import NukeninScrapper, parse_search_html

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FX = 150.0


@pytest.fixture
def search_html() -> bytes:
    return (FIXTURES / "nukenin_force_of_will.html").read_bytes()


def _page(*products: tuple[str, str, dict[str, tuple[str, int, str]]]) -> str:
    """A search page. Each product is (id, title, {class id: (label, stock,
    price)}); the classes become both the condition <select> and the
    ``eccube.productsClassCategories`` script the parser reads."""
    items, classes = [], {}
    for pid, title, conds in products:
        options = "".join(f'<option value="{cid}">{label}</option>' for cid, (label, _, _) in conds.items())
        items.append(f"""
          <li>
            <div class="item_name"><a href="https://nukeninmtg.com/products/detail/{pid}">{title}</a></div>
            <select id="classcategory_id1{pid}" name="classcategory_id1">
              <option value="__unselected">選択してください</option>{options}
            </select>
            <div>【在庫数：<span class="stock-default">---</span>】</div>
          </li>""")
        classes[pid] = {"__unselected": {"__unselected": {"name": "選択してください"}}}
        for cid, (_, stock, price) in conds.items():
            classes[pid][cid] = {"#": {"stock_find": stock > 0, "stock": str(stock),
                                       "price02_inc_tax": price, "is_stock_unlimited": False}}
    return f"""<html><body><ul class="item_list">{"".join(items)}</ul>
      <script>
        eccube.productsClassCategories = {json.dumps(classes, ensure_ascii=False)};
        $(function() {{ }});
      </script></body></html>"""


def _parse(*products, card="Force of Will"):
    return parse_search_html(_page(*products), card, FX)


NM = {"1": ("NM", 2, "15,500")}


def test_fixture_yields_in_stock_english_nm_records(search_html):
    records = parse_search_html(search_html, "Force of Will", FX)
    assert records, "expected English NM Force of Will rows in the fixture"
    for r in records:
        assert r["shop"] == "Nukenin"
        assert r["card"] == "Force of Will"
        assert r["set"]
        assert r["price_jpy"] > 0 and r["stock"] > 0
        assert r["condition"] == "NM"
        assert r["link"].startswith("https://nukeninmtg.com/products/detail/")


def test_fixture_other_card_yields_nothing(search_html):
    assert parse_search_html(search_html, "Some Other Card", FX) == []


def test_record_shape():
    (r,) = _parse(("341719", "【ENG】《意志の力/Force of Will》(050)[DMR]", NM))
    assert r == {
        "shop": "Nukenin",
        "card": "Force of Will",
        "set": "DMR",
        "number": "50",
        "variant": "",
        "price_jpy": 15500.0,
        "price_usd": 103.33,
        "stock": 2,
        "condition": "NM",
        "link": "https://nukeninmtg.com/products/detail/341719",
    }


@pytest.mark.parametrize("title", [
    "【JPN】《意志の力/Force of Will》[DMR]",
    "【ENG】【Foil】《意志の力/Force of Will》[DMR]",
    "【ENG】■エッチング■《意志の力/Force of Will》[STA]",  # etched is a foil finish
    "【ENG】■サージ■《意志の力/Force of Will》[MSC]",      # so is surge
    "【ENG】《意志の力/Force of Wills》[DMR]",
])
def test_skips_other_languages_foils_and_other_cards(title):
    assert _parse(("1", title, NM)) == []


def test_only_nm_in_stock_classes():
    conds = {"1": ("NM", 0, "15,500"), "2": ("SP", 3, "12,000")}
    assert _parse(("1", "【ENG】《意志の力/Force of Will》[DMR]", conds)) == []
    conds = {"2": ("SP", 3, "12,000"), "1": ("NM", 1, "15,500")}
    (r,) = _parse(("1", "【ENG】《意志の力/Force of Will》[DMR]", conds))
    assert (r["price_jpy"], r["stock"]) == (15500.0, 1)


def test_markers_become_variant_tags():
    (r,) = _parse(("1", "【ENG】■ボーダーレス■《意志の力/Force of Will》[2XM]", NM))
    assert r["variant"] == "borderless"
    (r,) = _parse(("1", "【ENG】■旧枠■《意志の力/Force of Will》[DMR]", NM))
    assert r["variant"] == "oldframe"


def test_the_list_parentheses_hold_a_set_not_a_number():
    (r,) = _parse(("1", "【ENG】《稲妻/Lightning Bolt》(e01)[PWシンボル付き再版]", NM),
                  card="Lightning Bolt")
    assert (r["set"], r["number"]) == ("PWシンボル付き再版", None)


def test_split_and_double_faced_names():
    (r,) = _parse(("1", "【ENG】《火+氷/Fire+Ice》[MH2]", NM), card="Fire // Ice")
    assert r["card"] == "Fire // Ice"
    (r,) = _parse(("1", "【ENG】《秘密を掘り下げる者/Delver of Secrets》[INR]", NM),
                  card="Delver of Secrets // Insectile Aberration")
    assert r["card"] == "Delver of Secrets // Insectile Aberration"


def test_queries_and_paging():
    s = NukeninScrapper(fx=FX)
    assert s.search_params("Force of Will") == {"name": "Force of Will", "disp_number": 60}
    assert s.query_variants("Fire // Ice") == ["Fire // Ice", "Fire+Ice", "Fire"]
    assert s._has_next_page(b'<a href="?name=x&amp;pageno=2">2</a>', 1)
    assert not s._has_next_page(b"<ul class=item_list></ul>", 1)
