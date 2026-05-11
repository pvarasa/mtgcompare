"""Tests for db.py (upsert, schema bootstrap, migration) and inventory.py
(user_id scoping, list_all_global, stats, import_csv).

All tests use a temporary SQLite engine so they never touch inventory.db.
"""
import textwrap

import pytest
from sqlalchemy import create_engine, text

import mtgcompare.db as db_module
from mtgcompare import inventory as inv

# ---------------------------------------------------------------------------
# Fixture: redirect db.engine to a fresh per-test SQLite file
# ---------------------------------------------------------------------------

@pytest.fixture()
def test_db(tmp_path, monkeypatch):
    """Swap the module-level engine for a fresh temp SQLite database."""
    db_path = tmp_path / "test.db"
    test_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(db_module, "engine", test_engine)
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    monkeypatch.setattr(db_module, "IS_POSTGRES", False)
    db_module.init_schema()
    yield db_module


# ---------------------------------------------------------------------------
# db.upsert
# ---------------------------------------------------------------------------

class TestUpsert:
    def test_inserts_new_row(self, test_db):
        with test_db.get_conn() as conn:
            test_db.upsert(conn, "app_meta", ["key"],
                           [{"key": "k1", "value": "v1"}])
            row = conn.execute(
                text("SELECT value FROM app_meta WHERE key = 'k1'")
            ).fetchone()
        assert row[0] == "v1"

    def test_updates_on_conflict(self, test_db):
        with test_db.get_conn() as conn:
            test_db.upsert(conn, "app_meta", ["key"],
                           [{"key": "k1", "value": "v1"}])
            test_db.upsert(conn, "app_meta", ["key"],
                           [{"key": "k1", "value": "v2"}])
            row = conn.execute(
                text("SELECT value FROM app_meta WHERE key = 'k1'")
            ).fetchone()
        assert row[0] == "v2"

    def test_inserts_multiple_rows(self, test_db):
        rows = [{"key": f"k{i}", "value": f"v{i}"} for i in range(5)]
        with test_db.get_conn() as conn:
            test_db.upsert(conn, "app_meta", ["key"], rows)
            count = conn.execute(text("SELECT COUNT(*) FROM app_meta")).scalar()
        assert count == 5

    def test_noop_on_empty_list(self, test_db):
        with test_db.get_conn() as conn:
            test_db.upsert(conn, "app_meta", ["key"], [])
            count = conn.execute(text("SELECT COUNT(*) FROM app_meta")).scalar()
        assert count == 0

    def test_multi_column_conflict_key(self, test_db):
        with test_db.get_conn() as conn:
            test_db.upsert(conn, "market_prices",
                           ["card_name", "set_code", "is_foil"],
                           [{"card_name": "Force of Will", "set_code": "ALL",
                             "is_foil": 0, "price_usd": 50.0,
                             "fetched_at": "2026-04-27"}])
            test_db.upsert(conn, "market_prices",
                           ["card_name", "set_code", "is_foil"],
                           [{"card_name": "Force of Will", "set_code": "ALL",
                             "is_foil": 0, "price_usd": 55.0,
                             "fetched_at": "2026-04-28"}])
            row = conn.execute(
                text("SELECT price_usd FROM market_prices"
                     " WHERE card_name='Force of Will'")
            ).fetchone()
        assert float(row[0]) == pytest.approx(55.0)


# ---------------------------------------------------------------------------
# db.init_schema / _migrate
# ---------------------------------------------------------------------------

