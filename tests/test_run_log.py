"""Tests for run_log.py — recording start/finish of daily price-update runs."""
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, text

import mtgcompare.db as db_module
import mtgcompare.run_log as run_log_module


@pytest.fixture()
def test_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    test_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(db_module, "engine", test_engine)
    monkeypatch.setattr(run_log_module, "engine", test_engine)
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    monkeypatch.setattr(db_module, "IS_POSTGRES", False)
    db_module.init_schema()
    return test_engine


def _row(engine, run_id):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT * FROM price_update_runs WHERE id = :id"),
            {"id": run_id},
        ).mappings().one()


def test_record_start_inserts_running_row(test_db):
    triggered = datetime(2026, 5, 2, 22, 0, 0, tzinfo=UTC)

    run_id = run_log_module.record_start(triggered, "cron", "abc123")

    assert isinstance(run_id, int)
    row = _row(test_db, run_id)
    assert row["status"] == "running"
    assert row["trigger_source"] == "cron"
    assert row["job_id"] == "abc123"
    assert row["finished_at"] is None
    assert row["rows_inserted"] is None


def test_record_finish_success_updates_all_fields(test_db):
    triggered = datetime(2026, 5, 2, 22, 0, 0, tzinfo=UTC)
    run_id = run_log_module.record_start(triggered, "cron", "abc123")

    run_log_module.record_finish(
        run_id=run_id,
        status="success",
        duration_ms=12345,
        uuids_streamed=70000,
        rows_inserted=146000,
        market_date=date(2026, 5, 2),
    )

    row = _row(test_db, run_id)
    assert row["status"] == "success"
    assert row["duration_ms"] == 12345
    assert row["uuids_streamed"] == 70000
    assert row["rows_inserted"] == 146000
    assert str(row["market_date"]) == "2026-05-02"
    assert row["finished_at"] is not None
    assert row["error_message"] is None


def test_record_finish_failed_records_error_message(test_db):
    triggered = datetime(2026, 5, 2, 22, 0, 0, tzinfo=UTC)
    run_id = run_log_module.record_start(triggered, "cron", "abc123")

    run_log_module.record_finish(
        run_id=run_id,
        status="failed",
        duration_ms=1500,
        error_message="MTGJSON returned 503",
    )

    row = _row(test_db, run_id)
    assert row["status"] == "failed"
    assert row["error_message"] == "MTGJSON returned 503"
    assert row["rows_inserted"] is None


def test_record_finish_truncates_long_error(test_db):
    triggered = datetime(2026, 5, 2, 22, 0, 0, tzinfo=UTC)
    run_id = run_log_module.record_start(triggered, "cron", "abc123")
    huge = "x" * 5000

    run_log_module.record_finish(
        run_id=run_id, status="failed", duration_ms=1, error_message=huge,
    )

    row = _row(test_db, run_id)
    assert len(row["error_message"]) == 2000


def test_record_finish_with_none_id_is_noop(test_db):
    # record_start may return None on DB error; record_finish must not crash.
    run_log_module.record_finish(run_id=None, status="success", duration_ms=0)


def test_record_start_swallows_db_errors(monkeypatch, caplog):
    # Force an engine that raises on connect.
    class BoomEngine:
        def begin(self):
            raise RuntimeError("DB exploded")
    monkeypatch.setattr(run_log_module, "engine", BoomEngine())

    triggered = datetime(2026, 5, 2, 22, 0, 0, tzinfo=UTC)
    with caplog.at_level("ERROR", logger="mtgcompare.run_log"):
        run_id = run_log_module.record_start(triggered, "cron", "xyz")

    assert run_id is None
    assert any("Failed to record price-update run start" in r.message for r in caplog.records)
