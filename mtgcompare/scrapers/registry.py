import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait

from .base import MtgScrapper
from .blackfrog import BlackFrogScrapper
from .cache import DEFAULT_TTL, CachedScrapper
from .cardrush import CardRushScrapper
from .enndalgames import EnndalGamesScrapper
from .hareruya import HareruyaScrapper
from .mintmall import MintMallScrapper
from .scryfall import ScryfallScrapper
from .serra import CardshopSerraScrapper
from .singlestar import SingleStarScrapper
from .tcgplayer import TcgPlayerJpScrapper
from .tokyomtg import TokyoMtgScrapper

_JP_FLAG = "\U0001F1EF\U0001F1F5"
_US_FLAG = "\U0001F1FA\U0001F1F8"

# JP shops: domestic tracked rates (ネコポス / クリックポスト equivalent, ~¥385).
# TCGPlayer: international from US to JP. Realistic landed cost via the
# typical TCG Direct multi-seller bundle + USPS First-Class Intl is now
# closer to ~¥2,000 (carrier fuel surcharges + handling). ¥1,200 was
# the 2024 estimate and was understating the shipping line item.
_DEFAULT_JP_SHIPPING = 385
_DEFAULT_INTL_SHIPPING = 2000


# Single source of truth: every known shop with its display flag, default
# shipping cost, enabled flag, marketplace flag, and scraper factory.
# Derived dicts/lists below stay in sync automatically — adding or
# disabling a shop is a one-line edit here.
#
# marketplace=True marks shops whose records carry the offer's own
# seller shipping as ship_jpy (the include-shipping sort uses it instead
# of a flat per-shop estimate, so their flat shipping stays ¥0 and is
# never user-editable), and whose per-offer shipping doesn't sum to an
# order total (so decklist pricing skips them).
_SHOPS: list[tuple[str, str, int, bool, bool, Callable[[float], MtgScrapper]]] = [
    # (display_name, flag, shipping_jpy, enabled, marketplace, factory)
    ("Hareruya",             _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  False, lambda fx: HareruyaScrapper(fx=fx)),
    # Renamed from "TCGPlayer (Scryfall)" in v1.9 — shop_listings /
    # shop_query_log rows under the old name are inert and can be purged.
    ("TCGPlayer market",     _US_FLAG, _DEFAULT_INTL_SHIPPING, True,  False, lambda fx: ScryfallScrapper(fx=fx)),
    # Cheapest listing that actually ships to Japan.
    ("TCGPlayer → JP",       _US_FLAG, 0,                      True,  True,  lambda fx: TcgPlayerJpScrapper(fx=fx)),
    ("SingleStar",           _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  False, lambda fx: SingleStarScrapper(fx=fx)),
    ("TokyoMTG",             _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  False, lambda fx: TokyoMtgScrapper(fx=fx)),
    ("Card Rush",            _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  False, lambda fx: CardRushScrapper(fx=fx)),
    ("Cardshop Serra",       _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  False, lambda fx: CardshopSerraScrapper(fx=fx)),
    ("BLACK FROG",           _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  False, lambda fx: BlackFrogScrapper(fx=fx)),
    ("MINT MALL",            _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  False, lambda fx: MintMallScrapper(fx=fx)),
    # ENNDAL GAMES still disabled. Public resolvers recovered on 2026-05-26
    # (dig +short www.enndalgames.com @8.8.8.8 → 13.159.57.5 / 52.193.201.15),
    # but the *cluster's* upstream DNS still can't resolve www — an in-pod
    # socket.gethostbyname('www.enndalgames.com') returns Errno -5 while the
    # apex enndalgames.com (219.94.128.207) does resolve. The apex isn't a
    # usable fallback: its TLS cert is valid only for www. So prod would just
    # log fast NameResolutionErrors and contribute nothing. Re-enable only
    # once `kubectl -n apps exec <pod> -- python -c
    # "import socket; socket.gethostbyname('www.enndalgames.com')"` succeeds.
    ("ENNDAL GAMES",         _JP_FLAG, _DEFAULT_JP_SHIPPING,   False, False, lambda fx: EnndalGamesScrapper(fx=fx)),
]


