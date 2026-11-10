"""Decklist pricing: parsing, basics, inventory deduction, the per-card fan-out
(incl. cancellation), and both /decklist routes (streaming SSE + sync fallback)."""
import time

import pytest

from mtgcompare import decklist, web


def test_decklist_search_rejects_oversized_lists():
    """Total card count above MAX_DECKLIST_CARDS should short-circuit
    with a clear error rather than fanning out shop scrapes."""
    web.app.config["WTF_CSRF_ENABLED"] = False
    over = decklist.MAX_DECKLIST_CARDS + 1
    body = f"{over} Sol Ring\n"  # one line, but qty exceeds the cap
    with web.app.test_client() as client:
        resp = client.post("/decklist", data={"decklist": body})
    assert resp.status_code == 200
    page = resp.data.decode()
    assert f"{over} cards" in page
    assert "limit is 100" in page


def test_decklist_search_accepts_at_cap():
    """Exactly MAX_DECKLIST_CARDS is allowed; the request reaches the FX
    fetch path (which we stub to return None to skip the actual scrape)."""
    web.app.config["WTF_CSRF_ENABLED"] = False
    body = f"{decklist.MAX_DECKLIST_CARDS} Sol Ring\n"
    # Stubbing FX out short-circuits with a different error; the point is
    # that the size cap doesn't fire.
    original = web._get_fx
    web._get_fx = lambda: None
    try:
        with web.app.test_client() as client:
            resp = client.post("/decklist", data={"decklist": body})
        assert resp.status_code == 200
        page = resp.data.decode()
        assert "limit is 100" not in page
    finally:
        web._get_fx = original


def test_iter_decklist_prices_yields_one_per_name():
    """The generator emits one tuple per requested name, even on failure,
    and rows are sorted ascending by price_jpy."""
    def fake_collect(name, fx, *, enabled, logger, timeouts_out):
        # name here is the canonical form (e.g. "Boom"), not the lowercased key.
        if name.lower() == "boom":
            raise RuntimeError("simulated")
        # Return descending so we can verify the sort.
        return [
            {"price_jpy": 300, "shop": "B", "card": name, "set": "X",
             "price_usd": 2.0, "stock": 1, "condition": "NM", "link": ""},
            {"price_jpy": 100, "shop": "A", "card": name, "set": "X",
             "price_usd": 0.7, "stock": 1, "condition": "NM", "link": ""},
        ]

    names = ["sol ring", "boom", "force of will"]
    canonical = {n: n.title() for n in names}
    out = dict(decklist.iter_decklist_prices(
        names, canonical, fx=150.0, enabled_shops=None, collect=fake_collect,
    ))

    assert set(out) == set(names)
    # Failed name yields an empty list, not raises.
    assert out["boom"] == []
    # Successful names have rows sorted ascending by JPY.
    for n in ("sol ring", "force of will"):
        prices = [r["price_jpy"] for r in out[n]]
        assert prices == sorted(prices)


def test_iter_decklist_prices_drops_marketplace_shops():
    """Decklist fan-outs never query marketplace shops — their per-card
    landed prices don't sum to an order total."""
    from mtgcompare.scrapers.registry import ACTIVE_SHOPS, MARKETPLACE_SHOPS

    captured = {}
    def fake_collect(name, fx, *, enabled, logger, timeouts_out):
        captured["enabled"] = enabled
        return []
    # Literal name on purpose: a rename that loses the marketplace flag
    # must fail here, not silently re-enter decklist totals.
    assert "TCGPlayer → JP" in MARKETPLACE_SHOPS

    # Default "all shops" search: every active shop except marketplaces.
    list(decklist.iter_decklist_prices(
        ["x"], {"x": "X"}, fx=150.0, enabled_shops=None, collect=fake_collect,
    ))
    assert captured["enabled"] == set(ACTIVE_SHOPS) - MARKETPLACE_SHOPS - {"TCGPlayer market"}

    # Explicit selection that includes a marketplace shop: still dropped.
    list(decklist.iter_decklist_prices(
        ["x"], {"x": "X"}, fx=150.0,
        enabled_shops={"Hareruya", "TCGPlayer → JP"}, collect=fake_collect,
    ))
    assert captured["enabled"] == {"Hareruya"}

    # A reference price (TCGPlayer market) isn't an offer, so a basket of
    # them isn't a buyable order total either.
    list(decklist.iter_decklist_prices(
        ["x"], {"x": "X"}, fx=150.0,
        enabled_shops={"Hareruya", "TCGPlayer market"}, collect=fake_collect,
    ))
    assert captured["enabled"] == {"Hareruya"}


