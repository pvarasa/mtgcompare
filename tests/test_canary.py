"""Canary tests — smoke-test all live shop endpoints and shared dependencies.

Detects when a shop changes its API, URL structure, or HTML layout, or when
a shared dependency (the FX providers, MTGJSON) stops responding.

Run:  uv run pytest -m canary
"""
import pytest
import requests

from mtgcompare.scrapers.blackfrog import BlackFrogScrapper
from mtgcompare.scrapers.cardrush import CardRushScrapper
from mtgcompare.scrapers.enndalgames import EnndalGamesScrapper
from mtgcompare.scrapers.hareruya import HareruyaScrapper
from mtgcompare.scrapers.mintmall import MintMallScrapper
from mtgcompare.scrapers.scryfall import ScryfallScrapper
from mtgcompare.scrapers.serra import CardshopSerraScrapper
from mtgcompare.scrapers.singlestar import SingleStarScrapper
from mtgcompare.scrapers.tcgplayer import TcgPlayerJpScrapper
from mtgcompare.scrapers.tokyomtg import TokyoMtgScrapper
from mtgcompare.utils import _from_er_api, _from_frankfurter, get_fx

_DEFAULT_PROBE_CARD = "Force of Will"
_FX = 150.0

# Per-shop probe-card overrides for shops where Force of Will is too thin in
# stock to be a reliable canary signal. These need to be cards that are
# routinely listed in plain English NM (no foil, no variant) *and* priced
# above the 50-yen floor asserted below — bulk commons like Cultivate or
# Giant Growth have the stock depth but sell for 10–50 yen, which trips the
# plausible-range check.
#
# SingleStar and BLACK FROG were added here after both reported a false
# canary failure on 2026-08-09: their parsers were fine, but every English
# NM Force of Will listing had sold out, so the probe returned zero rows and
# read as HTML drift.
_PROBE_CARD_BY_SHOP = {
    "MINT MALL": "Sol Ring",
    "SingleStar": "Swords to Plowshares",
    "BLACK FROG": "Swords to Plowshares",
}

_SHOPS = [
    pytest.param(HareruyaScrapper,        "Hareruya",            "https://www.hareruyamtg.com/", id="hareruya"),
    pytest.param(SingleStarScrapper,      "SingleStar",           "https://www.singlestar.jp/",   id="singlestar"),
    pytest.param(TokyoMtgScrapper,        "TokyoMTG",            "https://tokyomtg.com/",        id="tokyomtg"),
    pytest.param(CardRushScrapper,        "Card Rush",           "https://www.cardrush-mtg.jp/", id="cardrush"),
    pytest.param(CardshopSerraScrapper,   "Cardshop Serra",      "https://cardshop-serra.com/",  id="serra"),
    pytest.param(EnndalGamesScrapper,     "ENNDAL GAMES",        "https://www.enndalgames.com/", id="enndal"),
    pytest.param(BlackFrogScrapper,       "BLACK FROG",          "https://blackfrog.jp/",        id="blackfrog"),
    pytest.param(MintMallScrapper,        "MINT MALL",           "https://www.mint-mall.net/",   id="mintmall"),
    pytest.param(ScryfallScrapper,        "TCGPlayer market",    "http",                         id="scryfall"),
    pytest.param(TcgPlayerJpScrapper,     "TCGPlayer → JP",      "https://www.tcgplayer.com/product/", id="tcgplayer_jp"),
]