# All known shops keep entries in SHOP_FLAGS / SHIPPING_JPY (incl. disabled
# ones) so cached or in-flight rows for a shop that has just been turned
# off still render an emoji and can be re-enabled without a UI gap.
SHOP_FLAGS: dict[str, str] = {name: flag for name, flag, _, _, _, _ in _SHOPS}
SHIPPING_JPY: dict[str, int] = {name: ship for name, _, ship, _, _, _ in _SHOPS}

# Active set drives the filter checkboxes and which scrapers actually run.
ACTIVE_SHOPS: list[str] = [name for name, _, _, enabled, _, _ in _SHOPS if enabled]

# See the marketplace column above: ¥0-pinned shipping (already in the
# price) + excluded from decklist pricing.
MARKETPLACE_SHOPS: frozenset[str] = frozenset(
    name for name, _, _, _, marketplace, _ in _SHOPS if marketplace
)


def shop_slug(name: str) -> str:
    """URL/form-field-safe identifier for a shop name."""
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')


CACHE_ENABLED = os.environ.get("MTGCOMPARE_CACHE_ENABLED", "1") not in ("0", "false", "False")

# Per-shop wall-clock cap (seconds) for a single card query. Slow shops
# would otherwise pin the per-card fan-out at their tail latency, and a
# few bad apples (SingleStar, Cardshop Serra) routinely take 20–30s,
# which compounds across a 100-card decklist and trips upstream
# (Cloudflare) 524 timeouts. When a shop exceeds this, its results are
# dropped for this query; the late thread is left to finish in the
# background — if its result lands, the cache layer captures it so the
# next request for the same card is a fast hit.
SHOP_QUERY_TIMEOUT_S = float(os.environ.get("MTGCOMPARE_SHOP_QUERY_TIMEOUT_S", "30"))


def build_scrapers(fx: float, enabled: set[str] | None = None) -> list:
    """Construct the configured scrapers, optionally filtered to ``enabled``.

    ``enabled`` is a set of *display names* (e.g. ``{"Hareruya", "Card Rush"}``).
    None means "all on" — the default search behavior. Disabled shops in
    ``_SHOPS`` are always skipped.
    """
    raw = [
        (name, factory(fx))
        for name, _flag, _ship, is_enabled, _marketplace, factory in _SHOPS
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
    timeouts_out: set[str] | None = None,
) -> list[dict]:
    """Fetch and concatenate all shop results for a single card.

    Fan-out is parallel: total wall-clock is bounded by the slowest shop,
    not the sum. Per-scraper exceptions are isolated so one failing shop
    doesn't drop results from the rest. If ``enabled`` is provided, only
    shops whose display name is in the set are scraped.

    ``timeouts_out`` is an optional mutable set the caller can supply to
    learn which shops exceeded ``SHOP_QUERY_TIMEOUT_S`` on this call —
    display names are added in-place. Useful for surfacing partial-result
    warnings in the UI.
    """
    scrapers = build_scrapers(fx, enabled=enabled)
    results: list[dict] = []
    if not scrapers:
        return results
    ex = ThreadPoolExecutor(max_workers=len(scrapers))
    try:
        futures = {ex.submit(s.get_prices, card_name): s for s in scrapers}
        done, not_done = wait(futures, timeout=SHOP_QUERY_TIMEOUT_S)
        for fut in done:
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
        for fut in not_done:
            scraper = futures[fut]
            fut.cancel()
            shop_label = getattr(scraper, "shop_name", scraper.__class__.__name__)
            if timeouts_out is not None:
                timeouts_out.add(shop_label)
            if logger is not None:
                logger.warning(
                    "event=shop_query_timeout shop=%s card=%r timeout_s=%.1f",
                    shop_label, card_name, SHOP_QUERY_TIMEOUT_S,
                )
    finally:
        # Don't block the caller on stragglers — their threads finish in
        # the background; the cache write at the end of get_prices still
        # benefits the next request.
        ex.shutdown(wait=False, cancel_futures=True)
    return results
