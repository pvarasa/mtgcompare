"""Search-page form helpers: shop filter / shipping override parsing, marketplace
offer collapsing, the include-shipping sort, the static-asset cache token."""


from mtgcompare import search, web


def test_parse_enabled_shops_default_returns_none():
    """No shop_filter flag → all shops on (the historical behavior)."""
    source = {"q": "Sol Ring"}
    assert web._parse_enabled_shops(source) is None


def test_parse_enabled_shops_explicit_subset():
    """shop_filter=1 + a few shop_<slug>=1 keys → just those shops."""
    source = {
        "shop_filter": "1",
        "shop_hareruya": "1",
        "shop_card_rush": "1",
    }
    enabled = web._parse_enabled_shops(source)
    assert enabled == {"Hareruya", "Card Rush"}


def test_parse_enabled_shops_empty_when_user_deselects_all():
    """shop_filter=1 with no shop_<slug>=1 → an explicit empty set, not None."""
    source = {"shop_filter": "1"}
    enabled = web._parse_enabled_shops(source)
    assert enabled == set()  # empty, not None


def test_shop_filter_config_marks_unspecified_as_enabled():
    cfg = web._shop_filter_config(None)
    assert cfg, "should produce one entry per active shop"
    assert all(c["enabled"] for c in cfg)
    for c in cfg:
        assert c["slug"] == web.shop_slug(c["shop"])


def test_shop_filter_config_partial_selection():
    cfg = web._shop_filter_config({"Hareruya"})
    by_shop = {c["shop"]: c["enabled"] for c in cfg}
    assert by_shop["Hareruya"] is True
    # Pick any other shop and assert it's marked disabled
    any_other = next(s for s in by_shop if s != "Hareruya")
    assert by_shop[any_other] is False


def test_shipping_config_excludes_marketplace_shops():
    """Marketplace rows carry their offer's real shipping; rendering an
    editable flat override for them would invite double counting."""
    shops = {c["shop"] for c in web._shipping_config()}
    assert "TCGPlayer → JP" not in shops
    assert "TCGPlayer market" in shops


def test_shipping_config_excludes_disabled_shops():
    """MINT MALL is known but paused (never searched); no shipping box for it."""
    shops = {c["shop"] for c in web._shipping_config()}
    assert "MINT MALL" not in shops
    assert "Hareruya" in shops


def test_collapse_marketplace_offers_picks_the_active_modes_winner():
    """Shipping off → cheapest by item price, no shipping influence at
    all. Shipping on → cheapest by landed total. Never both rows."""
    by_item = {"shop": "TCGPlayer → JP", "card": "Foo", "set": "MH3",
               "price_jpy": 562.0, "ship_jpy": 3000.0}
    by_landed = {"shop": "TCGPlayer → JP", "card": "Foo", "set": "MH3",
                 "price_jpy": 2064.0, "ship_jpy": 300.0}
    other = {"shop": "Hareruya", "card": "Foo", "set": "MH3", "price_jpy": 1090.0}

    off = search.collapse_marketplace_offers([by_item, by_landed, other], False)
    assert other in off
    assert [r for r in off if r["shop"] == "TCGPlayer → JP"] == [by_item]

    on = search.collapse_marketplace_offers([by_item, by_landed, other], True)
    assert other in on
    assert [r for r in on if r["shop"] == "TCGPlayer → JP"] == [by_landed]


def test_collapse_keeps_marketplace_offers_of_distinct_printings():
    a = {"shop": "TCGPlayer → JP", "card": "Foo", "set": "MH3",
         "price_jpy": 562.0, "ship_jpy": 3000.0}
    b = {"shop": "TCGPlayer → JP", "card": "Foo", "set": "PLST",
         "price_jpy": 700.0, "ship_jpy": 300.0}
    out = search.collapse_marketplace_offers([a, b], False)
    assert len(out) == 2


def test_apply_shipping_prefers_row_level_over_flat_override():
    """Marketplace rows keep their offer's own ship_jpy; other rows get
    the flat per-shop estimate; sort is by the landed total."""
    marketplace_row = {"shop": "TCGPlayer → JP", "price_jpy": 562.0, "ship_jpy": 3000.0}
    flat_row = {"shop": "Hareruya", "price_jpy": 800.0}
    free_ship_row = {"shop": "TCGPlayer → JP", "price_jpy": 2000.0, "ship_jpy": 0.0}
    results = [marketplace_row, flat_row, free_ship_row]

    search.apply_shipping(results, {"Hareruya": 385})

    assert marketplace_row["price_jpy_with_shipping"] == 3562.0
    assert flat_row["ship_jpy"] == 385
    assert flat_row["price_jpy_with_shipping"] == 1185.0
    # ship_jpy == 0.0 is a real value (free shipping), not "missing".
    assert free_ship_row["price_jpy_with_shipping"] == 2000.0
    assert results == [flat_row, free_ship_row, marketplace_row]


def test_static_token_appears_on_every_script_tag(monkeypatch):
    """Cache-bust: every <script src="…"> in the rendered templates must
    carry ``?v=<token>`` so deploying new JS forces every browser to
    fetch it. Without this we'd hit the v1.7.1 "stale JS expects old
    DOM, page silently no-ops" failure mode again on the next change."""
    web.app.config["WTF_CSRF_ENABLED"] = False
    with web.app.test_client() as client:
        # The search page is the entry point for /decklist + carries
        # both the form-hijack JS and the autocomplete JS.
        resp = client.get("/")
        assert resp.status_code == 200, resp.data
        body = resp.data.decode()
    # Every script that should be cache-busted shows up with ?v=
    for js_file in (
        "searchautocomplete.js",
        "decklist-stream.js",
        "cardpreview.js",
    ):
        assert f"{js_file}?v=" in body, \
            f"{js_file} missing the static_token cache-buster"


def test_compute_static_token_returns_stable_positive_integer_string():
    """Function should be cheap, deterministic for a given checkout,
    and a positive integer (so query-strings stay short and ordered).
    Repeated calls within a process return the same token (we cache it
    at module import via ``_STATIC_TOKEN``)."""
    token = web._compute_static_token()
    assert isinstance(token, str)
    assert token.isdigit()
    assert int(token) > 0
    # _STATIC_TOKEN is the cached value used by the context processor;
    # it must match _compute_static_token() since the static dir hasn't
    # changed since import.
    assert token == web._STATIC_TOKEN


def test_mint_mall_is_disabled_while_its_site_is_down():
    from mtgcompare.scrapers.registry import ACTIVE_SHOPS
    assert "MINT MALL" not in ACTIVE_SHOPS


def test_parse_shipping_overrides_clamps_and_falls_back_to_defaults():
    source = {
        "ship_hareruya": "500",
        "ship_singlestar": "-5",
        "ship_tcgplayer_market": "oops",
    }

    overrides = web._parse_shipping_overrides(source)

    assert overrides["Hareruya"] == 500
    assert overrides["SingleStar"] == 0
    assert overrides["TokyoMTG"] == web.SHIPPING_JPY["TokyoMTG"]
    # Non-numeric input falls back to the default, not a crash or 0.
    assert overrides["TCGPlayer market"] == web.SHIPPING_JPY["TCGPlayer market"]
