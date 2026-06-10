"""Tests for the TCGPlayer ships-to-JP scraper.

Pure parts (payload builder, listing selection, record projection) are
tested directly; the orchestration in ``get_prices`` is tested with the
Scryfall lookup and the listings fetch stubbed out — no network.
"""
import pytest

from mtgcompare.scrapers import tcgplayer
from mtgcompare.scrapers.html_base import RateLimitedError, ScraperFetchError
from mtgcompare.scrapers.tcgplayer import (
    TcgPlayerJpScrapper,
    build_listings_payload,
    build_record,
    pick_best_listings,
)


def _page(listings: list[dict]) -> dict:
    return {"results": [{"totalResults": len(listings), "results": listings}]}


def _listing(price, ship, condition="Near Mint", quantity=1):
    return {
        "price": price,
        "shippingPrice": ship,
        "condition": condition,
        "quantity": quantity,
    }


# ---------------------------------------------------------------------------
# build_listings_payload
# ---------------------------------------------------------------------------

def test_payload_pins_destination_finish_and_conditions():
    p = build_listings_payload()
    assert p["context"]["shippingCountry"] == "JP"
    assert p["filters"]["term"]["printing"] == ["Normal"]
    assert p["filters"]["term"]["language"] == ["English"]
    assert set(p["filters"]["term"]["condition"]) == {"Near Mint", "Lightly Played"}
    assert p["filters"]["range"]["quantity"] == {"gte": 1}


# ---------------------------------------------------------------------------
# pick_best_listings — cheapest by item AND cheapest by landed total
# ---------------------------------------------------------------------------

def test_pick_returns_both_winners_when_they_differ():
    # $4.99 item with $15 shipping is the item-price winner; $13.76 with
    # $1.99 is the landed-total winner. Each sort mode needs its own row.
    cheap_item = _listing(4.99, 15.00)
    cheap_total = _listing(13.76, 1.99, condition="Lightly Played")
    picked = pick_best_listings(_page([cheap_total, cheap_item]))
    assert picked == [cheap_item, cheap_total]


def test_pick_collapses_to_one_when_same_listing_wins_both():
    a, b = _listing(10.0, 1.0), _listing(5.0, 2.0)
    assert pick_best_listings(_page([a, b])) == [b]
    assert pick_best_listings(_page([b, a])) == [b]


def test_pick_treats_missing_shipping_as_zero():
    free_ship = {"price": 8.0, "condition": "Near Mint", "quantity": 1}
    assert pick_best_listings(_page([_listing(8.5, 0.5), free_ship])) == [free_ship]


def test_pick_skips_malformed_entries():
    bad = {"price": "n/a", "shippingPrice": 1.0}
    good = _listing(9.0, 1.0)
    assert pick_best_listings(_page([bad, good])) == [good]
    assert pick_best_listings(_page([bad])) == []


def test_pick_handles_empty_and_missing_results():
    assert pick_best_listings(_page([])) == []
    assert pick_best_listings({"results": []}) == []
    assert pick_best_listings({}) == []


# ---------------------------------------------------------------------------
# build_record
# ---------------------------------------------------------------------------

def test_build_record_keeps_item_price_and_shipping_separate():
    """price_jpy is the item price (like every other shop); the offer's
    own shipping rides along as ship_jpy for the include-shipping sort."""
    r = build_record(
        _listing(13.76, 1.99, condition="Lightly Played", quantity=7),
        card_name="Witch Enchanter // Witch-Blessed Meadow",
        set_code="MH3",
        product_id=552336,
        fx_jpy_per_usd=150.0,
    )
    assert r == {
        "shop": "TCGPlayer → JP",
        "card": "Witch Enchanter // Witch-Blessed Meadow",
        "set": "MH3",
        "price_jpy": round(13.76 * 150.0, 2),
        "price_usd": 13.76,
        "ship_jpy": round(1.99 * 150.0, 2),
        "stock": 7,
        "condition": "LP",
        "link": "https://www.tcgplayer.com/product/552336"
                "?Language=English&Printing=Normal&Condition=Lightly+Played",
    }


def test_build_record_passes_through_unknown_condition():
    r = build_record(
        _listing(5.0, 1.0, condition="Moderately Played"),
        card_name="Foo", set_code="BAR", product_id=1, fx_jpy_per_usd=100.0,
    )
    assert r["condition"] == "Moderately Played"


# ---------------------------------------------------------------------------
# get_prices orchestration (stubbed lookup + fetch)
# ---------------------------------------------------------------------------

def _printing(set_code, tcgplayer_id, usd, name="Foo"):
    return {"name": name, "set": set_code, "tcgplayer_id": tcgplayer_id,
            "usd": usd, "link": ""}


def test_get_prices_one_record_per_product(monkeypatch):
    monkeypatch.setattr(tcgplayer, "fetch_card_summaries", lambda name: [
        _printing("MH3", 552336, 2.92),
        _printing("SLD", None, None),       # no product id → skipped
        _printing("PLST", 111, None),       # no market price → ordered last
    ])
    fetched: list[int] = []

    def fake_fetch(self, product_id):
        fetched.append(product_id)
        return _page([_listing(10.0, 2.0)])

    monkeypatch.setattr(TcgPlayerJpScrapper, "_fetch_listings", fake_fetch)
    records = TcgPlayerJpScrapper(fx=150.0).get_prices("Foo")

    # Fetch completion order is nondeterministic (parallel pool), but the
    # id-less printing must be dropped and records keep submission order
    # (priced printing first).
    assert sorted(fetched) == [111, 552336]
    assert [r["set"] for r in records] == ["MH3", "PLST"]
    for r in records:
        assert r["shop"] == "TCGPlayer → JP"
        assert r["price_usd"] == 10.0
        assert r["price_jpy"] == 1500.0
        assert r["ship_jpy"] == 300.0