def test_iter_decklist_prices_empty_input_is_empty_iter():
    """Edge case — caller passed no names; generator yields nothing
    and never spawns a thread pool."""
    assert list(decklist.iter_decklist_prices(
        [], {}, fx=150.0, enabled_shops=None,
        collect=lambda *a, **kw: pytest.fail("should not be called"),
    )) == []


def test_iter_decklist_prices_populates_timeouts_out():
    """Caller-supplied timeouts_out set is forwarded into collect_prices
    so streaming consumers can build the live timeout-warning list."""
    captured = []
    def fake_collect(name, fx, *, enabled, logger, timeouts_out):
        captured.append(timeouts_out)
        if timeouts_out is not None:
            timeouts_out.add("Slow Shop")
        return []

    timeouts: set[str] = set()
    list(decklist.iter_decklist_prices(
        ["x"], {"x": "X"}, fx=150.0, enabled_shops=None, timeouts_out=timeouts,
        collect=fake_collect,
    ))
    assert "Slow Shop" in timeouts
    assert captured[0] is timeouts


# ---------------------------------------------------------------------------
# SSE /decklist/stream flow (single streaming POST)
# ---------------------------------------------------------------------------

def _drain_sse(iter_encoded, timeout=10.0):
    """Read SSE chunks until a ``done`` or ``error`` event arrives.

    Returns the parsed event list. ``: keepalive`` comments are skipped.
    Raises ``AssertionError`` if the stream closes without a terminal
    event or the loop runs past ``timeout`` seconds.
    """
    import json as _json
    import time as _time
    deadline = _time.monotonic() + timeout
    buf = b""
    events: list[tuple[str, dict]] = []
    for chunk in iter_encoded:
        buf += chunk
        # SSE events are separated by blank lines.
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            text = block.decode()
            if text.startswith(":"):
                continue  # keepalive comment
            evt_type = ""
            data = ""
            for line in text.splitlines():
                if line.startswith("event: "):
                    evt_type = line[7:]
                elif line.startswith("data: "):
                    data = line[6:]
            events.append((evt_type, _json.loads(data) if data else {}))
            if evt_type in ("done", "error"):
                return events
        if _time.monotonic() > deadline:
            raise AssertionError(f"SSE drain timed out; got {len(events)} events")
    raise AssertionError(f"SSE stream closed without terminal event; got {events}")


@pytest.fixture
def _clean_in_flight():
    """Reset the per-user in-flight counter around each test so cap
    assertions in one test don't leak into the next."""
    web._in_flight_by_user.clear()
    yield
    web._in_flight_by_user.clear()


