"""SQLAlchemy connection + schema for mtgcompare.

DATABASE_URL env var selects the backend:
  - absent → local SQLite (development / desktop)
  - set    → use that URL (PostgreSQL in production)
"""
import os
import sys
from contextlib import contextmanager
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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.schema import CreateIndex


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
    # Cast NUMERIC columns to Python float (not Decimal) at the driver
    # level. Trade-off: prices fit comfortably in float64; anything
    # needing exact decimals must cast back at the use site.
    import psycopg2.extensions
    psycopg2.extensions.register_type(psycopg2.extensions.new_type(
        psycopg2.extensions.DECIMAL.values,
        "DEC2FLOAT",
        lambda v, _: float(v) if v is not None else None,
    ))
    # GIL caps effective concurrency, so the SQLAlchemy default (5 + 10
    # overflow) per gunicorn worker process is enough. Bumping
    # it under load measurably hurt — more in-flight queries → more
    # threads contending for the GIL → /healthz misses kubelet probes.
    _pool_size     = int(os.environ.get("DB_POOL_SIZE",     "5"))
    _pool_overflow = int(os.environ.get("DB_POOL_OVERFLOW", "10"))
    engine = create_engine(
        _DATABASE_URL,
        pool_pre_ping=True,
        pool_size=_pool_size,
        max_overflow=_pool_overflow,
    )
    DB_PATH = None
else:
    DB_PATH = _db_path()
    engine = create_engine(
        f"sqlite:///{DB_PATH}",
        connect_args={"check_same_thread": False},
    )

metadata = MetaData()

# MTGJSON card UUIDs: a native UUID on Postgres (half the index size of
# TEXT on the 19M-row price_rows), plain text on SQLite.
_UUID_TEXT = Text().with_variant(UUID(as_uuid=False), "postgresql")

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
# The filter UI's substring match (`LIKE '%bolt%'`) can't use a btree, but
# this still serves equality lookups and `ORDER BY lower(card_name)`, and
# the user_id prefix keeps any scan to one user's rows.
Index("idx_inventory_user_card_name_lower", _inventory.c.user_id, func.lower(_inventory.c.card_name))

# Keyed per printing (collector number included): a set's showcase or
# borderless variant is priced separately from the regular card.
_market_prices = Table(
    "market_prices", metadata,
    Column("card_name", Text, nullable=False),
    Column("set_code", Text, nullable=False),
    Column("card_number", Text, nullable=False, server_default=""),
    Column("is_foil", Integer, nullable=False, server_default="0"),
    Column("price_usd", Numeric(10, 4)),
    Column("fetched_at", Text, nullable=False),
    PrimaryKeyConstraint("card_name", "set_code", "card_number", "is_foil"),
)

_price_rows = Table(
    "price_rows", metadata,
    Column("uuid", _UUID_TEXT, nullable=False),
    Column("finish", Text, nullable=False),
    Column("market_date", Date, nullable=False),
    Column("price_usd", Numeric(10, 4)),
    PrimaryKeyConstraint("uuid", "finish", "market_date"),
)
# Covering index for the whole-portfolio value query (sum price_usd over a set
# of uuids, grouped by date): with price_usd in the leaf via INCLUDE, Postgres
# satisfies it with an index-only scan and never touches the 19M-row heap.
# INCLUDE is Postgres-only; on the (unused) SQLite price_rows it degrades to a
# plain key index, which is harmless.
Index(
    "price_rows_covering",
    _price_rows.c.uuid, _price_rows.c.finish, _price_rows.c.market_date,
    postgresql_include=["price_usd"],
)

_mtgjson_card_map = Table(
    "mtgjson_card_map", metadata,
    Column("card_name", Text, nullable=False),
    Column("set_code", Text, nullable=False),
    Column("card_number", Text, nullable=False, server_default=""),
    Column("is_foil", Integer, nullable=False, server_default="0"),
    Column("uuid", _UUID_TEXT, nullable=False),
    Column("updated_at", Text, nullable=False),
    PrimaryKeyConstraint("card_name", "set_code", "card_number", "is_foil"),
)