def test_get_prices_caps_products_to_cheapest_printings(monkeypatch):
    cap = tcgplayer._MAX_PRODUCTS_PER_CARD
    printings = [_printing(f"S{i:02d}", 1000 + i, float(i)) for i in range(cap + 3)]
    monkeypatch.setattr(
        tcgplayer, "fetch_card_summaries",
        lambda name: list(reversed(printings)),
    )
    fetched: list[int] = []
    monkeypatch.setattr(
        TcgPlayerJpScrapper, "_fetch_listings",
        lambda self, pid: fetched.append(pid) or _page([]),
    )
    records = TcgPlayerJpScrapper(fx=150.0).get_prices("Foo")

    assert sorted(fetched) == [1000 + i for i in range(cap)]  # cheapest `cap` by usd
    assert records == []  # no JP-shippable listings → no rows, no error


def test_get_prices_skips_products_with_no_jp_listings(monkeypatch):
    monkeypatch.setattr(tcgplayer, "fetch_card_summaries", lambda name: [
        _printing("AAA", 1, 1.0), _printing("BBB", 2, 2.0),
    ])
    monkeypatch.setattr(
        TcgPlayerJpScrapper, "_fetch_listings",
        lambda self, pid: _page([] if pid == 1 else [_listing(3.0, 1.0)]),
    )
    records = TcgPlayerJpScrapper(fx=100.0).get_prices("Foo")
    assert [r["set"] for r in records] == ["BBB"]


def test_get_prices_emits_both_offers_when_winners_differ(monkeypatch):
    monkeypatch.setattr(tcgplayer, "fetch_card_summaries", lambda name: [
        _printing("MH3", 552336, 2.92),
    ])
    monkeypatch.setattr(
        TcgPlayerJpScrapper, "_fetch_listings",
        lambda self, pid: _page([_listing(3.75, 19.99), _listing(13.76, 1.99)]),
    )
    records = TcgPlayerJpScrapper(fx=100.0).get_prices("Foo")
    assert [(r["price_usd"], r["ship_jpy"]) for r in records] == [
        (3.75, 1999.0),    # cheapest by item price
        (13.76, 199.0),    # cheapest by landed total
    ]


def test_get_prices_tolerates_partial_product_failures(monkeypatch):
    """One bad product must not discard the others' rows."""
    monkeypatch.setattr(tcgplayer, "fetch_card_summaries", lambda name: [
        _printing("AAA", 1, 1.0), _printing("BBB", 2, 2.0),
    ])

    def fake_fetch(self, product_id):
        if product_id == 1:
            raise ScraperFetchError("simulated 5xx")
        return _page([_listing(3.0, 1.0)])

    monkeypatch.setattr(TcgPlayerJpScrapper, "_fetch_listings", fake_fetch)
    records = TcgPlayerJpScrapper(fx=100.0).get_prices("Foo")
    assert [r["set"] for r in records] == ["BBB"]


def test_get_prices_raises_when_failures_leave_no_rows(monkeypatch):
    """Zero rows + at least one failed fetch must propagate, so the cache
    never stores a possibly-false 'no listings' for the TTL window."""
    monkeypatch.setattr(tcgplayer, "fetch_card_summaries", lambda name: [
        _printing("AAA", 1, 1.0), _printing("BBB", 2, 2.0),
    ])

    def fake_fetch(self, product_id):
        if product_id == 1:
            raise ScraperFetchError("simulated outage")
        return _page([])  # fetched fine but nothing ships to JP

    monkeypatch.setattr(TcgPlayerJpScrapper, "_fetch_listings", fake_fetch)
    with pytest.raises(ScraperFetchError):
        TcgPlayerJpScrapper(fx=100.0).get_prices("Foo")


def test_get_prices_propagates_rate_limiting(monkeypatch):
    monkeypatch.setattr(tcgplayer, "fetch_card_summaries", lambda name: [
        _printing("AAA", 1, 1.0), _printing("BBB", 2, 2.0),
    ])

    def fake_fetch(self, product_id):
        if product_id == 1:
            raise RateLimitedError("429")
        return _page([_listing(3.0, 1.0)])

    monkeypatch.setattr(TcgPlayerJpScrapper, "_fetch_listings", fake_fetch)
    with pytest.raises(RateLimitedError):
        TcgPlayerJpScrapper(fx=100.0).get_prices("Foo")


# ---------------------------------------------------------------------------
# _fetch_listings error mapping
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code: int, content: bytes = b""):
        self.status_code = status_code
        self.content = content


class _FakePostSession:
    def __init__(self, response: _FakeResponse):
        self._response = response

    def post(self, url, json=None, timeout=None):
        return self._response


@pytest.mark.parametrize("status,exc", [
    (403, ScraperFetchError),
    (429, RateLimitedError),
    (500, ScraperFetchError),
])
def test_fetch_listings_raises_on_http_errors(status, exc):
    s = TcgPlayerJpScrapper(fx=150.0, session=_FakePostSession(_FakeResponse(status)))
    with pytest.raises(exc):
        s._fetch_listings(552336)


def test_fetch_listings_raises_on_bad_json():
    s = TcgPlayerJpScrapper(
        fx=150.0, session=_FakePostSession(_FakeResponse(200, b"<html>nope")),
    )
    with pytest.raises(ScraperFetchError):
        s._fetch_listings(552336)
