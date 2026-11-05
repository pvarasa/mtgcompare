"""Data access for the ``sealed_products`` / ``sealed_lots`` /
``sealed_watch`` / ``sealed_prices`` tables.

Every function takes an open connection (``db.get_conn()``) so callers
control transaction scope. Dates are bound as ISO strings and read back
through ``_iso`` because SQLite returns them as text and Postgres as
``datetime.date``.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import bindparam, text

from .. import db


def purge_user(conn, user_id: str) -> None:
    """Delete a user's sealed lots and watchlist (account deletion). The
    catalog and price tables are global and untouched."""
    for table in ("sealed_lots", "sealed_watch"):
        conn.execute(text(f"DELETE FROM {table} WHERE user_id = :uid"), {"uid": user_id})  # noqa: S608


# ``openable``: the product has fixed contents MTGJSON lists card by card
# (a precon / Secret Lair), so a lot of it can be opened into inventory.
_PRODUCT_COLS = (
    "p.uuid, p.set_code, p.set_name, p.name, p.category, p.subtype,"
    " p.release_date, p.tcgplayer_product_id,"
    " CASE WHEN p.contents IS NULL THEN 0 ELSE 1 END AS openable"
)


def _iso(value) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)[:10]


# --- catalog -----------------------------------------------------------------

def upsert_products(conn, rows: list[dict]) -> None:
    db.upsert(conn, "sealed_products", ["uuid"], rows)


def count_products(conn) -> int:
    return int(conn.execute(text("SELECT COUNT(*) FROM sealed_products")).scalar() or 0)


def get_product(conn, uuid: str) -> dict | None:
    row = conn.execute(
        text(f"SELECT {_PRODUCT_COLS} FROM sealed_products p WHERE p.uuid = :u"),  # noqa: S608
        {"u": uuid},
    ).mappings().fetchone()
    return dict(row) if row else None


def find_product_by_name(conn, name: str) -> dict | None:
    row = conn.execute(
        text(
            f"SELECT {_PRODUCT_COLS} FROM sealed_products p"  # noqa: S608
            " WHERE lower(p.name) = lower(:n)"
            " ORDER BY p.release_date DESC LIMIT 1"
        ),
        {"n": name.strip()},
    ).mappings().fetchone()
    return dict(row) if row else None


def get_product_contents(conn, uuid: str) -> str | None:
    """The product's raw ``contents`` JSON (None for boosters and for decks
    MTGJSON doesn't list)."""
    return conn.execute(
        text("SELECT contents FROM sealed_products WHERE uuid = :u"), {"u": uuid},
    ).scalar()


def search_openable(conn, q: str, limit: int = 20) -> list[dict]:
    """Openable products whose name (or set code) contains every word of
    ``q``, newest first (the inventory's "Precon" add mode). LIKE
    wildcards typed into ``q`` just widen the match, as in the inventory
    name filter."""
    terms = [f"%{w}%" for w in q.lower().split()[:6]]
    if not terms:
        return []
    where = " AND ".join(
        f"(lower(p.name) LIKE :t{i} OR lower(p.set_code) LIKE :t{i})" for i in range(len(terms))
    )
    rows = conn.execute(
        text(
            f"SELECT {_PRODUCT_COLS} FROM sealed_products p"  # noqa: S608
            f" WHERE p.contents IS NOT NULL AND {where}"
            " ORDER BY p.release_date DESC, p.name LIMIT :lim"
        ),
        {**{f"t{i}": t for i, t in enumerate(terms)}, "lim": limit},
    ).mappings().fetchall()
    return [dict(r) for r in rows]


def list_catalog(conn) -> list[dict]:
    """Every catalog product, newest set first (for the add-form picker)."""
    rows = conn.execute(text(
        f"SELECT {_PRODUCT_COLS} FROM sealed_products p"  # noqa: S608
        " ORDER BY p.release_date DESC, p.name"
    )).mappings().fetchall()
    return [dict(r) for r in rows]


# --- lots / watchlist --------------------------------------------------------

def add_lot(conn, user_id: str, lot: dict) -> None:
    conn.execute(text(
        "INSERT INTO sealed_lots"
        " (user_id, product_uuid, language, quantity, price_bought, currency, date_bought)"
        " VALUES (:user_id, :product_uuid, :language, :quantity, :price_bought,"
        " :currency, :date_bought)"
    ), {"user_id": user_id, **lot})


def delete_lot(conn, user_id: str, lot_id: int) -> int:
    return conn.execute(
        text("DELETE FROM sealed_lots WHERE id = :id AND user_id = :u"),
        {"id": lot_id, "u": user_id},
    ).rowcount


def get_lot(conn, user_id: str, lot_id: int) -> dict | None:
    row = conn.execute(text(
        f"SELECT l.id, l.language, l.quantity, l.price_bought, l.currency,"  # noqa: S608
        f" l.date_bought, {_PRODUCT_COLS}"
        " FROM sealed_lots l JOIN sealed_products p ON p.uuid = l.product_uuid"
        " WHERE l.id = :id AND l.user_id = :u"
    ), {"id": lot_id, "u": user_id}).mappings().fetchone()
    return dict(row) if row else None


def consume_lot(conn, user_id: str, lot_id: int, product_uuid: str) -> bool:
    """Take one unit out of a lot (opening it): decrement, or delete the
    lot when it was the last one. False when the user has no such lot of
    that product. Two statements, each guarded on the quantity, so two
    concurrent opens of a 2-unit lot consume both units, not one twice."""
    params = {"id": lot_id, "u": user_id, "p": product_uuid}
    where = "id = :id AND user_id = :u AND product_uuid = :p"
    if conn.execute(
        text(f"UPDATE sealed_lots SET quantity = quantity - 1 WHERE {where} AND quantity > 1"),  # noqa: S608
        params,
    ).rowcount:
        return True
    return bool(conn.execute(
        text(f"DELETE FROM sealed_lots WHERE {where} AND quantity <= 1"), params,  # noqa: S608
    ).rowcount)


