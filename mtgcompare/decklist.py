"""Decklist pricing domain logic.

Pulled out of ``web.py`` so the Flask layer is left with routing and SSE
plumbing only. Everything here is free of Flask request/response state:
the two pieces of context the orchestrator needs — the user's inventory
quantity map and the JPY/USD rate — are injected as callables by the web
layer, and the per-card price fetcher (``collect_prices``) plus the
logger are injected into the fan-out helpers. That keeps the module
import-light and unit-testable without spinning up the app.

Pipeline:

  parse_decklist          raw text → [(qty, name)]
  strip_basic_lands       drop basics (shops carry hundreds of printings)
  consolidate_decklist    sum duplicate lines, remember first-seen casing
  deduct_inventory        subtract owned copies → per-name "still needed"
  iter/fetch_decklist_prices   parallel per-card shop fan-out
  build_card_rows         project per-name state into template rows
  compute_shop_totals     aggregate per-shop + grand totals (with shipping)

``prepare_decklist_search`` ties parse → strip → size-check → consolidate
→ deduct → FX into a single validation step returning either a
``DecklistPrep`` (happy path) or a ``DecklistReject``.
"""
import logging
import os
import re
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from .scrapers.registry import SHIPPING_JPY, collect_prices

logger = logging.getLogger(__name__)


_DECK_LINE_RE = re.compile(
    r'^(\d+)x?\s+(.+?)(?:\s+\([A-Za-z0-9]+\)(?:\s+\d+[a-z]?)?)?\s*$'
)

# Hard cap on the total card count of a single decklist search. Sized to
# fit a full Commander deck (99 + commander = 100). Beyond this the
# parallel fan-out across cards × shops gets large enough to look like
# an attack to upstream sites and to lock up the worker pool.
MAX_DECKLIST_CARDS = 100

# Concurrency cap for the per-card fan-out in /decklist. The work is
# I/O-bound (each task triggers a parallel shop scrape), so the right
# number is "as many as we can dispatch without overloading the upstream
# shops or our own worker pool". 12 keeps an 8-shop × ~37-name search
# from queueing through more than ~3 batches, while leaving headroom
# under the gunicorn thread limits. Overridable via env var for tuning.
DECKLIST_FAN_OUT_WORKERS = int(os.environ.get("MTGCOMPARE_DECKLIST_FAN_OUT_WORKERS", "12"))

# Basic lands are excluded from price searches: shops return hundreds of
# near-identical printings (and Scryfall is by far the slowest of all
# queries on those), and nobody actually price-shops basics across stores.
_BASIC_LANDS = frozenset({
    "plains", "island", "swamp", "mountain", "forest", "wastes",
    "snow-covered plains", "snow-covered island", "snow-covered swamp",
    "snow-covered mountain", "snow-covered forest",
})


def is_basic_land(name: str) -> bool:
    return name.strip().lower() in _BASIC_LANDS


def strip_basic_lands(
    items: list[tuple[int, str]],
) -> tuple[list[tuple[int, str]], int]:
    """Drop basic-land entries and return (kept_items, skipped_copies)."""
    kept: list[tuple[int, str]] = []
    skipped_copies = 0
    for qty, name in items:
        if is_basic_land(name):
            skipped_copies += qty
        else:
            kept.append((qty, name))
    return kept, skipped_copies


def parse_decklist(text: str) -> list[tuple[int, str]]:
    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue
        if re.match(r'^(commander|sideboard|deck|maybeboard):?$', line, re.IGNORECASE):
            continue
        m = _DECK_LINE_RE.match(line)
        if m:
            qty = int(m.group(1))
            name = m.group(2).strip()
            if qty > 0 and name:
                result.append((qty, name))
    return result


def deduct_inventory(
    name_qty: dict[str, int],
    inv_map: dict[str, int],
) -> tuple[dict[str, int], dict[str, int]]:
    """Return (qty_from_inventory, qty_still_needed) per lowercase card key.

    Inventory quantities are capped at the requested amount — extra copies
    don't produce negative need.
    """
    name_inv_qty: dict[str, int] = {}
    name_needed: dict[str, int] = {}
    for key, wanted in name_qty.items():
        have = min(inv_map.get(key, 0), wanted)
        name_inv_qty[key] = have
        name_needed[key] = wanted - have
    return name_inv_qty, name_needed


def consolidate_decklist(
    card_items: list[tuple[int, str]],
) -> tuple[dict[str, int], dict[str, str]]:
    """Sum duplicate lines and remember the first-seen casing of each name."""
    name_qty: dict[str, int] = {}
    name_canonical: dict[str, str] = {}
    for qty, name in card_items:
        key = name.lower()
        name_qty[key] = name_qty.get(key, 0) + qty
        if key not in name_canonical:
            name_canonical[key] = name
    return name_qty, name_canonical


