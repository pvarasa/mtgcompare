import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from .cache import DEFAULT_TTL, CachedScrapper
from .scrappers.blackfrog import BlackFrogScrapper
from .scrappers.cardrush import CardRushScrapper
from .scrappers.enndalgames import EnndalGamesScrapper  # noqa: F401  re-enabled in build_scrapers when DNS comes back
from .scrappers.hareruya import HareruyaScrapper
from .scrappers.mintmall import MintMallScrapper
from .scrappers.scryfall import ScryfallScrapper
from .scrappers.serra import CardshopSerraScrapper
from .scrappers.singlestar import SingleStarScrapper
from .scrappers.tokyomtg import TokyoMtgScrapper

SHOP_FLAGS = {
    "Hareruya": "\U0001F1EF\U0001F1F5",
    "SingleStar": "\U0001F1EF\U0001F1F5",
    "TokyoMTG": "\U0001F1EF\U0001F1F5",
    "Card Rush": "\U0001F1EF\U0001F1F5",
    "Cardshop Serra": "\U0001F1EF\U0001F1F5",
    "ENNDAL GAMES": "\U0001F1EF\U0001F1F5",
    "BLACK FROG": "\U0001F1EF\U0001F1F5",
    "MINT MALL": "\U0001F1EF\U0001F1F5",
    "TCGPlayer (Scryfall)": "\U0001F1FA\U0001F1F8",
}

# Default per-order shipping in JPY, assuming buyer is in Japan.
# JP shops: domestic tracked rates (ネコポス / クリックポスト equivalent, ~¥385).
# TCGPlayer: international to Japan via USPS First-Class Intl (~$8 ≈ ¥1,200).
SHIPPING_JPY: dict[str, int] = {
    "Hareruya":             385,
    "SingleStar":           385,
    "TokyoMTG":             385,
    "Card Rush":            385,
    "Cardshop Serra":       385,
    "ENNDAL GAMES":         385,
    "BLACK FROG":           385,
    "MINT MALL":            385,
    "TCGPlayer (Scryfall)": 1200,
}


def shop_slug(name: str) -> str:
    """URL/form-field-safe identifier for a shop name."""
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')


CACHE_ENABLED = os.environ.get("MTGCOMPARE_CACHE_ENABLED", "1") not in ("0", "false", "False")


def build_scrapers(fx: float) -> list:
    raw = [
        ("Hareruya",             HareruyaScrapper(fx=fx)),
        ("TCGPlayer (Scryfall)", ScryfallScrapper(fx=fx)),
        ("SingleStar",           SingleStarScrapper(fx=fx)),
        ("TokyoMTG",             TokyoMtgScrapper(fx=fx)),
        ("Card Rush",            CardRushScrapper(fx=fx)),
        ("Cardshop Serra",       CardshopSerraScrapper(fx=fx)),
        ("BLACK FROG",           BlackFrogScrapper(fx=fx)),
        ("MINT MALL",            MintMallScrapper(fx=fx)),
        # ENNDAL GAMES temporarily disabled — www.enndalgames.com has no A
        # record at the AWS auth NS as of 2026-05-04, so cluster DNS lookups
        # fail. The scraper + tests + canary remain in place; re-enable this
        # line once dig +short www.enndalgames.com @8.8.8.8 returns an IP.
        # ("ENNDAL GAMES",         EnndalGamesScrapper(fx=fx)),
    ]
    if not CACHE_ENABLED:
        return [s for _, s in raw]
    return [CachedScrapper(s, shop_name=name, ttl=DEFAULT_TTL) for name, s in raw]


def collect_prices(card_name: str, fx: float, logger=None) -> list[dict]:
    """Fetch and concatenate all shop results for a single card.

    Fan-out is parallel: total wall-clock is bounded by the slowest shop,
    not the sum. Per-scraper exceptions are isolated so one failing shop
    doesn't drop results from the rest.
    """
    scrapers = build_scrapers(fx)
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(scrapers)) as ex:
        futures = {ex.submit(s.get_prices, card_name): s for s in scrapers}
        for fut in as_completed(futures):
            scraper = futures[fut]
            try:
                results.extend(fut.result())
            except Exception as exc:
                if logger is not None:
                    logger.error(
                        "Scraper %s failed for %r: %s",
                        scraper.__class__.__name__,
                        card_name,
                        exc,
                    )
    return results
