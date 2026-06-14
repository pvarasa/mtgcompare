"""/market render computation + price-history read views (the read side).

Owns the half of the pricing package that *reads* priced inventory for the
UI: the process-wide price/render caches, the per-row market-price + PnL
projection, the market summary aggregates, pagination, and the per-card /
whole-portfolio price-history series.

Free of Flask: the two web-layer values it needs — the FX rate and the
pagination choices — are injected into ``compute_market_ctx`` by the route.
Depends only on the data layer and the shared ``common`` core (never on
``import_service``).
"""
import logging
import math
import os
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from threading import Lock
from time import monotonic

from cachetools import TTLCache

from .. import db
from .. import inventory as inv
from . import market_repo, meta
from .common import (
    has_price_history,
    is_foil,
    load_card_map_for_inventory,
    normalize_set_code,
    portfolio_value_series,
    row_key_for_mapping,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MARKET_HISTORY_PERIODS",
    "MKT_SORT_CHOICES",
    "attach_market_prices",
    "attach_pnl_in_place",
    "build_market_summary",
    "compute_market_ctx",
    "compute_portfolio_history",
    "densify_daily_points",
    "format_ago",
    "get_price_cache",
    "history_cutoff",
    "market_cache_clear",
    "market_cache_get",
    "market_cache_set",
    "paginate_market_rows",
    "sort_key_market",
]

MARKET_HISTORY_PERIODS = {
    "1w": 7,
    "1m": 30,
    "3m": 90,
    "6m": 180,
    "all": None,
}

MKT_SORT_CHOICES = (
    "card_name", "set_code", "quantity", "price_bought",
    "market_price_usd", "market_value_jpy", "pnl_usd", "pnl_pct",
)


# --- small date helpers ----------------------------------------------------

