"""Fixtures for Playwright-driven end-to-end tests.

The Flask app runs in a background thread on a free port, pointed at a
temp SQLite database. Per-test isolation comes from the `clean_inventory`
fixture truncating the inventory table between tests.

Run with:
    uv run --group e2e pytest -m e2e

First run only:
    uv run --group e2e playwright install chromium
"""
from __future__ import annotations

import socket
import threading
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from werkzeug.serving import make_server

import mtgcompare.db as db_module
import mtgcompare.web as web_module
from mtgcompare import inventory as inv
from mtgcompare.web import app as flask_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def e2e_base_url(tmp_path_factory) -> str:
    """Start the Flask app on a free port against a temp SQLite DB.

    Session-scoped so the server boots once. Per-test data isolation is
    handled by `clean_inventory` below.
    """
    db_path = tmp_path_factory.mktemp("e2e") / "e2e.db"
    test_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    mp = pytest.MonkeyPatch()
    mp.setattr(db_module, "engine", test_engine)
    mp.setattr(db_module, "DB_PATH", db_path)
    mp.setattr(db_module, "IS_POSTGRES", False)
    db_module.init_schema()

    flask_app.config["WTF_CSRF_ENABLED"] = False
    flask_app.config["TESTING"] = True

    port = _free_port()
    server = make_server("127.0.0.1", port, flask_app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://127.0.0.1:{port}"

    server.shutdown()
    thread.join(timeout=5)
    mp.undo()


@pytest.fixture()
def clean_inventory(e2e_base_url):
    """Reset all per-user state before each test: inventory + market_prices.

    Also clears the in-process market computation cache so the next /market
    request rebuilds from the fresh DB rows.
    """
    with db_module.get_conn() as conn:
        conn.execute(text("DELETE FROM inventory"))
        conn.execute(text("DELETE FROM market_prices"))
    web_module.market_cache_clear()
    yield


@pytest.fixture()
def seed_inventory(clean_inventory) -> Callable[[int], int]:
    """Insert N synthetic inventory rows for the default 'local' user.

    Card names sort lexicographically; set_code is 'TST' for all rows so
    set-filter tests stay deterministic.
    """
    def _seed(count: int) -> int:
        for i in range(count):
            inv.add_one({
                "card_name":   f"Card {i:03d}",
                "set_code":    "TST",
                "set_name":    "Test Set",
                "card_number": str(i),
                "quantity":    1,
                "condition":   "NM",
                "printing":    "Normal",
                "language":    "English",
                "price_bought": float(i),
                "date_bought": "2026-01-01",
            })
        return count
    return _seed


@pytest.fixture()
def seed_market_data(clean_inventory, monkeypatch):
    """Seed N inventory rows AND matching market_prices rows so the /market
    page enters the `has_cache=True` branch (which renders the chart-trigger
    JS and the modal).

    Also pins _get_fx() to a constant so the page doesn't depend on Yahoo
    Finance reachability.
    """
    monkeypatch.setattr(web_module, "_fx", 150.0)
    monkeypatch.setattr(web_module, "_get_fx", lambda: 150.0)

    def _seed(count: int = 1) -> int:
        now_iso = datetime.now(UTC).isoformat(timespec="seconds")
        for i in range(count):
            inv.add_one({
                "card_name":    f"Card {i:03d}",
                "set_code":     "TST",
                "set_name":     "Test Set",
                "card_number":  str(i),
                "quantity":     1,
                "condition":    "NM",
                "printing":     "Normal",
                "language":     "English",
                "price_bought": 1.00,
                "date_bought":  "2026-01-01",
            })
        rows = [
            {
                "card_name":  f"Card {i:03d}",
                "set_code":   "TST",
                "is_foil":    0,
                "price_usd":  2.50,
                "fetched_at": now_iso,
            }
            for i in range(count)
        ]
        with db_module.get_conn() as conn:
            db_module.upsert(
                conn, "market_prices",
                ["card_name", "set_code", "is_foil"],
                rows,
            )
        web_module.market_cache_clear()
        return count
    yield _seed
    web_module.market_cache_clear()


# Playwright's default `browser_context_args` ignores HTTPS errors and sets
# a viewport; override here if needed. Keeping defaults for now.