def test_decklist_stream_happy_path_streams_meta_rows_totals_done(monkeypatch, _clean_in_flight):
    """End-to-end: a single POST returns text/event-stream and emits
    the expected event sequence (meta → row × N → totals → done) in the
    response body — no follow-up GET, no job_id."""
    web.app.config["WTF_CSRF_ENABLED"] = False
    monkeypatch.setattr(web, "_get_fx", lambda: 150.0)

    def fake_collect(name, fx, *, enabled, logger, timeouts_out):
        return [{
            "shop": "Hareruya", "card": name, "set": "TST",
            "price_jpy": 100.0, "price_usd": 0.67, "stock": 1,
            "condition": "NM", "link": f"https://example.com/{name}",
        }]
    monkeypatch.setattr(web, "collect_prices", fake_collect)

    with web.app.test_client() as client:
        resp = client.post(
            "/decklist/stream",
            data={"decklist": "1 Sol Ring\n1 Mana Drain"},
            buffered=False,
        )
        assert resp.status_code == 200, resp.data
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        events = _drain_sse(resp.response, timeout=10.0)

    types = [t for t, _ in events]
    assert types[0] == "meta"
    assert types.count("row") == 2
    assert "totals" in types
    assert types[-1] == "done"

    meta = next(d for t, d in events if t == "meta")
    assert meta["distinct_names"] == 2
    assert meta["names_to_search"] == 2

    for t, d in events:
        if t == "row":
            assert {"key", "html", "qty_needed", "has_best"} <= d.keys()
            assert "Hareruya" in d["html"]
            assert d["has_best"] is True


def test_decklist_stream_row_event_key_matches_lowercased_card_name(monkeypatch, _clean_in_flight):
    """The streaming JS uses ``row.key`` as the sort key when inserting
    each <tr> into the alphabetical slot. The key must be the lowercased
    canonical name — sort-aware behavior on the client depends on it."""
    web.app.config["WTF_CSRF_ENABLED"] = False
    monkeypatch.setattr(web, "_get_fx", lambda: 150.0)
    monkeypatch.setattr(web, "collect_prices",
                        lambda *a, **kw: [])

    with web.app.test_client() as client:
        resp = client.post(
            "/decklist/stream",
            data={"decklist": "1 Sol Ring\n1 Ancestral Recall"},
            buffered=False,
        )
        events = _drain_sse(resp.response, timeout=10.0)

    row_keys = [d["key"] for t, d in events if t == "row"]
    assert sorted(row_keys) == row_keys or set(row_keys) == {"sol ring", "ancestral recall"}
    for k in row_keys:
        assert k == k.lower(), f"row.key must be lowercased; got {k!r}"


def test_decklist_stream_emits_inventory_rows_before_searched_rows(monkeypatch, _clean_in_flight):
    """Cards covered by inventory get row events up-front (qty_needed=0,
    has_best=False), before any shop fan-out runs. Without this, the
    streamed table would be shorter than the synchronous one — missing
    the ✓-in-inventory rows the user expects to see."""
    web.app.config["WTF_CSRF_ENABLED"] = False
    monkeypatch.setattr(web, "_get_fx", lambda: 150.0)

    def fake_load_inventory(use_inventory):
        return {"sol ring": 1} if use_inventory else {}
    monkeypatch.setattr(web, "_load_inventory_qty_map", fake_load_inventory)

    def fake_collect(name, fx, *, enabled, logger, timeouts_out):
        return [{
            "shop": "Hareruya", "card": name, "set": "TST",
            "price_jpy": 200.0, "price_usd": 1.33, "stock": 1,
            "condition": "NM", "link": "https://example.com",
        }]
    monkeypatch.setattr(web, "collect_prices", fake_collect)

    with web.app.test_client() as client:
        resp = client.post(
            "/decklist/stream",
            data={"decklist": "1 Sol Ring\n1 Mana Drain", "use_inventory": "1"},
            buffered=False,
        )
        assert resp.status_code == 200, resp.data
        events = _drain_sse(resp.response, timeout=10.0)

    row_events = [d for t, d in events if t == "row"]
    assert len(row_events) == 2
    # The inventory-covered row arrives first, marked qty_needed=0.
    assert row_events[0]["qty_needed"] == 0
    assert row_events[0]["has_best"] is False
    assert "Sol Ring" in row_events[0]["html"]
    assert "in inventory" in row_events[0]["html"]
    # The shop-searched row arrives after.
    assert row_events[1]["has_best"] is True
    assert "Mana Drain" in row_events[1]["html"]


