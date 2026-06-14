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

``produce_decklist_events`` (bottom of the file) drives the same pipeline for
the streaming ``/decklist/stream`` route: it runs the fan-out and pushes
``(event_type, payload)`` tuples onto a queue as rows/timeouts/totals arrive.
The web layer owns only the SSE transport; this owns the event production.
"""
import logging
import os
import queue
import re
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from time import monotonic

from .scrapers.registry import (
    ACTIVE_SHOPS,
    MARKETPLACE_SHOPS,
    SHIPPING_JPY,
    SHOP_FLAGS,
    collect_prices,
)

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


def effective_search_shops(enabled_shops: set[str] | None) -> set[str]:
    """Shops a decklist fan-out actually queries.

    Marketplace shops are dropped even when the form selected them: their
    per-card landed prices (item + that seller's shipping) don't sum to
    an order total. ``None`` means the default "all shops on" search.
    """
    base = set(ACTIVE_SHOPS) if enabled_shops is None else enabled_shops
    return base - MARKETPLACE_SHOPS


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
    enabled_shops = effective_search_shops(enabled_shops)
    shops_count = len(enabled_shops)
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


# --- streaming (SSE) event production --------------------------------------
#
# The /decklist/stream route runs a single-request Server-Sent-Events search.
# The Flask layer owns the transport (the text/event-stream Response, the
# keepalive comments, the per-user in-flight cap); this is the domain side.
# `produce_decklist_events` runs the same parse→deduct→fan-out as the
# synchronous handler but pushes (event_type, payload) tuples onto a queue as
# each card row, shop timeout, and debounced running total arrives — the
# route drains the queue and frames each tuple as an SSE event. Web-layer
# state is injected (the pre-loaded Jinja row template, the collect_prices
# reference, the logger) so this stays free of Flask app/request context.

_TOTALS_DEBOUNCE_S = 0.5


def _emit_meta(prep: DecklistPrep, q: queue.Queue) -> None:
    q.put(("meta", {
        "total_cards": prep.total_cards,
        "skipped_basics": prep.skipped_basics,
        "distinct_names": len(prep.name_qty),
        "inventory_hits": prep.inventory_hits,
        "names_to_search": len(prep.names_to_search),
        "use_inventory": prep.use_inventory,
        "fx": prep.fx,
        "shop_filter_active": prep.enabled_shops is not None,
    }))


def _emit_card_row(
    name: str,
    prep: DecklistPrep,
    rows: list[dict],
    row_template,
    q: queue.Queue,
) -> None:
    # Pre-render the <tr> server-side so the client can just innerHTML-append.
    # render() runs in the producer thread (no Flask app context), but the
    # Jinja env is process-global and thread-safe to read from — template
    # loading + render takes no Flask state.
    row = build_one_card_row(
        name, prep.name_qty, prep.name_canonical,
        prep.name_inv_qty, prep.name_needed, rows,
    )
    row_html = row_template.render(
        row=row,
        use_inventory=prep.use_inventory,
        shop_flags=SHOP_FLAGS,
    )
    q.put(("row", {
        "key": name,
        "html": row_html,
        "qty_needed": row["qty_needed"],
        "has_best": row["best"] is not None,
    }))


def _emit_totals(
    prep: DecklistPrep,
    prices_by_name: dict[str, list[dict]],
    q: queue.Queue,
) -> list[dict]:
    """Snapshot card_rows + shop_totals and enqueue one ``totals`` event.

    Returns the freshly-built card_rows so the caller can reuse them for
    final logging without re-running ``build_card_rows`` twice.
    """
    card_rows = build_card_rows(
        prep.name_qty, prep.name_canonical, prep.name_inv_qty,
        prep.name_needed, prices_by_name,
    )
    shop_list, totals = compute_shop_totals(
        card_rows, prep.shipping_overrides_jpy, prep.fx,
    )
    q.put(("totals", {"shop_list": shop_list, **totals}))
    return card_rows


def _emit_inventory_only_rows(
    prep: DecklistPrep, row_template, q: queue.Queue,
) -> None:
    # Inventory-covered cards never enter the fan-out (qty_needed is 0) so the
    # streamed table would otherwise drop them silently, while the synchronous
    # /decklist path shows them as "✓ in inventory" rows. Emit in canonical
    # alphabetical order so the inventory section is stable from first paint.
    searched_set = set(prep.names_to_search)
    inventory_only = sorted(
        (n for n in prep.name_qty if n not in searched_set),
        key=lambda x: prep.name_canonical[x].lower(),
    )
    for name in inventory_only:
        _emit_card_row(name, prep, [], row_template, q)


def _run_fanout(
    prep: DecklistPrep,
    prices_by_name: dict[str, list[dict]],
    timed_out: set[str],
    row_template,
    q: queue.Queue,
    *,
    collect: Callable[..., list[dict]],
    logger: logging.Logger,
) -> None:
    """Drive the per-card fan-out, emitting row + shop_timeout + debounced
    totals events as results stream in."""
    if prep.fx is None:
        return
    timed_out_emitted: set[str] = set()
    last_totals_emit = 0.0
    for name, rows in iter_decklist_prices(
        prep.names_to_search, prep.name_canonical, prep.fx, prep.enabled_shops,
        timeouts_out=timed_out, collect=collect, logger=logger,
    ):
        prices_by_name[name] = rows
        _emit_card_row(name, prep, rows, row_template, q)

        for shop in sorted(timed_out - timed_out_emitted):
            q.put(("shop_timeout", {"shop": shop}))
            timed_out_emitted.add(shop)

        now = monotonic()
        if now - last_totals_emit > _TOTALS_DEBOUNCE_S:
            _emit_totals(prep, prices_by_name, q)
            last_totals_emit = now


def produce_decklist_events(
    prep: DecklistPrep,
    q: queue.Queue,
    *,
    row_template,
    collect: Callable[..., list[dict]] = collect_prices,
    logger: logging.Logger = logger,
) -> None:
    """Run the decklist fan-out and push (event_type, payload) tuples to
    ``q``. Terminal sentinel is ``None``. Runs in a daemon thread driven by
    the SSE response generator in the web layer.

    Mirrors the synchronous /decklist handler's behavior but yields each card
    row, each shop timeout, and debounced running totals as they arrive
    instead of bundling them into one rendered page. ``row_template`` is the
    pre-loaded Jinja ``_decklist_row.html`` template; ``collect`` and
    ``logger`` are injected by the web layer.
    """
    t0 = monotonic()
    prices_by_name: dict[str, list[dict]] = {n: [] for n in prep.name_qty}
    timed_out: set[str] = set()
    try:
        _emit_meta(prep, q)
        _emit_inventory_only_rows(prep, row_template, q)
        _run_fanout(
            prep, prices_by_name, timed_out, row_template, q,
            collect=collect, logger=logger,
        )

        card_rows = _emit_totals(prep, prices_by_name, q)
        rows_with_match = sum(1 for r in card_rows if r["best"] is not None)
        duration_ms = int((monotonic() - t0) * 1000)
        q.put(("done", {
            "duration_ms": duration_ms,
            "rows_with_match": rows_with_match,
            "timed_out_shops": sorted(timed_out),
        }))
        logger.info(
            "event=decklist_search status=ok size=%d distinct_names=%d "
            "names_searched=%d inventory_hits=%d shops_enabled=%s use_inventory=%d "
            "rows_with_match=%d skipped_basics=%d timed_out_shops=%s "
            "transport=sse duration_ms=%d",
            prep.total_cards, len(prep.name_qty), len(prep.names_to_search), prep.inventory_hits,
            len(effective_search_shops(prep.enabled_shops)),
            int(prep.use_inventory), rows_with_match, prep.skipped_basics,
            ",".join(sorted(timed_out)) or "none",
            duration_ms,
        )
    except Exception:
        logger.exception("event=decklist_search_stream_failed")
        q.put(("error", {"message": "Internal error during search."}))
    finally:
        q.put(None)
