"""Manual price downloads are a local-app feature: on the hosted site
(Postgres) the daily cron does them, and the routes refuse a visitor."""
import pytest

from mtgcompare import db, jobs, pricing, web


@pytest.fixture
def client():
    with web.app.test_client() as c:
        yield c


def test_local_app_can_start_the_mtgjson_import(client, monkeypatch):
    started = []
    monkeypatch.setattr(pricing, "import_mtgjson_history",
                        lambda rows, progress_cb=None: started.append(rows) or (0, 0))
    resp = client.post("/market/history/download")
    assert resp.status_code == 200 and resp.get_json()["ok"]
    jobs.update(resp.get_json()["job_id"], state="done")


@pytest.mark.parametrize("path", ["/market/history/download", "/sealed/refresh"])
def test_hosted_site_refuses_manual_price_updates(client, monkeypatch, path):
    called = []
    monkeypatch.setattr(pricing, "import_mtgjson_history", lambda *a, **k: called.append(1))
    monkeypatch.setattr(db, "IS_POSTGRES", True)
    resp = client.post(path, headers={"X-User-ID": "u1"})
    assert resp.status_code == 403 and resp.get_json()["ok"] is False
    assert called == []


def test_update_button_flag_comes_from_one_place(client, monkeypatch):
    monkeypatch.setattr(web, "_get_fx", lambda: 150.0)
    assert 'id="mkt-history-download-btn"' in client.get("/market").get_data(as_text=True)
    monkeypatch.setattr(db, "IS_POSTGRES", True)
    with web.app.test_request_context(headers={"X-User-ID": "u1"}):
        flags = {}
        for fn in web.app.template_context_processors[None]:
            flags.update(fn())
    assert flags["allow_price_update"] is False


# --- error messages never echo exception text --------------------------------

def test_unexpected_errors_show_a_reference_not_the_exception(client, monkeypatch):
    from mtgcompare import inventory as inv

    def boom(*_a, **_k):
        raise RuntimeError("psycopg2.OperationalError: password=hunter2 at /app/x.py")

    monkeypatch.setattr(inv, "add_many", boom)
    monkeypatch.setattr(inv, "delete", boom)
    for path, payload in (("/inventory/add-bulk", {"records": [{"card_name": "x"}]}),
                          ("/inventory/delete", {"ids": [1]})):
        resp = client.post(path, json=payload)
        body = resp.get_json()
        assert resp.status_code == 500 and "hunter2" not in body["error"]
        assert "Reference:" in body["error"]


def test_csv_import_explains_a_wrong_file(client, tmp_path):
    import io
    resp = client.post("/inventory/import", data={
        "file": (io.BytesIO(b"Name,Count\nSol Ring,1\n"), "wrong.csv"), "mode": "append",
    }, content_type="multipart/form-data", follow_redirects=True)
    assert "has no &#39;Card Name&#39; column" in resp.get_data(as_text=True)
