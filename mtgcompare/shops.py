import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from .cache import DEFAULT_TTL, CachedScrapper
from .scrapper import MtgScrapper
from .scrappers.blackfrog import BlackFrogScrapper
from .scrappers.cardrush import CardRushScrapper
from .scrappers.enndalgames import EnndalGamesScrapper
from .scrappers.hareruya import HareruyaScrapper
from .scrappers.mintmall import MintMallScrapper
from .scrappers.scryfall import ScryfallScrapper
from .scrappers.serra import CardshopSerraScrapper
from .scrappers.singlestar import SingleStarScrapper
from .scrappers.tokyomtg import TokyoMtgScrapper

_JP_FLAG = "\U0001F1EF\U0001F1F5"
_US_FLAG = "\U0001F1FA\U0001F1F8"

# JP shops: domestic tracked rates (ネコポス / クリックポスト equivalent, ~¥385).
# TCGPlayer: international to Japan via USPS First-Class Intl (~$8 ≈ ¥1,200).
_DEFAULT_JP_SHIPPING = 385
_DEFAULT_INTL_SHIPPING = 1200


# Single source of truth: every known shop with its display flag, default
# shipping cost, enabled flag, and scraper factory. Derived dicts/lists
# below stay in sync automatically — adding or disabling a shop is a one-
# line edit here.
_SHOPS: list[tuple[str, str, int, bool, Callable[[float], MtgScrapper]]] = [
    # (display_name, flag, shipping_jpy, enabled, factory)
    ("Hareruya",             _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  lambda fx: HareruyaScrapper(fx=fx)),
    ("TCGPlayer (Scryfall)", _US_FLAG, _DEFAULT_INTL_SHIPPING, True,  lambda fx: ScryfallScrapper(fx=fx)),
    ("SingleStar",           _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  lambda fx: SingleStarScrapper(fx=fx)),
    ("TokyoMTG",             _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  lambda fx: TokyoMtgScrapper(fx=fx)),
    ("Card Rush",            _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  lambda fx: CardRushScrapper(fx=fx)),
    ("Cardshop Serra",       _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  lambda fx: CardshopSerraScrapper(fx=fx)),
    ("BLACK FROG",           _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  lambda fx: BlackFrogScrapper(fx=fx)),
    ("MINT MALL",            _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  lambda fx: MintMallScrapper(fx=fx)),
    # ENNDAL GAMES temporarily disabled — www.enndalgames.com has no A
    # record at the AWS auth NS as of 2026-05-04, so cluster DNS lookups
    # fail. The scraper, tests, and canary remain in place; flip ``enabled``
    # to True once dig +short www.enndalgames.com @8.8.8.8 returns an IP.
    ("ENNDAL GAMES",         _JP_FLAG, _DEFAULT_JP_SHIPPING,   False, lambda fx: EnndalGamesScrapper(fx=fx)),
]


# All known shops keep entries in SHOP_FLAGS / SHIPPING_JPY (incl. disabled
# ones) so cached or in-flight rows for a shop that has just been turned
# off still render an emoji and can be re-enabled without a UI gap.
SHOP_FLAGS: dict[str, str] = {name: flag for name, flag, _, _, _ in _SHOPS}
SHIPPING_JPY: dict[str, int] = {name: ship for name, _, ship, _, _ in _SHOPS}

# Active set drives the filter checkboxes and which scrapers actually run.
ACTIVE_SHOPS: list[str] = [name for name, _, _, enabled, _ in _SHOPS if enabled]


def shop_slug(name: str) -> str:
    """URL/form-field-safe identifier for a shop name."""
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')


CACHE_ENABLED = os.environ.get("MTGCOMPARE_CACHE_ENABLED", "1") not in ("0", "false", "False")


def build_scrapers(fx: float, enabled: set[str] | None = None) -> list:
    """Construct the configured scrapers, optionally filtered to ``enabled``.

    ``enabled`` is a set of *display names* (e.g. ``{"Hareruya", "Card Rush"}``).
    None means "all on" — the default search behavior. Disabled shops in
    ``_SHOPS`` are always skipped.
    """
    raw = [
        (name, factory(fx))
        for name, _flag, _ship, is_enabled, factory in _SHOPS
        if is_enabled and (enabled is None or name in enabled)
    ]
    if not CACHE_ENABLED:
        return [s for _, s in raw]
    return [CachedScrapper(s, shop_name=name, ttl=DEFAULT_TTL) for name, s in raw]


def collect_prices(
    card_name: str,
    fx: float,
    *,
    enabled: set[str] | None = None,
    logger=None,
) -> list[dict]:
    """Fetch and concatenate all shop results for a single card.

    Fan-out is parallel: total wall-clock is bounded by the slowest shop,
    not the sum. Per-scraper exceptions are isolated so one failing shop
    doesn't drop results from the rest. If ``enabled`` is provided, only
    shops whose display name is in the set are scraped.
    """
    scrapers = build_scrapers(fx, enabled=enabled)
    results: list[dict] = []
    if not scrapers:
        return results
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