class TestSchema:
    def test_all_tables_created(self, test_db):
        with test_db.get_conn() as conn:
            tables = {r[0] for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()}
        assert {"inventory", "market_prices", "price_rows",
                "mtgjson_card_map", "app_meta"} <= tables

    def test_migration_adds_user_id_to_existing_table(self, tmp_path, monkeypatch):
        """_migrate should add user_id when upgrading an old schema."""
        db_path = tmp_path / "old.db"
        old_engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        # Create the old schema without user_id
        with old_engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE inventory (
                    id INTEGER PRIMARY KEY,
                    card_name TEXT NOT NULL,
                    set_code TEXT NOT NULL,
                    quantity INTEGER NOT NULL
                )
            """))
            conn.execute(text(
                "INSERT INTO inventory (card_name, set_code, quantity)"
                " VALUES ('Sol Ring', 'C21', 1)"
            ))

        monkeypatch.setattr(db_module, "engine", old_engine)
        monkeypatch.setattr(db_module, "DB_PATH", db_path)
        monkeypatch.setattr(db_module, "IS_POSTGRES", False)
        db_module.init_schema()

        with old_engine.connect() as conn:
            cols = {r[1] for r in conn.execute(
                text("PRAGMA table_info(inventory)")
            ).fetchall()}
            row = conn.execute(
                text("SELECT user_id FROM inventory WHERE card_name='Sol Ring'")
            ).fetchone()

        assert "user_id" in cols
        assert row[0] == "local"


# ---------------------------------------------------------------------------
# inventory.py — user_id scoping
# ---------------------------------------------------------------------------

_CARD_A = {
    "card_name": "Force of Will", "set_code": "ALL", "set_name": "Alliances",
    "card_number": "1", "quantity": 2, "condition": "NM",
    "printing": "Normal", "language": "English",
    "price_bought": 50.0, "date_bought": "2026-01-01",
}
_CARD_B = {
    "card_name": "Mana Drain", "set_code": "LEG", "set_name": "Legends",
    "card_number": "56", "quantity": 1, "condition": "LP",
    "printing": "Normal", "language": "English",
    "price_bought": 80.0, "date_bought": "2026-02-01",
}


class TestInventoryUserScoping:
    def test_add_one_and_list_all_scoped(self, test_db):
        inv.add_one(_CARD_A, user_id="alice")
        inv.add_one(_CARD_B, user_id="bob")

        alice_rows = inv.list_all("alice")
        bob_rows = inv.list_all("bob")

        assert len(alice_rows) == 1
        assert alice_rows[0]["card_name"] == "Force of Will"
        assert len(bob_rows) == 1
        assert bob_rows[0]["card_name"] == "Mana Drain"

    def test_list_all_empty_for_unknown_user(self, test_db):
        inv.add_one(_CARD_A, user_id="alice")
        assert inv.list_all("nobody") == []

    def test_add_many_scoped(self, test_db):
        inv.add_many([_CARD_A, _CARD_B], user_id="alice")
        inv.add_many([_CARD_A], user_id="bob")

        assert len(inv.list_all("alice")) == 2
        assert len(inv.list_all("bob")) == 1

    def test_list_all_global_returns_all_users(self, test_db):
        inv.add_one(_CARD_A, user_id="alice")
        inv.add_one(_CARD_B, user_id="bob")

        all_rows = inv.list_all_global()
        names = {r["card_name"] for r in all_rows}
        assert names == {"Force of Will", "Mana Drain"}

    def test_stats_scoped_to_user(self, test_db):
        inv.add_many([_CARD_A, _CARD_B], user_id="alice")
        inv.add_one(_CARD_A, user_id="bob")

        alice_stats = inv.stats("alice")
        bob_stats = inv.stats("bob")

        assert alice_stats["printings"] == 2
        assert alice_stats["total_copies"] == 3
        assert bob_stats["printings"] == 1
        assert bob_stats["total_copies"] == 2

    def test_stats_total_cost_float(self, test_db):
        inv.add_one(_CARD_A, user_id="alice")
        s = inv.stats("alice")
        assert isinstance(s["total_cost"], float)
        assert s["total_cost"] == pytest.approx(100.0)

    def test_stats_empty_user_returns_zeros(self, test_db):
        s = inv.stats("nobody")
        assert s == {"printings": 0, "total_copies": 0, "total_cost": 0.0}

    def test_import_csv_replace_only_affects_own_user(self, test_db, tmp_path):
        inv.add_one(_CARD_A, user_id="alice")
        inv.add_one(_CARD_B, user_id="bob")

        csv_content = textwrap.dedent("""\
            Card Name,Set Code,Set Name,Card Number,Quantity,Condition,Printing,Language,Price Bought,Date Bought
            Mox Pearl,LEA,Limited Edition Alpha,1,1,NM,Normal,English,500.0,2026-03-01
        """)
        csv_path = tmp_path / "import.csv"
        csv_path.write_text(csv_content, encoding="utf-8")

        inv.import_csv(str(csv_path), replace=True, user_id="alice")

        alice_rows = inv.list_all("alice")
        bob_rows = inv.list_all("bob")

        assert len(alice_rows) == 1
        assert alice_rows[0]["card_name"] == "Mox Pearl"
        # bob's inventory is untouched
        assert len(bob_rows) == 1
        assert bob_rows[0]["card_name"] == "Mana Drain"

    def test_import_csv_append_adds_to_own_user(self, test_db, tmp_path):
        inv.add_one(_CARD_A, user_id="alice")

        csv_content = textwrap.dedent("""\
            Card Name,Set Code,Set Name,Card Number,Quantity,Condition,Printing,Language,Price Bought,Date Bought
            Mox Pearl,LEA,Limited Edition Alpha,1,1,NM,Normal,English,500.0,2026-03-01
        """)
        csv_path = tmp_path / "import.csv"
        csv_path.write_text(csv_content, encoding="utf-8")

        inv.import_csv(str(csv_path), replace=False, user_id="alice")

        alice_rows = inv.list_all("alice")
        assert len(alice_rows) == 2

    def test_default_user_id_is_local(self, test_db):
        inv.add_one(_CARD_A)
        rows = inv.list_all()
        assert len(rows) == 1
        rows_local = inv.list_all("local")
        assert len(rows_local) == 1

    def test_list_all_price_bought_is_float(self, test_db):
        # price_bought is NUMERIC; list_all() must return float so callers can
        # do arithmetic without backend-specific casts. Use a non-integer value
        # to avoid SQLite's numeric affinity returning int for whole numbers.
        card = {**_CARD_A, "price_bought": 50.75}
        inv.add_one(card, user_id="alice")
        row = inv.list_all("alice")[0]
        assert isinstance(row["price_bought"], float)
        assert row["price_bought"] == pytest.approx(50.75)

    def test_list_all_price_bought_none_stays_none(self, test_db):
        card = {**_CARD_A, "price_bought": None}
        inv.add_one(card, user_id="alice")
        row = inv.list_all("alice")[0]
        assert row["price_bought"] is None

    def test_list_all_includes_id(self, test_db):
        inv.add_one(_CARD_A, user_id="alice")
        row = inv.list_all("alice")[0]
        assert isinstance(row["id"], int)

    def test_delete_removes_only_listed_ids(self, test_db):
        inv.add_many([_CARD_A, _CARD_B], user_id="alice")
        rows = inv.list_all("alice")
        target_id = next(r["id"] for r in rows if r["card_name"] == "Force of Will")

        count = inv.delete([target_id], user_id="alice")

        assert count == 1
        remaining = inv.list_all("alice")
        assert {r["card_name"] for r in remaining} == {"Mana Drain"}

    def test_delete_returns_zero_for_empty_list(self, test_db):
        inv.add_one(_CARD_A, user_id="alice")
        assert inv.delete([], user_id="alice") == 0
        assert len(inv.list_all("alice")) == 1

    def test_delete_does_not_cross_users(self, test_db):
        inv.add_one(_CARD_A, user_id="alice")
        inv.add_one(_CARD_B, user_id="bob")
        bob_id = inv.list_all("bob")[0]["id"]

        # Alice tries to delete one of Bob's rows by guessing the id.
        count = inv.delete([bob_id], user_id="alice")

        assert count == 0
        # Bob's inventory is untouched.
        assert len(inv.list_all("bob")) == 1


# ---------------------------------------------------------------------------
# db.row_to_dict — Decimal handling lives at the engine layer now (a
# psycopg2 type-cast registers `DECIMAL -> float` at engine create time),
# so row_to_dict itself is a thin `dict(row)`. The behaviour we still
# care about is end-to-end: rows fetched via SQLAlchemy come back as a
# regular dict with native Python types.
# ---------------------------------------------------------------------------

class TestRowToDict:
    def test_returns_plain_dict(self):
        """Real SQLAlchemy row → plain dict, preserves values verbatim."""
        db_module.init_schema()
        from sqlalchemy import text as _text
        with db_module.get_conn() as conn:
            conn.execute(_text(
                "INSERT INTO inventory "
                "(user_id, card_name, set_code, quantity, price_bought) "
                "VALUES ('rtd_test', 'Sol Ring', 'C21', 4, 1.25)"
            ))
            row = conn.execute(_text(
                "SELECT card_name, quantity, price_bought "
                "FROM inventory WHERE user_id = 'rtd_test'"
            )).mappings().first()
            try:
                result = db_module.row_to_dict(row)
                assert result["card_name"] == "Sol Ring"
                assert result["quantity"] == 4
                # Float, not Decimal, on both backends (SQLite is native;
                # Postgres is via DEC2FLOAT registered at engine create).
                assert isinstance(result["price_bought"], float)
                assert result["price_bought"] == pytest.approx(1.25)
            finally:
                conn.execute(_text("DELETE FROM inventory WHERE user_id = 'rtd_test'"))