def test_decklist_stream_rejects_validation_failures_with_json_400(monkeypatch, _clean_in_flight):
    """An only-basics decklist gets a 400 + ``reason`` so the client can
    surface the same error message the synchronous handler shows,
    without opening a stream that would only carry a single ``error``
    event."""
    web.app.config["WTF_CSRF_ENABLED"] = False
    with web.app.test_client() as client:
        resp = client.post("/decklist/stream",
                           data={"decklist": "10 Forest\n5 Island"})
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["reason"] == "only_basics"
    assert "only basic lands" in body["error"].lower()


def test_decklist_stream_caps_per_user_with_429(monkeypatch, _clean_in_flight):
    """Pre-populating the per-user in-flight counter at the cap forces
    the next POST to 429 — this is the OOM guard from the earlier
    analysis (~1 GB peak per concurrent search). The counter is
    per-process; with multiple gunicorn workers the effective cap is
    cap × N_workers."""
    web.app.config["WTF_CSRF_ENABLED"] = False
    monkeypatch.setattr(web, "_get_fx", lambda: 150.0)
    monkeypatch.setattr(web, "collect_prices", lambda *a, **kw: [])
    web._in_flight_by_user["local"] = web._MAX_IN_FLIGHT_PER_USER
    with web.app.test_client() as client:
        resp = client.post("/decklist/stream", data={"decklist": "1 Sol Ring"})
    assert resp.status_code == 429
    assert resp.get_json()["reason"] == "rate_limited"


def test_is_basic_land_recognizes_all_basics_case_insensitive():
    for name in [
        "Plains", "ISLAND", "swamp", "Mountain", "Forest", "Wastes",
        "Snow-Covered Plains", "snow-covered forest",
        "  Forest  ",
    ]:
        assert decklist.is_basic_land(name), name


def test_is_basic_land_rejects_non_basics():
    for name in [
        "Sol Ring", "Misty Rainforest", "Wooded Foothills",
        "Cavern of Souls", "Snow-Covered Wastes",  # not a real card
        "Plainsrider",
    ]:
        assert not decklist.is_basic_land(name), name


def test_strip_basic_lands_counts_skipped_copies():
    items = [
        (4, "Sol Ring"),
        (10, "Forest"),
        (2, "Lightning Bolt"),
        (5, "Snow-Covered Island"),
    ]
    kept, skipped = decklist.strip_basic_lands(items)
    assert kept == [(4, "Sol Ring"), (2, "Lightning Bolt")]
    assert skipped == 15


def test_strip_basic_lands_no_basics_is_passthrough():
    items = [(4, "Sol Ring"), (1, "Force of Will")]
    kept, skipped = decklist.strip_basic_lands(items)
    assert kept == items
    assert skipped == 0


def test_decklist_search_rejects_only_basics():
    """A list of pure basic lands should short-circuit with an explanatory
    error — basics are excluded from the search fan-out."""
    web.app.config["WTF_CSRF_ENABLED"] = False
    body = "10 Forest\n5 Island\n3 Snow-Covered Plains\n"
    with web.app.test_client() as client:
        resp = client.post("/decklist", data={"decklist": body})
    assert resp.status_code == 200
    page = resp.data.decode()
    assert "only basic lands" in page.lower()


def test_decklist_search_oversize_check_runs_after_basics_stripped():
    """A 99-non-basic + 50-basic list (149 total) should be accepted —
    basics are stripped before the MAX_DECKLIST_CARDS check fires."""
    web.app.config["WTF_CSRF_ENABLED"] = False
    body = f"{decklist.MAX_DECKLIST_CARDS} Sol Ring\n50 Forest\n"
    original = web._get_fx
    web._get_fx = lambda: None  # short-circuit before scraping
    try:
        with web.app.test_client() as client:
            resp = client.post("/decklist", data={"decklist": body})
        assert resp.status_code == 200
        page = resp.data.decode()
        assert "limit is 100" not in page


    finally:
        web._get_fx = original


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

    assert decklist.parse_decklist_report(text).items == [
        (1, "Sol Ring"),
        (4, "Force of Will"),
        (2, "Rhystic Study"),
    ]


