"""The daily price-update endpoint the K8s CronJob calls: bearer auth,
coalescing, and that card and sealed updates fail independently."""
import time

import pytest
from sqlalchemy import text

from mtgcompare import db, jobs, pricing, web
from mtgcompare.sealed import service as sealed_service


@pytest.fixture
def cron(monkeypatch):
    monkeypatch.setattr(web, "_CRON_SECRET", _TOKEN)
    with web.app.test_client() as client:
        yield client


_TOKEN = "s3cret"  # noqa: S105 — test-only secret


def _post(client, token=_TOKEN):
    return client.post("/internal/cron/update-prices", headers={"Authorization": f"Bearer {token}"})


def _wait_done(job_id):
    for _ in range(200):
        job = jobs.get(job_id)
        if job and job["state"] != "running":
            return job
        time.sleep(0.01)
    raise AssertionError("job never finished")


def _last_run():
    with db.get_conn() as conn:
        return conn.execute(text(
            "SELECT status, rows_inserted, error_message FROM price_update_runs ORDER BY id DESC LIMIT 1"
        )).mappings().first()


def test_rejects_a_wrong_token(cron):
    client = cron
    assert _post(client, token="nope").status_code == 401  # noqa: S106
    assert client.post("/internal/cron/update-prices").status_code == 401


def test_runs_cards_then_sealed_and_records_the_run(cron, monkeypatch):
    client = cron
    sealed_ran = []
    monkeypatch.setattr(pricing, "run_daily_price_update",
                        lambda progress_cb=None: (3, 120, 40, None))
    monkeypatch.setattr(sealed_service, "run_daily_update", lambda: sealed_ran.append(1))
    body = _post(client).get_json()
    assert body["ok"] and not body["already_running"]
    job = _wait_done(body["job_id"])
    for _ in range(100):  # sealed runs after the job is marked done
        if sealed_ran:
            break
        time.sleep(0.01)
    assert job["state"] == "done" and "120 price points for 3 lot(s)" in job["detail"]
    assert sealed_ran == [1]
    run = _last_run()
    assert run["status"] == "success" and run["rows_inserted"] == 120


def test_card_failure_still_runs_sealed(cron, monkeypatch):
    client = cron
    sealed_ran = []

    def boom(progress_cb=None):
        raise RuntimeError("MTGJSON down")

    monkeypatch.setattr(pricing, "run_daily_price_update", boom)
    monkeypatch.setattr(sealed_service, "run_daily_update", lambda: sealed_ran.append(1))
    job = _wait_done(_post(client).get_json()["job_id"])
    for _ in range(100):
        if sealed_ran:
            break
        time.sleep(0.01)
    assert job["state"] == "error" and sealed_ran == [1]
    run = _last_run()
    assert run["status"] == "failed" and "MTGJSON down" in run["error_message"]


def test_coalesces_onto_a_running_import(cron, monkeypatch):
    client = cron
    jobs.init("already-going")
    try:
        body = _post(client).get_json()
        assert body["already_running"] and body["job_id"] == "already-going"
    finally:
        jobs.update("already-going", state="done")
