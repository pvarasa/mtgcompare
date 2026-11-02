"""Sealed page: add a box through the form, see it valued, open its chart.

TCGPlayer is replaced by a fake client and the catalog is seeded directly,
so the test needs no network.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

import mtgcompare.db as db_module
import mtgcompare.web as web_module
from mtgcompare.sealed import repo
from mtgcompare.sealed import service as sealed_service
from mtgcompare.sealed import tcgplayer as tcg

pytestmark = pytest.mark.e2e

_PRODUCT = {
    "uuid": "e2e-box", "set_code": "TST", "set_name": "Test Set",
    "name": "Test Set Play Booster Box", "category": "booster_box", "subtype": "play",
    "release_date": "2026-01-01", "tcgplayer_product_id": 1, "updated_at": "t",
}


class _FakeClient:
    def market_price(self, _pid):
        return 150.0

    def history(self, _pid):
        today = datetime.now(UTC).date()
        return {today - timedelta(days=60): 100.0, today - timedelta(days=7): 140.0}


@pytest.fixture()
def sealed_env(e2e_base_url, monkeypatch):
    with db_module.get_conn() as conn:
        for table in ("sealed_lots", "sealed_watch", "sealed_prices", "sealed_products"):
            conn.execute(text(f"DELETE FROM {table}"))  # noqa: S608
        repo.upsert_products(conn, [_PRODUCT])
    monkeypatch.setattr(web_module, "_get_fx", lambda: 150.0)
    monkeypatch.setattr(tcg, "TcgSealedClient", _FakeClient)
    monkeypatch.setattr(sealed_service, "_REQUEST_PACING_S", 0)
    yield


def test_add_box_shows_value_and_chart(e2e_base_url, sealed_env, page):
    page.goto(f"{e2e_base_url}/sealed")
    page.fill("#sealed-product", _PRODUCT["name"])
    page.fill("input[name=price_bought]", "15000")   # ¥15,000 = $100 at 150
    page.select_option("select[name=currency]", "JPY")
    page.click("#sealed-add-btn")

    row = page.locator("#sealed-lots-table tbody tr").first
    row.wait_for(timeout=5000)
    assert "Test Set Play Booster Box" in row.inner_text()
    assert "$150.00" in row.inner_text()   # market / unit
    assert "+$50.00" in row.inner_text()   # PnL vs $100 cost

    row.locator(".market-chart-trigger").click()
    page.locator("#market-chart-modal.open").wait_for(timeout=3000)
    page.locator("#market-chart-stage svg").wait_for(timeout=3000)
    assert page.locator("#market-chart-current").inner_text() == "$150.00"
    page.keyboard.press("Escape")

    page.click("#sealed-lots-table .icon-btn")  # confirm() auto-dismissed → no delete
    assert page.locator("#sealed-lots-table tbody tr").count() == 1


def test_watch_only_adds_to_watchlist(e2e_base_url, sealed_env, page):
    page.goto(f"{e2e_base_url}/sealed")
    page.fill("#sealed-product", _PRODUCT["name"])
    page.click("#sealed-watch-btn")
    page.locator("#sealed-watch-table tbody tr").first.wait_for(timeout=5000)
    assert page.locator("#sealed-lots-table").count() == 0
