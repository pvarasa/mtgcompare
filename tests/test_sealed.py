"""Sealed-product tracking: parsers, catalog, repo, service, routes.

All offline: TCGPlayer is replaced by ``FakeClient`` and the DB by the
per-test SQLite engine from ``tests/conftest.py``.
"""
from datetime import date, timedelta

import pytest

from mtgcompare import jobs
from mtgcompare.sealed import catalog, repo, service
from mtgcompare.sealed import tcgplayer as tcg

TODAY = date(2026, 11, 2)

# Shapes captured from the live endpoints on 2026-11-02 (trimmed).
PRICEPOINTS = [
    {"printingType": "Normal", "marketPrice": 535.50, "buylistMarketPrice": None, "listedMedianPrice": 579.20},
    {"printingType": "Foil", "marketPrice": None, "buylistMarketPrice": None, "listedMedianPrice": None},
]
HISTORY = {
    "count": 1,
    "result": [{
        "skuId": "7010571", "variant": "Normal", "language": "English", "condition": "Unopened",
        "buckets": [
            {"marketPrice": "536.23", "quantitySold": "0", "bucketStartDate": "2026-11-01"},
            {"marketPrice": "0", "quantitySold": "0", "bucketStartDate": "2026-10-31"},
            {"marketPrice": "520.10", "quantitySold": "2", "bucketStartDate": "2026-10-04"},
        ],
    }],
}

SETLIST = {"data": [
    {"code": "CMM", "name": "Commander Masters", "releaseDate": "2023-08-04", "sealedProduct": [
        {"uuid": "box-1", "name": "Commander Masters Draft Booster Box", "category": "booster_box",
         "subtype": "draft", "setCode": "CMM", "releaseDate": "2023-08-04",
         "identifiers": {"tcgplayerProductId": "484924"}},
        {"uuid": "pack-1", "name": "Commander Masters Draft Booster Pack", "category": "booster_pack",
         "subtype": "draft", "identifiers": {"tcgplayerProductId": "484923"}},
        {"uuid": "deck-1", "name": "Commander Masters Commander Deck Sliver Swarm", "category": "deck",
         "subtype": "commander", "identifiers": {"tcgplayerProductId": "484933"}},
    ]},
    {"code": "XYZ", "name": "No Sealed"},
    {"code": "OLD", "name": "Old Set", "releaseDate": "1995-01-01", "sealedProduct": [
        {"uuid": "box-old", "name": "Old Set Booster Box", "category": "booster_box", "identifiers": {}},
    ]},
]}


class FakeClient:
    def __init__(self, price=100.0, history=None, fail=False):
        self.price = price
        self.history_points = history if history is not None else {TODAY - timedelta(days=40): 80.0, TODAY - timedelta(days=1): 99.0}
        self.fail = fail
        self.history_calls: list[int] = []
        self.price_calls: list[int] = []

    def market_price(self, product_id):
        self.price_calls.append(product_id)
        if self.fail:
            raise RuntimeError("boom")
        return self.price

    def history(self, product_id):
        self.history_calls.append(product_id)
        return dict(self.history_points)


# --- parsers -----------------------------------------------------------------

def test_parse_pricepoints_takes_normal_printing():
    assert tcg.parse_pricepoints(PRICEPOINTS) == 535.50


@pytest.mark.parametrize("payload", [None, {}, [], [{"printingType": "Normal", "marketPrice": None}],
                                     [{"printingType": "Normal", "marketPrice": 0}]])
def test_parse_pricepoints_missing_or_zero_is_none(payload):
    assert tcg.parse_pricepoints(payload) is None


def test_parse_history_drops_zero_buckets():
    assert tcg.parse_history(HISTORY) == {date(2026, 11, 1): 536.23, date(2026, 10, 4): 520.10}


def test_parse_history_empty_shapes():
    assert tcg.parse_history({"count": 0, "result": []}) == {}
    assert tcg.parse_history(None) == {}


