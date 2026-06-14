"""MTGJSON price-history import orchestration (the write side).

Owns the half of the pricing package that *refreshes* data:

- MTGJSON file download + set-file candidate resolution
- inventory-lot → MTGJSON UUID mapping
- the price-history import pipeline (download → rebuild/merge → card map →
  market_prices), driven through ``PriceHistoryStore`` and the repos
- the daily cron price update

It sits on top of the data layer (``db``, ``inventory``, ``market_repo``,
``meta``, ``history_store``, ``history_import``) and the shared ``common``
core. Free of Flask: the only web-layer value it needs is a progress
callback, passed in by the route.
"""
import json
import logging
import lzma
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import requests

from .. import db
from .. import inventory as inv
from . import history_import, history_store, market_repo, meta
from .common import (
    is_foil,
    load_existing_card_map,
    mtgjson_cache_dir,
    mtgjson_history_duckdb_path,
    normalize_set_code,
    price_store,
    row_key_for_mapping,
)
from .market_service import market_cache_clear

logger = logging.getLogger(__name__)

__all__ = [
    "MTGJSON_BASE_URL",
    "MTGJSON_HEADERS",
    "candidate_uuid_map",
    "collector_sort_key",
    "download_file",
    "download_mtgjson_set_file",
    "download_or_unavailable",
    "ensure_history_loaded",
    "import_mtgjson_history",
    "load_set_cards",
    "mtgjson_history_path",
    "mtgjson_set_candidates",
    "mtgjson_set_path",
    "persist_card_map_and_meta",
    "populate_market_prices_from_history",
    "resolve_candidate_uuid",
    "resolve_inventory_uuids",
    "run_daily_price_update",
]

MTGJSON_BASE_URL = "https://mtgjson.com/api/v5"
MTGJSON_HEADERS = {"User-Agent": "mtgcompare/0.1", "Accept": "application/json"}


# --- MTGJSON paths / downloads ---------------------------------------------

def mtgjson_history_path() -> Path:
    return mtgjson_cache_dir() / "AllPrices.json.xz"


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

    # With no price history at all, skip the upsert entirely rather than
    # writing NULL prices for every lot. Once history exists, latest_prices
    # is always a dict — an empty result still writes NULL prices for lots
    # MTGJSON hasn't priced, matching the prior behaviour.
    uuid_list = list({u for (u, _) in uuid_to_db_key})
    store = history_store.get_store(duckdb_path)
    if not store.has_history():
        return
    latest = store.latest_prices(uuid_list)

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
    store = history_store.get_store(history_duckdb_path)
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
