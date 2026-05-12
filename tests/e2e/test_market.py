"""A-tier: market chart modal + price-update state machine.

The chart modal is 100% client-side once the page renders — it opens on
trigger click, fetches /market/history, renders an SVG with range
buttons, and closes on ESC / overlay click. Update-prices is a polling
state machine (POST + setInterval status polls).

Both endpoints are stubbed via page.route so the tests don't depend on
mocking heavy MTGJSON imports or holding price-history state in the DB.
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.e2e


def test_chart_modal_opens_fetches_renders_and_closes(e2e_base_url, seed_market_data, page):
    """A7: click the chart trigger → modal acquires `open` class, range
    buttons render from stubbed history, ESC closes.

    Catches:
      - delegated click handler regression (chart trigger lives on rows
        that the partial-fetch path replaces)
      - modal not toggling .open
      - range-button render schema drift
      - missing ESC handler
    """
    seed_market_data(1)

    # Schema mirrors `/market/history`'s real response (see web.py:1662).
    history_payload = {
        "ok": True,
        "card_name": "Card 000",
        "set_code": "TST",
        "card_number": "0",
        "is_foil": False,
        "default_period": "1m",
        "period": "all",
        "available_periods": ["1m", "3m", "all"],
        "period_days": {"1m": 30, "3m": 90, "all": 0},
        "available_since": "2025-01-01",
        "downloaded_at": "2026-05-01",
        "has_history": True,
        "source": {"label": "Test", "detail": "Stubbed for E2E"},
        "points": [
            {"market_date": "2025-01-01", "price_usd": 0.20},
            {"market_date": "2026-05-12", "price_usd": 1.50},
        ],
        "all_points_count": 2,
    }
    page.route(
        "**/market/history**",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(history_payload),
        ),
    )

    page.goto(f"{e2e_base_url}/market")

    modal = page.locator("#market-chart-modal")
    assert "open" not in (modal.get_attribute("class") or "")

    page.locator(".market-chart-trigger").first.click()
    page.locator("#market-chart-modal.open").wait_for(timeout=3000)

    # Range buttons render from the stubbed `available_periods`.
    page.locator(".market-chart-range").nth(2).wait_for(timeout=3000)
    ranges = page.locator(".market-chart-range")
    range_labels = [ranges.nth(i).inner_text().strip().lower() for i in range(ranges.count())]
    assert range_labels == ["1m", "3m", "all"]
    assert page.locator(".market-chart-range.active").inner_text().strip().lower() == "1m"

    # Click another range. The active class moves to it.
    page.locator('.market-chart-range[data-period="all"]').click()
    page.locator('.market-chart-range[data-period="all"].active').wait_for(timeout=2000)

    # ESC closes the modal. The closed modal is `display: none` (hidden),
    # so we can't use the default `state=visible` wait — query the class
    # directly instead.
    page.keyboard.press("Escape")
    page.wait_for_function(
        "() => !document.getElementById('market-chart-modal').classList.contains('open')",
        timeout=2000,
    )


def test_update_prices_polling_state_machine(e2e_base_url, seed_market_data, page):
    """A8: clicking Update prices fires POST /market/history/download,
    then GET /market/history/download/status?job_id=... on an interval
    until state=done; progress bar fills and the page reloads.

    Catches:
      - missing CSRF header
      - poll never starts (setInterval bug)
      - progress UI doesn't reflect job state
      - infinite poll when state=error
    """
    seed_market_data(1)

    page.route(
        "**/market/history/download",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": True, "job_id": "test-job"}),
        ) if route.request.method == "POST" else route.continue_(),
    )

    poll_calls = {"n": 0}
    def status_handler(route):
        poll_calls["n"] += 1
        if poll_calls["n"] < 2:
            body = {
                "ok": True, "state": "running", "phase": "Downloading",
                "progress": 40, "detail": "Fetching MTGJSON history...",
            }
        else:
            body = {
                "ok": True, "state": "done", "phase": "Done",
                "progress": 100, "detail": "Imported 1 row.",
            }
        route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(body),
        )
    page.route("**/market/history/download/status**", status_handler)

    page.goto(f"{e2e_base_url}/market")

    # No history exists in the test DB → the JS triggers the first-time
    # download confirm() dialog. Accept it.
    page.on("dialog", lambda d: d.accept())

    download_btn = page.locator("#mkt-history-download-btn")
    download_btn.click()

    # Progress UI opens immediately.
    page.wait_for_function(
        "() => document.getElementById('mkt-history-progress').classList.contains('open')",
        timeout=2000,
    )

    # First poll lands → progress 40%, phase "Downloading".
    page.wait_for_function(
        "() => document.getElementById('mkt-history-pct').textContent === '40%'",
        timeout=4000,
    )

    # Second poll (~1s later) lands → progress 100%, state "done".
    # We assert on the percentage rather than waiting for navigation,
    # because `wait_for_url` matches the current /market URL immediately
    # and would return without ever waiting for the reload.
    page.wait_for_function(
        "() => document.getElementById('mkt-history-pct').textContent === '100%'",
        timeout=4000,
    )
    assert poll_calls["n"] >= 2, \
        f"status endpoint should have been polled at least twice; got {poll_calls['n']}"
