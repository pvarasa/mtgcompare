"""A-tier: inventory add flows (single card, paste decklist, CSV import).

The single-card add depends on Scryfall autocomplete + the set <select>
populating from a second Scryfall call. The decklist flow walks
parse -> resolve via /cards/collection -> preview render -> commit POST.
CSV import is a plain multipart upload with replace/append semantics.

Each of these has multiple async failure modes that can't be seen
without a real browser. External Scryfall calls are intercepted via
page.route so the tests are deterministic and offline.
"""
from __future__ import annotations

import json

import pytest

from mtgcompare import inventory as inv

pytestmark = pytest.mark.e2e


def _stub_scryfall_autocomplete(page, names):
    page.route(
        "**api.scryfall.com/cards/autocomplete**",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"data": names}),
        ),
    )


def _stub_scryfall_search(page, prints):
    page.route(
        "**api.scryfall.com/cards/search**",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"data": prints}),
        ),
    )


def _stub_scryfall_collection(page, found, not_found=None):
    page.route(
        "**api.scryfall.com/cards/collection**",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"data": found, "not_found": not_found or []}),
        ),
    )


def test_add_single_card_via_set_dropdown(e2e_base_url, clean_inventory, page):
    """A4: type a card name, set dropdown populates from Scryfall search,
    pick a set, submit, redirect to /inventory with the new row in the DB.

    Catches:
      - autocomplete failing silently (the set <select> never enables)
      - set <select> populated but options missing data-set-name
      - form submitting without set_code / set_name
    """
    _stub_scryfall_autocomplete(page, ["Sol Ring"])
    _stub_scryfall_search(page, [
        {"set": "c21", "set_name": "Commander 2021",
         "collector_number": "260"},
        {"set": "lea", "set_name": "Limited Edition Alpha",
         "collector_number": "270"},
    ])

    page.goto(f"{e2e_base_url}/inventory")

    name_input = page.locator("#add-name")
    name_input.fill("Sol Ring")
    # The `change` event fires populateSets in inventoryadd.js. Tab out to
    # trigger it the way a user would.
    name_input.press("Tab")

    set_select = page.locator("#add-set")
    page.wait_for_function(
        "() => !document.getElementById('add-set').disabled",
        timeout=3000,
    )
    assert set_select.locator("option").count() == 2

    set_select.select_option("C21")

    # Submit drives a server-side redirect to /inventory.
    with page.expect_navigation(url=lambda u: "/inventory" in u):
        page.locator('#add-single button[type="submit"]').click()

    rows = inv.list_all()
    assert len(rows) == 1
    assert rows[0]["card_name"] == "Sol Ring"
    assert rows[0]["set_code"] == "C21"
    assert rows[0]["set_name"] == "Commander 2021"


def test_paste_decklist_resolve_preview_commit(e2e_base_url, clean_inventory, page):
    """A5: paste 2-line decklist, click Resolve (Scryfall /cards/collection
    stubbed), preview table renders editable rows, click Add — and both
    lots land in the DB via /inventory/add-bulk.

    Catches:
      - preview render schema drift
      - commit button never enabling (resolved.length == 0 path)
      - /inventory/add-bulk payload shape changes silently
    """
    _stub_scryfall_collection(page, found=[
        {"name": "Sol Ring", "set": "c21",
         "set_name": "Commander 2021", "collector_number": "260"},
        {"name": "Lightning Bolt", "set": "lea",
         "set_name": "Limited Edition Alpha", "collector_number": "161"},
    ])

    page.goto(f"{e2e_base_url}/inventory")
    page.locator('.add-mode button[data-mode="decklist"]').click()

    page.locator("#decklist-text").fill("1 Sol Ring (C21)\n2 Lightning Bolt (LEA)")
    page.locator("#decklist-resolve").click()

    page.wait_for_function(
        "() => document.querySelectorAll('#decklist-preview tbody tr').length === 2",
        timeout=3000,
    )

    commit_btn = page.locator("#decklist-commit")
    assert not commit_btn.is_disabled()

    with page.expect_navigation(url=lambda u: u.rstrip("/").endswith("/inventory")):
        commit_btn.click()

    rows = sorted(inv.list_all(), key=lambda r: r["card_name"])
    assert len(rows) == 2
    assert rows[0]["card_name"] == "Lightning Bolt"
    assert rows[0]["quantity"] == 2
    assert rows[1]["card_name"] == "Sol Ring"
    assert rows[1]["quantity"] == 1


def test_csv_import_replace_mode(e2e_base_url, clean_inventory, page):
    """A6: upload a tiny CSV with `mode=replace`, redirect lands on
    /inventory, the row count matches the upload. Catches multipart form
    name regressions and the replace-mode default.
    """
    # Pre-seed something the import must clear out in replace mode.
    inv.add_one({
        "card_name": "Stale Card", "set_code": "OLD",
        "set_name": "Outdated", "card_number": "1",
        "quantity": 1, "condition": "NM", "printing": "Normal",
        "language": "English", "price_bought": None, "date_bought": None,
    })
    assert len(inv.list_all()) == 1

    page.goto(f"{e2e_base_url}/inventory")
    page.locator('.add-mode button[data-mode="csv"]').click()

    csv_body = (
        "Card Name,Set Code,Quantity,Condition,Printing,Language\r\n"
        "Black Lotus,LEA,1,NM,Normal,English\r\n"
        "Mox Sapphire,LEA,1,LP,Normal,English\r\n"
    )
    page.locator('#add-csv input[type=file]').set_input_files(
        files=[{
            "name": "inventory.csv",
            "mimeType": "text/csv",
            "buffer": csv_body.encode("utf-8"),
        }],
    )

    with page.expect_navigation(url=lambda u: u.rstrip("/").endswith("/inventory")):
        page.locator('#add-csv button[type="submit"]').click()

    rows = sorted(inv.list_all(), key=lambda r: r["card_name"])
    names = [r["card_name"] for r in rows]
    assert names == ["Black Lotus", "Mox Sapphire"], \
        f"replace mode should have wiped Stale Card; got {names}"