def format_ago(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        dt  = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        sec = int((datetime.now(UTC) - dt).total_seconds())
        if sec <    60: return "just now"
        if sec <  3600: return f"{sec // 60} min ago"
        if sec < 86400: return f"{sec // 3600} hr ago"
        return f"{sec // 86400} days ago"
    except Exception:
        return iso


def history_cutoff(period: str, *, now: datetime | None = None) -> datetime | None:
    days = MARKET_HISTORY_PERIODS.get(period)
    if days is None:
        return None
    anchor = now or datetime.now(UTC)
    return anchor - timedelta(days=days)


# --- /market render computation + caches -----------------------------------

# Cache the heavy per-request /market computation. Cleared after
# price-update runs so users don't see stale PnL.
_MARKET_CACHE_TTL = int(os.environ.get("MARKET_CACHE_TTL", "60"))
_MARKET_CACHE_ENABLED = _MARKET_CACHE_TTL > 0
_market_data_cache: TTLCache = TTLCache(  # keys = tuples, values = template ctx dicts
    maxsize=512,
    ttl=_MARKET_CACHE_TTL if _MARKET_CACHE_ENABLED else 1,  # ttl=0 not allowed by cachetools
)
_market_data_cache_lock = Lock()

# Process-wide price cache; invalidated by market_cache_clear() after a
# price import. 1-hour soft TTL is a safety net for missed invalidations.
_PRICE_CACHE_MAX_AGE_S = 3600
_price_cache_state: dict = {
    "dict": None,                    # {(card_name_lower, set_code_lower, is_foil): price_usd}
    "last_fetched_at": None,
    "mtgjson_downloaded_at": None,
    "built_at_mono": 0.0,
}
_price_cache_lock = Lock()


def market_cache_get(key):
    if not _MARKET_CACHE_ENABLED:
        return None
    with _market_data_cache_lock:
        return _market_data_cache.get(key)


def market_cache_set(key, value):
    if not _MARKET_CACHE_ENABLED:
        return
    with _market_data_cache_lock:
        _market_data_cache[key] = value


def market_cache_clear() -> None:
    """Flush the /market computation cache + the in-memory price dict.

    Called by the price-update cron once new prices land so users don't
    keep seeing stale PnL for up to a TTL, and so the next /market
    request triggers a fresh `SELECT * FROM market_prices` rebuild.
    """
    with _market_data_cache_lock:
        _market_data_cache.clear()
    with _price_cache_lock:
        _price_cache_state["dict"] = None
        _price_cache_state["last_fetched_at"] = None
        _price_cache_state["mtgjson_downloaded_at"] = None
        _price_cache_state["built_at_mono"] = 0.0


def get_price_cache() -> tuple[dict, str | None, str | None]:
    """Return (price_dict, last_fetched_at, mtgjson_downloaded_at).

    Lazily built per worker process on first /market request after a boot
    or cache clear. The price dict maps
    `(card_name_lower, set_code_lower, is_foil) -> price_usd`. Two
    threads can race to rebuild on expiry — that's benign (last writer
    wins, same data either way), but we don't hold the lock during the
    DB query so reader latency isn't gated on the rebuild.
    """
    now = monotonic()
    with _price_cache_lock:
        snap = _price_cache_state
        if snap["dict"] is not None and now - snap["built_at_mono"] < _PRICE_CACHE_MAX_AGE_S:
            return snap["dict"], snap["last_fetched_at"], snap["mtgjson_downloaded_at"]

    # Build outside the lock — readers concurrently can still serve from
    # a stale snapshot via the early return above until we publish.
    with db.get_conn() as conn:
        cache_rows = market_repo.load_market_prices(conn)
        mtgjson_downloaded_at = meta.read(conn, "mtgjson_history_downloaded_at")

    price_dict: dict[tuple, float | None] = {}
    last_fetched_at: str | None = None
    for cr in cache_rows:
        key = (cr["card_name"].lower(), cr["set_code"].lower(), cr["is_foil"])
        price_dict[key] = cr["price_usd"]
        if last_fetched_at is None or cr["fetched_at"] > last_fetched_at:
            last_fetched_at = cr["fetched_at"]

    with _price_cache_lock:
        _price_cache_state["dict"] = price_dict
        _price_cache_state["last_fetched_at"] = last_fetched_at
        _price_cache_state["mtgjson_downloaded_at"] = mtgjson_downloaded_at
        _price_cache_state["built_at_mono"] = monotonic()
    return price_dict, last_fetched_at, mtgjson_downloaded_at


def sort_key_market(col: str, descending: bool):
    """Return a `key=` callable for sorting the priced rows list.

    Nulls always sort to the end — clicking "sort by PnL desc" should put
    rows with PnL=null at the bottom, not above the best-performing ones.
    """
    def key(row):
        v = row.get(col)
        # First tuple element pushes nulls to the bottom in both orders;
        # second element is the sort value with sign-flip for descending.
        if v is None:
            return (1, 0)
        v_cmp = v.lower() if isinstance(v, str) else (-float(v) if descending else float(v))
        return (0, v_cmp)
    return key


def attach_market_prices(
    inventory_rows: list[dict],
    price_cache: dict,
    fx: float | None,
    has_cache: bool,
) -> list[dict]:
    priced = []
    for row in inventory_rows:
        is_foil_int = int(is_foil(row.get("printing")))
        key = (row["card_name"].lower(), normalize_set_code(row["set_code"]), is_foil_int)
        price_usd = price_cache.get(key) if has_cache else None
        priced.append({
            **row,
            "market_price_usd": price_usd,
            "market_price_jpy": round(price_usd * fx) if (price_usd is not None and fx) else None,
        })
    return priced


def attach_pnl_in_place(priced: list[dict]) -> None:
    for row in priced:
        pb  = row.get("price_bought")
        mp  = row.get("market_price_usd")
        qty = row["quantity"]
        row["cost_basis_usd"]   = round(pb * qty, 2) if pb is not None else None
        row["market_value_usd"] = round(mp * qty, 2) if mp is not None else None
        row["market_value_jpy"] = round(row["market_price_jpy"] * qty) if row["market_price_jpy"] is not None else None
        if pb is not None and mp is not None:
            row["pnl_usd"] = round((mp - pb) * qty, 2)
            row["pnl_pct"] = round((mp / pb - 1) * 100, 1) if pb > 0 else 0.0
        else:
            row["pnl_usd"] = None
            row["pnl_pct"] = None


def build_market_summary(priced: list[dict]) -> dict:
    # Aggregates run across the WHOLE filtered set (not just the current
    # page), because that's what the user expects "Cost basis $X" to mean
    # in the summary header.
    pnl_rows    = [r for r in priced if r["pnl_usd"]          is not None]
    cost_rows   = [r for r in priced if r["cost_basis_usd"]   is not None]
    market_rows = [r for r in priced if r["market_value_usd"] is not None]

    total_cost       = sum(r["cost_basis_usd"]   for r in cost_rows)
    total_pnl        = sum(r["pnl_usd"]          for r in pnl_rows)
    total_market     = sum(r["market_value_usd"]  for r in market_rows)
    total_market_jpy = sum(r["market_value_jpy"]  for r in market_rows if r["market_value_jpy"] is not None)

    return {
        "total_cost_usd":   round(total_cost,   2),
        "total_pnl_usd":    round(total_pnl,    2),
        "pnl_pct":          round(total_pnl / total_cost * 100, 1) if total_cost > 0 else None,
        "total_market_usd": round(total_market, 2),
        "total_market_jpy": round(total_market_jpy),
        "lots_total":       len(priced),
        "lots_no_cost":     len(priced) - len(cost_rows),
        "lots_no_market":   len(priced) - len(market_rows),
        "lots_in_pnl":      len(pnl_rows),
    }


def densify_daily_points(
    price_points: dict[str, float],
    *,
    start_day: date | None = None,
    end_day: date | None = None,
) -> list[dict]:
    if not price_points:
        return []
    normalized = {
        datetime.fromisoformat(stamp).date(): value
        for stamp, value in price_points.items()
    }
    lo = start_day or min(normalized)
    hi = end_day or max(normalized)
    points: list[dict] = []
    current = lo
    while current <= hi:
        value = normalized.get(current)
        points.append({
            "market_date": current.isoformat(),
            "price_usd": value,
        })
        current += timedelta(days=1)
    return points


def compute_portfolio_history(
    inventory_rows: list[dict],
    *,
    card_map: dict[tuple[str, str, str, int], str] | None = None,
    value_series: Callable[[list[tuple[str, str, int]]], dict[str, float]] = portfolio_value_series,
) -> dict:
    """Whole-portfolio USD value across MTGJSON price history.

    A deliberate simplification: the *current* inventory (current lots and
    quantities) is valued at every day MTGJSON has a price, ignoring when
    each lot was actually bought or sold.

    The heavy lifting is pushed into the database: lots are collapsed to a
    ``(uuid, finish, quantity)`` weight list and handed to ``value_series``,
    which returns one pre-summed total per priced day — so a 5000-lot
    portfolio is one ``GROUP BY`` query, not millions of price rows pulled
    into Python. ``densify_daily_points`` then fills calendar gaps with
    ``None`` (no forward-fill: a day with no priced holding is blank, like
    the per-card chart). The payload mirrors the per-card
    ``/market/history`` shape — ``points`` is ``[{market_date, price_usd}]``
    where ``price_usd`` is the whole-portfolio value — so the same renderer
    draws it. ``card_map``/``value_series`` are injectable for tests.
    """
    if card_map is None:
        card_map = load_card_map_for_inventory(inventory_rows)

    # Collapse lots to unique (uuid, finish), weighted by total quantity, so
    # one printing held across several lots is valued once.
    qty_by_series: dict[tuple[str, str], int] = defaultdict(int)
    mapped_count = 0
    for row in inventory_rows:
        qty = row.get("quantity") or 0
        if qty <= 0:
            continue
        uuid = card_map.get(row_key_for_mapping(row))
        if not uuid:
            continue
        finish = "foil" if is_foil(row.get("printing")) else "normal"
        qty_by_series[(uuid, finish)] += qty
        mapped_count += 1

    empty = {
        "points": [],
        "lot_count": len(inventory_rows),
        "mapped_count": mapped_count,
        "has_history": False,
        "available_since": None,
    }
    if not qty_by_series:
        return empty

    weights = [(uuid, finish, qty) for (uuid, finish), qty in qty_by_series.items()]
    totals = value_series(weights)  # {date-string: total_usd}, one row per priced day
    if not totals:
        return empty

    points = densify_daily_points(totals)
    return {
        "points": points,
        "lot_count": len(inventory_rows),
        "mapped_count": mapped_count,
        "has_history": True,
        # densify spans [min priced day, max priced day]; the first point is
        # always priced, so it marks when coverage begins.
        "available_since": points[0]["market_date"],
    }


def paginate_market_rows(priced: list[dict], params: dict) -> tuple[list[dict], int, int]:
    # Clamps params['page'] in place so a stale page param (e.g. user
    # changes filter while on page 7) lands on the last available page
    # instead of an empty render.
    total = len(priced)
    total_pages = max(1, math.ceil(total / params["per_page"])) if total else 1
    if params["page"] > total_pages:
        params["page"] = total_pages
    start = (params["page"] - 1) * params["per_page"]
    page_rows = priced[start:start + params["per_page"]]
    return page_rows, total, total_pages


def compute_market_ctx(
    user_id: str,
    params: dict,
    *,
    get_fx: Callable[[], float | None],
    per_page_choices: tuple,
) -> dict:
    """All the expensive /market computation, factored out so the route
    handler can cache the result.

    ``get_fx`` and ``per_page_choices`` are injected by the web layer (the
    FX rate is a process-cached value that logs through the app logger;
    the pagination choices are a UI/route constant).

    Returns the full template context dict (whichever branch applied —
    empty inventory, no-price-cache, or full). The handler picks the
    template based on `partial=tbody` and renders.
    """
    inventory_rows = inv.list_filtered_for_market(
        user_id,
        q=params["q"] or None,
        price_mode=params["price_mode"],
        price_value=params["price_value"],
    )

    # Process-wide cached price dict. The lazy rebuild on first call /
    # invalidation handles freshness; on the hot path this is an in-RAM
    # dict lookup, not a `SELECT * FROM market_prices`.
    price_cache, last_fetched_at, mtgjson_downloaded_at = get_price_cache()
    has_cache = bool(price_cache)
    history_db_exists = has_price_history()

    common = {
        "last_refreshed": format_ago(last_fetched_at),
        "mtgjson_last_downloaded": format_ago(mtgjson_downloaded_at) if history_db_exists else None,
        "history_db_exists": history_db_exists,
        "allow_price_update": not db.IS_POSTGRES,
        "active": "market",
        "params": params,
        "per_page_choices": per_page_choices,
    }

    if not inventory_rows:
        return {
            "rows": [], "summary": None, "fx": None, "error": None,
            "has_cache": has_cache,
            "total": 0, "total_pages": 1,
            **common,
        }

    fx = get_fx() if has_cache else None
    priced = attach_market_prices(inventory_rows, price_cache, fx, has_cache)

    if not has_cache:
        return {
            "rows": priced, "summary": None, "fx": None, "error": None,
            "has_cache": False,
            "total": len(priced), "total_pages": 1,
            **common,
            "last_refreshed": None,
        }

    attach_pnl_in_place(priced)
    priced.sort(key=sort_key_market(params["sort"], params["direction"] == "desc"))
    summary = build_market_summary(priced)
    page_rows, total, total_pages = paginate_market_rows(priced, params)

    return {
        "rows": page_rows,
        "summary": summary,
        "fx": fx,
        "error": None,
        "has_cache": True,
        "total": total,
        "total_pages": total_pages,
        **common,
    }
