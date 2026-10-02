from pathlib import Path

import pytest

from mtgcompare.scrapers.cardrush import parse_search_html

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def search_html() -> str:
    return (FIXTURES / "cardrush_force_of_will.html").read_text(encoding="utf-8")


def test_parse_returns_records_for_matching_card(search_html):
    records = parse_search_html(search_html, "Force of Will", fx_jpy_per_usd=150.0)
    assert records, "expected at least one Force of Will record in the fixture"
    for r in records:
        assert r["shop"] == "Card Rush"
        assert r["card"] == "Force of Will"
        assert isinstance(r["set"], str) and r["set"]
        assert isinstance(r["price_jpy"], float) and r["price_jpy"] > 0
        assert isinstance(r["price_usd"], float) and r["price_usd"] > 0
        assert isinstance(r["stock"], int) and r["stock"] > 0
        assert r["condition"] == "NM"
        assert r["link"].startswith("https://www.cardrush-mtg.jp/")


def test_parse_case_insensitive_match(search_html):
    upper = parse_search_html(search_html, "FORCE OF WILL", fx_jpy_per_usd=150.0)
    mixed = parse_search_html(search_html, "Force of Will", fx_jpy_per_usd=150.0)
    assert len(upper) == len(mixed) > 0


def test_parse_ignores_non_matching_card(search_html):
    assert parse_search_html(search_html, "Some Other Card", fx_jpy_per_usd=150.0) == []


def test_parse_skips_japanese_listings():
    html = """
    <li class="list_item_cell">
      <div class="item_data">
        <a href="/p/jp1" class="item_data_link">
          <p class="item_name"><span class="goods_name">意志の力/Force of Will《日本語》【SOA】</span></p>
          <div class="item_info">
            <div class="price"><p class="selling_price"><span class="figure">9,000円</span></p></div>
            <p class="stock">在庫数 5枚</p>
          </div>
        </a>
      </div>
    </li>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_skips_graded_below_nm():
    """[EX], [PLD], [HPLD], [POOR] grades should be dropped."""
    html = """
    <li class="list_item_cell">
      <div class="item_data" data-product-id="1">
        <a href="https://www.cardrush-mtg.jp/product/1" class="item_data_link">
          <p class="item_name">
            <span class="goods_name">[EX]意志の力/Force of Will《英語》【ALL】</span>
          </p>
          <div class="item_info">
            <div class="price"><p class="selling_price"><span class="figure">15,000円</span></p></div>
            <p class="stock">在庫数 4枚</p>
          </div>
        </a>
      </div>
    </li>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_accepts_unprefixed_listing_as_nm():
    """Listings with no condition bracket are NM by Card Rush convention."""
    html = """
    <li class="list_item_cell">
      <div class="item_data" data-product-id="2">
        <a href="https://www.cardrush-mtg.jp/product/2" class="item_data_link">
          <p class="item_name">
            <span class="goods_name">意志の力/Force of Will《英語》【DMR】</span>
          </p>
          <div class="item_info">
            <div class="price"><p class="selling_price"><span class="figure">12,000円</span></p></div>
            <p class="stock">在庫数 3枚</p>
          </div>
        </a>
      </div>
    </li>
    """
    records = parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0)
    assert len(records) == 1
    assert records[0]["set"] == "DMR"
    assert records[0]["price_jpy"] == 12000.0
    assert records[0]["stock"] == 3
    assert records[0]["condition"] == "NM"


def test_parse_handles_search_emphasis_spans():
    """The site wraps matched search terms in <span class='result_emphasis'><b>...</b></span>."""
    html = """
    <li class="list_item_cell">
      <div class="item_data" data-product-id="3">
        <a href="https://www.cardrush-mtg.jp/product/3" class="item_data_link">
          <p class="item_name">
            <span class="goods_name">意志の力/<span class="result_emphasis"><b>Force</b></span> <span class="result_emphasis"><b>of</b></span> <span class="result_emphasis"><b>Will</b></span>《英語》【EMA】</span>
          </p>
          <div class="item_info">
            <div class="price"><p class="selling_price"><span class="figure">14,500円</span></p></div>
            <p class="stock">在庫数 2枚</p>
          </div>
        </a>
      </div>
    </li>
    """
    records = parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0)
    assert len(records) == 1
    assert records[0]["card"] == "Force of Will"