# Short-lived, encrypted record of a refresh-token redemption so concurrent
# requests on other workers/pods reuse it instead of re-redeeming a rotated
# token (see auth.refresh_once). Rows are pruned after a few minutes.
_auth_refresh_memo = Table(
    "auth_refresh_memo", metadata,
    Column("token_hash", Text, primary_key=True),
    Column("payload", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
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

_users = Table(
    "users", metadata,
    Column("workos_user_id", Text, primary_key=True),
    Column("email", Text, nullable=False),
    Column("first_name", Text),
    Column("last_name", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
Index("idx_users_email", _users.c.email, unique=True)

# Cached shop listings: one row per (shop, card, set, language, finish,
# condition). Populated lazily by CachedScrapper on user search.
_shop_listings = Table(
    "shop_listings", metadata,
    Column("id", BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True),
    Column("shop", Text, nullable=False),
    Column("card_name", Text, nullable=False),  # lowercased lookup key
    Column("card_display", Text, nullable=False),  # original casing from the scraper
    Column("set_code", Text, nullable=False, server_default=""),
    Column("language", Text, nullable=False, server_default="EN"),
    Column("finish", Text, nullable=False, server_default="normal"),
    Column("condition", Text, nullable=False, server_default="NM"),
    Column("price_jpy", Numeric(12, 2), nullable=False),
    Column("price_usd", Numeric(12, 2)),
    # Per-offer seller shipping (marketplace shops). NULL means "no
    # per-offer shipping known — use the flat per-shop estimate".
    Column("ship_jpy", Numeric(12, 2)),
    # Printing treatment (scrapers.variants tags; "" = regular, "?" =
    # unknown) and collector number. NULL variant marks a row written
    # before the column existed — the cache treats it as stale.
    Column("variant", Text),
    Column("card_number", Text),
    Column("stock", Integer),
    Column("url", Text),
    Column("last_checked", DateTime(timezone=True), nullable=False),
    # Forward-compat: bulk crawler will write source='bulk'; read path
    # currently doesn't filter on this, but the column avoids a future
    # schema migration when bulk lands.
    Column("source", Text, nullable=False, server_default="search"),
)
Index("idx_shop_listings_shop_card", _shop_listings.c.shop, _shop_listings.c.card_name)
Index("idx_shop_listings_last_checked", _shop_listings.c.last_checked)

# Memoizes the act of querying a shop, separate from the listings themselves.
# Lets us cache *zero-result* lookups so we don't re-scrape every time a user
# searches for a card the shop genuinely doesn't carry.
_shop_query_log = Table(
    "shop_query_log", metadata,
    Column("shop", Text, nullable=False),
    Column("card_name", Text, nullable=False),
    Column("queried_at", DateTime(timezone=True), nullable=False),
    Column("result_count", Integer, nullable=False, server_default="0"),
    # Vestigial: only ever "ok" (failures are not logged, so the next
    # request retries). Kept for releases that still write it.
    Column("status", Text, nullable=False, server_default="ok"),
    PrimaryKeyConstraint("shop", "card_name"),
)


# --- Sealed product tracking ------------------------------------------------
# A separate model from the card inventory: a booster box has no collector
# number / foil / condition, and every card table is keyed on those.

# Catalog of trackable sealed products, sourced from MTGJSON SetList.json
# (`sealedProduct`). Global, refreshed weekly; rows are never deleted
# because lots reference them.
_sealed_products = Table(
    "sealed_products", metadata,
    Column("uuid", Text, primary_key=True),
    Column("set_code", Text, nullable=False),
    Column("set_name", Text),
    Column("name", Text, nullable=False),
    Column("category", Text, nullable=False),   # booster_box / booster_pack
    Column("subtype", Text),                    # draft / set / collector / play ...
    Column("release_date", Text),
    Column("tcgplayer_product_id", Integer),
    Column("updated_at", Text, nullable=False),
)
Index("idx_sealed_products_name", _sealed_products.c.name)

# One row per owned lot (same "lot, not printing" rule as `inventory`).
# Language matters: a JP and an EN box of one set trade at different prices.
_sealed_lots = Table(
    "sealed_lots", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Text, nullable=False, server_default="local"),
    Column("product_uuid", Text, nullable=False),
    Column("language", Text, nullable=False, server_default="EN"),
    Column("quantity", Integer, nullable=False),
    Column("price_bought", Numeric(12, 2)),
    Column("currency", Text, nullable=False, server_default="JPY"),
    Column("date_bought", Text),
)
Index("idx_sealed_lots_user", _sealed_lots.c.user_id)

# Products a user tracks without owning them.
_sealed_watch = Table(
    "sealed_watch", metadata,
    Column("user_id", Text, nullable=False),
    Column("product_uuid", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    PrimaryKeyConstraint("user_id", "product_uuid"),
)

# Daily price points per product × source × language. Small (a few hundred
# products), so no price_rows-style covering index is needed. Price is kept
# in the source's own currency and converted at render time.
_sealed_prices = Table(
    "sealed_prices", metadata,
    Column("product_uuid", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("language", Text, nullable=False, server_default="EN"),
    Column("market_date", Date, nullable=False),
    Column("price", Numeric(12, 2), nullable=False),
    Column("currency", Text, nullable=False),
    # Shop sources: the cheapest in-stock offer's page and stock count.
    # NULL for TCGPlayer (a market price, not an offer).
    Column("url", Text),
    Column("stock", Integer),
    PrimaryKeyConstraint("product_uuid", "source", "language", "market_date"),
)


def row_to_dict(row) -> dict:
    """Convert a SQLAlchemy RowMapping to a plain dict.

    Postgres NUMERIC values arrive as Python floats — see the DEC2FLOAT
    typecast registered at engine creation above. SQLite already returns
    floats. So the dict() conversion is uniform across backends and the
    per-cell isinstance(Decimal) check that used to live here is gone.
    """
    return dict(row)


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
    # table/column identifiers come from internal callers (table defs in this
    # module, dict keys from typed row builders); values are bound via `rows`.
    if IS_POSTGRES:
        conflict = ", ".join(conflict_cols)
        set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in non_conflict)
        sql = text(
            f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholders})"  # noqa: S608
            f" ON CONFLICT ({conflict}) DO UPDATE SET {set_clause}"
        )
    else:
        sql = text(f"INSERT OR REPLACE INTO {table_name} ({col_list}) VALUES ({placeholders})")  # noqa: S608
    conn.execute(sql, rows)


def _column_exists(conn, table: str, column: str) -> bool:
    if IS_POSTGRES:
        return conn.execute(text("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = :t AND column_name = :c
        """), {"t": table, "c": column}).fetchone() is not None
    cols = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}
    return column in cols


# Columns added after their table first shipped. ``create_all`` only creates
# missing tables, so an existing DB (a desktop user's old inventory.db, or
# the shared Postgres while an older release still runs against it) gets
# them here. Nullable / defaulted, so each ALTER is metadata-only and safe
# while an older release, which names its columns, shares the table.
_ADDED_COLUMNS = (
    ("inventory", "user_id", "TEXT NOT NULL DEFAULT 'local'"),
    ("shop_listings", "ship_jpy", "NUMERIC(12,2)"),
    ("shop_listings", "variant", "TEXT"),
    ("shop_listings", "card_number", "TEXT"),
)


def _migrate(conn) -> None:
    """Bring a DB created by an older release up to the current schema."""
    # market_prices gained card_number in its primary key. The table is a
    # derived cache the price import rewrites, so drop and recreate it
    # rather than migrate rows keyed without a collector number; the next
    # import (daily cron, or "Update prices") repopulates it.
    if not _column_exists(conn, "market_prices", "card_number"):
        conn.execute(text("DROP TABLE market_prices"))
        _market_prices.create(conn)
    for table, column, ddl in _ADDED_COLUMNS:
        if not _column_exists(conn, table, column):
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
    # Indexes declared after their table existed (create_all skips them).
    # IF NOT EXISTS rather than checkfirst: SQLite's index reflection
    # misses expression indexes like lower(card_name).
    for index in _inventory.indexes:
        conn.execute(CreateIndex(index, if_not_exists=True))


def init_schema() -> None:
    with engine.begin() as conn:
        metadata.create_all(conn, checkfirst=True)
        _migrate(conn)
