from mtgcompare import web


def test_parse_decklist_skips_headers_and_comments():
    text = """
    // comment
    Commander:
    1 Sol Ring
    4x Force of Will (ALL)
    Sideboard:
    # another comment
    2 Rhystic Study (C21) 79
    """

    assert web._parse_decklist(text) == [
        (1, "Sol Ring"),
        (4, "Force of Will"),
        (2, "Rhystic Study"),
    ]


def test_parse_shipping_overrides_clamps_and_falls_back_to_defaults():
    source = {
        "ship_hareruya": "500",
        "ship_singlestar": "-5",
        "ship_tcgplayer_scryfall": "oops",
    }

    overrides = web._parse_shipping_overrides(source)

    assert overrides["Hareruya"] == 500
    assert overrides["SingleStar"] == 0
    assert overrides["TokyoMTG"] == web.SHIPPING_JPY["TokyoMTG"]
    assert overrides["TCGPlayer (Scryfall)"] == web.SHIPPING_JPY["TCGPlayer (Scryfall)"]


def test_normalize_set_code_and_foil_helpers():
    assert web._normalize_set_code("neo_123") == "neo"
    assert web._normalize_set_code("neo_123", upper=True) == "NEO"
    assert web._normalize_set_code(None) == ""
    assert web._is_foil("Foil") is True
    assert web._is_foil("Normal") is False


def test_deduct_inventory_empty_inventory():
    name_qty = {"sol ring": 4, "force of will": 2}
    inv_qty, needed = web._deduct_inventory(name_qty, {})
    assert inv_qty == {"sol ring": 0, "force of will": 0}
    assert needed == {"sol ring": 4, "force of will": 2}


def test_deduct_inventory_full_coverage():
    name_qty = {"sol ring": 2, "rhystic study": 1}
    inv_map = {"sol ring": 5, "rhystic study": 3}
    inv_qty, needed = web._deduct_inventory(name_qty, inv_map)
    assert inv_qty == {"sol ring": 2, "rhystic study": 1}
    assert needed == {"sol ring": 0, "rhystic study": 0}


def test_deduct_inventory_partial_coverage():
    name_qty = {"force of will": 4}
    inv_map = {"force of will": 2}
    inv_qty, needed = web._deduct_inventory(name_qty, inv_map)
    assert inv_qty == {"force of will": 2}
    assert needed == {"force of will": 2}


def test_deduct_inventory_excess_inventory_is_capped():
    # Having more copies than requested should never produce negative need
    name_qty = {"lightning bolt": 1}
    inv_map = {"lightning bolt": 99}
    inv_qty, needed = web._deduct_inventory(name_qty, inv_map)
    assert inv_qty["lightning bolt"] == 1
    assert needed["lightning bolt"] == 0


def test_deduct_inventory_case_insensitive_matching():
    # Inventory names are lowercased before building inv_map; decklist keys
    # are also lowercased — so mixed-case variants must match.
    name_qty = {"counterspell": 3}
    inv_map = {"counterspell": 1}   # already lowercased by the caller
    inv_qty, needed = web._deduct_inventory(name_qty, inv_map)
    assert inv_qty["counterspell"] == 1
    assert needed["counterspell"] == 2


def test_deduct_inventory_unrelated_inventory_cards_ignored():
    name_qty = {"sol ring": 1}
    inv_map = {"black lotus": 10, "mox pearl": 4}
    inv_qty, needed = web._deduct_inventory(name_qty, inv_map)
    assert inv_qty["sol ring"] == 0
    assert needed["sol ring"] == 1


def test_deduct_inventory_multiple_lots_aggregated():
    # The caller sums quantities across lots before passing inv_map; verify
    # the helper handles already-aggregated values correctly.
    name_qty = {"dark ritual": 4}
    inv_map = {"dark ritual": 3}    # 2 lots of 1 + 1 lot of 2, pre-summed
    inv_qty, needed = web._deduct_inventory(name_qty, inv_map)
    assert inv_qty["dark ritual"] == 3
    assert needed["dark ritual"] == 1


def test_fetch_card_prices_uses_shared_collector(monkeypatch):
    expected = [{"shop": "Test Shop", "price_jpy": 100}]

    def fake_collect_prices(card_name, fx, logger=None):
        assert card_name == "Force of Will"
        assert fx == 150.0
        assert logger is web.app.logger
        return expected

    monkeypatch.setattr(web, "collect_prices", fake_collect_prices)

    assert web._fetch_card_prices("Force of Will", 150.0) == expected
