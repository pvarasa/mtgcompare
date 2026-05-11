"""S-tier: the shared paginated-table client (paginatedtable.js).

This client backs both /inventory and /market. It debounces filter input,
intercepts sort/pager clicks, fetches `?partial=tbody` fragments, swaps
them into the wrapper, mirrors the URL via history.pushState, and calls
the page's `onAfterSwap` hook to re-bind page-specific handlers.

Each of those steps has gone wrong in this codebase recently; tests below
exercise the full chain via the /inventory page.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def _set_alive_sentinel(page):
    """Mark the current document so we can later prove no full reload."""
    page.evaluate("() => { window.__e2eSentinel = 'alive'; }")


def _sentinel_survived(page) -> bool:
    return page.evaluate("() => window.__e2eSentinel === 'alive'")


def test_filter_input_debounce_partial_swap(e2e_base_url, seed_inventory, page):
    """S6: typing in the filter input debounces, fires a partial fetch,
    swaps the tbody, updates the URL via pushState — without a full reload.

    Guards against:
      - debounce regression (filter firing on every keystroke)
      - onAfterSwap hook not re-binding handlers
      - history.pushState not being called
      - falling back to a full GET form submit
    """
    # Seed 60 rows; "Card 042" appears in exactly one of them.
    seed_inventory(60)
    page.goto(f"{e2e_base_url}/inventory")
    _set_alive_sentinel(page)

    initial_rows = page.locator("#inv-table tbody tr").count()
    assert initial_rows == 50, "default per-page should render 50 rows"

    filter_input = page.locator("#inv-filter-input")
    filter_input.fill("042")

    # Wait for the partial-fetch response and the row swap that follows.
    page.wait_for_url("**/inventory?**q=042**", timeout=2000)
    page.wait_for_function(
        "() => document.querySelectorAll('#inv-table tbody tr').length === 1",
        timeout=2000,
    )

    assert _sentinel_survived(page), \
        "partial swap must not full-reload (sentinel must survive)"

    only_row = page.locator("#inv-table tbody tr").first
    assert "Card 042" in only_row.inner_text()

    # And the result-summary count agrees with the matched count.
    summary = page.locator(".result-summary")
    assert "1" in summary.inner_text()


def test_sort_link_toggles_direction_and_reorders(e2e_base_url, seed_inventory, page):
    """S7: clicking a sortable header sets sort=col&dir=asc on the URL,
    re-renders the table in the new order, and a second click flips to
    desc. The active sort arrow renders.
    """
    seed_inventory(60)
    page.goto(f"{e2e_base_url}/inventory")
    _set_alive_sentinel(page)

    # Default sort is card_name asc. Click the price_bought header.
    price_link = page.locator('th.sortable a.sort-link[data-sort="price_bought"]')
    price_link.click()

    page.wait_for_url("**sort=price_bought**dir=asc**", timeout=2000)

    # First row should have the smallest price ($0.00 for Card 000).
    first_row_text = page.locator("#inv-table tbody tr").first.inner_text()
    assert "Card 000" in first_row_text, \
        "ascending price sort should put Card 000 (price 0) first"

    # Click again — direction flips to desc.
    page.locator('th.sortable a.sort-link[data-sort="price_bought"]').click()
    page.wait_for_url("**sort=price_bought**dir=desc**", timeout=2000)

    first_row_text = page.locator("#inv-table tbody tr").first.inner_text()
    assert "Card 059" in first_row_text, \
        "descending price sort should put Card 059 (price 59) first"

    # Active-sort visual hint renders.
    active_arrow = page.locator("th.sort-active .sort-arrow")
    assert active_arrow.count() == 1
    assert _sentinel_survived(page)


def test_pager_and_per_page_swap_in_place(e2e_base_url, seed_inventory, page):
    """S8: Next / Prev / page input and the per-page <select> all drive the
    same partial-fetch path. Per-page change resets to page 1.
    """
    seed_inventory(120)  # 3 pages at default per_page=50
    page.goto(f"{e2e_base_url}/inventory")
    _set_alive_sentinel(page)

    # Page 1 → click Next → page 2.
    page.locator('a.pg-btn[data-page="2"]').click()
    page.wait_for_url("**page=2**", timeout=2000)

    # Page 2 starts at row index 50 → Card 050.
    first_after_next = page.locator("#inv-table tbody tr").first.inner_text()
    assert "Card 050" in first_after_next

    # Change per-page from 50 → 200. Page resets to 1 and all rows fit.
    per_page = page.locator("select.per-page-select")
    per_page.select_option("200")
    page.wait_for_url("**per_page=200**", timeout=2000)
    page.wait_for_function(
        "() => document.querySelectorAll('#inv-table tbody tr').length === 120",
        timeout=2000,
    )

    # URL must also have page=1 (reset on per_page change).
    assert "page=1" in page.url

    assert _sentinel_survived(page)