def test_parse_setlist_keeps_boosters_only():
    rows = catalog.parse_setlist(SETLIST, updated_at="t")
    by_uuid = {r["uuid"]: r for r in rows}
    assert set(by_uuid) == {"box-1", "pack-1", "box-old"}
    box = by_uuid["box-1"]
    assert box["tcgplayer_product_id"] == 484924
    assert box["set_code"] == "CMM" and box["set_name"] == "Commander Masters"
    # Falls back to the set's code / release date when the product omits them.
    assert by_uuid["pack-1"]["set_code"] == "CMM"
    assert by_uuid["pack-1"]["release_date"] == "2023-08-04"
    assert by_uuid["box-old"]["tcgplayer_product_id"] is None


# --- service / repo ----------------------------------------------------------

@pytest.fixture
def seeded(test_db):
    service.refresh_catalog(force=True, fetch=lambda: SETLIST)
    return test_db


def test_refresh_catalog_skips_when_fresh(seeded):
    calls = []
    assert service.refresh_catalog(fetch=lambda: calls.append(1) or SETLIST) is None
    assert calls == []


def test_refresh_catalog_upsert_keeps_existing_lots_valid(seeded):
    with seeded.get_conn() as conn:
        repo.add_lot(conn, "u1", {"product_uuid": "box-1", "language": "EN", "quantity": 1,
                                  "price_bought": None, "currency": "USD", "date_bought": None})
    service.refresh_catalog(force=True, fetch=lambda: SETLIST)
    with seeded.get_conn() as conn:
        assert len(repo.list_lots(conn, "u1")) == 1


def test_refresh_prices_backfills_once_then_records_daily(seeded):
    with seeded.get_conn() as conn:
        repo.add_watch(conn, "u1", "box-1", "t")
        repo.add_watch(conn, "u1", "box-old", "t")  # no TCGPlayer id → skipped
    client = FakeClient()
    stats = service.refresh_prices(client=client, today=TODAY, pacing_s=0)
    assert stats == {"products": 1, "priced": 1, "backfilled": 1, "failed": 0}
    assert client.history_calls == [484924]

    client2 = FakeClient(price=110.0)
    service.refresh_prices(client=client2, today=TODAY + timedelta(days=1), pacing_s=0)
    assert client2.history_calls == []  # already backfilled
    with seeded.get_conn() as conn:
        hist = repo.price_history(conn, "box-1", tcg.SOURCE)
    assert [p["price"] for p in hist] == [80.0, 99.0, 100.0, 110.0]
    assert hist[-1]["market_date"] == (TODAY + timedelta(days=1)).isoformat()


def test_refresh_prices_failure_is_counted_not_raised(seeded):
    with seeded.get_conn() as conn:
        repo.add_watch(conn, "u1", "box-1", "t")
    stats = service.refresh_prices(client=FakeClient(fail=True), today=TODAY, pacing_s=0)
    assert stats["failed"] == 1 and stats["priced"] == 0


def test_compute_ctx_values_lots_and_converts_jpy_cost(seeded):
    with seeded.get_conn() as conn:
        repo.add_lot(conn, "u1", {"product_uuid": "box-1", "language": "JP", "quantity": 2,
                                  "price_bought": 15000, "currency": "JPY", "date_bought": "2026-01-01"})
        repo.add_lot(conn, "u1", {"product_uuid": "pack-1", "language": "EN", "quantity": 3,
                                  "price_bought": None, "currency": "USD", "date_bought": None})
        repo.add_lot(conn, "other", {"product_uuid": "box-1", "language": "EN", "quantity": 9,
                                     "price_bought": 1, "currency": "USD", "date_bought": None})
    service.refresh_prices(["box-1"], client=FakeClient(price=120.0), today=TODAY, pacing_s=0)

    ctx = service.compute_sealed_ctx("u1", fx=150.0, today=TODAY)
    lots = {r["uuid"]: r for r in ctx["lots"]}
    assert set(lots) == {"box-1", "pack-1"}  # other user's lot not visible
    box = lots["box-1"]
    assert box["market_usd"] == 120.0 and box["market_jpy"] == 18000
    assert box["cost_usd"] == 200.0   # 2 × ¥15,000 / 150
    assert box["value_usd"] == 240.0
    assert box["pnl_usd"] == 40.0 and box["pnl_pct"] == 20.0
    assert box["reference_mismatch"] is True
    # 30-day change compares against the point on/before today-30 (80.0).
    assert box["change_pct"] == 50.0
    assert lots["pack-1"]["market_usd"] is None and lots["pack-1"]["pnl_usd"] is None

    s = ctx["summary"]
    assert s["total_market_usd"] == 240.0 and s["total_cost_usd"] == 200.0
    assert s["lots_no_market"] == 1 and s["lots_no_cost"] == 1 and s["units_total"] == 5


