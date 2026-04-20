from pathlib import Path

import pytest

from mtgcompare.scrappers.tokyomtg import parse_search_html

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def search_html() -> str:
    return (FIXTURES / "tokyomtg_force_of_will.html").read_text(encoding="utf-8")


def test_parse_returns_records_for_matching_card(search_html):
    records = parse_search_html(search_html, "Force of Will", fx_jpy_per_usd=150.0)
    assert records, "expected at least one Force of Will record in the fixture"
    for r in records:
        assert r["shop"] == "TokyoMTG"
        assert r["card"] == "Force of Will"
        assert isinstance(r["set"], str) and r["set"]
        assert isinstance(r["price_jpy"], float) and r["price_jpy"] > 0
        assert isinstance(r["price_usd"], float) and r["price_usd"] > 0
        assert isinstance(r["stock"], int) and r["stock"] > 0
        assert r["condition"] == "NM"
        assert r["link"].startswith("https://tokyomtg.com/carddetails.html?sc=")


def test_parse_case_insensitive_match(search_html):
    upper = parse_search_html(search_html, "FORCE OF WILL", fx_jpy_per_usd=150.0)
    mixed = parse_search_html(search_html, "Force of Will", fx_jpy_per_usd=150.0)
    assert len(upper) == len(mixed) > 0


def test_parse_ignores_non_matching_card(search_html):
    assert parse_search_html(search_html, "Some Other Card", fx_jpy_per_usd=150.0) == []


def test_parse_handcrafted_in_stock_record():
    html = """
    <div class="pwrapper"><div class="border row m-2 py-1">
      <div class="w-25 mx-1">
        <a href="carddetails.html?sc=797"><div class="pwimgcontainer"></div></a>
        <span class="lang-badge">English Version</span>
      </div>
      <div class="col mx-2">
        <a href="carddetails.html?sc=797"><h3>Force of Will</h3></a>
        <h3><a href="./cardpage.html?p=s&s=17"><b>Alliances</b></a></h3>
        <h3>Uncommon</h3>
        <ul class="nav nav-tabs">
          <li><a class="active" href="#reg_1_0_0">Regular</a></li>
        </ul>
        <div class="tab-content">
          <div id="reg_1_0_0" class="tab-pane fade show active">
            <h3 class="mt-2 price-text">&yen;14,990<br />Stock: 1</h3>
          </div>
        </div>
      </div>
    </div></div>
    """
    records = parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0)
    assert len(records) == 1
    assert records[0] == {
        "shop": "TokyoMTG",
        "card": "Force of Will",
        "set": "Alliances",
        "price_jpy": 14990.0,
        "price_usd": 99.93,
        "stock": 1,
        "condition": "NM",
        "link": "https://tokyomtg.com/carddetails.html?sc=797",
    }


def test_parse_skips_out_of_stock_regular():
    html = """
    <div class="pwrapper"><div class="border row m-2 py-1">
      <div class="w-25 mx-1">
        <a href="carddetails.html?sc=1"><div></div></a>
        <span class="lang-badge">English Version</span>
      </div>
      <div class="col mx-2">
        <a href="carddetails.html?sc=1"><h3>Force of Will</h3></a>
        <h3><a href="x"><b>Alliances</b></a></h3>
        <h3>Uncommon</h3>
        <div class="tab-content">
          <div id="reg_1_0_0" class="tab-pane fade show active">
            <h3 class="mt-2 out-of-stock-text text-left pb-5 mb-3">Out of stock</h3>
          </div>
        </div>
      </div>
    </div></div>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_skips_japanese_version_handcrafted():
    html = """
    <div class="pwrapper"><div class="border row m-2 py-1">
      <div class="w-25 mx-1">
        <a href="carddetails.html?sc=2"><div></div></a>
        <span class="lang-badge">Japanese Version</span>
      </div>
      <div class="col mx-2">
        <a href="carddetails.html?sc=2"><h3>Force of Will</h3></a>
        <h3><a href="x"><b>Alliances</b></a></h3>
        <h3>Uncommon</h3>
        <div class="tab-content">
          <div id="reg_2_0_0" class="tab-pane fade show active">
            <h3 class="mt-2 price-text">&yen;9,990<br />Stock: 2</h3>
          </div>
        </div>
      </div>
    </div></div>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_ignores_foil_only_stock():
    """Regular out of stock but Foil has stock — should skip (we only want NM non-foil)."""
    html = """
    <div class="pwrapper"><div class="border row m-2 py-1">
      <div class="w-25 mx-1">
        <a href="carddetails.html?sc=3"><div></div></a>
        <span class="lang-badge">English Version</span>
      </div>
      <div class="col mx-2">
        <a href="carddetails.html?sc=3"><h3>Force of Will</h3></a>
        <h3><a href="x"><b>Alliances</b></a></h3>
        <h3>Uncommon</h3>
        <div class="tab-content">
          <div id="reg_3_0_0" class="tab-pane fade show active">
            <h3 class="mt-2 out-of-stock-text">Out of stock</h3>
          </div>
          <div id="regfoil_3_0_0" class="tab-pane fade">
            <h3 class="mt-2 price-text">&yen;99,999<br />Stock: 1</h3>
          </div>
        </div>
      </div>
    </div></div>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


@pytest.mark.live
def test_live_tokyomtg_returns_results():
    """Hits TokyoMTG for real. Opt in: `uv run pytest -m live`."""
    from mtgcompare.scrappers.tokyomtg import TokyoMtgScrapper

    scraper = TokyoMtgScrapper(fx=150.0)
    records = scraper.get_prices("Force of Will")
    assert records, "expected live TokyoMTG to return at least one result"
    for r in records:
        assert r["card"].lower() == "force of will"
        assert r["price_jpy"] > 0
        assert r["link"].startswith("https://tokyomtg.com/")
