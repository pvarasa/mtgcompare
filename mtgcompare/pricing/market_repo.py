"""Data access for the market-pricing tables.

``market_prices`` holds the latest price per (card_name, set_code, is_foil);
``mtgjson_card_map`` maps an inventory lot's identity to its MTGJSON UUID.
Both are populated by the price-import pipeline and read by the /market
views. Functions take a live connection so callers can compose them inside
the import transaction; dialect-aware writes go through ``db.upsert``.
"""
from sqlalchemy import bindparam, text

from .. import db

_MARKET_PRICES_CONFLICT = ["card_name", "set_code", "is_foil"]
_CARD_MAP_CONFLICT = ["card_name", "set_code", "card_number", "is_foil"]


# --- market_prices ---------------------------------------------------------

def load_market_prices(conn) -> list[dict]:
    """All market_prices rows as plain dicts.

    Columns: card_name, set_code, is_foil, price_usd, fetched_at.
    """
    return [db.row_to_dict(r) for r in conn.execute(
        text("SELECT card_name, set_code, is_foil, price_usd, fetched_at FROM market_prices")
    ).mappings().all()]


def upsert_market_prices(conn, rows: list[dict]) -> None:
    db.upsert(conn, "market_prices", _MARKET_PRICES_CONFLICT, rows)


# --- mtgjson_card_map ------------------------------------------------------

def load_card_map(conn) -> list[dict]:
    """All mtgjson_card_map rows.

    Columns: card_name, set_code, card_number, is_foil, uuid.
    """
    return [db.row_to_dict(r) for r in conn.execute(
        text("SELECT card_name, set_code, card_number, is_foil, uuid FROM mtgjson_card_map")
    ).mappings().all()]


def find_card_uuid(conn, *, card_name: str, set_code: str,
                   card_number: str, is_foil: int) -> str | None:
    """Resolve one lot's MTGJSON UUID by exact identity, or None."""
    row = conn.execute(
        text("""SELECT uuid
                FROM mtgjson_card_map
                WHERE lower(card_name) = lower(:card_name)
                  AND set_code = :set_code
                  AND card_number = :card_number
                  AND is_foil = :is_foil
                LIMIT 1"""),
        {"card_name": card_name, "set_code": set_code,
         "card_number": card_number, "is_foil": is_foil},
    ).mappings().first()
    return row["uuid"] if row else None


def delete_card_map_for_sets(conn, set_codes) -> None:
    """Evict all card-map rows for the given set codes.

    Used to refresh a set's mappings before re-upserting them. A no-op for
    an empty collection.
    """
    set_codes = list(set_codes)
    if not set_codes:
        return
    conn.execute(
        text("DELETE FROM mtgjson_card_map WHERE set_code IN :sets")
        .bindparams(bindparam("sets", expanding=True)),
        {"sets": set_codes},
    )


def upsert_card_map(conn, rows: list[dict]) -> None:
    db.upsert(conn, "mtgjson_card_map", _CARD_MAP_CONFLICT, rows)