@pytest.mark.parametrize("text,expected", [
    # No quantity means one copy, so a bare list of names works.
    ("Sol Ring\nCounterspell", [(1, "Sol Ring"), (1, "Counterspell")]),
    ("4x Lightning Bolt\n1X Sol Ring\n2 x Opt",
     [(4, "Lightning Bolt"), (1, "Sol Ring"), (2, "Opt")]),
    # MTGO plain-text export.
    ("4 Thoughtseize\nSB: 1 Pithing Needle", [(4, "Thoughtseize"), (1, "Pithing Needle")]),
    # Archidekt: lower-case set, categories, tags.
    ("1x Sol Ring (cmm) 464 [Ramp]\n"
     "1x Swords to Plowshares (sta) 10 *F* [Removal,Draw] ^Have,#37d67a^\n"
     "1x Opt [Draw]",
     [(1, "Sol Ring"), (1, "Swords to Plowshares"), (1, "Opt")]),
    # TCGplayer mass entry.
    ("1 Sol Ring [CMM]\n4 Lightning Bolt [2X2]", [(1, "Sol Ring"), (4, "Lightning Bolt")]),
    # MTGGoldfish / TappedOut type headers with counts.
    ("Creatures (12)\n4 Delver of Secrets\nInstants:\n4 Brainstorm\nLands (20)\n4 Polluted Delta",
     [(4, "Delver of Secrets"), (4, "Brainstorm"), (4, "Polluted Delta")]),
    # Arena's About block holds the deck name, not a card.
    ("About\nName My Deck\n\nDeck\n4 Lightning Bolt (2X2) 117", [(4, "Lightning Bolt")]),
    ("1 Borrowing 100,000 Arrows\n1 Fire // Ice",
     [(1, "Borrowing 100,000 Arrows"), (1, "Fire // Ice")]),
])
def test_parse_decklist_export_formats(text, expected):
    assert decklist.parse_decklist_report(text).items == expected


def test_parse_decklist_report_lists_what_was_left_out():
    report = decklist.parse_decklist_report(
        "1 Sol Ring\n4\n0 Island\nMaybeboard\n1 Opt\n2 Brainstorm\nSideboard\n1 Duress"
    )
    assert report.items == [(1, "Sol Ring"), (1, "Duress")]
    assert report.unparsed == ["4"]
    assert report.maybeboard == 3


def test_parse_printings_reads_tcgplayer_set_brackets():
    assert decklist.parse_printings("1 Sol Ring [CMM]") == {"sol ring": {("CMM", None)}}


def test_decklist_page_reports_unparsed_and_maybeboard(monkeypatch):
    web.app.config["WTF_CSRF_ENABLED"] = False
    monkeypatch.setattr(web, "_get_fx", lambda: 150.0)
    monkeypatch.setattr(web, "collect_prices", lambda *a, **kw: [])
    body = "1 Sol Ring\n12\nMaybeboard\n2 Opt"
    with web.app.test_client() as client:
        resp = client.post("/decklist", data={"decklist": body})
    page = resp.data.decode()
    assert "1 line not understood: 12" in page
    assert "2 maybeboard cards excluded" in page


def test_deduct_inventory_empty_inventory():
    name_qty = {"sol ring": 4, "force of will": 2}
    inv_qty, needed = decklist.deduct_inventory(name_qty, {})
    assert inv_qty == {"sol ring": 0, "force of will": 0}
    assert needed == {"sol ring": 4, "force of will": 2}


