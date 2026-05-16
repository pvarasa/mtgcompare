import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# CSRF protection is on by default in production but disabled in unit tests
# so existing form-POST tests don't need to pre-fetch a token. The single
# `test_csrf_protection_blocks_unauthenticated_post` test re-enables it
# explicitly to verify the protection actually works.
import mtgcompare.db as db_module  # noqa: E402
from mtgcompare.web import app as _app  # noqa: E402

_app.config["WTF_CSRF_ENABLED"] = False


@pytest.fixture()
def test_db(tmp_path, monkeypatch):
    """Redirect db.engine/db.DB_PATH/db.IS_POSTGRES at a fresh per-test
    SQLite file so DB-layer tests never touch the real inventory.db."""
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
