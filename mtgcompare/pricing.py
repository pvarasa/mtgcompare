"""Market pricing + MTGJSON price-history orchestration.

Extracted from ``web.py`` so the Flask layer keeps only routing, SSE, and
the download-job registry. This module owns:

- MTGJSON file download + cache-dir/path resolution
- inventory-lot → MTGJSON UUID mapping (set-file candidate resolution)
- the price-history import pipeline (download → rebuild/merge → card map →
  market_prices), driven through ``PriceHistoryStore`` and the repos
- the /market render computation + its process-wide caches

It sits on top of the data layer (``db``, ``inventory``, ``market_repo``,
``meta``, ``pricehistory``, ``history_import``) and is free of Flask: the
two web-layer values it needs — the FX rate and the pagination choices —
are injected into ``compute_market_ctx`` by the route.
"""
import json
import logging
import lzma
import math
import os
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Lock
from time import monotonic

import requests
from cachetools import TTLCache

from . import db, history_import, market_repo, meta, pricehistory
from . import inventory as inv

logger = logging.getLogger(__name__)

MTGJSON_BASE_URL = "https://mtgjson.com/api/v5"
MTGJSON_HEADERS = {"User-Agent": "mtgcompare/0.1", "Accept": "application/json"}

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


# --- small shared card helpers ---------------------------------------------

def normalize_set_code(code: str | None, *, upper: bool = False) -> str:
    normalized = code.split("_")[0] if code else ""
    return normalized.upper() if upper else normalized.lower()


def is_foil(printing: str | None) -> bool:
    return (printing or "").lower() == "foil"


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


# --- MTGJSON cache dir / paths / downloads ---------------------------------

def mtgjson_cache_dir() -> Path:
    if db.IS_POSTGRES:
        # Linux containers only; overridable via env var. CLAUDE.md documents the default.
        cache_dir = Path(os.environ.get("MTGJSON_CACHE_DIR", "/tmp/mtgjson"))  # noqa: S108
    else:
        cache_dir = db.DB_PATH.parent / "mtgjson"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def mtgjson_history_path() -> Path:
    return mtgjson_cache_dir() / "AllPrices.json.xz"


def mtgjson_history_duckdb_path() -> Path:
    return mtgjson_cache_dir() / "AllPricesHistory.duckdb"


def mtgjson_set_path(set_code: str) -> Path:
    return mtgjson_cache_dir() / f"{normalize_set_code(set_code, upper=True)}.json.xz"


def mtgjson_set_candidates(set_code: str) -> list[str]:
    normalized = normalize_set_code(set_code, upper=True)
    candidates: list[str] = []
    for value in (
        normalized,
        normalized.split("_")[0],
        normalized.split("-")[0],
        re.sub(r"\d+$", "", normalized),
    ):
        value = value.strip()
        if value and value not in candidates:
            candidates.append(value)
    trimmed = normalized
    while len(trimmed) > 3:
        trimmed = trimmed[:-1]
        if trimmed and trimmed not in candidates:
            candidates.append(trimmed)
    return candidates