def test_compute_ctx_jpy_cost_without_fx_is_uncosted(seeded):
    with seeded.get_conn() as conn:
        repo.add_lot(conn, "u1", {"product_uuid": "box-1", "language": "EN", "quantity": 1,
                                  "price_bought": 15000, "currency": "JPY", "date_bought": None})
    ctx = service.compute_sealed_ctx("u1", fx=None, today=TODAY)
    assert ctx["lots"][0]["cost_usd"] is None


# --- routes ------------------------------------------------------------------

@pytest.fixture
def client(seeded, monkeypatch):
    from mtgcompare import web
    monkeypatch.setattr(web, "_get_fx", lambda: 150.0)
    monkeypatch.setattr(tcg, "TcgSealedClient", lambda: FakeClient(price=120.0))
    monkeypatch.setattr(service, "_REQUEST_PACING_S", 0)
    with web.app.test_client() as c:
        yield c


def test_sealed_page_renders_empty(client):
    resp = client.get("/sealed")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Commander Masters Draft Booster Box" in body  # catalog datalist
    assert "No sealed product yet" in body


def test_add_lot_prices_it_immediately_and_renders(client):
    resp = client.post("/sealed/lots", data={
        "product": "commander masters draft booster box", "language": "EN",
        "quantity": "2", "price_bought": "100", "currency": "USD",
    })
    assert resp.status_code == 302
    body = client.get("/sealed").get_data(as_text=True)
    assert "$240.00" in body          # 2 × $120 market value
    assert "+$40.00" in body          # PnL vs $200 cost


def test_add_lot_rejects_unknown_product(client, seeded):
    client.post("/sealed/lots", data={"product": "Nope Box", "quantity": "1"})
    with seeded.get_conn() as conn:
        assert repo.list_lots(conn, "local") == []


def test_delete_lot_is_user_scoped(client, seeded):
    with seeded.get_conn() as conn:
        repo.add_lot(conn, "someone-else", {"product_uuid": "box-1", "language": "EN", "quantity": 1,
                                            "price_bought": None, "currency": "USD", "date_bought": None})
        lot_id = repo.list_lots(conn, "someone-else")[0]["id"]
    client.post(f"/sealed/lots/{lot_id}/delete")
    with seeded.get_conn() as conn:
        assert len(repo.list_lots(conn, "someone-else")) == 1


def test_watch_add_remove_and_history(client, seeded):
    client.post("/sealed/watch", data={"product": "Commander Masters Draft Booster Box"})
    with seeded.get_conn() as conn:
        assert [w["uuid"] for w in repo.list_watch(conn, "local")] == ["box-1"]

    hist = client.get("/sealed/history?uuid=box-1").get_json()
    assert hist["ok"] and hist["has_history"]
    assert hist["points"][-1]["price_usd"] == 120.0
    assert client.get("/sealed/history?uuid=nope").status_code == 404

    client.post("/sealed/watch/delete", data={"product_uuid": "box-1"})
    with seeded.get_conn() as conn:
        assert repo.list_watch(conn, "local") == []


def test_jobs_kinds_do_not_coalesce():
    jobs.init("a-sealed", kind="sealed")
    try:
        assert jobs.find_running() is None or jobs.find_running()["id"] != "a-sealed"
        assert jobs.find_running("sealed")["id"] == "a-sealed"
    finally:
        jobs.update("a-sealed", state="done")
