"""SQLite connection + schema for mtgcompare.

Single local DB file — currently holds the inventory table; future
price-history feature will share the same file.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "inventory.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory (
    id            INTEGER PRIMARY KEY,
    card_name     TEXT NOT NULL,
    set_code      TEXT NOT NULL,
    set_name      TEXT,
    card_number   TEXT,
    quantity      INTEGER NOT NULL,
    condition     TEXT,
    printing      TEXT,
    language      TEXT,
    price_bought  REAL,
    date_bought   TEXT
);
CREATE INDEX IF NOT EXISTS idx_inventory_card_name ON inventory(card_name);

CREATE TABLE IF NOT EXISTS market_prices (
    card_name   TEXT    NOT NULL,
    set_code    TEXT    NOT NULL,
    is_foil     INTEGER NOT NULL DEFAULT 0,
    price_usd   REAL,
    fetched_at  TEXT    NOT NULL,
    PRIMARY KEY (card_name, set_code, is_foil)
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
