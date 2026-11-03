import shutil
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


def _all_subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from _all_subclasses(sub)


@pytest.fixture(autouse=True)
def _reset_shop_throttles():
    """Scraper throttles are class-level, so a 429 in one test would put
    the shop in back-off for every later test in the session."""
    from mtgcompare.scrapers.html_base import HtmlSearchScrapper
    yield
    for cls in _all_subclasses(HtmlSearchScrapper):
        cls._THROTTLE.reset()


def _sqlite_engine(db_path):
    return create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})


@pytest.fixture(scope="session")
def _schema_template(tmp_path_factory):
    """A migrated empty DB built once; each test copies the file, which is
    ~30x cheaper than running create_all + _migrate per test."""
    path = tmp_path_factory.mktemp("schema") / "template.db"
    mp = pytest.MonkeyPatch()
    mp.setattr(db_module, "engine", _sqlite_engine(path))
    mp.setattr(db_module, "IS_POSTGRES", False)
    db_module.init_schema()
    db_module.engine.dispose()
    mp.undo()
    return path


def _point_db_at(db_path, monkeypatch):
    monkeypatch.setattr(db_module, "engine", _sqlite_engine(db_path))
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    monkeypatch.setattr(db_module, "IS_POSTGRES", False)


@pytest.fixture(autouse=True)
def _isolated_db(request, tmp_path, monkeypatch, _schema_template):
    """Every unit test gets its own SQLite file, whether or not it asks for
    ``test_db``: a test that forgot to was writing to the developer's real
    inventory.db. e2e (its own session DB), pg (a real Postgres) and
    canary (no DB) tests are left alone."""
    if any(request.node.get_closest_marker(m) for m in ("e2e", "pg", "canary")):
        yield
        return
    db_path = tmp_path / "test.db"
    shutil.copyfile(_schema_template, db_path)
    _point_db_at(db_path, monkeypatch)
    yield


@pytest.fixture()
def test_db(_isolated_db):
    """The per-test SQLite DB (always active; request this for the handle)."""
    return db_module
