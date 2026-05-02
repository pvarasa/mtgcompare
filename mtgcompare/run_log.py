"""Operational tracking of daily price-update runs.

Writes to the `price_update_runs` table. Failures here are logged and
swallowed — bookkeeping must never block the price-update path itself.
"""
import logging
from datetime import date, datetime, timezone

from sqlalchemy import text

from .db import engine

logger = logging.getLogger(__name__)

_INSERT = text("""
    INSERT INTO price_update_runs
        (triggered_at, started_at, status, trigger_source, job_id)
    VALUES
        (:triggered_at, :started_at, 'running', :trigger_source, :job_id)
    RETURNING id
""")

_FINISH = text("""
    UPDATE price_update_runs
       SET status         = :status,
           finished_at    = :finished_at,
           duration_ms    = :duration_ms,
           uuids_streamed = :uuids_streamed,
           rows_inserted  = :rows_inserted,
           market_date    = :market_date,
           error_message  = :error_message
     WHERE id = :id
""")


def record_start(triggered_at: datetime, trigger_source: str, job_id: str) -> int | None:
    """Insert a 'running' row and return its id. Returns None on DB error."""
    try:
        with engine.begin() as conn:
            return conn.execute(_INSERT, {
                "triggered_at": triggered_at,
                "started_at": triggered_at,
                "trigger_source": trigger_source,
                "job_id": job_id,
            }).scalar_one()
    except Exception:
        logger.exception("Failed to record price-update run start")
        return None


def record_finish(
    *,
    run_id: int | None,
    status: str,
    duration_ms: int,
    uuids_streamed: int | None = None,
    rows_inserted: int | None = None,
    market_date: date | None = None,
    error_message: str | None = None,
) -> None:
    """Update an existing run row with terminal state. No-op if run_id is None."""
    if run_id is None:
        return
    try:
        with engine.begin() as conn:
            conn.execute(_FINISH, {
                "id": run_id,
                "status": status,
                "finished_at": datetime.now(timezone.utc),
                "duration_ms": duration_ms,
                "uuids_streamed": uuids_streamed,
                "rows_inserted": rows_inserted,
                "market_date": market_date,
                "error_message": (error_message[:2000] if error_message else None),
            })
    except Exception:
        logger.exception("Failed to record price-update run finish (run_id=%s)", run_id)
