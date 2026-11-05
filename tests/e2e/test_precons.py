"""Precon / Secret Lair → inventory, in a real browser.

Two entry points share one preview: the inventory's "Precon / Secret Lair"
add mode (search → load → edit → add), and the sealed page's Open button
on a held precon lot (preloaded; adding consumes the lot). MTGJSON's deck
file is served from a trimmed fixture via a patched ``decks.fetch_deck``
(the server runs in this process), so no network is needed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

import mtgcompare.db as db_module
from mtgcompare import inventory as inv
from mtgcompare.sealed import decks, repo

pytestmark = pytest.mark.e2e

_DECK = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "mtgjson_deck_virtue_and_valor.json").read_text(encoding="utf-8")
)
_NAME = "Wilds of Eldraine Commander Deck Virtue and Valor"
_PRODUCT = {
    "uuid": "e2e-precon", "set_code": "WOC", "set_name": "Wilds of Eldraine Commander",
    "name": _NAME, "category": "deck", "subtype": "commander",
    "release_date": "2023-09-08", "tcgplayer_product_id": None,
    "contents": json.dumps({"decks": ["VirtueAndValor_WOC"], "cards": []}), "updated_at": "t",
}
# Commander + three non-basic main-deck cards in the fixture.
_NON_BASIC_LINES = 4


@pytest.fixture()
def precon_env(e2e_base_url, clean_inventory, monkeypatch):
    with db_module.get_conn() as conn:
        for table in ("sealed_lots", "sealed_watch", "sealed_prices", "sealed_products"):
            conn.execute(text(f"DELETE FROM {table}"))  # noqa: S608
        repo.upsert_products(conn, [_PRODUCT])
    monkeypatch.setattr(decks, "fetch_deck", lambda _name: _DECK)
    decks.cache_clear()
    yield
    decks.cache_clear()


def _wait_preview_rows(page, n: int) -> None:
    page.wait_for_function(
        f"() => document.querySelectorAll('#precon-preview tbody tr').length === {n}",
        timeout=5000,
    )


def test_precon_mode_search_load_edit_add(e2e_base_url, precon_env, page):
    page.goto(f"{e2e_base_url}/inventory")
    page.locator('.add-mode button[data-mode="precon"]').click()

    # Load before picking a product is refused, not sent.
    page.locator("#precon-load").click()
    assert "Pick a product" in page.locator("#precon-status").inner_text()
    assert page.locator("#precon-commit").is_disabled()

    page.locator("#precon-product").fill("virtue")
    page.wait_for_function(
        "() => document.querySelectorAll('#precon-suggest option').length === 1", timeout=5000,
    )
    page.locator("#precon-product").fill(_NAME)
    page.locator("#precon-load").click()
    _wait_preview_rows(page, _NON_BASIC_LINES)
    assert "basic land(s) skipped" in page.locator("#precon-status").inner_text()

    # Edits in the preview are what gets added.
    page.locator("#precon-preview tbody tr").first.locator(".cell-qty").fill("2")
    with page.expect_navigation(url=lambda u: u.rstrip("/").endswith("/inventory")):
        page.locator("#precon-commit").click()

    rows = {r["card_name"]: r for r in inv.list_all()}
    assert len(rows) == _NON_BASIC_LINES
    commander = rows["Ellivere of the Wild Court"]
    assert commander["quantity"] == 2 and commander["printing"] == "Foil"
    assert commander["set_code"] == "WOC" and commander["card_number"] == "2"


def test_open_sealed_lot_preloads_and_consumes_it(e2e_base_url, precon_env, page):
    with db_module.get_conn() as conn:
        repo.add_lot(conn, "local", {"product_uuid": "e2e-precon", "language": "JP", "quantity": 1,
                                     "price_bought": None, "currency": "JPY", "date_bought": None})

    page.goto(f"{e2e_base_url}/sealed")
    with page.expect_navigation(url=lambda u: "open_lot=" in u):
        page.locator("#sealed-lots-table a.open-btn").click()

    # Precon mode is preselected and the deck loads without a click.
    assert page.locator("#add-precon").is_visible()
    _wait_preview_rows(page, _NON_BASIC_LINES)
    assert "Opening one" in page.locator("#precon-note").inner_text()
    assert page.locator("#precon-language").input_value() == "JP"

    with page.expect_navigation(url=lambda u: u.rstrip("/").endswith("/inventory")):
        page.locator("#precon-commit").click()

    assert len(inv.list_all()) == _NON_BASIC_LINES
    assert {r["language"] for r in inv.list_all()} == {"Japanese"}
    with db_module.get_conn() as conn:
        assert repo.list_lots(conn, "local") == []
