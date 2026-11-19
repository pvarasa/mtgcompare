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


def test_parse_shipping_overrides_rejects_non_finite_numbers():
    """float() accepts "inf"/"nan"; int(inf) raised OverflowError → a 500."""
    overrides = web._parse_shipping_overrides({"ship_hareruya": "inf", "ship_singlestar": "nan"})

    assert overrides["Hareruya"] == web.SHIPPING_JPY["Hareruya"]
    assert overrides["SingleStar"] == web.SHIPPING_JPY["SingleStar"]


def test_oversized_request_body_is_rejected_before_parsing():
    """MAX_CONTENT_LENGTH bounds uploads/JSON bodies, which Werkzeug's form
    limits don't cover."""
    limit = web.app.config["MAX_CONTENT_LENGTH"]
    assert limit
    with web.app.test_client() as client:
        resp = client.post(
            "/inventory/add-bulk",
            data=b"x" * (limit + 1),
            content_type="application/json",
        )
    assert resp.status_code == 413


def test_get_fx_refetches_after_ttl_and_keeps_last_rate_on_failure(monkeypatch):
    rates = iter([150.0, RuntimeError("down"), 152.0])

    def fake_get_fx(_ccy):
        r = next(rates)
        if isinstance(r, Exception):
            raise r
        return r

    clock = {"t": 1000.0}
    monkeypatch.setattr(web, "get_fx", fake_get_fx)
    monkeypatch.setattr(web, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(web, "_fx", None)
    monkeypatch.setattr(web, "_fx_at", 0.0)
    monkeypatch.setattr(web, "_fx_failed_at", None)
    monkeypatch.setattr(web, "_fx_refreshing", False)

    assert web._get_fx() == 150.0
    clock["t"] += 60
    assert web._get_fx() == 150.0          # cached, no fetch
    clock["t"] += web._FX_TTL_S
    assert web._get_fx() == 150.0          # refresh failed → last good rate
    clock["t"] += web._FX_RETRY_S + 1
    assert web._get_fx() == 152.0          # retried after the back-off


def _honesty_rows():
    base = {"card": "Mistrise Village", "set": "TDM", "number": "261", "variant": "",
            "condition": "NM", "link": "https://example.test/"}
    return [
        {**base, "shop": "TCGPlayer market", "price_jpy": 1001.0, "price_usd": 6.34,
         "stock": None},
        {**base, "shop": "TCGPlayer → JP", "price_jpy": 1069.0, "price_usd": 6.77,
         "ship_jpy": 3157.0, "seller": "CITY Cards N More", "stock": 1},
        {**base, "shop": "Hareruya", "price_jpy": 2000.0, "price_usd": 12.66, "stock": 3},
    ]


def _search_page(monkeypatch, fake_shops, query: str) -> str:
    monkeypatch.setattr(web, "_get_fx", lambda: 158.0)
    fake_shops(_honesty_rows())
    with web.app.test_client() as client:
        resp = client.get(query)
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def _row(html: str, shop: str) -> str:
    start = html.index(f"</span>{shop}")
    return html[html.rfind("<tr", 0, start):html.index("</tr>", start)]


def test_a_reference_price_is_labelled_and_never_badged_cheapest(monkeypatch, fake_shops):
    """The market average sorts first but can't be bought: the badge goes
    to the first real offer, and the row says what it is."""
    html = _search_page(monkeypatch, fake_shops, "/?q=Mistrise+Village")
    market = _row(html, "TCGPlayer market")
    assert 'class="explain ref-tag"' in market and "cheapest" not in market
    # The explanation is in the page, behind a tap — not a hover-only title.
    assert "not an offer you can buy" in market
    assert ">view</a>" in market and ">open</a>" not in market
    assert "cheapest" in _row(html, "TCGPlayer → JP")


def test_a_marketplace_offer_shows_its_seller_and_shipping(monkeypatch, fake_shops):
    """With shipping off the price stays the item price, but the row still
    says what the seller charges to Japan and who the seller is, so the
    user can find that listing on TCGPlayer's page."""
    off = _row(_search_page(monkeypatch, fake_shops, "/?q=Mistrise+Village"), "TCGPlayer → JP")
    assert "sold by CITY Cards N More" in off
    assert "+ ¥3,157 ship" in off
    on = _row(_search_page(monkeypatch, fake_shops, "/?q=Mistrise+Village&shipping=1"), "TCGPlayer → JP")
    assert "ship</div>" not in on  # the w/ship column carries it instead
    assert "¥4,226" in on


def _slow_search(monkeypatch, fake_shops, rows, delays, query="/?q=Mistrise+Village"):
    """A search whose ``delays`` shops miss the first paint."""
    from mtgcompare.scrapers import registry
    monkeypatch.setattr(web, "SEARCH_FIRST_PAINT_S", 0.05)
    monkeypatch.setattr(registry, "SHOP_QUERY_TIMEOUT_S", 2.0)
    monkeypatch.setattr(web, "_get_fx", lambda: 158.0)
    fake_shops(rows, delays)
    return web.app.test_client().get(query)


def test_slow_shops_stream_their_rows_after_the_first_paint(monkeypatch, fake_shops):
    base = {"card": "Mistrise Village", "set": "TDM", "number": "261", "variant": "",
            "condition": "NM", "link": "https://example.test/", "price_usd": 1.0, "stock": 1}
    rows = [{**base, "shop": "Hareruya", "price_jpy": 2000.0},
            {**base, "shop": "Nukenin", "price_jpy": 1500.0, "set": "OTJ"}]
    resp = _slow_search(monkeypatch, fake_shops, rows, {"Nukenin": 0.3})
    assert resp.status_code == 200
    assert resp.headers["X-Accel-Buffering"] == "no"
    html = resp.get_data(as_text=True)

    first_paint, _, streamed = html.partition("<template data-late ")
    # The page went out with Hareruya and a "still searching" line ...
    assert 'data-pending-shop="Nukenin"' in first_paint
    assert "</span>Hareruya" in first_paint and "</span>Nukenin" not in first_paint
    # ... then Nukenin's row, with the filters recomputed over both shops,
    # then the end of the stream and of the document.
    assert 'data-shop="Nukenin"' in streamed and "</span>Nukenin" in streamed
    assert 'data-sort="1500.0"' in streamed
    assert '<option value="OTJ">OTJ (1)</option>' in streamed
    assert "data-late-done" in streamed and "Partial results" not in streamed
    assert html.rstrip().endswith("</html>")


def test_a_shop_that_never_answers_is_reported_at_the_end_of_the_stream(monkeypatch, fake_shops):
    from mtgcompare.scrapers import registry
    base = {"card": "Mistrise Village", "set": "TDM", "number": "261", "variant": "",
            "condition": "NM", "link": "https://example.test/", "price_usd": 1.0, "stock": 1}
    monkeypatch.setattr(web, "SEARCH_FIRST_PAINT_S", 0.05)
    monkeypatch.setattr(registry, "SHOP_QUERY_TIMEOUT_S", 0.2)
    monkeypatch.setattr(web, "_get_fx", lambda: 158.0)
    fake_shops([{**base, "shop": "Hareruya", "price_jpy": 2000.0}], {"Nukenin": 1.0})
    html = web.app.test_client().get("/?q=Mistrise+Village").get_data(as_text=True)
    done = html[html.index("data-late-done"):]
    assert "Partial results" in done and "Nukenin" in done


def test_a_search_with_no_rows_yet_hides_the_table_until_they_arrive(monkeypatch, fake_shops):
    monkeypatch.setattr(web, "SEARCH_FIRST_PAINT_S", 0.05)
    monkeypatch.setattr(web, "_get_fx", lambda: 158.0)
    fake_shops([], {"Nukenin": 0.2})
    html = web.app.test_client().get("/?q=Mistrise+Village").get_data(as_text=True)
    first_paint = html.partition("<template data-late ")[0]
    assert '<div class="table-scroll" hidden>' in first_paint
    assert "data-results-empty hidden" in first_paint  # not "No results" yet


def test_a_search_every_shop_answers_in_time_is_not_streamed(monkeypatch, fake_shops):
    html = _search_page(monkeypatch, fake_shops, "/?q=Mistrise+Village")
    assert "data-late-status" not in html and "<template data-late" not in html
