"""S-tier: inventory selection UI.

These tests would have caught the two bugs in v1.6.5:

  - banner-always-visible (CSS specificity: .select-banner display:flex
    overrode the [hidden] attribute)
  - selection handlers never wired up (<script defer> on inline scripts
    is ignored, so the inline IIFE ran before paginatedtable.js had
    loaded and window.mtgcompare was undefined)

Both are pure-frontend; Python-only tests can't see them.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_banner_hidden_on_initial_load(e2e_base_url, seed_inventory, page):
    """Regression: the .select-banner CSS class set display:flex which
    overrode the browser's [hidden] { display:none } user-agent rule, so
    the banner was visible on load with nothing selected.
    """
    seed_inventory(75)
    page.goto(f"{e2e_base_url}/inventory")

    banner = page.locator("#inv-select-banner")
    assert banner.count() == 1, "banner element should exist in the DOM"
    assert banner.is_hidden(), "banner must be hidden until the user selects rows"


def test_top_checkbox_wires_up_selection(e2e_base_url, seed_inventory, page):
    """Regression: the inline <script defer> at the bottom of inventory.html
    had `defer` silently ignored (HTML spec: defer applies only to scripts
    with src). It ran during parsing, before the deferred external
    paginatedtable.js had loaded, so window.mtgcompare.attachPaginatedTable
    was undefined and the selection IIFE bailed — no handlers attached.

    The proof JS is running: clicking the master checkbox must check every
    row checkbox AND enable the Delete button AND show the banner.
    """
    seed_inventory(75)
    page.goto(f"{e2e_base_url}/inventory")

    delete_btn = page.locator("#inv-delete")
    assert delete_btn.is_disabled(), "Delete starts disabled with nothing selected"

    page.locator("#inv-check-all").check()

    row_cbs = page.locator("#inv-table tbody tr input[type=checkbox]")
    n = row_cbs.count()
    assert n == 50, f"expected the default page of 50 rows, got {n}"
    for i in range(n):
        assert row_cbs.nth(i).is_checked(), f"row {i} should be checked"

    assert not delete_btn.is_disabled()
    assert "50" in delete_btn.inner_text()

    banner = page.locator("#inv-select-banner")
    assert banner.is_visible()
    assert page.locator('[data-banner-state="page"]').is_visible()
    assert page.locator('[data-banner-state="all"]').is_hidden()


def test_virtual_selection_mode_toggles_and_collapses(e2e_base_url, seed_inventory, page):
    """End-to-end of the Gmail-style virtual-selection flow.

    Catches:
      - banner state machine (page-state vs all-state visibility)
      - toolbar count + Delete button label reflect virtual count
      - Export buttons disabled with tooltip in virtual mode
      - clicking a row checkbox collapses virtual mode back to page mode
    """
    seed_inventory(75)
    page.goto(f"{e2e_base_url}/inventory")

    page.locator("#inv-check-all").check()
    page.locator("#inv-select-all-matching").click()

    page_state = page.locator('[data-banner-state="page"]')
    all_state  = page.locator('[data-banner-state="all"]')
    assert page_state.is_hidden()
    assert all_state.is_visible()
    assert "75" in all_state.inner_text()

    sel_count = page.locator("#inv-sel-count").inner_text()
    assert "75" in sel_count and "all matching" in sel_count

    deck_btn = page.locator("#inv-export-deck")
    csv_btn  = page.locator("#inv-export-csv")
    assert deck_btn.is_disabled()
    assert csv_btn.is_disabled()
    tooltip = deck_btn.get_attribute("title") or ""
    assert "Clear selection" in tooltip

    delete_btn = page.locator("#inv-delete")
    assert "75" in delete_btn.inner_text()

    # Clicking any row checkbox collapses virtual mode. With one box
    # toggled off the page-state banner condition (all 50 checked + more
    # off-page) is no longer satisfied, so the whole banner hides.
    page.locator("#inv-table tbody tr input[type=checkbox]").first.click()

    assert all_state.is_hidden(), "virtual-state banner must collapse"
    assert page.locator("#inv-select-banner").is_hidden(), \
        "banner hides entirely when fewer than the full page is checked"
    assert "selected on this page" in page.locator("#inv-sel-count").inner_text()


def test_per_page_delete_removes_only_checked_rows(e2e_base_url, seed_inventory, page):
    """S4: tick two row checkboxes, accept the confirm(), the POST goes out
    with the correct id list, those two rows disappear from the DB.

    Guards against:
      - regressions in the id-collection from data-id attributes
      - missing CSRF header
      - wrong endpoint URL
      - reload-after-delete behaviour
    """
    from sqlalchemy import text

    import mtgcompare.db as db_module
    from mtgcompare import inventory as inv

    seed_inventory(5)
    page.goto(f"{e2e_base_url}/inventory")

    rows_before = inv.list_all()
    assert len(rows_before) == 5
    # Pick two specific lots to delete; the table is sorted by card_name asc
    # by default, so the first two rendered rows correspond to Card 000/001.
    target_ids = {rows_before[0]["id"], rows_before[1]["id"]}

    row_cbs = page.locator("#inv-table tbody tr input[type=checkbox]")
    row_cbs.nth(0).check()
    row_cbs.nth(1).check()

    delete_btn = page.locator("#inv-delete")
    assert "2" in delete_btn.inner_text()

    page.once("dialog", lambda d: d.accept())
    # The handler calls window.location.reload() on success; waiting for
    # the next response to /inventory completing is the most reliable way
    # to know the reload finished.
    with page.expect_response(lambda r: r.url.endswith("/inventory") and r.status == 200):
        delete_btn.click()

    with db_module.get_conn() as conn:
        remaining = {row[0] for row in conn.execute(text("SELECT id FROM inventory"))}
    assert target_ids.isdisjoint(remaining), \
        "the two checked rows should no longer be in the DB"
    assert len(remaining) == 3


def test_virtual_delete_requires_typed_DELETE_above_threshold(e2e_base_url, seed_inventory, page):
    """S5: in virtual-selection mode with >100 matching rows, Delete fires
    a prompt() that requires the literal string DELETE. Anything else
    (cancel, wrong word) must abort the delete.

    Guards against:
      - threshold logic regression (TYPED_CONFIRM_THRESHOLD)
      - wrong payload shape (must be {match: {...}} not {ids: [...]})
      - missing typed-confirm safety
    """
    from sqlalchemy import text

    import mtgcompare.db as db_module
    from mtgcompare import inventory as inv

    seed_inventory(150)  # above the 100-row threshold
    page.goto(f"{e2e_base_url}/inventory")

    page.locator("#inv-check-all").check()
    page.locator("#inv-select-all-matching").click()

    delete_btn = page.locator("#inv-delete")
    assert "150" in delete_btn.inner_text()

    # First attempt: dismiss the prompt — must NOT delete anything.
    page.once("dialog", lambda d: d.dismiss())
    delete_btn.click()
    # Brief settle; if a request fires, this is enough to catch it.
    page.wait_for_timeout(200)
    assert len(inv.list_all()) == 150, "dismissed prompt must not delete"

    # Second attempt: type the wrong word — must NOT delete.
    page.once("dialog", lambda d: d.accept("delete"))  # lowercase, wrong
    delete_btn.click()
    page.wait_for_timeout(200)
    assert len(inv.list_all()) == 150, "wrong-typed confirmation must not delete"

    # Third attempt: type DELETE exactly — full wipe.
    page.once("dialog", lambda d: d.accept("DELETE"))
    with page.expect_response(lambda r: r.url.endswith("/inventory") and r.status == 200):
        delete_btn.click()

    with db_module.get_conn() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM inventory")).scalar()
    assert count == 0, "after typed DELETE all rows should be gone"
