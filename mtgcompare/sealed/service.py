"""Sealed-product orchestration and /sealed page computation (Flask-free).

Write side:
- ``refresh_catalog``  — reload the catalog from MTGJSON SetList.json when
  it is empty or older than ``CATALOG_MAX_AGE``.
- ``refresh_prices``   — for every tracked product (or the given ones):
  backfill a year of TCGPlayer history the first time, then record today's
  market price. Per-product failures are logged and counted, never fatal.
- ``run_daily_update`` — both of the above; called by the daily cron.
- ``add_precon_cards`` — "open" a precon / Secret Lair: add its cards to
  the inventory (optionally consuming one unit of a held lot) and record
  their exact MTGJSON uuids in ``mtgjson_card_map``.

Read side: ``compute_sealed_ctx`` (holdings + watchlist + summary),
``product_history`` (chart payload), ``precon_preview`` /
``search_openable`` (the inventory's Precon add mode).
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from threading import Lock

from .. import db, inventory, meta
from ..pricing import common as pricing_common
from ..pricing import import_service as pricing_import
from ..pricing import market_repo
from ..pricing.market_service import market_cache_clear
from . import catalog, decks, jp_shops, repo
from . import tcgplayer as tcg

logger = logging.getLogger(__name__)

CATALOG_META_KEY = "sealed_catalog_updated_at"
CATALOG_MAX_AGE = timedelta(days=7)
# Bumped when parse_setlist starts keeping something new, so the next daily
# run reloads the catalog instead of waiting out CATALOG_MAX_AGE.
# 2: precons / Secret Lairs (+ contents).
CATALOG_VERSION = "2"
CATALOG_VERSION_META_KEY = "sealed_catalog_version"
LANGUAGES = ("EN", "JP")
CURRENCIES = ("JPY", "USD")
CATEGORY_LABELS = {
    "booster_box": "Box", "booster_pack": "Pack", "deck": "Deck",
    "multiple_decks": "Decks", "deck_box": "Deck box", "box_set": "Box set",
}
SUBTYPE_LABELS = {"secret_lair": "Secret Lair"}
# app_meta: {SET CODE: set name} from SetList.json, for deck cards' set names.
SET_NAMES_ALL_META_KEY = "mtgjson_set_names"
# Inventory ``language`` for a lot opened from a sealed lot of that language.
INVENTORY_LANGUAGES = {"EN": "English", "JP": "Japanese"}
CHANGE_WINDOW_DAYS = 30
# Sell sources (asking prices) and buy sources (what a shop pays).
SHOP_SOURCES = (jp_shops.HARERUYA, jp_shops.SINGLESTAR, jp_shops.SERRA)
BUY_SOURCES = (jp_shops.HARERUYA_BUY,)
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
        version = meta.read(conn, CATALOG_VERSION_META_KEY)
    if not stamp or version != CATALOG_VERSION:
        return True
    try:
        return now - datetime.fromisoformat(stamp) > CATALOG_MAX_AGE
    except ValueError:
        return True


def refresh_catalog(
    *, force: bool = False,
    fetch: Callable[[], dict] = catalog.fetch_setlist,
    fetch_decks: Callable[[], dict] = catalog.fetch_deck_list,
) -> int | None:
    """Reload the catalog if stale (or forced). Returns rows written, or
    None when the existing catalog was fresh enough to keep.

    Both MTGJSON files are read before anything is written: a failed
    DeckList download must not upsert every deck product with NULL
    contents (the upsert would overwrite contents resolved last week)."""
    if not force and not catalog_is_stale():
        return None
    updated_at = _now_iso()
    setlist = fetch()
    deck_files = catalog.deck_file_index(fetch_decks())
    rows = catalog.parse_setlist(setlist, updated_at=updated_at, deck_files=deck_files)
    with db.get_conn() as conn:
        repo.upsert_products(conn, rows)
        meta.write(conn, CATALOG_META_KEY, updated_at)
        meta.write(conn, CATALOG_VERSION_META_KEY, CATALOG_VERSION)
        meta.write(conn, SET_NAMES_ALL_META_KEY, json.dumps(catalog.set_names(setlist), sort_keys=True))
    logger.info(
        "event=sealed_catalog_refreshed products=%d openable=%d",
        len(rows), sum(1 for r in rows if r["contents"]),
    )
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
    # Hareruya (shop + buylist) first: its titles carry set codes and teach
    # the set-name map SingleStar and Serra need.
    jp_shops.HARERUYA: jp_shops.crawl_hareruya,
    jp_shops.HARERUYA_BUY: jp_shops.crawl_hareruya_buylist,
    jp_shops.SINGLESTAR: jp_shops.crawl_singlestar,
    jp_shops.SERRA: jp_shops.crawl_serra,
}
_SET_NAME_TEACHERS = (jp_shops.HARERUYA, jp_shops.HARERUYA_BUY)


def refresh_shop_prices(
    *,
    crawlers: dict[str, ShopCrawler] | None = None,
    today: date | None = None,
    progress: ProgressFn = _noop_progress,
) -> dict[str, dict]:
    """Crawl each Japanese shop's sealed listing and record today's cheapest
    in-stock offer (lowest bid, for the buylist) per (product, language)
    for every catalog product found.

    Every matched product is recorded, not just tracked ones, so a product
    added later already has shop history. A failing shop is logged and
    skipped; the others still run.
    """
    crawlers = crawlers if crawlers is not None else DEFAULT_CRAWLERS
    today = today or datetime.now(UTC).date()
    with db.get_conn() as conn:
        catalog_by_set: dict[str, list[dict]] = defaultdict(list)
        # Boosters only: the Japanese shop crawls read booster listings, and
        # deck names would only widen the English-name match.
        for p in repo.list_catalog(conn):
            if p["category"] in catalog.BOOSTER_CATEGORIES:
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
        if shop in _SET_NAME_TEACHERS:
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


def product_kind(p: dict) -> str:
    category = p.get("category") or ""
    return SUBTYPE_LABELS.get(p.get("subtype") or "") or CATEGORY_LABELS.get(category, category)


def _product_label(p: dict) -> dict:
    return {
        **p,
        "openable": bool(p.get("openable")),
        "kind": product_kind(p),
        "tcgplayer_url": (
            tcg.PRODUCT_URL.format(product_id=p["tcgplayer_product_id"])
            if p.get("tcgplayer_product_id") else None
        ),
    }


def _recent_offer(
    points: dict, uuid: str, language: str, fresh_since: str,
    sources: tuple[str, ...], pick: Callable = min,
) -> dict | None:
    """One product × language's price across ``sources``, from points seen
    since ``fresh_since``: ``min`` for the cheapest asking price, ``max``
    for the best buylist bid.

    Shops only record in-stock offers (and live bids), so a point older
    than the freshness window means it has since sold out / stopped."""
    offers = [
        {**pt, "source": src, "label": jp_shops.SHOP_LABELS[src]}
        for src in sources
        if (pt := points.get((uuid, src, language))) and pt["market_date"] >= fresh_since
    ]
    return pick(offers, key=lambda o: o["price"]) if offers else None


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
    row["offer_en"] = _recent_offer(points, uuid, "EN", fresh_since, SHOP_SOURCES)
    row["offer_jp"] = _recent_offer(points, uuid, "JP", fresh_since, SHOP_SOURCES)
    own_offer = row["offer_jp"] if language == "JP" else row["offer_en"]
    row["offer"] = own_offer
    row["buy_en"] = _recent_offer(points, uuid, "EN", fresh_since, BUY_SOURCES, max)
    row["buy_jp"] = _recent_offer(points, uuid, "JP", fresh_since, BUY_SOURCES, max)
    row["buy"] = row["buy_jp"] if language == "JP" else row["buy_en"]

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
        bid = lot["buy"]
        lot["buy_value_jpy"] = round(bid["price"] * qty) if bid else None
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
    bid_rows = [r for r in lots if r.get("buy_value_jpy") is not None]
    total_buy_jpy = sum(r["buy_value_jpy"] for r in bid_rows)
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
        # What the buylist would pay today for the lots it bids on.
        "lots_with_buy": len(bid_rows),
        "total_buy_jpy": total_buy_jpy,
        "total_buy_usd": round(total_buy_jpy / fx, 2) if fx else None,
    }


def parse_series(value: str | None) -> tuple[str, str] | None:
    source, _, language = (value or "").partition(":")
    if source in (tcg.SOURCE, *SHOP_SOURCES, *BUY_SOURCES) and language in LANGUAGES:
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
            f"{series_label(*series)}: what the shop pays for the product, recorded daily."
            " Days it wasn't buying have no point."
            if series[0] in BUY_SOURCES else
            f"Cheapest in-stock {series_label(*series)} asking price, recorded daily. Days"
            " the product was sold out there have no point."
        ),
    }


# --- opening precons into the inventory ---------------------------------------

class PreconError(LookupError):
    """The product (or lot) can't be opened; ``str()`` is user-facing."""