def download_file(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with requests.get(url, headers=MTGJSON_HEADERS, stream=True, timeout=(20, 300)) as resp:
        resp.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if chunk:
                    fh.write(chunk)
    tmp.replace(target)


def download_or_unavailable(url: str, target: Path, unavailable_msg: str) -> None:
    # MTGJSON returns 404 during nightly publish windows or before the next
    # day's file is ready; translate into a user-facing RuntimeError so callers
    # don't surface a raw HTTPError. Other HTTP errors stay as HTTPError.
    try:
        download_file(url, target)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            raise RuntimeError(unavailable_msg) from exc
        raise


def download_mtgjson_set_file(set_code: str) -> tuple[str, Path] | None:
    for candidate in mtgjson_set_candidates(set_code):
        path = mtgjson_set_path(candidate)
        if path.exists():
            return candidate, path
        try:
            download_file(f"{MTGJSON_BASE_URL}/{candidate}.json.xz", path)
            return candidate, path
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                continue
            raise
    return None


# --- price-history store wrappers ------------------------------------------

def price_store() -> pricehistory.PriceHistoryStore:
    """Price-history store for the active backend (Postgres or local DuckDB).

    Constructed per call so a per-test ``db.IS_POSTGRES`` flip is honoured;
    construction is cheap and the local store's DuckDB lock is module-level
    in ``pricehistory``, so serialization holds across instances. The local
    DuckDB path is only resolved (and its cache dir created) off the
    Postgres path, matching the previous behaviour.
    """
    duckdb_path = None if db.IS_POSTGRES else mtgjson_history_duckdb_path()
    return pricehistory.get_store(duckdb_path)


def has_price_history() -> bool:
    return price_store().has_history()


def query_history(uuid: str, finish: str) -> dict[str, float]:
    return price_store().query(uuid, finish)


# --- inventory lot → MTGJSON UUID mapping ----------------------------------

def candidate_uuid_map(cards: list[dict], set_code: str) -> dict[tuple[str, str, str], dict[str, str]]:
    candidates: dict[tuple[str, str, str], dict[str, str]] = {}
    normalized_set = normalize_set_code(set_code, upper=True)
    for card in cards:
        name = (card.get("name") or "").strip()
        if not name:
            continue
        card_number = (card.get("number") or "").strip()
        identifiers = card.get("identifiers") or {}
        finishes = {finish.lower() for finish in (card.get("finishes") or [])}
        normal_uuid = identifiers.get("mtgjsonNonFoilVersionId")
        foil_uuid = identifiers.get("mtgjsonFoilVersionId")
        if not normal_uuid and "nonfoil" in finishes:
            normal_uuid = card.get("uuid")
        if not foil_uuid and "foil" in finishes:
            foil_uuid = card.get("uuid")
        key = (name.lower(), normalized_set, card_number)
        bucket = candidates.setdefault(key, {})
        if normal_uuid and "normal" not in bucket:
            bucket["normal"] = normal_uuid
        if foil_uuid and "foil" not in bucket:
            bucket["foil"] = foil_uuid
    return candidates


def collector_sort_key(num: str) -> tuple:
    """Sort '1', '2', ..., '99', '100' numerically; suffixed numbers come after plain ones."""
    match = re.match(r"^(\d+)(.*)$", num or "")
    if match:
        return (0, int(match.group(1)), match.group(2))
    return (1, num or "")


def resolve_candidate_uuid(row: dict, candidates: dict[tuple[str, str, str], dict[str, str]]) -> str | None:
    name_key = row["card_name"].lower()
    set_key = normalize_set_code(row["set_code"], upper=True)
    card_number = (row.get("card_number") or "").strip()
    finish_key = "foil" if is_foil(row.get("printing")) else "normal"
    for key in [(name_key, set_key, card_number), (name_key, set_key, "")]:
        bucket = candidates.get(key)
        if bucket and bucket.get(finish_key):
            return bucket[finish_key]
    # Fallback: any printing of this name in this set with the right finish.
    # Catches manual entries with mistyped or missing collector numbers.
    matches = sorted(
        (
            (cnum, bucket[finish_key])
            for (cname, cset, cnum), bucket in candidates.items()
            if cname == name_key and cset == set_key and bucket.get(finish_key)
        ),
        key=lambda pair: collector_sort_key(pair[0]),
    )
    return matches[0][1] if matches else None


def load_set_cards(path: Path) -> list[dict]:
    with lzma.open(path, "rt", encoding="utf-8") as fh:
        payload = json.load(fh)
    return ((payload.get("data") or {}).get("cards")) or []


def row_key_for_mapping(row: dict) -> tuple[str, str, str, int]:
    """Identity key for an inventory row in the MTGJSON map table."""
    return (
        row["card_name"].lower(),
        normalize_set_code(row["set_code"], upper=True),
        (row.get("card_number") or "").strip(),
        int(is_foil(row.get("printing"))),
    )


def load_existing_card_map() -> dict[tuple[str, str, str, int], str]:
    """Read mtgjson_card_map keyed by row identity for fast lookup."""
    with db.get_conn() as conn:
        existing_rows = market_repo.load_card_map(conn)
    return {
        (r["card_name"].lower(), r["set_code"], r["card_number"], r["is_foil"]): r["uuid"]
        for r in existing_rows
    }


def resolve_inventory_uuids(
    inventory_rows: list[dict],
    downloaded_at: str,
    progress: Callable[[int, str, str], None],
) -> tuple[list[tuple[str, str, str, int, str, str]], set[str]]:
    """Map every inventory lot to an MTGJSON UUID.

    Downloads MTGJSON set files only for sets that have at least one
    unmapped lot — already-resolved sets are taken from
    ``mtgjson_card_map`` directly.

    Returns ``(card_maps, sets_needing_load)`` where ``card_maps`` is the
    full list of resolvable lots with their UUIDs, and ``sets_needing_load``
    is the set of normalized set codes whose mapping rows should be
    refreshed (used by the caller to evict stale rows before upsert).
    """
    existing_uuid = load_existing_card_map()

    sets_needing_load: set[str] = {
        normalize_set_code(row["set_code"], upper=True)
        for row in inventory_rows
        if row.get("set_code") and row_key_for_mapping(row) not in existing_uuid
    }

    candidates_by_set: dict[str, dict[tuple[str, str, str], dict[str, str]]] = {}
    sets_to_load = sorted(sets_needing_load)
    if sets_to_load:
        total_to_load = len(sets_to_load)
        for index, set_code in enumerate(sets_to_load, start=1):
            progress(
                5 + round(index / total_to_load * 20),
                "Downloading set data",
                f"Downloading MTGJSON set file for {set_code} ({index}/{total_to_load})...",
            )
            resolved = download_mtgjson_set_file(set_code)
            if not resolved:
                logger.warning("No MTGJSON set file found for inventory set %s", set_code)
                candidates_by_set[set_code] = {}
                continue
            resolved_set_code, set_path = resolved
            candidates_by_set[set_code] = candidate_uuid_map(load_set_cards(set_path), resolved_set_code)
    else:
        progress(25, "Set data", "All sets already mapped — skipping set file load.")

    progress(28, "Mapping inventory", "Resolving MTGJSON card UUIDs for inventory lots...")
    card_maps: list[tuple[str, str, str, int, str, str]] = []
    for row in inventory_rows:
        key = row_key_for_mapping(row)
        set_code = normalize_set_code(row["set_code"], upper=True)
        if set_code in sets_needing_load:
            uuid = resolve_candidate_uuid(row, candidates_by_set.get(set_code, {}))
        else:
            uuid = existing_uuid.get(key)
        if not uuid:
            continue
        is_foil_int = int(is_foil(row.get("printing")))
        card_number = (row.get("card_number") or "").strip()
        card_maps.append((row["card_name"], set_code, card_number, is_foil_int, uuid, downloaded_at))

    return card_maps, sets_needing_load


# --- price-history import orchestration ------------------------------------

def populate_market_prices_from_history(
    card_maps: list[tuple],
    duckdb_path: Path | None,
    fetched_at: str,
) -> None:
    """Write the latest price for each mapped inventory lot into market_prices.

    card_maps: list of (card_name, set_code, card_number, is_foil, uuid, updated_at)
    """
    if not card_maps:
        return

    # Deduplicate to one market_prices row per (card_name, set_code, is_foil).
    uuid_to_db_key: dict[tuple[str, str], tuple[str, str, int]] = {}
    seen_db_keys: set[tuple[str, str, int]] = set()
    for card_name, set_code, _card_number, is_foil_int, uuid, _ in card_maps:
        finish = "foil" if is_foil_int else "normal"
        db_key = (card_name, set_code, is_foil_int)
        if db_key not in seen_db_keys:
            seen_db_keys.add(db_key)
            uuid_to_db_key[(str(uuid), finish)] = db_key

    if not uuid_to_db_key:
        return

    # latest_prices returns None for the local backend when no DuckDB file
    # exists yet — skip the upsert entirely in that case (matches the prior
    # early-return). An empty dict means "history present, no matching rows":
    # the inserts below still write NULL prices, as the old code did.
    uuid_list = list({u for (u, _) in uuid_to_db_key})
    latest = pricehistory.get_store(duckdb_path).latest_prices(uuid_list)
    if latest is None:
        return

    inserts = [
        {
            "card_name": card_name,
            "set_code":  set_code,
            "is_foil":   is_foil_int,
            "price_usd": latest.get((uuid, finish)),
            "fetched_at": fetched_at,
        }
        for (uuid, finish), (card_name, set_code, is_foil_int) in uuid_to_db_key.items()
    ]
    with db.get_conn() as conn:
        market_repo.upsert_market_prices(conn, inserts)


def ensure_history_loaded(
    history_duckdb_path: Path,
    progress: Callable[[int, str, str], None],
) -> int:
    """Ensure MTGJSON price history is loaded into the active backend.

    Downloads AllPrices.json.xz and runs the rebuild pipeline (DuckDB or
    PostgreSQL depending on ``db.IS_POSTGRES``) only when the local store
    is empty. Returns the row count written, or 0 if the existing store
    was reused (caller can fall back to the meta table for the count).
    """
    store = pricehistory.get_store(history_duckdb_path)
    if store.has_history():
        progress(40, "History ready", "Using existing price history.")
        return 0

    history_path = mtgjson_history_path()
    progress(32, "Downloading history", "Downloading MTGJSON AllPrices history...")
    download_or_unavailable(
        f"{MTGJSON_BASE_URL}/AllPrices.json.xz",
        history_path,
        "MTGJSON price files are temporarily unavailable. Please try again later.",
    )
    try:
        return store.rebuild(history_path, progress_cb=progress)
    finally:
        history_path.unlink(missing_ok=True)


def persist_card_map_and_meta(
    card_maps: list[tuple[str, str, str, int, str, str]],
    sets_needing_load: set[str],
    downloaded_at: str,
    history_row_count: int,
) -> int:
    """Write fresh mtgjson_card_map rows + history meta. Returns effective row count.

    If ``history_row_count`` is 0 (existing history was reused), reads the
    last persisted count from the meta table so callers can report a
    consistent number.
    """
    with db.get_conn() as conn:
        market_repo.delete_card_map_for_sets(conn, sets_needing_load)
        if card_maps:
            card_map_dicts = [
                {"card_name": m[0], "set_code": m[1], "card_number": m[2],
                 "is_foil": m[3], "uuid": m[4], "updated_at": m[5]}
                for m in card_maps
            ]
            market_repo.upsert_card_map(conn, card_map_dicts)
        meta.write(conn, "mtgjson_history_downloaded_at", downloaded_at)
        if history_row_count:
            meta.write(conn, "mtgjson_history_db_built_at", downloaded_at)
            meta.write(conn, "mtgjson_history_db_row_count", str(history_row_count))

    if history_row_count:
        return history_row_count
    with db.get_conn() as conn:
        row_count = meta.read(conn, "mtgjson_history_db_row_count")
    return int(row_count) if row_count else 0


def import_mtgjson_history(rows: list[dict], *, progress_cb=None) -> tuple[int, int]:
    def _progress(progress: int, phase: str, detail: str) -> None:
        if progress_cb:
            progress_cb(progress, phase, detail)

    inventory_rows = [dict(row) for row in rows]
    downloaded_at = datetime.now(UTC).isoformat(timespec="seconds")

    card_maps, sets_needing_load = resolve_inventory_uuids(
        inventory_rows, downloaded_at, _progress,
    )

    history_duckdb_path = mtgjson_history_duckdb_path()
    history_row_count = ensure_history_loaded(history_duckdb_path, _progress)

    _progress(96, "Saving mappings", "Updating local card-to-MTGJSON mappings...")
    history_row_count = persist_card_map_and_meta(
        card_maps, sets_needing_load, downloaded_at, history_row_count,
    )

    _progress(98, "Updating prices", "Writing latest prices to market table...")
    populate_market_prices_from_history(
        card_maps,
        None if db.IS_POSTGRES else history_duckdb_path,
        downloaded_at,
    )

    _progress(100, "Done", f"Indexed {history_row_count:,} MTGJSON price points and mapped {len(card_maps)} lot(s).")
    return len(card_maps), history_row_count


def run_daily_price_update(
    progress_cb=None,
) -> tuple[int, int, int, "date | None"]:
    """Download today's prices for all cards and update UUID mappings for inventory.

    Used by the cron endpoint. Returns
    (mapped_count, rows_inserted, uuids_streamed, market_date).
    """
    def _progress(progress: int, phase: str, detail: str) -> None:
        if progress_cb:
            progress_cb(progress, phase, detail)

    inventory_rows = inv.list_all_global()
    cache_dir = mtgjson_cache_dir()

    today_xz = cache_dir / "AllPricesToday.json.xz"
    _progress(10, "Downloading today's prices", "Downloading AllPricesToday.json.xz...")
    download_or_unavailable(
        f"{MTGJSON_BASE_URL}/AllPricesToday.json.xz",
        today_xz,
        "MTGJSON AllPricesToday not available yet.",
    )

    market_date = history_import.read_meta_date(today_xz)

    uuids_streamed, rows_inserted = price_store().merge_today(
        today_xz, progress_cb=_progress,
    )
    today_xz.unlink(missing_ok=True)

    mapped_count, _ = import_mtgjson_history(inventory_rows, progress_cb=_progress)
    # New prices landed — flush the /market data cache so users don't
    # keep seeing stale PnL for up to the TTL.
    market_cache_clear()
    return mapped_count, rows_inserted, uuids_streamed, market_date


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