@pytest.mark.canary
@pytest.mark.parametrize("scraper_cls,expected_shop,link_prefix", _SHOPS)
def test_shop_canary(scraper_cls, expected_shop, link_prefix):
    scraper = scraper_cls(fx=_FX)
    probe_card = _PROBE_CARD_BY_SHOP.get(expected_shop, _DEFAULT_PROBE_CARD)

    try:
        records = scraper.get_prices(probe_card)
    except Exception as exc:
        pytest.fail(
            f"[{expected_shop}] network/HTTP error — endpoint may have moved or be down: {exc}"
        )

    assert records, (
        f"[{expected_shop}] parser returned no results for {probe_card!r} — "
        "HTML/API structure may have changed"
    )

    for r in records:
        assert r.get("shop") == expected_shop, \
            f"[{expected_shop}] wrong shop name in record: {r.get('shop')!r}"
        assert r.get("card", "").lower() == probe_card.lower(), \
            f"[{expected_shop}] wrong card name: {r.get('card')!r}"

        price_jpy = r.get("price_jpy")
        assert isinstance(price_jpy, (int, float)) and price_jpy > 0, \
            f"[{expected_shop}] invalid price_jpy: {price_jpy!r}"
        assert 50 < price_jpy < 5_000_000, \
            f"[{expected_shop}] price_jpy {price_jpy:,.0f} out of plausible range — pricing format may have changed"

        link = r.get("link", "")
        assert link.startswith(link_prefix), \
            f"[{expected_shop}] unexpected link domain: {link!r} (expected prefix: {link_prefix!r})"


@pytest.mark.canary
def test_fx_rate_is_plausible():
    try:
        rate = get_fx("jpy")
    except Exception as exc:
        pytest.fail(f"get_fx failed — every FX provider is down or has changed shape: {exc}")

    assert isinstance(rate, (int, float)), \
        f"get_fx returned {type(rate).__name__}, expected a number"
    assert 50 < rate < 500, \
        f"JPY/USD rate {rate} is implausible — an FX provider's response shape may have changed"


@pytest.mark.canary
@pytest.mark.parametrize("source", [_from_frankfurter, _from_er_api])
def test_each_fx_source_is_healthy(source):
    """Probe both providers separately.

    ``get_fx`` falls back to the second source, so testing it alone would
    stay green while the primary quietly rots. Each provider gets its own
    assertion so drift shows up in the one that broke.
    """
    try:
        rate = source("JPY")
    except Exception as exc:
        pytest.fail(f"{source.__name__} failed — endpoint or response shape may have changed: {exc}")

    assert 50 < rate < 500, \
        f"{source.__name__} returned an implausible JPY/USD rate: {rate}"


@pytest.mark.canary
def test_mtgjson_meta_is_reachable():
    try:
        resp = requests.get("https://mtgjson.com/api/v5/Meta.json", timeout=10)
    except Exception as exc:
        pytest.fail(f"MTGJSON Meta.json unreachable: {exc}")

    assert resp.status_code == 200, \
        f"MTGJSON Meta.json returned HTTP {resp.status_code}"

    data = resp.json()
    assert "data" in data and "date" in data.get("data", {}), \
        f"MTGJSON Meta.json structure changed — missing data.date: {data}"


# Sealed products: a long-lived, liquid booster box (Commander Masters Draft
# Booster Box) so a zero/None price means drift, not a sold-out product.
_SEALED_PROBE_PRODUCT_ID = 484924


@pytest.mark.canary
def test_tcgplayer_sealed_market_price():
    from mtgcompare.sealed.tcgplayer import TcgSealedClient
    price = TcgSealedClient().market_price(_SEALED_PROBE_PRODUCT_ID)
    assert price is not None and 50 < price < 5000, \
        f"TCGPlayer pricepoints returned an implausible sealed price: {price}"


@pytest.mark.canary
def test_tcgplayer_sealed_history_backfill():
    from mtgcompare.sealed.tcgplayer import TcgSealedClient
    points = TcgSealedClient().history(_SEALED_PROBE_PRODUCT_ID)
    # annual (52 weekly) + month (30 daily) buckets, minus overlaps/zeros.
    assert len(points) >= 30, f"TCGPlayer price history returned only {len(points)} points"


