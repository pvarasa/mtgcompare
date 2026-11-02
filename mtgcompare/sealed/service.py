"""Sealed-product orchestration and /sealed page computation (Flask-free).

Write side:
- ``refresh_catalog``  — reload the catalog from MTGJSON SetList.json when
  it is empty or older than ``CATALOG_MAX_AGE``.
- ``refresh_prices``   — for every tracked product (or the given ones):
  backfill a year of TCGPlayer history the first time, then record today's
  market price. Per-product failures are logged and counted, never fatal.
- ``run_daily_update`` — both of the above; called by the daily cron.

Read side: ``compute_sealed_ctx`` (holdings + watchlist + summary) and
``product_history`` (chart payload).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from threading import Lock

from .. import db
from ..pricing import meta
from . import catalog, repo
from . import tcgplayer as tcg

logger = logging.getLogger(__name__)

CATALOG_META_KEY = "sealed_catalog_updated_at"
CATALOG_MAX_AGE = timedelta(days=7)
LANGUAGES = ("EN", "JP")
CURRENCIES = ("JPY", "USD")
CATEGORY_LABELS = {"booster_box": "Box", "booster_pack": "Pack"}
CHANGE_WINDOW_DAYS = 30
# Spacing between products during a refresh: a few hundred products a day
# is nowhere near crawling, but there is no reason to burst them either.
_REQUEST_PACING_S = 0.3

_refresh_lock = Lock()

ProgressFn = Callable[[int, str, str], None]


def _noop_progress(_p: int, _phase: str, _detail: str) -> None:
    pass


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- catalog -----------------------------------------------------------------

def catalog_is_stale(now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    with db.get_conn() as conn:
        if repo.count_products(conn) == 0:
            return True
        stamp = meta.read(conn, CATALOG_META_KEY)
    if not stamp:
        return True
    try:
        return now - datetime.fromisoformat(stamp) > CATALOG_MAX_AGE
    except ValueError:
        return True


def refresh_catalog(
    *, force: bool = False, fetch: Callable[[], dict] = catalog.fetch_setlist,
) -> int | None:
    """Reload the catalog if stale (or forced). Returns rows written, or
    None when the existing catalog was fresh enough to keep."""
    if not force and not catalog_is_stale():
        return None
    updated_at = _now_iso()
    rows = catalog.parse_setlist(fetch(), updated_at=updated_at)
    with db.get_conn() as conn:
        repo.upsert_products(conn, rows)
        meta.write(conn, CATALOG_META_KEY, updated_at)
    logger.info("event=sealed_catalog_refreshed products=%d", len(rows))
    return len(rows)


# --- prices ------------------------------------------------------------------

def refresh_prices(
    uuids: list[str] | None = None,
    *,
    client: tcg.TcgSealedClient | None = None,
    today: date | None = None,
    progress: ProgressFn = _noop_progress,
    pacing_s: float = _REQUEST_PACING_S,
) -> dict:
    """Record today's TCGPlayer price for the given (default: all tracked)
    products, backfilling history for products with fewer than two points.

    Returns ``{"products", "priced", "backfilled", "failed"}`` counts.
    """
    client = client or tcg.TcgSealedClient()
    today = today or datetime.now(UTC).date()
    with _refresh_lock:
        with db.get_conn() as conn:
            if uuids is None:
                products = repo.tracked_products(conn)
            else:
                products = [p for p in (repo.get_product(conn, u) for u in uuids) if p]
            products = [p for p in products if p.get("tcgplayer_product_id")]
            counts = repo.price_counts(conn, [p["uuid"] for p in products], tcg.SOURCE)

        stats = {"products": len(products), "priced": 0, "backfilled": 0, "failed": 0}
        for i, product in enumerate(products):
            progress(
                int(100 * i / max(len(products), 1)), "Fetching prices",
                f"{product['name']} ({i + 1}/{len(products)})",
            )
            if i and pacing_s:
                time.sleep(pacing_s)
            try:
                points = _fetch_product_points(
                    client, product, backfill=counts.get(product["uuid"], 0) < 2, today=today,
                )
            except Exception as exc:
                stats["failed"] += 1
                logger.warning(
                    "event=sealed_price_fetch_failed uuid=%s product_id=%s class=%s err=%s",
                    product["uuid"], product["tcgplayer_product_id"], type(exc).__name__, exc,
                )
                continue
            if not points:
                continue
            with db.get_conn() as conn:
                repo.upsert_prices(conn, [
                    {"product_uuid": product["uuid"], "source": tcg.SOURCE, "language": "EN",
                     "market_date": d, "price": round(p, 2), "currency": "USD"}
                    for d, p in points.items()
                ])
            if today in points:
                stats["priced"] += 1
            if len(points) > 1:
                stats["backfilled"] += 1
        progress(100, "Done", f"Priced {stats['priced']} of {stats['products']} product(s).")
    logger.info("event=sealed_prices_refreshed %s", " ".join(f"{k}={v}" for k, v in stats.items()))
    return stats


def _fetch_product_points(
    client: tcg.TcgSealedClient, product: dict, *, backfill: bool, today: date,
) -> dict[date, float]:
    pid = int(product["tcgplayer_product_id"])
    points = client.history(pid) if backfill else {}
    current = client.market_price(pid)
    if current is not None:
        points[today] = current
    return points


def run_daily_update(progress_cb: ProgressFn | None = None) -> dict:
    progress = progress_cb or _noop_progress
    progress(2, "Catalog", "Checking the sealed product catalog...")
    refresh_catalog()
    return refresh_prices(progress=progress)


# --- add / remove ------------------------------------------------------------

def resolve_product(name: str) -> dict | None:
    with db.get_conn() as conn:
        return repo.find_product_by_name(conn, name)


def ensure_priced(product: dict) -> None:
    """Fetch prices right away for a product nobody tracked before, so a
    fresh add shows a value instead of waiting for tomorrow's cron.
    Best-effort: a TCGPlayer outage only delays the price."""
    with db.get_conn() as conn:
        has_prices = repo.price_counts(conn, [product["uuid"]], tcg.SOURCE)
    if has_prices or not product.get("tcgplayer_product_id"):
        return
    try:
        refresh_prices([product["uuid"]])
    except Exception:
        logger.exception("event=sealed_initial_price_failed uuid=%s", product["uuid"])


# --- read side ---------------------------------------------------------------

def _to_usd(amount: float | None, currency: str, fx: float | None) -> float | None:
    if amount is None:
        return None
    if currency == "USD":
        return amount
    if currency == "JPY" and fx:
        return amount / fx
    return None


def _product_label(p: dict) -> dict:
    return {
        **p,
        "kind": CATEGORY_LABELS.get(p.get("category") or "", p.get("category") or ""),
        "tcgplayer_url": (
            tcg.PRODUCT_URL.format(product_id=p["tcgplayer_product_id"])
            if p.get("tcgplayer_product_id") else None
        ),
    }


def _attach_market(rows: list[dict], latest: dict, past: dict, fx: float | None) -> None:
    for r in rows:
        cur = latest.get(r["uuid"])
        old = past.get(r["uuid"])
        r["market_usd"] = cur["price"] if cur else None
        r["market_date"] = cur["market_date"] if cur else None
        r["market_jpy"] = round(cur["price"] * fx) if cur and fx else None
        r["change_pct"] = (
            round((cur["price"] / old["price"] - 1) * 100, 1)
            if cur and old and old["price"] and old["market_date"] != cur["market_date"] else None
        )


def compute_sealed_ctx(user_id: str, *, fx: float | None, today: date | None = None) -> dict:
    today = today or datetime.now(UTC).date()
    with db.get_conn() as conn:
        lots = [_product_label(r) for r in repo.list_lots(conn, user_id)]
        watch = [_product_label(r) for r in repo.list_watch(conn, user_id)]
        catalog_rows = repo.list_catalog(conn)
        catalog_stamp = meta.read(conn, CATALOG_META_KEY)
        uuids = list({r["uuid"] for r in lots + watch})
        latest = repo.latest_prices(conn, uuids, tcg.SOURCE)
        past = repo.latest_prices(
            conn, uuids, tcg.SOURCE,
            on_or_before=(today - timedelta(days=CHANGE_WINDOW_DAYS)).isoformat(),
        )

    _attach_market(lots, latest, past, fx)
    _attach_market(watch, latest, past, fx)
    for lot in lots:
        qty = lot["quantity"]
        cost_unit = _to_usd(lot["price_bought"], lot["currency"], fx)
        lot["cost_usd"] = round(cost_unit * qty, 2) if cost_unit is not None else None
        lot["value_usd"] = round(lot["market_usd"] * qty, 2) if lot["market_usd"] is not None else None
        if lot["cost_usd"] is not None and lot["value_usd"] is not None:
            lot["pnl_usd"] = round(lot["value_usd"] - lot["cost_usd"], 2)
            lot["pnl_pct"] = round((lot["value_usd"] / lot["cost_usd"] - 1) * 100, 1) if lot["cost_usd"] > 0 else None
        else:
            lot["pnl_usd"] = lot["pnl_pct"] = None
        # TCGPlayer only prices English product; a JP lot is valued at the
        # EN reference until a JP source exists. Flag it in the UI.
        lot["reference_mismatch"] = lot["language"] != "EN"

    return {
        "lots": lots,
        "watch": watch,
        "summary": build_summary(lots, fx),
        "catalog": [_product_label(r) for r in catalog_rows],
        "catalog_updated_at": catalog_stamp,
        "last_price_date": max((r["market_date"] for r in lots + watch if r["market_date"]), default=None),
        "fx": fx,
        "languages": LANGUAGES,
        "currencies": CURRENCIES,
        "today": today.isoformat(),
    }


def build_summary(lots: list[dict], fx: float | None) -> dict:
    valued = [r for r in lots if r["value_usd"] is not None]
    pnl_rows = [r for r in lots if r["pnl_usd"] is not None]
    total_value = sum(r["value_usd"] for r in valued)
    total_cost = sum(r["cost_usd"] for r in pnl_rows)
    total_pnl = sum(r["pnl_usd"] for r in pnl_rows)
    return {
        "lots_total": len(lots),
        "units_total": sum(r["quantity"] for r in lots),
        "lots_no_market": len(lots) - len(valued),
        "lots_no_cost": sum(1 for r in lots if r["cost_usd"] is None),
        "lots_in_pnl": len(pnl_rows),
        "total_market_usd": round(total_value, 2),
        "total_market_jpy": round(total_value * fx) if fx else None,
        "total_cost_usd": round(total_cost, 2),
        "total_pnl_usd": round(total_pnl, 2),
        "pnl_pct": round(total_pnl / total_cost * 100, 1) if total_cost > 0 else None,
    }


def product_history(uuid: str) -> dict | None:
    with db.get_conn() as conn:
        product = repo.get_product(conn, uuid)
        if not product:
            return None
        points = repo.price_history(conn, uuid, tcg.SOURCE)
    return {
        "product": _product_label(product),
        "points": [{"market_date": p["market_date"], "price_usd": p["price"]} for p in points],
    }