def test_deduct_inventory_full_coverage():
    name_qty = {"sol ring": 2, "rhystic study": 1}
    inv_map = {"sol ring": 5, "rhystic study": 3}
    inv_qty, needed = decklist.deduct_inventory(name_qty, inv_map)
    assert inv_qty == {"sol ring": 2, "rhystic study": 1}
    assert needed == {"sol ring": 0, "rhystic study": 0}


def test_deduct_inventory_partial_coverage():
    name_qty = {"force of will": 4}
    inv_map = {"force of will": 2}
    inv_qty, needed = decklist.deduct_inventory(name_qty, inv_map)
    assert inv_qty == {"force of will": 2}
    assert needed == {"force of will": 2}


def test_deduct_inventory_excess_inventory_is_capped():
    # Having more copies than requested should never produce negative need
    name_qty = {"lightning bolt": 1}
    inv_map = {"lightning bolt": 99}
    inv_qty, needed = decklist.deduct_inventory(name_qty, inv_map)
    assert inv_qty["lightning bolt"] == 1
    assert needed["lightning bolt"] == 0


def test_deduct_inventory_case_insensitive_matching():
    # Inventory names are lowercased before building inv_map; decklist keys
    # are also lowercased — so mixed-case variants must match.
    name_qty = {"counterspell": 3}
    inv_map = {"counterspell": 1}   # already lowercased by the caller
    inv_qty, needed = decklist.deduct_inventory(name_qty, inv_map)
    assert inv_qty["counterspell"] == 1
    assert needed["counterspell"] == 2


def test_deduct_inventory_unrelated_inventory_cards_ignored():
    name_qty = {"sol ring": 1}
    inv_map = {"black lotus": 10, "mox pearl": 4}
    inv_qty, needed = decklist.deduct_inventory(name_qty, inv_map)
    assert inv_qty["sol ring"] == 0
    assert needed["sol ring"] == 1


def test_deduct_inventory_multiple_lots_aggregated():
    # The caller sums quantities across lots before passing inv_map; verify
    # the helper handles already-aggregated values correctly.
    name_qty = {"dark ritual": 4}
    inv_map = {"dark ritual": 3}    # 2 lots of 1 + 1 lot of 2, pre-summed
    inv_qty, needed = decklist.deduct_inventory(name_qty, inv_map)
    assert inv_qty["dark ritual"] == 3
    assert needed["dark ritual"] == 1


# ---------------------------------------------------------------------------
# decklist fan-out: cancellation + shared per-user cap
# ---------------------------------------------------------------------------

def test_iter_decklist_prices_stops_starting_cards_once_cancelled():
    import threading

    names = [f"card {i}" for i in range(60)]
    calls = []

    def slow_collect(name, fx, *, enabled, logger, timeouts_out):
        calls.append(name)
        time.sleep(0.05)
        return []

    cancel = threading.Event()
    yielded = []
    for n, _rows in decklist.iter_decklist_prices(
        names, {n: n for n in names}, 150.0, None,
        collect=slow_collect, cancel=cancel,
    ):
        yielded.append(n)
        cancel.set()

    assert len(yielded) == 1
    assert len(calls) < len(names)


def test_decklist_sync_endpoint_shares_the_per_user_cap(monkeypatch, _clean_in_flight):
    web.app.config["WTF_CSRF_ENABLED"] = False
    monkeypatch.setattr(web, "_get_fx", lambda: 150.0)
    monkeypatch.setattr(web, "collect_prices", lambda *a, **kw: [])
    web._in_flight_by_user["local"] = web._MAX_IN_FLIGHT_PER_USER
    with web.app.test_client() as client:
        assert client.post("/decklist", data={"decklist": "1 Sol Ring"}).status_code == 429
        web._in_flight_by_user.clear()
        assert client.post("/decklist", data={"decklist": "1 Sol Ring"}).status_code == 200
    assert web._in_flight_by_user == {}
