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


def test_fetch_card_prices_uses_shared_collector(monkeypatch):
    expected = [{"shop": "Test Shop", "price_jpy": 100}]

    def fake_collect_prices(card_name, fx, logger=None):
        assert card_name == "Force of Will"
        assert fx == 150.0
        assert logger is web.app.logger
        return expected

    monkeypatch.setattr(web, "collect_prices", fake_collect_prices)

    assert web._fetch_card_prices("Force of Will", 150.0) == expected
