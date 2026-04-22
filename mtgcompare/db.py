"""SQLite connection + schema for mtgcompare.

Single local DB file — currently holds the inventory table; future
price-history feature will share the same file.
"""
import os
import sqlite3
import sys
from pathlib import Path


def _data_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "mtgcompare"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "mtgcompare"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "mtgcompare"


def _db_path() -> Path:
    if getattr(sys, "frozen", False):
        data_dir = _data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / "inventory.db"
    return Path(__file__).resolve().parent.parent / "inventory.db"


DB_PATH = _db_path()

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
