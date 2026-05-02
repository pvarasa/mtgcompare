"""SQLAlchemy connection + schema for mtgcompare.

DATABASE_URL env var selects the backend:
  - absent → local SQLite (development / desktop)
  - set    → use that URL (PostgreSQL in production)
"""
import os
import sys
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    Text,
    create_engine,
    text,
)


def _db_path() -> Path:
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            data_dir = Path(os.environ.get("APPDATA", Path.home())) / "mtgcompare"
        elif sys.platform == "darwin":
            data_dir = Path.home() / "Library" / "Application Support" / "mtgcompare"
        else:
            xdg = os.environ.get("XDG_DATA_HOME")
            data_dir = (Path(xdg) if xdg else Path.home() / ".local" / "share") / "mtgcompare"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / "inventory.db"
    return Path(__file__).resolve().parent.parent / "inventory.db"


_DATABASE_URL = os.environ.get("DATABASE_URL")
IS_POSTGRES = bool(_DATABASE_URL)

if _DATABASE_URL:
    engine = create_engine(_DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=10)
    DB_PATH = None
else:
    DB_PATH = _db_path()
    engine = create_engine(
        f"sqlite:///{DB_PATH}",
        connect_args={"check_same_thread": False},
    )

metadata = MetaData()

_inventory = Table(
    "inventory", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Text, nullable=False, server_default="local"),
    Column("card_name", Text, nullable=False),
    Column("set_code", Text, nullable=False),
    Column("set_name", Text),
    Column("card_number", Text),
    Column("quantity", Integer, nullable=False),
    Column("condition", Text),
    Column("printing", Text),
    Column("language", Text),
    Column("price_bought", Numeric(10, 4)),
    Column("date_bought", Text),
)
Index("idx_inventory_user_card", _inventory.c.user_id, _inventory.c.card_name)

_market_prices = Table(
    "market_prices", metadata,
    Column("card_name", Text, nullable=False),
    Column("set_code", Text, nullable=False),
    Column("is_foil", Integer, nullable=False, server_default="0"),
    Column("price_usd", Numeric(10, 4)),
    Column("fetched_at", Text, nullable=False),
    PrimaryKeyConstraint("card_name", "set_code", "is_foil"),
)

_price_rows = Table(
    "price_rows", metadata,
    Column("uuid", Text, nullable=False),
    Column("finish", Text, nullable=False),
    Column("market_date", Date, nullable=False),
    Column("price_usd", Numeric(10, 4)),
    PrimaryKeyConstraint("uuid", "finish", "market_date"),
)
Index("price_rows_uuid_date", _price_rows.c.uuid, _price_rows.c.finish, _price_rows.c.market_date)

_mtgjson_card_map = Table(
    "mtgjson_card_map", metadata,
    Column("card_name", Text, nullable=False),
    Column("set_code", Text, nullable=False),
    Column("card_number", Text, nullable=False, server_default=""),
    Column("is_foil", Integer, nullable=False, server_default="0"),
    Column("uuid", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    PrimaryKeyConstraint("card_name", "set_code", "card_number", "is_foil"),
)

_app_meta = Table(
    "app_meta", metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text),
)

_price_update_runs = Table(
    "price_update_runs", metadata,
    Column("id", BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True),
    Column("triggered_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("status", Text, nullable=False),
    Column("duration_ms", Integer),
    Column("uuids_streamed", Integer),
    Column("rows_inserted", Integer),
    Column("market_date", Date),
    Column("trigger_source", Text),
    Column("error_message", Text),
    Column("job_id", Text),
    CheckConstraint(
        "status IN ('running','success','failed')",
        name="price_update_runs_status_check",
    ),
)
Index("price_update_runs_triggered_at_desc", _price_update_runs.c.triggered_at.desc())


def row_to_dict(row) -> dict:
    """Convert a SQLAlchemy RowMapping to a plain dict with uniform numeric types.

    psycopg2 returns decimal.Decimal for NUMERIC columns; SQLite returns float.
    Coercing Decimal→float here means callers never need backend-specific casts.
    """
    return {k: float(v) if isinstance(v, Decimal) else v for k, v in row.items()}


@contextmanager
def get_conn():
    with engine.begin() as conn:
        yield conn


def upsert(conn, table_name: str, conflict_cols: list[str], rows: list[dict]) -> None:
    """Dialect-aware INSERT OR REPLACE / ON CONFLICT DO UPDATE."""
    if not rows:
        return
    cols = list(rows[0].keys())
    non_conflict = [c for c in cols if c not in conflict_cols]
    col_list = ", ".join(cols)
    placeholders = ", ".join(f":{c}" for c in cols)
    if IS_POSTGRES:
        conflict = ", ".join(conflict_cols)
        set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in non_conflict)
        sql = text(
            f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholders})"
            f" ON CONFLICT ({conflict}) DO UPDATE SET {set_clause}"
        )
    else:
        sql = text(f"INSERT OR REPLACE INTO {table_name} ({col_list}) VALUES ({placeholders})")
    conn.execute(sql, rows)


def _migrate(conn) -> None:
    """Add columns absent from older schema versions."""
    if IS_POSTGRES:
        exists = conn.execute(text("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'inventory' AND column_name = 'user_id'
        """)).fetchone()
        if not exists:
            conn.execute(text(
                "ALTER TABLE inventory ADD COLUMN user_id TEXT NOT NULL DEFAULT 'local'"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_inventory_user_card ON inventory (user_id, card_name)"
            ))

        for table in ("price_rows", "mtgjson_card_map"):
            col_type = conn.execute(text("""
                SELECT data_type FROM information_schema.columns
                WHERE table_name = :t AND column_name = 'uuid'
            """), {"t": table}).scalar()
            if col_type == "text":
                conn.execute(text(
                    f"ALTER TABLE {table} ALTER COLUMN uuid TYPE UUID USING uuid::UUID"
                ))
    else:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(inventory)")).fetchall()}
        if "user_id" not in cols:
            conn.execute(text(
                "ALTER TABLE inventory ADD COLUMN user_id TEXT NOT NULL DEFAULT 'local'"
            ))


def init_schema() -> None:
    with engine.begin() as conn:
        metadata.create_all(conn, checkfirst=True)
        _migrate(conn)