def iter_decklist_prices(
    names_to_search: list[str],
    name_canonical: dict[str, str],
    fx: float,
    enabled_shops: set[str] | None,
    timeouts_out: set[str] | None = None,
    *,
    collect: Callable[..., list[dict]] = collect_prices,
    logger: logging.Logger = logger,
) -> Iterator[tuple[str, list[dict]]]:
    """Stream ``(lower_name, sorted_rows)`` per card in fan-out
    completion order. Per-name failures yield ``(name, [])`` rather than
    aborting. ``timeouts_out``, if given, is mutated in place with the
    union of shops that hit the per-shop timeout.

    ``collect`` is the per-card shop fetcher (injected so the web layer
    can supply its monkeypatchable ``collect_prices`` reference); ``logger``
    is injected so log lines stay on the caller's logger namespace.
    """
    if not names_to_search:
        return
    shops_count = len(enabled_shops) if enabled_shops is not None else "all"
    workers = min(len(names_to_search), DECKLIST_FAN_OUT_WORKERS)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_name = {
            executor.submit(
                collect, name_canonical[n], fx,
                enabled=enabled_shops, logger=logger,
                timeouts_out=timeouts_out,
            ): n
            for n in names_to_search
        }
        for future in as_completed(future_to_name):
            n = future_to_name[future]
            try:
                rows = future.result()
            except Exception as exc:
                logger.error(
                    "event=price_fetch_failed card=%r decklist_size=%d shops_enabled=%s detail=%s",
                    name_canonical[n], len(names_to_search), shops_count, exc,
                )
                rows = []
            rows.sort(key=lambda r: r["price_jpy"])
            yield n, rows


def fetch_decklist_prices(
    names_to_search: list[str],
    name_canonical: dict[str, str],
    fx: float,
    enabled_shops: set[str] | None,
    timeouts_out: set[str] | None = None,
    *,
    collect: Callable[..., list[dict]] = collect_prices,
    logger: logging.Logger = logger,
) -> dict[str, list[dict]]:
    """Dict-returning wrapper around ``iter_decklist_prices``. Names
    with no matches still appear with an empty list.
    """
    prices_by_name: dict[str, list[dict]] = {n: [] for n in names_to_search}
    for n, rows in iter_decklist_prices(
        names_to_search, name_canonical, fx, enabled_shops, timeouts_out,
        collect=collect, logger=logger,
    ):
        prices_by_name[n] = rows
    return prices_by_name


def build_one_card_row(
    n: str,
    name_qty: dict[str, int],
    name_canonical: dict[str, str],
    name_inv_qty: dict[str, int],
    name_needed: dict[str, int],
    results: list[dict],
) -> dict:
    """Project a single name's state into the row shape the template expects."""
    qty_needed = name_needed[n]
    return {
        "name": name_canonical[n],
        "qty": name_qty[n],
        "qty_inventory": name_inv_qty[n],
        "qty_needed": qty_needed,
        "best": results[0] if (results and qty_needed > 0) else None,
        "all": results,
    }


def build_card_rows(
    name_qty: dict[str, int],
    name_canonical: dict[str, str],
    name_inv_qty: dict[str, int],
    name_needed: dict[str, int],
    prices_by_name: dict[str, list[dict]],
) -> list[dict]:
    """Project the per-name state into the row shape the template expects."""
    return [
        build_one_card_row(
            n, name_qty, name_canonical, name_inv_qty, name_needed,
            prices_by_name.get(n, []),
        )
        for n in sorted(name_qty, key=lambda x: name_canonical[x].lower())
    ]


def compute_shop_totals(
    card_rows: list[dict],
    shipping_overrides_jpy: dict[str, int],
    fx: float | None,
) -> tuple[list[dict], dict[str, float]]:
    """Aggregate per-shop totals and grand totals from already-built card rows.

    Returns ``(shop_list_sorted_by_total_desc, grand_totals)`` where
    ``grand_totals`` carries USD/JPY raw + with-shipping figures plus
    ``shipping_total_jpy`` for the template.
    """
    shop_totals: dict[str, dict] = {}
    grand_total_usd = 0.0
    grand_total_jpy = 0.0

    for row in card_rows:
        if row["best"] is None:
            continue
        shop = row["best"]["shop"]
        qty = row["qty_needed"]
        unit_usd = row["best"]["price_usd"]
        unit_jpy = row["best"]["price_jpy"]
        grand_total_usd += unit_usd * qty
        grand_total_jpy += unit_jpy * qty
        if shop not in shop_totals:
            ship_jpy = shipping_overrides_jpy.get(shop, SHIPPING_JPY.get(shop, 0))
            shop_totals[shop] = {
                "shop": shop,
                "unique_cards": 0,
                "total_copies": 0,
                "total_usd": 0.0,
                "total_jpy": 0.0,
                "shipping_jpy": ship_jpy,
                "shipping_usd": round(ship_jpy / fx, 2) if fx else 0.0,
            }
        shop_totals[shop]["unique_cards"] += 1
        shop_totals[shop]["total_copies"] += qty
        shop_totals[shop]["total_usd"] += unit_usd * qty
        shop_totals[shop]["total_jpy"] += unit_jpy * qty

    for s in shop_totals.values():
        s["total_usd_with_shipping"] = round(s["total_usd"] + s["shipping_usd"], 2)
        s["total_jpy_with_shipping"] = round(s["total_jpy"] + s["shipping_jpy"], 0)

    shop_list = sorted(shop_totals.values(), key=lambda s: -s["total_usd_with_shipping"])

    shipping_total_jpy = sum(s["shipping_jpy"] for s in shop_totals.values())
    shipping_total_usd = round(shipping_total_jpy / fx, 2) if fx else 0.0
    grand_totals = {
        "grand_total_usd": grand_total_usd,
        "grand_total_jpy": grand_total_jpy,
        "grand_total_usd_with_shipping": round(grand_total_usd + shipping_total_usd, 2),
        "grand_total_jpy_with_shipping": round(grand_total_jpy + shipping_total_jpy, 0),
        "shipping_total_jpy": shipping_total_jpy,
    }
    return shop_list, grand_totals


