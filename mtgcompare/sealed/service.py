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

import json
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from threading import Lock

from .. import db
from ..pricing import meta
from . import catalog, jp_shops, repo
from . import tcgplayer as tcg

logger = logging.getLogger(__name__)

CATALOG_META_KEY = "sealed_catalog_updated_at"
CATALOG_MAX_AGE = timedelta(days=7)
LANGUAGES = ("EN", "JP")
CURRENCIES = ("JPY", "USD")
CATEGORY_LABELS = {"booster_box": "Box", "booster_pack": "Pack"}
CHANGE_WINDOW_DAYS = 30
SHOP_SOURCES = (jp_shops.HARERUYA, jp_shops.SINGLESTAR)
# A shop offer older than this is treated as sold out (shops only record
# in-stock offers, so the last point outlives the stock).
SHOP_FRESH_DAYS = 7
SHOP_PRICES_META_KEY = "sealed_shop_prices_updated_at"
SET_NAMES_META_KEY = "sealed_jp_set_names"
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


ShopCrawler = Callable[[], list[jp_shops.ShopListing]]
DEFAULT_CRAWLERS: dict[str, ShopCrawler] = {
    # Hareruya first: its titles teach the set-name map SingleStar needs.
    jp_shops.HARERUYA: jp_shops.crawl_hareruya,
    jp_shops.SINGLESTAR: jp_shops.crawl_singlestar,
}


def refresh_shop_prices(
    *,
    crawlers: dict[str, ShopCrawler] | None = None,
    today: date | None = None,
    progress: ProgressFn = _noop_progress,
) -> dict[str, dict]:
    """Crawl each Japanese shop's sealed listing and record today's cheapest
    in-stock offer per (product, language) for every catalog product found.

    Every matched product is recorded, not just tracked ones, so a product
    added later already has shop history. A failing shop is logged and
    skipped; the others still run.
    """
    crawlers = crawlers if crawlers is not None else DEFAULT_CRAWLERS
    today = today or datetime.now(UTC).date()
    with db.get_conn() as conn:
        catalog_by_set: dict[str, list[dict]] = defaultdict(list)
        for p in repo.list_catalog(conn):
            catalog_by_set[p["set_code"]].append(p)
        stored_names = meta.read(conn, SET_NAMES_META_KEY)
    # The set-name map persists so SingleStar still resolves on a day
    # Hareruya's crawl fails.
    set_names: dict[str, str] = json.loads(stored_names) if stored_names else {}

    results: dict[str, dict] = {}
    for i, (shop, crawl) in enumerate(crawlers.items()):
        label = jp_shops.SHOP_LABELS.get(shop, shop)
        progress(int(100 * i / max(len(crawlers), 1)), "Shop prices", f"Reading {label}...")
        try:
            listings = crawl()
        except Exception as exc:
            logger.warning("event=sealed_shop_crawl_failed shop=%s class=%s err=%s", shop, type(exc).__name__, exc)
            results[shop] = {"error": str(exc)}
            continue
        if shop == jp_shops.HARERUYA:
            set_names.update(jp_shops.hareruya_set_names(listings))
            with db.get_conn() as conn:
                meta.write(conn, SET_NAMES_META_KEY, json.dumps(set_names, ensure_ascii=False, sort_keys=True))
        offers, stats = jp_shops.best_offers(shop, listings, catalog_by_set, set_names)
        with db.get_conn() as conn:
            repo.upsert_prices(conn, [
                {"product_uuid": uuid, "source": shop, "language": lang, "market_date": today,
                 "price": o.price_jpy, "currency": "JPY", "url": o.url, "stock": o.stock}
                for (uuid, lang), o in offers.items()
            ])
        stats["recorded"] = len(offers)
        results[shop] = stats
        logger.info("event=sealed_shop_prices shop=%s %s", shop, " ".join(f"{k}={v}" for k, v in stats.items()))
    with db.get_conn() as conn:
        meta.write(conn, SHOP_PRICES_META_KEY, _now_iso())
    return results


def run_daily_update(progress_cb: ProgressFn | None = None) -> dict:
    progress = progress_cb or _noop_progress
    progress(2, "Catalog", "Checking the sealed product catalog...")
    refresh_catalog()
    stats = refresh_prices(progress=lambda p, ph, d: progress(int(p * 0.5), ph, d))
    stats["shops"] = refresh_shop_prices(progress=lambda p, ph, d: progress(50 + int(p * 0.5), ph, d))
    return stats


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


def _to_jpy(amount: float | None, currency: str, fx: float | None) -> float | None:
    if amount is None:
        return None
    if currency == "JPY":
        return amount
    if currency == "USD" and fx:
        return amount * fx
    return None


def series_label(source: str, language: str) -> str:
    if source == tcg.SOURCE:
        return "TCGPlayer market"
    return f"{jp_shops.SHOP_LABELS.get(source, source)} ({language})"


def _product_label(p: dict) -> dict:
    return {
        **p,
        "kind": CATEGORY_LABELS.get(p.get("category") or "", p.get("category") or ""),
        "tcgplayer_url": (
            tcg.PRODUCT_URL.format(product_id=p["tcgplayer_product_id"])
            if p.get("tcgplayer_product_id") else None
        ),
    }


def _cheapest_shop_offer(points: dict, uuid: str, language: str, fresh_since: str) -> dict | None:
    """Cheapest recent in-stock JP-shop offer for one product × language.

    Shops only record in-stock offers, so a point older than the
    freshness window means the product has since sold out there."""
    offers = [
        {**pt, "source": src, "label": jp_shops.SHOP_LABELS[src]}
        for src in SHOP_SOURCES
        if (pt := points.get((uuid, src, language))) and pt["market_date"] >= fresh_since
    ]
    return min(offers, key=lambda o: o["price"]) if offers else None


