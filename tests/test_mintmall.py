from pathlib import Path

import pytest

from mtgcompare.scrappers.mintmall import _stock_map, parse_search_html

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def search_html() -> str:
    return (FIXTURES / "mintmall_force_of_will.html").read_text(encoding="utf-8")


def test_stock_map_extracts_inventory(search_html):
    """The JS const ``specificationTreeSearchProductsTree`` is the source of truth
    for per-spec stock and price."""
    s = _stock_map(search_html)
    assert s, "stock map should be non-empty when the JSON is present"
    in_stock = {k: v for k, v in s.items() if v["stock"] > 0}
    # The current fixture has 2 specs with stock; both ought to show up.
    assert in_stock, "fixture should have at least one in-stock spec"


def test_parse_returns_records_when_in_stock_eng_nm_exists():
    """Build a synthetic page with a plain ENG-NM in-stock entry."""
    html = """
    <script>
    var specificationTreeSearchProductsTree = {"99":["2",false,true,"15000"]};
    </script>
    <div class="list_area">
      <a class="thumbnail" href="/products/detail.php?product_id=1"><span></span></a>
      <h4 class="recommend-title">【EMA】【ENG】《意志の力/Force of Will》</h4>
      <select name="specification">
        <option value="99">NM〜NM-</option>
      </select>
    </div>
    """
    records = parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0)
    assert len(records) == 1
    r = records[0]
    assert r["shop"] == "MINT MALL"
    assert r["card"] == "Force of Will"
    assert r["set"] == "EMA"
    assert r["condition"] == "NM"
    assert r["stock"] == 2
    # 15000 base × 1.1 tax = 16500
    assert r["price_jpy"] == pytest.approx(16500.0)
    assert r["price_usd"] == pytest.approx(110.0)
    assert r["link"].startswith("https://www.mint-mall.net/")


def test_parse_skips_japanese_listings():
    html = """
    <script>var specificationTreeSearchProductsTree = {"1":["3",false,true,"10000"]};</script>
    <div class="list_area">
      <a class="thumbnail" href="/p/1"></a>
      <h4 class="recommend-title">【EMA】【JPN】《意志の力/Force of Will》</h4>
      <select name="specification"><option value="1">NM〜NM-</option></select>
    </div>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_skips_foil_listings():
    html = """
    <script>var specificationTreeSearchProductsTree = {"1":["3",false,true,"30000"]};</script>
    <div class="list_area">
      <a class="thumbnail" href="/p/1"></a>
      <h4 class="recommend-title">【EMA】【ENG】【Foil】《意志の力/Force of Will》</h4>
      <select name="specification"><option value="1">NM〜NM-</option></select>
    </div>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_skips_variant_suffix_listings():
    """ボーダーレス版 / ショーケース版 / 日本画版 are alt-art variants — skip."""
    cases = [
        "【SOA】【ENG】〈019-M-U〉《意志の力/Force of Will》ショーケース版",
        "【2XM】【ENG】《意志の力/Force of Will》 ボーダーレス版",
        "【SOA】【ENG】《意志の力/Force of Will》日本画版",
    ]
    for title in cases:
        html = f"""
        <script>var specificationTreeSearchProductsTree = {{"1":["1",false,true,"15000"]}};</script>
        <div class="list_area">
          <a class="thumbnail" href="/p/1"></a>
          <h4 class="recommend-title">{title}</h4>
          <select name="specification"><option value="1">NM〜NM-</option></select>
        </div>
        """
        assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == [], \
            f"variant title not filtered: {title!r}"


def test_parse_skips_zero_stock():
    html = """
    <script>var specificationTreeSearchProductsTree = {"1":["0",false,true,"15000"]};</script>
    <div class="list_area">
      <a class="thumbnail" href="/p/1"></a>
      <h4 class="recommend-title">【EMA】【ENG】《意志の力/Force of Will》</h4>
      <select name="specification"><option value="1">NM〜NM-</option></select>
    </div>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_skips_below_nm_options():
    """Options labeled EX, EX-, PLD must be ignored even when in stock."""
    html = """
    <script>var specificationTreeSearchProductsTree = {"1":["3",false,true,"12000"],"2":["5",false,true,"10000"]};</script>
    <div class="list_area">
      <a class="thumbnail" href="/p/1"></a>
      <h4 class="recommend-title">【ALL】【ENG】《意志の力/Force of Will》</h4>
      <select name="specification">
        <option value="1">EX</option>
        <option value="2">EX-</option>
      </select>
    </div>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_ignores_non_matching_card():
    html = """
    <script>var specificationTreeSearchProductsTree = {"1":["3",false,true,"15000"]};</script>
    <div class="list_area">
      <a class="thumbnail" href="/p/1"></a>
      <h4 class="recommend-title">【EMA】【ENG】《意志の力/Force of Will》</h4>
      <select name="specification"><option value="1">NM〜NM-</option></select>
    </div>
    """
    assert parse_search_html(html, "Some Other Card", fx_jpy_per_usd=150.0) == []


def test_parse_against_real_fixture(search_html):
    """The live-fixture state may have no plain ENG-NM Force of Will in stock —
    test that the parser at minimum doesn't crash and returns shape-valid rows."""
    records = parse_search_html(search_html, "Force of Will", fx_jpy_per_usd=150.0)
    for r in records:
        assert r["shop"] == "MINT MALL"
        assert r["card"] == "Force of Will"
        assert isinstance(r["set"], str) and r["set"]
        assert isinstance(r["price_jpy"], float) and r["price_jpy"] > 0
        assert isinstance(r["stock"], int) and r["stock"] > 0
        assert r["condition"] == "NM"