@pytest.mark.canary
def test_mtgjson_setlist_has_sealed_boosters():
    from mtgcompare.sealed.catalog import fetch_setlist, parse_setlist
    rows = parse_setlist(fetch_setlist(), updated_at="canary")
    boxes = [r for r in rows if r["category"] == "booster_box" and r["tcgplayer_product_id"]]
    assert len(boxes) >= 200, f"SetList.json yielded only {len(boxes)} priced booster boxes"


def _sealed_catalog_by_set():
    from mtgcompare.sealed.catalog import fetch_setlist, parse_setlist
    by_set: dict[str, list[dict]] = {}
    for row in parse_setlist(fetch_setlist(), updated_at="canary"):
        by_set.setdefault(row["set_code"], []).append(row)
    return by_set


@pytest.mark.canary
def test_hareruya_sealed_crawl_matches_catalog():
    from mtgcompare.sealed import jp_shops
    listings = jp_shops.crawl_hareruya()
    offers, stats = jp_shops.best_offers(
        jp_shops.HARERUYA, listings, _sealed_catalog_by_set(), jp_shops.hareruya_set_names(listings),
    )
    # 2026-11: ~970 parsed titles, all matched, ~240 in stock.
    assert stats["parsed"] >= 500, f"Hareruya sealed titles stopped parsing: {stats}"
    assert stats["matched"] >= 0.9 * stats["parsed"], f"Hareruya sealed match rate dropped: {stats}"
    assert len(offers) >= 50, f"Hareruya sealed in-stock offers suspiciously few: {stats}"


@pytest.mark.canary
def test_singlestar_sealed_crawl_matches_catalog():
    from mtgcompare.sealed import jp_shops
    listings = jp_shops.crawl_singlestar()
    names = jp_shops.hareruya_set_names(jp_shops.crawl_hareruya())
    offers, stats = jp_shops.best_offers(jp_shops.SINGLESTAR, listings, _sealed_catalog_by_set(), names)
    # 2026-11: ~400 parsed booster titles, ~97% matched.
    assert stats["parsed"] >= 200, f"SingleStar sealed titles stopped parsing: {stats}"
    assert stats["matched"] >= 0.85 * stats["parsed"], f"SingleStar sealed match rate dropped: {stats}"
    assert len(offers) >= 50, f"SingleStar sealed in-stock offers suspiciously few: {stats}"


@pytest.mark.canary
def test_serra_sealed_crawl_matches_catalog():
    from mtgcompare.sealed import jp_shops
    listings = jp_shops.crawl_serra()
    names = jp_shops.hareruya_set_names(jp_shops.crawl_hareruya())
    offers, stats = jp_shops.best_offers(jp_shops.SERRA, listings, _sealed_catalog_by_set(), names)
    # 2026-11: ~2370 products, ~900 booster titles, ~99% matched, ~130 in stock.
    assert stats["listings"] >= 1000, f"Serra sealed collection shrank or moved: {stats}"
    assert stats["parsed"] >= 400, f"Serra sealed titles stopped parsing: {stats}"
    assert stats["matched"] >= 0.9 * stats["parsed"], f"Serra sealed match rate dropped: {stats}"
    assert len(offers) >= 30, f"Serra sealed in-stock offers suspiciously few: {stats}"


@pytest.mark.canary
def test_hareruya_buylist_crawl_matches_catalog():
    from mtgcompare.sealed import jp_shops
    listings = jp_shops.crawl_hareruya_buylist()
    offers, stats = jp_shops.best_offers(
        jp_shops.HARERUYA_BUY, listings, _sealed_catalog_by_set(), jp_shops.hareruya_set_names(listings),
    )
    # 2026-11: ~1020 bids (boxes + packs), ~880 parsed, all matched.
    assert stats["parsed"] >= 400, f"Hareruya buylist titles stopped parsing: {stats}"
    assert stats["matched"] >= 0.9 * stats["parsed"], f"Hareruya buylist match rate dropped: {stats}"
    assert len(offers) >= 200, f"Hareruya buylist bids suspiciously few: {stats}"