def _attach_market(
    row: dict, language: str | None, points: dict, past: dict,
    fx: float | None, fresh_since: str,
) -> None:
    """Fill a holdings/watch row's prices.

    ``language`` is the lot's; watch rows (None) are valued at TCGPlayer and
    show both languages' cheapest shop offers. The value basis is TCGPlayer
    for English product and the cheapest Japanese-language shop offer for
    Japanese product (TCGPlayer's EN price, flagged, when no shop has it).
    """
    uuid = row["uuid"]
    tcg_pt = points.get((uuid, tcg.SOURCE, "EN"))
    row["tcg_usd"] = tcg_pt["price"] if tcg_pt else None
    row["offer_en"] = _cheapest_shop_offer(points, uuid, "EN", fresh_since)
    row["offer_jp"] = _cheapest_shop_offer(points, uuid, "JP", fresh_since)
    own_offer = row["offer_jp"] if language == "JP" else row["offer_en"]
    row["offer"] = own_offer

    basis: dict | None = None
    if language == "JP" and own_offer:
        basis = {**own_offer, "language": "JP"}
    elif tcg_pt:
        basis = {**tcg_pt, "source": tcg.SOURCE, "label": "TCGPlayer", "language": "EN"}
    elif own_offer:
        basis = {**own_offer, "language": language or "EN"}
    row["reference_mismatch"] = bool(language and basis and basis["language"] != language)
    row["basis_label"] = basis["label"] if basis else None
    row["series"] = f"{basis['source']}:{basis['language']}" if basis else f"{tcg.SOURCE}:EN"
    row["market_date"] = basis["market_date"] if basis else None
    row["market_usd"] = _to_usd(basis["price"], basis["currency"], fx) if basis else None
    row["market_jpy"] = _to_jpy(basis["price"], basis["currency"], fx) if basis else None
    if row["market_usd"] is not None:
        row["market_usd"] = round(row["market_usd"], 2)
    if row["market_jpy"] is not None:
        row["market_jpy"] = round(row["market_jpy"])

    old = past.get((uuid, basis["source"], basis["language"])) if basis else None
    row["change_pct"] = (
        round((basis["price"] / old["price"] - 1) * 100, 1)
        if basis and old and old["price"] and old["market_date"] != basis["market_date"] else None
    )


def compute_sealed_ctx(user_id: str, *, fx: float | None, today: date | None = None) -> dict:
    today = today or datetime.now(UTC).date()
    fresh_since = (today - timedelta(days=SHOP_FRESH_DAYS)).isoformat()
    with db.get_conn() as conn:
        lots = [_product_label(r) for r in repo.list_lots(conn, user_id)]
        watch = [_product_label(r) for r in repo.list_watch(conn, user_id)]
        catalog_rows = repo.list_catalog(conn)
        catalog_stamp = meta.read(conn, CATALOG_META_KEY)
        uuids = list({r["uuid"] for r in lots + watch})
        points = repo.latest_points(conn, uuids)
        past = repo.latest_points(
            conn, uuids,
            on_or_before=(today - timedelta(days=CHANGE_WINDOW_DAYS)).isoformat(),
        )

    for lot in lots:
        _attach_market(lot, lot["language"], points, past, fx, fresh_since)
    for w in watch:
        _attach_market(w, None, points, past, fx, fresh_since)

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

    with db.get_conn() as conn:
        shop_stamp = meta.read(conn, SHOP_PRICES_META_KEY)
    return {
        "lots": lots,
        "watch": watch,
        "summary": build_summary(lots, fx),
        "catalog": [_product_label(r) for r in catalog_rows],
        "catalog_updated_at": catalog_stamp,
        "shop_prices_updated_at": shop_stamp,
        "last_price_date": max(
            (pt["market_date"] for (_, src, _), pt in points.items() if src == tcg.SOURCE), default=None,
        ),
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


def parse_series(value: str | None) -> tuple[str, str] | None:
    source, _, language = (value or "").partition(":")
    if source in (tcg.SOURCE, *SHOP_SOURCES) and language in LANGUAGES:
        return source, language
    return None


def product_history(uuid: str, series: tuple[str, str] | None = None) -> dict | None:
    """Chart payload for one product: the requested series (default: the
    first of TCGPlayer, then any shop), plus the list of series it has."""
    with db.get_conn() as conn:
        product = repo.get_product(conn, uuid)
        if not product:
            return None
        available = repo.list_series(conn, uuid)
        keys = [(s["source"], s["language"]) for s in available]
        if series not in keys:
            series = (tcg.SOURCE, "EN") if (tcg.SOURCE, "EN") in keys else (keys[0] if keys else (tcg.SOURCE, "EN"))
        points = repo.price_history(conn, uuid, *series)
    return {
        "product": _product_label(product),
        "series": f"{series[0]}:{series[1]}",
        "currency": points[0]["currency"] if points else ("USD" if series[0] == tcg.SOURCE else "JPY"),
        "series_options": [
            {
                "key": f"{s['source']}:{s['language']}",
                "label": series_label(s["source"], s["language"]),
                "count": s["n"],
            }
            for s in available
        ],
        "points": [{"market_date": p["market_date"], "price": p["price"]} for p in points],
        "source_detail": (
            "TCGPlayer market price (recent sales, English product). The first year is"
            " backfilled at weekly resolution, then one point per day."
            if series[0] == tcg.SOURCE else
            f"Cheapest in-stock {series_label(*series)} asking price, recorded daily. Days"
            " the product was sold out there have no point."
        ),
    }
