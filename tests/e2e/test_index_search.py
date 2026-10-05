"""A-tier: search-page mode toggle, shipping override, shop filter chips.

The single-card / decklist mode toggle, shipping toggle, and shop-filter
chips are pure-JS state-coupled UI; a regression here silently sends the
wrong query to the server. Server tests can't see any of it.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_mode_toggle_shows_and_hides_panels(e2e_base_url, page):
    """A1: clicking the mode buttons toggles `active` class and panel
    visibility. The forms in the hidden panel must not be reachable to the
    user, so their submit doesn't fire by accident.
    """
    page.goto(f"{e2e_base_url}/")

    single_btn = page.locator("#mode-single")
    decklist_btn = page.locator("#mode-decklist")
    single_panel = page.locator("#panel-single")
    decklist_panel = page.locator("#panel-decklist")

    # Defaults: single mode active.
    assert "active" in (single_btn.get_attribute("class") or "")
    assert "active" not in (decklist_btn.get_attribute("class") or "")
    assert single_panel.is_visible()
    assert decklist_panel.is_hidden()

    decklist_btn.click()
    assert "active" not in (single_btn.get_attribute("class") or "")
    assert "active" in (decklist_btn.get_attribute("class") or "")
    assert single_panel.is_hidden()
    assert decklist_panel.is_visible()

    single_btn.click()
    assert single_panel.is_visible()
    assert decklist_panel.is_hidden()


def test_shipping_toggle_syncs_hidden_input_and_panel(e2e_base_url, page):
    """A2: the user-facing checkbox toggles the hidden `shipping` input
    that actually gets submitted. Without sync, the user thinks they're
    enabling shipping but the server sees `shipping=0`.

    Asserts the form actually serializes the right values by waiting for
    the navigation triggered by submit and reading the URL.
    """
    page.goto(f"{e2e_base_url}/?q=Sol+Ring")

    hidden = page.locator("#shipping-val")
    toggle = page.locator("#ship-toggle")
    panel  = page.locator("#ship-cfg-panel")

    assert hidden.input_value() == "0"
    assert panel.is_hidden()

    toggle.check()
    assert hidden.input_value() == "1"
    assert panel.is_visible()

    toggle.uncheck()
    assert hidden.input_value() == "0"
    assert panel.is_hidden()

    # And the value reaches the server on submit. Enable + submit, then
    # check the URL after navigation includes shipping=1.
    toggle.check()
    with page.expect_navigation(url=lambda u: "shipping=1" in u):
        page.locator('#panel-single button[type="submit"]').click()


def test_shop_filter_chips_select_all_none_and_submit(e2e_base_url, page):
    """A3: filter-shops toggle reveals the panel, sets the hidden flag to 1,
    and the per-shop checkboxes drive the summary + the submitted query.
    All / None bulk buttons must check or uncheck every chip.
    """
    page.goto(f"{e2e_base_url}/?q=Sol+Ring")

    # Restrict locators to the single-card panel — decklist has its own
    # copy of the chips.
    panel = page.locator("#panel-single")
    flag = panel.locator('[data-shop-filter-flag]')
    toggle = panel.locator('[data-shop-filter-toggle]')
    chips_panel = panel.locator('[data-shop-filter-panel]')
    chips = panel.locator('[data-shop-filter-checkbox]')
    summary = panel.locator('[data-shop-filter-summary]')

    assert flag.input_value() == "0"
    assert chips_panel.is_hidden()

    toggle.check()
    assert flag.input_value() == "1"
    assert chips_panel.is_visible()
    total = chips.count()
    assert total > 0
    assert "of" in summary.inner_text() or "all" in summary.inner_text()

    panel.locator('[data-shop-filter-none]').click()
    assert "0 of " in summary.inner_text()
    for i in range(total):
        assert not chips.nth(i).is_checked()

    panel.locator('[data-shop-filter-all]').click()
    assert f"all {total}" in summary.inner_text()
    for i in range(total):
        assert chips.nth(i).is_checked()

    # Uncheck one, submit; URL must keep `shop_filter=1` and carry the
    # remaining shop_<slug>=1 keys, *without* the unchecked one.
    first_name = chips.nth(0).get_attribute("name")
    chips.nth(0).uncheck()
    with page.expect_navigation(url=lambda u: "shop_filter=1" in u):
        panel.locator('button[type="submit"]').click()
    final_url = page.url
    assert f"{first_name}=1" not in final_url, \
        f"unchecked shop {first_name} should not be in the submitted URL"


def _printing_rows():
    base = {"card": "Force of Will", "price_usd": 100.0, "stock": 1,
            "condition": "NM", "link": "https://example.test/"}
    return [
        {**base, "shop": "Hareruya", "set": "DMR", "number": "50", "variant": "", "price_jpy": 14000.0},
        {**base, "shop": "Card Rush", "set": "DMR", "number": "418",
         "variant": "fullart,borderless", "price_jpy": 15000.0},
        {**base, "shop": "TokyoMTG", "set": "DMR", "number": None, "variant": None, "price_jpy": 13000.0},
        {**base, "shop": "Cardshop Serra", "set": "2XM", "number": "340",
         "variant": "fullart,borderless", "price_jpy": 50000.0},
    ]


@pytest.fixture
def stub_search(monkeypatch):
    from mtgcompare import web as web_module
    seen: list[str] = []

    def fake_collect(name, fx, **kw):
        seen.append(name)
        return [dict(r) for r in _printing_rows()]

    monkeypatch.setattr(web_module, "collect_prices", fake_collect)
    monkeypatch.setattr(web_module, "_get_fx", lambda: 150.0)
    return seen


def _visible_shops(page):
    return page.locator("#results-table tbody tr:visible td:first-child").all_inner_texts()


def test_printing_filters_narrow_rows_and_move_the_cheapest_badge(e2e_base_url, page, stub_search):
    """Version chips + set select filter client-side; "?" rows stay visible
    under a version filter; the cheapest badge follows the first visible row."""
    page.goto(f"{e2e_base_url}/?q=Force+of+Will")
    assert len(_visible_shops(page)) == 4

    page.locator('[data-filter-variant="fullart"]').click()
    shops = _visible_shops(page)
    assert len(shops) == 3
    assert not any("Hareruya" in s for s in shops)          # regular hidden
    assert any("TokyoMTG" in s for s in shops)              # unknown kept
    assert "cheapest" in shops[0] and "TokyoMTG" in shops[0]

    page.locator("[data-filter-set]").select_option("2XM")
    shops = _visible_shops(page)
    assert len(shops) == 1 and "Cardshop Serra" in shops[0] and "cheapest" in shops[0]

    page.locator('[data-filter-variant=""]').click()
    page.locator("[data-filter-set]").select_option("")
    assert len(_visible_shops(page)) == 4


def test_query_filter_tokens_search_the_bare_name_and_preselect(e2e_base_url, page, stub_search):
    page.goto(f"{e2e_base_url}/?q=Force+of+Will+set%3ADMR+is%3Aregular+%23999")
    assert stub_search[-1] == "Force of Will"
    assert page.locator("[data-filter-set]").input_value() == "DMR"
    assert page.locator('[data-filter-variant="regular"]').get_attribute("aria-pressed") == "true"
    # #999 matches nothing with a known number; only the "?" row survives.
    shops = _visible_shops(page)
    assert len(shops) == 1 and "TokyoMTG" in shops[0]

    page.locator("[data-filter-number]").click()   # clear #999
    shops = _visible_shops(page)
    assert len(shops) == 2
    assert "TokyoMTG" in shops[0] and "Hareruya" in shops[1]
