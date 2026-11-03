"""Key/value access to the ``app_meta`` table.

A tiny generic store for app-wide bookkeeping: the price import
(``mtgjson_history_downloaded_at``, …), the sealed refresh
(``sealed_catalog_updated_at``, ``sealed_jp_set_names``, …).
Functions take a live connection so callers can compose reads and writes
inside a larger transaction.
"""
from sqlalchemy import text

from . import db


def read(conn, key: str) -> str | None:
    row = conn.execute(
        text("SELECT value FROM app_meta WHERE key = :key"), {"key": key}
    ).mappings().first()
    return row["value"] if row else None


def write(conn, key: str, value: str) -> None:
    db.upsert(conn, "app_meta", ["key"], [{"key": key, "value": value}])
