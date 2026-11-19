import os
import re
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed, wait
from concurrent.futures import TimeoutError as FuturesTimeout
from time import monotonic
from typing import NamedTuple

from .azumaya import AzumayaScrapper
from .base import MtgScrapper
from .bigweb import BigwebScrapper
from .blackfrog import BlackFrogScrapper
from .cache import DEFAULT_TTL, CachedScrapper
from .cardrush import CardRushScrapper
from .enrich import EnrichingScrapper
from .hareruya import HareruyaScrapper
from .mintmall import MintMallScrapper
from .mythic import MythicStoreScrapper
from .nukenin import NukeninScrapper
from .scryfall import ScryfallScrapper
from .serra import CardshopSerraScrapper
from .singlestar import SingleStarScrapper
from .tcgplayer import TcgPlayerJpScrapper
from .tokyomtg import TokyoMtgScrapper

_JP_FLAG = "\U0001F1EF\U0001F1F5"
_US_FLAG = "\U0001F1FA\U0001F1F8"
_CA_FLAG = "\U0001F1E8\U0001F1E6"

# JP shops: domestic tracked rates (ネコポス / クリックポスト equivalent, ~¥385).
# TCGPlayer: international from US to JP. Realistic landed cost via the
# typical TCG Direct multi-seller bundle + USPS First-Class Intl is now
# closer to ~¥2,000 (carrier fuel surcharges + handling). ¥1,200 was
# the 2024 estimate and was understating the shipping line item.
_DEFAULT_JP_SHIPPING = 385
_DEFAULT_INTL_SHIPPING = 2000
# The Mythic Store (Quebec) → Japan: ChitChats Select, CA$8.95 for one card
# in a cart quote on 2026-11-10 (≈¥1,000 at the day's rate).
_MYTHIC_SHIPPING = 1000


class Shop(NamedTuple):
    """One known shop and everything the registry needs to drive it."""
    name: str          # display name shown in the UI, e.g. "Hareruya"
    flag: str          # country-flag emoji
    shipping_jpy: int  # flat per-shop shipping estimate (¥0 for marketplaces)
    enabled: bool      # False = known but not scraped; kept so cached rows still render
    marketplace: bool  # records carry the offer's own ship_jpy; skipped by decklist pricing
    factory: Callable[[float], MtgScrapper]
    # The price is a reference, not an offer anyone can buy at: shown
    # labelled as such, never badged "cheapest", kept out of decklist totals.
    reference: bool = False
    # False keeps a shop out of decklist pricing: too slow to answer a
    # whole deck's cards inside SHOP_QUERY_TIMEOUT_S without piling
    # requests onto a small shop. Single-card search still queries it.
    decklists: bool = True


# Single source of truth: every known shop. Derived dicts/lists below stay in
# sync automatically — adding or disabling a shop is a one-line edit here.
#
# marketplace=True marks shops whose records carry the offer's own seller
# shipping as ship_jpy (the include-shipping sort uses it instead of a flat
# per-shop estimate, so their flat shipping stays ¥0 and is never
# user-editable), and whose per-offer shipping doesn't sum to an order total
# (so decklist pricing skips them).
_SHOPS: list[Shop] = [
    Shop("Hareruya",         _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  False, lambda fx: HareruyaScrapper(fx=fx)),
    # Renamed from "TCGPlayer (Scryfall)" in v1.9 — shop_listings /
    # shop_query_log rows under the old name are inert and can be purged.
    # Scryfall's prices.usd: TCGPlayer's average of recent US sales. From
    # Japan the offers that ship cost more, plus $12-20 shipping (Mistrise
    # Village 2026-11: market $6.34, cheapest NM shipping to JP $6.77 +
    # $19.99), so it is a reference line, not a shop.
    Shop("TCGPlayer market", _US_FLAG, _DEFAULT_INTL_SHIPPING, True,  False, lambda fx: ScryfallScrapper(fx=fx),
         reference=True),
    # Cheapest listing that actually ships to Japan.
    Shop("TCGPlayer → JP",   _US_FLAG, 0,                      True,  True,  lambda fx: TcgPlayerJpScrapper(fx=fx)),
    Shop("SingleStar",       _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  False, lambda fx: SingleStarScrapper(fx=fx)),
    Shop("TokyoMTG",         _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  False, lambda fx: TokyoMtgScrapper(fx=fx)),
    Shop("Card Rush",        _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  False, lambda fx: CardRushScrapper(fx=fx)),
    Shop("Cardshop Serra",   _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  False, lambda fx: CardshopSerraScrapper(fx=fx)),
    Shop("BLACK FROG",       _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  False, lambda fx: BlackFrogScrapper(fx=fx)),
    Shop("BIGWEB",           _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  False, lambda fx: BigwebScrapper(fx=fx)),
    # ~14 s a search from the cluster, 4 requests at a time: a 9-card
    # deck would already push its last cards past the 30 s cap.
    Shop("Nukenin",          _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  False, lambda fx: NukeninScrapper(fx=fx),
         decklists=False),
    Shop("Azumaya",          _JP_FLAG, _DEFAULT_JP_SHIPPING,   True,  False, lambda fx: AzumayaScrapper(fx=fx)),
    Shop("The Mythic Store", _CA_FLAG, _MYTHIC_SHIPPING,       True,  False, lambda fx: MythicStoreScrapper(fx=fx)),
    # MINT MALL is disabled while its own site is down: since 2026-10-22
    # 10:00 JST every URL answers 503 with a maintenance page announcing a
    # service-wide suspension for a security review, end date undecided
    # (night-time access had been restricted from 10-19). Our last good
    # fetch was 10-12. Re-enable once https://www.mint-mall.net/ serves
    # listings again and `uv run pytest -m canary -k mintmall` passes.
    Shop("MINT MALL",        _JP_FLAG, _DEFAULT_JP_SHIPPING,   False, False, lambda fx: MintMallScrapper(fx=fx)),
]