def list_lots(conn, user_id: str) -> list[dict]:
    rows = conn.execute(text(
        f"SELECT l.id, l.language, l.quantity, l.price_bought, l.currency,"  # noqa: S608
        f" l.date_bought, {_PRODUCT_COLS}"
        " FROM sealed_lots l JOIN sealed_products p ON p.uuid = l.product_uuid"
        " WHERE l.user_id = :u ORDER BY p.release_date DESC, p.name, l.id"
    ), {"u": user_id}).mappings().fetchall()
    return [dict(r) for r in rows]


def add_watch(conn, user_id: str, product_uuid: str, created_at: str) -> None:
    db.upsert(conn, "sealed_watch", ["user_id", "product_uuid"], [{
        "user_id": user_id, "product_uuid": product_uuid, "created_at": created_at,
    }])


def delete_watch(conn, user_id: str, product_uuid: str) -> int:
    return conn.execute(
        text("DELETE FROM sealed_watch WHERE user_id = :u AND product_uuid = :p"),
        {"u": user_id, "p": product_uuid},
    ).rowcount


def list_watch(conn, user_id: str) -> list[dict]:
    rows = conn.execute(text(
        f"SELECT {_PRODUCT_COLS} FROM sealed_watch w"  # noqa: S608
        " JOIN sealed_products p ON p.uuid = w.product_uuid"
        " WHERE w.user_id = :u ORDER BY p.release_date DESC, p.name"
    ), {"u": user_id}).mappings().fetchall()
    return [dict(r) for r in rows]


def tracked_products(conn) -> list[dict]:
    """Products any user holds or watches (the daily refresh set)."""
    rows = conn.execute(text(
        f"SELECT {_PRODUCT_COLS} FROM sealed_products p"  # noqa: S608
        " WHERE p.uuid IN (SELECT product_uuid FROM sealed_lots)"
        "    OR p.uuid IN (SELECT product_uuid FROM sealed_watch)"
        " ORDER BY p.uuid"
    )).mappings().fetchall()
    return [dict(r) for r in rows]


# --- prices ------------------------------------------------------------------

def upsert_prices(conn, rows: list[dict]) -> None:
    """Rows: product_uuid, source, language, market_date (date|str), price, currency."""
    db.upsert(
        conn, "sealed_prices",
        ["product_uuid", "source", "language", "market_date"],
        [{**r, "market_date": _iso(r["market_date"])} for r in rows],
    )


def price_counts(conn, uuids: list[str], source: str) -> dict[str, int]:
    if not uuids:
        return {}
    rows = conn.execute(
        text(
            "SELECT product_uuid, COUNT(*) FROM sealed_prices"
            " WHERE source = :s AND product_uuid IN :uuids GROUP BY product_uuid"
        ).bindparams(bindparam("uuids", expanding=True)),
        {"s": source, "uuids": list(uuids)},
    ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def latest_points(
    conn, uuids: list[str], *, on_or_before: str | None = None,
) -> dict[tuple[str, str, str], dict]:
    """``{(uuid, source, language): point}`` — the newest point of every
    series of the given products, optionally capped at a date (for "N days
    ago" comparisons). A point is ``{market_date, price, currency, url, stock}``."""
    if not uuids:
        return {}
    sql = text(
        "SELECT p.product_uuid, p.source, p.language, p.market_date, p.price,"
        " p.currency, p.url, p.stock"
        " FROM sealed_prices p JOIN ("
        "   SELECT product_uuid, source, language, MAX(market_date) AS md"
        "   FROM sealed_prices"
        "   WHERE product_uuid IN :uuids AND market_date <= :cap"
        "   GROUP BY product_uuid, source, language"
        " ) m ON m.product_uuid = p.product_uuid AND m.source = p.source"
        "   AND m.language = p.language AND m.md = p.market_date"
    ).bindparams(bindparam("uuids", expanding=True))
    params = {"uuids": list(uuids), "cap": on_or_before or "9999-12-31"}
    out: dict[tuple[str, str, str], dict] = {}
    for r in conn.execute(sql, params).mappings():
        out[(r["product_uuid"], r["source"], r["language"])] = {
            "market_date": _iso(r["market_date"]),
            "price": float(r["price"]),
            "currency": r["currency"],
            "url": r["url"],
            "stock": r["stock"],
        }
    return out


def list_series(conn, uuid: str) -> list[dict]:
    """Every (source, language) series a product has, with point counts."""
    rows = conn.execute(text(
        "SELECT source, language, currency, COUNT(*) AS n FROM sealed_prices"
        " WHERE product_uuid = :u GROUP BY source, language, currency"
        " ORDER BY source, language"
    ), {"u": uuid}).mappings().fetchall()
    return [dict(r) for r in rows]


def price_history(conn, uuid: str, source: str, language: str = "EN") -> list[dict]:
    rows = conn.execute(text(
        "SELECT market_date, price, currency FROM sealed_prices"
        " WHERE product_uuid = :u AND source = :s AND language = :l"
        " ORDER BY market_date"
    ), {"u": uuid, "s": source, "l": language}).mappings().fetchall()
    return [
        {"market_date": _iso(r["market_date"]), "price": float(r["price"]), "currency": r["currency"]}
        for r in rows
    ]
