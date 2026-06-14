"""In-memory registry for background import/price-update jobs.

A process-local ``job_id → status dict`` map guarded by a lock. The
``/market/history/download`` and ``/internal/cron/update-prices`` routes
start at most one long-running MTGJSON import at a time and surface its
progress through ``/market/history/download/status``. State is not
persisted: a worker restart drops in-flight job state, which is acceptable
for these advisory progress views. Per-process — with N gunicorn workers a
job started on one worker isn't visible from another, but the cron/download
flows run a single import cluster-wide so that's fine in practice.
"""
from datetime import UTC, datetime
from threading import Lock

_jobs: dict[str, dict] = {}
_lock = Lock()


def init(job_id: str) -> None:
    with _lock:
        _jobs[job_id] = {
            "id": job_id,
            "state": "running",
            "phase": "Queued",
            "detail": "Waiting to start...",
            "progress": 0,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "error": None,
        }


def update(job_id: str, **updates) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def find_running() -> dict | None:
    """Return a copy of the currently-running job, or None.

    Used to coalesce concurrent download/cron triggers onto the single
    in-flight import rather than starting a second one.
    """
    with _lock:
        running = next((j for j in _jobs.values() if j["state"] == "running"), None)
        return dict(running) if running else None
