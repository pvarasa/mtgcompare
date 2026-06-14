"""Shared primitives for the pricing subsystems.

The two halves of the pricing package — the MTGJSON import/ETL pipeline
(``import_service``) and the /market render computation (``market_service``)
— both need a small common core: card-identity helpers, the MTGJSON cache
dir / DuckDB path, the price-history store factory, and the
inventory-lot → MTGJSON card-map lookups. Keeping them here lets the two
subsystems depend on a shared base without importing each other.

Flask-free; sits directly on the data layer (``db``, ``market_repo``,
``history_store``).
"""
from pathlib import Path

from .. import db
from . import history_store, market_repo

__all__ = [
    "has_price_history",
    "is_foil",
    "load_existing_card_map",
    "mtgjson_cache_dir",
    "mtgjson_history_duckdb_path",
    "normalize_set_code",
    "portfolio_value_series",
    "price_store",
    "query_history",
    "row_key_for_mapping",
]


# --- small shared card helpers ---------------------------------------------

def normalize_set_code(code: str | None, *, upper: bool = False) -> str:
    normalized = code.split("_")[0] if code else ""
    return normalized.upper() if upper else normalized.lower()


def is_foil(printing: str | None) -> bool:
    return (printing or "").lower() == "foil"


# --- MTGJSON cache dir / DuckDB path ---------------------------------------

def mtgjson_cache_dir() -> Path:
    if db.IS_POSTGRES:
        import os
        # Linux containers only; overridable via env var. CLAUDE.md documents the default.
        cache_dir = Path(os.environ.get("MTGJSON_CACHE_DIR", "/tmp/mtgjson"))  # noqa: S108
    else:
        cache_dir = db.DB_PATH.parent / "mtgjson"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def mtgjson_history_duckdb_path() -> Path:
    return mtgjson_cache_dir() / "AllPricesHistory.duckdb"


# --- price-history store wrappers ------------------------------------------

def price_store() -> history_store.PriceHistoryStore:
    """Price-history store for the active backend (Postgres or local DuckDB).

    Constructed per call so a per-test ``db.IS_POSTGRES`` flip is honoured;
    construction is cheap and the local store's DuckDB lock is module-level
    in ``history_store``, so serialization holds across instances. The local
    DuckDB path is only resolved (and its cache dir created) off the
    Postgres path, matching the previous behaviour.
    """
    duckdb_path = None if db.IS_POSTGRES else mtgjson_history_duckdb_path()
    return history_store.get_store(duckdb_path)


def has_price_history() -> bool:
    return price_store().has_history()


def query_history(uuid: str, finish: str) -> dict[str, float]:
    return price_store().query(uuid, finish)


def portfolio_value_series(weights: list[tuple[str, str, int]]) -> dict[str, float]:
    """Whole-portfolio USD value per market day, summed in the DB.

    ``weights`` is ``[(uuid, finish, quantity), ...]``; returns
    ``{date-string: total_usd}``. Delegates to the price store so the
    weighted aggregation runs in-engine rather than in Python.
    """
    return price_store().portfolio_value_series(weights)


# --- inventory lot → MTGJSON card-map lookups ------------------------------

def row_key_for_mapping(row: dict) -> tuple[str, str, str, int]:
    """Identity key for an inventory row in the MTGJSON map table."""
    return (
        row["card_name"].lower(),
        normalize_set_code(row["set_code"], upper=True),
        (row.get("card_number") or "").strip(),
        int(is_foil(row.get("printing"))),
    )


def _index_card_map(rows: list[dict]) -> dict[tuple[str, str, str, int], str]:
    """Index card-map rows by lot identity. The key shape MUST stay in lock-step
    with ``row_key_for_mapping`` — that is how callers look uuids up."""
    return {
        (r["card_name"].lower(), r["set_code"], r["card_number"], r["is_foil"]): r["uuid"]
        for r in rows
    }


def load_existing_card_map() -> dict[tuple[str, str, str, int], str]:
    """Read the whole mtgjson_card_map keyed by row identity for fast lookup."""
    with db.get_conn() as conn:
        return _index_card_map(market_repo.load_card_map(conn))


def load_card_map_for_inventory(inventory_rows: list[dict]) -> dict[tuple[str, str, str, int], str]:
    """Card-map lookup narrowed to just the sets present in ``inventory_rows``.

    Unlike ``load_existing_card_map`` (whole table, every user), this only
    scans the bounded set of set codes the portfolio actually touches —
    there are <1000 MTG sets total, so the ``IN`` list is always small.
    """
    set_codes = sorted({
        normalize_set_code(row["set_code"], upper=True)
        for row in inventory_rows if row.get("set_code")
    })
    with db.get_conn() as conn:
        return _index_card_map(market_repo.load_card_map_for_sets(conn, set_codes))