def search_openable(q: str, limit: int = 20) -> list[dict]:
    with db.get_conn() as conn:
        rows = repo.search_openable(conn, q, limit)
    return [
        {"uuid": r["uuid"], "name": r["name"], "set_code": r["set_code"],
         "kind": product_kind(r), "year": (r["release_date"] or "")[:4]}
        for r in rows
    ]


def _openable(uuid: str) -> tuple[dict, dict]:
    with db.get_conn() as conn:
        product = repo.get_product(conn, uuid)
        contents = decks.parse_contents(repo.get_product_contents(conn, uuid)) if product else None
    if not product:
        raise PreconError("Unknown product.")
    if not contents:
        raise PreconError(f"MTGJSON doesn't list the cards in {product['name']}.")
    return product, contents


def precon_preview(
    uuid: str, *, skip_basics: bool = True, include_sideboard: bool = False,
    language: str = "EN", load: Callable[[str], dict] | None = None,
) -> dict:
    """The cards of an openable product as inventory add-preview records
    (the same shape the decklist preview builds). Raises PreconError;
    deck download failures propagate (the route reports them)."""
    product, contents = _openable(uuid)
    cards = decks.product_cards(contents, include_sideboard=include_sideboard, load=load or decks.load_deck)
    with db.get_conn() as conn:
        stored = meta.read(conn, SET_NAMES_ALL_META_KEY)
    set_names: dict[str, str] = json.loads(stored) if stored else {}
    kept = [c for c in cards if not (skip_basics and c["is_basic"])]
    records = [
        {
            "card_name": c["card_name"], "set_code": c["set_code"],
            "set_name": set_names.get(c["set_code"], ""), "card_number": c["card_number"],
            "quantity": c["quantity"], "condition": "NM", "printing": c["printing"],
            "language": INVENTORY_LANGUAGES.get(language, "English"),
            "price_bought": None, "date_bought": None,
        }
        for c in kept
    ]
    return {
        "product": _product_label(product),
        "records": records,
        "cards_total": sum(c["quantity"] for c in kept),
        "basics_skipped": sum(c["quantity"] for c in cards if c["is_basic"]) if skip_basics else 0,
    }