# All known shops keep entries in SHOP_FLAGS / SHIPPING_JPY (incl. disabled
# ones) so cached or in-flight rows for a shop that has just been turned
# off still render an emoji and can be re-enabled without a UI gap.
SHOP_FLAGS: dict[str, str] = {s.name: s.flag for s in _SHOPS}
SHIPPING_JPY: dict[str, int] = {s.name: s.shipping_jpy for s in _SHOPS}

# Active set drives the filter checkboxes and which scrapers actually run.
ACTIVE_SHOPS: list[str] = [s.name for s in _SHOPS if s.enabled]

# See the marketplace column above: ¥0-pinned shipping (already in the
# price) + excluded from decklist pricing.
MARKETPLACE_SHOPS: frozenset[str] = frozenset(s.name for s in _SHOPS if s.marketplace)

# See the reference column above.
REFERENCE_SHOPS: frozenset[str] = frozenset(s.name for s in _SHOPS if s.reference)

# Shops decklist pricing never queries, for any of the reasons above.
DECKLIST_SKIPPED_SHOPS: frozenset[str] = (
    MARKETPLACE_SHOPS | REFERENCE_SHOPS
    | frozenset(s.name for s in _SHOPS if not s.decklists)
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
        (s.name, EnrichingScrapper(s.factory(fx), s.name))
        for s in _SHOPS
        if s.enabled and (enabled is None or s.name in enabled)
    ]
    if not CACHE_ENABLED:
        return [s for _, s in raw]
    return [CachedScrapper(s, shop_name=name, ttl=DEFAULT_TTL) for name, s in raw]


def _shop_label(scraper) -> str:
    # The fan-out runs wrappers (CachedScrapper / EnrichingScrapper), whose
    # class name says nothing; both carry the shop's display name.
    return getattr(scraper, "shop_name", None) or scraper.__class__.__name__


class PriceFanout:
    """One card's queries to every shop, running in parallel.

    ``first`` hands back what the shops that answered within some seconds
    found, and ``rest`` the others' rows as each one answers, up to
    ``SHOP_QUERY_TIMEOUT_S`` after the start. So single-card search can
    show the fast shops while the slow ones (Nukenin, ~14 s) are still
    answering. Per-shop exceptions are logged and count as no rows.

    Shops still running at the deadline are timed out (``timed_out``)
    and their threads left to finish in the background: if their result
    lands, the cache layer keeps it for the next search.
    """

    def __init__(self, card_name: str, fx: float, *, enabled: set[str] | None = None, logger=None):
        self.card_name = card_name
        self.logger = logger
        self.timed_out: set[str] = set()
        self._deadline = monotonic() + SHOP_QUERY_TIMEOUT_S
        scrapers = build_scrapers(fx, enabled=enabled)
        self._ex = ThreadPoolExecutor(max_workers=len(scrapers)) if scrapers else None
        self._pending: dict[Future, str] = {
            self._ex.submit(s.get_prices, card_name): _shop_label(s) for s in scrapers
        } if self._ex else {}

    def pending(self) -> list[str]:
        """Shops that haven't answered yet, in registry order."""
        return list(self._pending.values())

    def first(self, timeout_s: float) -> list[dict]:
        """Rows of every shop that answers within ``timeout_s`` (never past
        the overall deadline). The others stay ``pending``."""
        if not self._pending:
            return []
        done, _ = wait(self._pending, timeout=max(0.0, min(timeout_s, self._remaining())))
        results: list[dict] = []
        for fut in [f for f in self._pending if f in done]:
            results.extend(self._take(fut))
        if not self._pending:
            self.close()
        return results

    def rest(self) -> Iterator[tuple[str, list[dict]]]:
        """``(shop, rows)`` for each pending shop as it answers; at the
        deadline the ones still running are timed out."""
        try:
            for fut in as_completed(list(self._pending), timeout=self._remaining()):
                shop = self._pending[fut]
                yield shop, self._take(fut)
        except FuturesTimeout:
            pass
        finally:
            self.expire()

    def expire(self) -> None:
        """Time out whatever hasn't answered and release the pool."""
        for fut, shop in self._pending.items():
            fut.cancel()
            self.timed_out.add(shop)
            if self.logger is not None:
                self.logger.warning(
                    "event=shop_query_timeout shop=%s card=%r timeout_s=%.1f",
                    shop, self.card_name, SHOP_QUERY_TIMEOUT_S,
                )
        self._pending.clear()
        self.close()

    def close(self) -> None:
        # Don't block the caller on stragglers — their threads finish in
        # the background; the cache write at the end of get_prices still
        # benefits the next request.
        if self._ex is not None:
            self._ex.shutdown(wait=False, cancel_futures=True)

    def _remaining(self) -> float:
        return max(0.0, self._deadline - monotonic())

    def _take(self, fut: Future) -> list[dict]:
        shop = self._pending.pop(fut)
        try:
            return fut.result()
        except Exception as exc:
            if self.logger is not None:
                self.logger.error(
                    "event=shop_query_failed shop=%s card=%r class=%s detail=%s",
                    shop, self.card_name, type(exc).__name__, exc,
                )
            return []


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
    fanout = PriceFanout(card_name, fx, enabled=enabled, logger=logger)
    results = fanout.first(SHOP_QUERY_TIMEOUT_S)
    fanout.expire()
    if timeouts_out is not None:
        timeouts_out.update(fanout.timed_out)
    return results
