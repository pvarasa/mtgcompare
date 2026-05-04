"""Canary tests — smoke-test all live shop endpoints and shared dependencies.

Detects when a shop changes its API, URL structure, or HTML layout, or when
a shared dependency (yfinance FX, MTGJSON) stops responding.

Run:  uv run pytest -m canary
"""
import pytest
import requests

from mtgcompare.scrappers.blackfrog import BlackFrogScrapper
from mtgcompare.scrappers.cardrush import CardRushScrapper
from mtgcompare.scrappers.enndalgames import EnndalGamesScrapper
from mtgcompare.scrappers.hareruya import HareruyaScrapper
from mtgcompare.scrappers.mintmall import MintMallScrapper
from mtgcompare.scrappers.scryfall import ScryfallScrapper
from mtgcompare.scrappers.serra import CardshopSerraScrapper
from mtgcompare.scrappers.singlestar import SingleStarScrapper
from mtgcompare.scrappers.tokyomtg import TokyoMtgScrapper
from mtgcompare.utils import get_fx

_DEFAULT_PROBE_CARD = "Force of Will"
_FX = 150.0

# Per-shop probe-card overrides for shops where Force of Will is too thin in
# stock to be a reliable canary signal. These need to be cards that are
# routinely listed in plain English NM (no foil, no variant).
_PROBE_CARD_BY_SHOP = {
    "MINT MALL": "Sol Ring",
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
    pytest.param(ScryfallScrapper,        "TCGPlayer (Scryfall)", "http",                         id="scryfall"),
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
        pytest.fail(f"get_fx failed — yfinance API may have changed: {exc}")

    assert isinstance(rate, (int, float)), \
        f"get_fx returned {type(rate).__name__}, expected a number"
    assert 50 < rate < 500, \
        f"JPY/USD rate {rate} is implausible — yfinance ticker or field name may have changed"


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