def _identity(row: dict) -> tuple[str, str, str, int]:
    return pricing_common.row_key_for_mapping(row)


def add_precon_cards(
    user_id: str, records: list[dict], *, product_uuid: str, lot_id: int | None = None,
    load: Callable[[str], dict] | None = None,
) -> int:
    """Add an opened product's (previewed, possibly edited) cards to the
    user's inventory, in one transaction with consuming one unit of
    ``lot_id`` when given. Returns the number of inventory rows added.

    The card-map rows come from the product's own MTGJSON deck data, never
    from the request: ``mtgjson_card_map`` is shared by every user, so a
    submitted uuid would let one user mis-price everyone's copies of a
    card. A record the user edited (other set / finish) just isn't mapped
    here and goes through the price import's usual matching.
    """
    _product, contents = _openable(product_uuid)
    trusted = {
        _identity(c): c
        for c in decks.product_cards(contents, include_sideboard=True, load=load or decks.load_deck)
    }
    now = _now_iso()
    card_maps: list[tuple[str, str, str, int, str, str]] = []
    seen: set[tuple[str, str, str, int]] = set()
    for r in records:
        key = _identity(r)
        if key in trusted and key not in seen:
            seen.add(key)
            card_maps.append((r["card_name"], key[1], key[2], key[3], trusted[key]["uuid"], now))

    with db.get_conn() as conn:
        if lot_id is not None and not repo.consume_lot(conn, user_id, lot_id, product_uuid):
            raise PreconError("That sealed lot isn't in your holdings any more.")
        added = inventory.insert_many(conn, records, user_id)
        market_repo.upsert_card_map(conn, [
            {"card_name": m[0], "set_code": m[1], "card_number": m[2],
             "is_foil": m[3], "uuid": m[4], "updated_at": m[5]}
            for m in card_maps
        ])

    # Price the new lots now rather than at tomorrow's import. Best-effort:
    # the daily run prices them anyway.
    try:
        pricing_import.populate_market_prices_from_history(
            card_maps, None if db.IS_POSTGRES else pricing_common.mtgjson_history_duckdb_path(), now,
        )
        market_cache_clear()
    except Exception:
        logger.exception("event=precon_initial_price_failed product=%s", product_uuid)
    return added
