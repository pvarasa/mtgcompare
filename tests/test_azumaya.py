from pathlib import Path

import pytest

from mtgcompare.scrapers import azumaya
from mtgcompare.scrapers.azumaya import (
    AzumayaScrapper,
    Listing,
    Printing,
    parse_detail_html,
    parse_search_html,
)
from mtgcompare.scrapers.html_base import ScraperFetchError

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FX = 150.0


@pytest.fixture(autouse=True)
def _empty_printing_cache():
    azumaya._PRINTINGS.clear()
    yield
    azumaya._PRINTINGS.clear()


def _item(pid: str, title: str, *rows: tuple[str, str, str]) -> str:
    """One listing; rows are (condition, price, stock) cell texts."""
    trs = "".join(
        f"<tr><td>{c}　</td><td><span>{p}　</span></td><td><span>{s}</span></td></tr>"
        for c, p, s in rows
    )
    return f"""
    <div id="result_list_box--{pid}" class="col-sm-3 col-xs-6">
      <div class="product_item">
        <a href="https://azumayamtg.com/products/detail/{pid}">
          <div class="item_photo"><img src="/x.jpg"></div>
          <dl><dt class="item_name">{title}</dt>
            <table><thead><tr><th></th><th></th><th></th></tr></thead>
              <tbody>{trs}</tbody></table></dl>
        </a>
      </div>
    </div>"""


def _detail(code: str, *category_paths: list[str]) -> str:
    ols = "".join(
        "<ol>" + "".join(f'<li><a href="?category_id=1">{c}</a></li>' for c in path) + "</ol>"
        for path in category_paths
    )
    return f"""
    <p class="item_code">商品コード： <span id="item_code_default">
        {code}
    </span></p>
    <div id="relative_category_box" class="relative_cat"><p>関連カテゴリ</p>{ols}</div>"""


NM = ("NM", "15990円", "在庫：2")


def test_fixture_listings():
    html = (FIXTURES / "azumaya_force_of_will.html").read_bytes()
    listings = parse_search_html(html, "Force of Will")
    assert listings, "expected English NM Force of Will listings in the fixture"
    for listing in listings:
        assert listing.card == "Force of Will"
        assert listing.price_jpy > 0 and listing.stock > 0
        assert listing.link == f"https://azumayamtg.com/products/detail/{listing.product_id}"
        # capture_fixtures.py saves each listed product's page next to it.
        printing = parse_detail_html((FIXTURES / f"azumaya_detail_{listing.product_id}.html").read_bytes())
        assert printing.set and printing.number
    assert parse_search_html(html, "Some Other Card") == []


def test_listing_shape():
    (listing,) = parse_search_html(_item("233164", "[ENG]意志の力/Force of Will", NM), "Force of Will")
    assert listing == Listing("233164", "Force of Will", 15990.0, 2,
                              "https://azumayamtg.com/products/detail/233164")


@pytest.mark.parametrize("title", [
    "[JPN]意志の力/Force of Will",
    "[Foil][ENG]意志の力/Force of Will",
    "[エッチング][Foil][ENG]意志の力/Force of Will",
    "[プロモ][Foil][ENG]意志の力/Force of Will",
    "[ENG]意志の力/Force of Wills",
])
def test_skips_other_languages_foils_and_other_cards(title):
    assert parse_search_html(_item("1", title, NM), "Force of Will") == []


def test_non_foil_and_promo_tags_are_kept():
    assert parse_search_html(_item("1", "[Non-Foil][ENG]意志の力/Force of Will", NM), "Force of Will")
    assert parse_search_html(_item("1", "[プロモ][ENG]意志の力/Force of Will", NM), "Force of Will")


def test_only_nm_in_stock_rows():
    html = _item("1", "[ENG]意志の力/Force of Will",
                 ("NM-", "14990円", "在庫：1"), ("SP", "12990円", "在庫：3"), ("NM", "15990円", "在庫：0"))
    assert parse_search_html(html, "Force of Will") == []


def test_double_faced_names_match_the_front_or_both_faces():
    title = "[ENG]秘密を掘り下げる者/Delver of Secrets // 昆虫の逸脱者/Insectile Aberration"
    (listing,) = parse_search_html(_item("1", title, NM), "Delver of Secrets // Insectile Aberration")
    assert listing.card == "Delver of Secrets // Insectile Aberration"


@pytest.mark.parametrize("html,expected", [
    (_detail("DMR-0050E", ["【MTG：リマスター】", "ドミナリア・リマスター"]), Printing("DMR", "50")),
    (_detail("a25-0141EF", ["マスターズ", "マスターズ25th"]), Printing("A25", "141")),
    # Azumaya's own abbreviation; the category names the real code.
    (_detail("Al-0042E", ["アイスエイジブロック", "アライアンス(Alliances/AL/ALL)"]), Printing("ALL", "42")),
    (_detail("RV-0054E", ["基本セット", "リバイズド・エディション(Revised Edition/RV/3E/3ED)"]),
     Printing("3ED", "54")),
    # No usable code: an English set name, for the enrichment to map.
    (_detail("MPSAKH-010E", ["【MTG：パイオニア】", "マスターピース", "Amonkhet Invocations"]),
     Printing("Amonkhet Invocations", "10")),
    # No code at all.
    (_detail("", ["The List(MB,The List)"]), Printing("The List", None)),
    (_detail("", ["プロモーションカード", "グランプリプロモ"]), Printing(None, None)),
])
def test_detail_printing(html, expected):
    assert parse_detail_html(html) == expected


class _Resp:
    def __init__(self, body: str, status: int = 200):
        self.content = body.encode()
        self.status_code = status
        self.headers: dict = {}


class _Session:
    """Serves the listing for SEARCH_URL and a product page per id."""

    def __init__(self, listing: str, details: dict[str, str]):
        self.listing, self.details = listing, details
        self.detail_gets: list[str] = []

    def get(self, url, params=None, timeout=None):
        if url == azumaya.SEARCH_URL:
            return _Resp(self.listing)
        pid = url.rsplit("/", 1)[1]
        self.detail_gets.append(pid)
        return _Resp(self.details[pid]) if pid in self.details else _Resp("", 503)


def test_scraper_joins_listings_with_product_pages_and_remembers_them():
    listing = _item("1", "[ENG]意志の力/Force of Will", NM) + _item("2", "[ENG]意志の力/Force of Will", NM)
    session = _Session(listing, {
        "1": _detail("DMR-0050E", ["ドミナリア・リマスター"]),
        "2": _detail("", ["プロモーションカード"]),  # no set: dropped
    })
    scraper = AzumayaScrapper(fx=FX, session=session)  # type: ignore[arg-type]
    (r,) = scraper.get_prices("Force of Will")
    assert (r["shop"], r["set"], r["number"], r["variant"]) == ("Azumaya", "DMR", "50", None)
    assert r["price_usd"] == round(15990 / FX, 2)
    assert sorted(session.detail_gets) == ["1", "2"]

    scraper.get_prices("Force of Will")
    assert sorted(session.detail_gets) == ["1", "2"], "product pages are fetched once"


def test_a_failed_product_page_fails_the_search():
    session = _Session(_item("1", "[ENG]意志の力/Force of Will", NM), {})
    with pytest.raises(ScraperFetchError):
        AzumayaScrapper(fx=FX, session=session).get_prices("Force of Will")  # type: ignore[arg-type]