@dataclass(frozen=True)
class DecklistFormBasics:
    """Raw form fields shared by both /decklist code paths."""
    decklist_text: str
    shipping_overrides_jpy: dict[str, int]
    use_inventory: bool
    enabled_shops: set[str] | None


@dataclass
class DecklistPrep:
    """Output of `prepare_decklist_search` on the happy path."""
    decklist_text: str
    total_cards: int
    skipped_basics: int
    name_qty: dict[str, int]
    name_canonical: dict[str, str]
    name_inv_qty: dict[str, int]
    name_needed: dict[str, int]
    names_to_search: list[str]
    inventory_hits: int
    fx: float | None
    enabled_shops: set[str] | None
    shipping_overrides_jpy: dict[str, int]
    use_inventory: bool


@dataclass
class DecklistReject:
    """A validation-stage rejection. Callers translate to either an error
    page (sync endpoint) or a 400 JSON response (streaming endpoint)."""
    reason: str
    message: str


def prepare_decklist_search(
    basics: DecklistFormBasics,
    *,
    load_inv_map: Callable[[], dict[str, int]],
    get_fx: Callable[[], float | None],
) -> DecklistPrep | DecklistReject:
    """Parse / strip basics / consolidate / deduct inventory / fetch FX.
    Pure — never touches Flask response context.

    ``load_inv_map`` returns the ``{lower_card_name: qty}`` map for the
    current user (``{}`` when inventory deduction is off); ``get_fx``
    returns the JPY/USD rate or ``None``. Both are injected by the web
    layer so this stays free of Flask request/app state. They're only
    invoked after the cheap parse/size validation passes, preserving the
    original short-circuit ordering.
    """
    text = basics.decklist_text
    shipping_overrides_jpy = basics.shipping_overrides_jpy
    use_inventory = basics.use_inventory
    enabled_shops = basics.enabled_shops

    card_items = parse_decklist(text)
    if not card_items:
        return DecklistReject(
            reason="parse_empty",
            message="No cards parsed. Use format: '1 Card Name' or '4x Card Name (SET)'",
        )

    card_items, skipped_basics = strip_basic_lands(card_items)
    if not card_items:
        return DecklistReject(
            reason="only_basics",
            message=(
                "Decklist contains only basic lands, which aren't searched. "
                "Add non-basic cards and try again."
            ),
        )

    total_cards = sum(qty for qty, _ in card_items)
    if total_cards > MAX_DECKLIST_CARDS:
        return DecklistReject(
            reason="too_large",
            message=(
                f"Decklist is {total_cards} cards (after excluding basic lands) — "
                f"the limit is {MAX_DECKLIST_CARDS}. "
                "Trim it or split into multiple searches."
            ),
        )

    name_qty, name_canonical = consolidate_decklist(card_items)
    inv_map = load_inv_map()
    name_inv_qty, name_needed = deduct_inventory(name_qty, inv_map)
    names_to_search = [n for n in name_qty if name_needed[n] > 0]
    inventory_hits = sum(1 for n in name_qty if name_inv_qty[n] > 0)

    fx = get_fx()
    if fx is None and names_to_search:
        return DecklistReject(
            reason="fx_unavailable",
            message="Could not fetch FX rate; try again later.",
        )

    return DecklistPrep(
        decklist_text=text,
        total_cards=total_cards,
        skipped_basics=skipped_basics,
        name_qty=name_qty,
        name_canonical=name_canonical,
        name_inv_qty=name_inv_qty,
        name_needed=name_needed,
        names_to_search=names_to_search,
        inventory_hits=inventory_hits,
        fx=fx,
        enabled_shops=enabled_shops,
        shipping_overrides_jpy=shipping_overrides_jpy,
        use_inventory=use_inventory,
    )
