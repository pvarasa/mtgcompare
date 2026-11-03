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


def test_chart_series_picker_switches_to_shop_in_yen(e2e_base_url, sealed_env, page):
    today = datetime.now(UTC).date()
    with db_module.get_conn() as conn:
        repo.add_watch(conn, "local", _PRODUCT["uuid"], "t")
        repo.upsert_prices(conn, [
            {"product_uuid": _PRODUCT["uuid"], "source": src, "language": lang, "market_date": d,
             "price": price, "currency": cur, "url": None, "stock": None}
            for src, lang, cur, d, price in [
                ("tcgplayer", "EN", "USD", today - timedelta(days=3), 140.0),
                ("tcgplayer", "EN", "USD", today, 150.0),
                ("hareruya", "JP", "JPY", today - timedelta(days=3), 19800),
                ("hareruya", "JP", "JPY", today, 18900),
            ]
        ])
    page.goto(f"{e2e_base_url}/sealed")
    page.locator("#sealed-watch-table .market-chart-trigger").click()
    page.locator("#market-chart-stage svg").wait_for(timeout=3000)
    assert page.locator("#market-chart-current").inner_text() == "$150.00"

    page.locator("#market-chart-series button", has_text="Hareruya (JP)").click()
    page.locator('#market-chart-series button.active', has_text="Hareruya (JP)").wait_for(timeout=3000)
    page.wait_for_function(
        "() => document.getElementById('market-chart-current').textContent === '¥18,900'", timeout=3000,
    )
    assert page.locator("#market-chart-delta").inner_text() == "-¥900"


def test_remove_lot_asks_then_deletes(e2e_base_url, sealed_env, page):
    """The confirm prompt is wired by data-confirm in common.js (no inline
    onsubmit under the CSP): dismissing keeps the lot, accepting removes it."""
    with db_module.get_conn() as conn:
        repo.add_lot(conn, "local", {"product_uuid": _PRODUCT["uuid"], "language": "EN", "quantity": 1,
                                     "price_bought": None, "currency": "USD", "date_bought": None})
    page.goto(f"{e2e_base_url}/sealed")
    prompts: list[str] = []
    page.once("dialog", lambda d: (prompts.append(d.message), d.dismiss()))
    page.click("#sealed-lots-table .icon-btn")
    assert prompts == ["Remove this lot?"]
    assert page.locator("#sealed-lots-table tbody tr").count() == 1

    page.once("dialog", lambda d: d.accept())
    page.click("#sealed-lots-table .icon-btn")
    page.locator("#sealed-lots-table").wait_for(state="detached", timeout=5000)


def test_refresh_button_runs_job_and_reloads(e2e_base_url, sealed_env, page, monkeypatch):
    """Local app: Refresh prices starts a job through jobs.js, shows its
    progress, then reloads with the job's summary."""
    monkeypatch.setattr(sealed_service, "run_daily_update",
                        lambda progress_cb=None: {"products": 1, "priced": 1, "failed": 0, "shops": {}})
    page.goto(f"{e2e_base_url}/sealed")
    with page.expect_navigation(timeout=8000):
        page.click("#sealed-refresh-btn")
        page.wait_for_function(
            "() => document.getElementById('sealed-refresh-detail').textContent.includes('TCGPlayer: 1 of 1')",
            timeout=5000,
        )