def test_parse_skips_zero_stock():
    html = """
    <li class="list_item_cell">
      <div class="item_data">
        <a href="/product/4" class="item_data_link">
          <p class="item_name"><span class="goods_name">意志の力/Force of Will《英語》【ALL】</span></p>
          <div class="item_info">
            <div class="price"><p class="selling_price"><span class="figure">15,000円</span></p></div>
            <p class="stock">在庫数 0枚</p>
          </div>
        </a>
      </div>
    </li>
    """
    assert parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0) == []


def test_parse_handles_variant_flavor_brackets():
    """Variants like (旧枠仕様)/old-frame still parse and match the card."""
    html = """
    <li class="list_item_cell">
      <div class="item_data">
        <a href="https://www.cardrush-mtg.jp/product/5" class="item_data_link">
          <p class="item_name"><span class="goods_name">(旧枠仕様)意志の力/Force of Will《英語》【DMR】</span></p>
          <div class="item_info">
            <div class="price"><p class="selling_price"><span class="figure">20,000円</span></p></div>
            <p class="stock">在庫数 1枚</p>
          </div>
        </a>
      </div>
    </li>
    """
    records = parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0)
    assert len(records) == 1
    assert records[0]["set"] == "DMR"


def test_parse_price_jpy_to_usd_conversion():
    html = """
    <li class="list_item_cell">
      <div class="item_data">
        <a href="/p/6" class="item_data_link">
          <p class="item_name"><span class="goods_name">意志の力/Force of Will《英語》【ALL】</span></p>
          <div class="item_info">
            <div class="price"><p class="selling_price"><span class="figure">15,000円</span></p></div>
            <p class="stock">在庫数 4枚</p>
          </div>
        </a>
      </div>
    </li>
    """
    records = parse_search_html(html, "Force of Will", fx_jpy_per_usd=150.0)
    assert records[0]["price_jpy"] == 15000.0
    assert records[0]["price_usd"] == pytest.approx(100.0)


def _cell(name: str, price: str = "2,190円", stock: str = "在庫数 1枚") -> str:
    return f"""
    <li class="list_item_cell"><div class="item_data">
      <a href="https://www.cardrush-mtg.jp/product/9" class="item_data_link">
        <p class="item_name"><span class="goods_name">{name}</span></p>
        <div class="item_info">
          <div class="price"><p class="selling_price"><span class="figure">{price}</span></p></div>
          <p class="stock">{stock}</p>
        </div>
      </a>
    </div></li>
    """


@pytest.mark.parametrize("name", [
    "(FOIL)太陽の指輪/ Sol Ring 《英語》【CC1】",
    "(サージFOIL)太陽の指輪/ Sol Ring 《英語》【TMC】",
    "(FOIL)(1604)太陽の指輪/ Sol Ring 《英語》【SLD】",
])
def test_parse_skips_foil_tagged_listings(name):
    """Foils used to parse as plain English listings via the flavor group."""
    assert parse_search_html(_cell(name), "Sol Ring", fx_jpy_per_usd=150.0) == []


def test_parse_accepts_stacked_tags_and_collector_number_suffix():
    html = _cell("(1604)(旧枠仕様)太陽の指輪/ Sol Ring 《英語》【SLD】#292")
    [record] = parse_search_html(html, "Sol Ring", fx_jpy_per_usd=150.0)
    assert record["set"] == "SLD"


def test_parse_matches_multi_face_query_against_front_face_listing():
    html = _cell("砕骨の巨人/ Bonecrusher Giant 《英語》【ELD】")
    [record] = parse_search_html(html, "Bonecrusher Giant // Stomp", fx_jpy_per_usd=150.0)
    assert record["card"] == "Bonecrusher Giant // Stomp"
