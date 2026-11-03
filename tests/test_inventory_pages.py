"""/inventory and /market pages: id- and filter-based delete, the ?partial=tbody
contract, and the getting-started empty states."""

import pytest

from mtgcompare import inventory as inv
from mtgcompare import pricing, web

# ---------------------------------------------------------------------------
# /inventory/delete
# ---------------------------------------------------------------------------

_DEL_CARD = {
    "card_name": "Force of Will", "set_code": "ALL", "set_name": "Alliances",
    "card_number": "1", "quantity": 2, "condition": "NM",
    "printing": "Normal", "language": "English",
    "price_bought": 50.0, "date_bought": "2026-01-01",
}


def test_inventory_delete_happy_path(test_db):
    inv.add_one(_DEL_CARD)  # default user_id=local, matches the route
    target_id = inv.list_all()[0]["id"]
    with web.app.test_client() as client:
        resp = client.post(
            "/inventory/delete",
            json={"ids": [target_id]},
        )
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "count": 1}
    assert inv.list_all() == []


def test_inventory_delete_rejects_empty_ids(test_db):
    with web.app.test_client() as client:
        resp = client.post("/inventory/delete", json={"ids": []})
    assert resp.status_code == 400


def test_inventory_delete_rejects_invalid_ids(test_db):
    with web.app.test_client() as client:
        resp = client.post("/inventory/delete", json={"ids": ["not-an-int"]})
    assert resp.status_code == 400


def test_inventory_delete_does_not_cross_users(test_db, monkeypatch):
    """A user's POST cannot remove another user's row even if they guess the id."""
    inv.add_one(_DEL_CARD, user_id="bob")
    bob_id = inv.list_all("bob")[0]["id"]

    # Pin the request user to alice; bob's row must survive.
    monkeypatch.setattr(web, "_get_user_id", lambda: "alice")
    with web.app.test_client() as client:
        resp = client.post("/inventory/delete", json={"ids": [bob_id]})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "count": 0}
    assert len(inv.list_all("bob")) == 1


def test_inventory_delete_match_filtered(test_db):
    inv.add_one(_DEL_CARD)
    other = dict(_DEL_CARD, card_name="Lightning Bolt", set_code="LEA")
    inv.add_one(other)
    with web.app.test_client() as client:
        resp = client.post(
            "/inventory/delete",
            json={"match": {"q": "Force"}},
        )
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "count": 1}
    names = [r["card_name"] for r in inv.list_all()]
    assert names == ["Lightning Bolt"]


def test_inventory_delete_match_no_filter_wipes_user(test_db, monkeypatch):
    """Empty match payload wipes the requesting user's full inventory.

    The frontend gates this with typed-DELETE confirmation; the server
    trusts the request once it's authenticated to the user.
    """
    inv.add_one(_DEL_CARD)  # default user_id=local
    other = dict(_DEL_CARD, card_name="Lightning Bolt", set_code="LEA")
    inv.add_one(other)
    inv.add_one(_DEL_CARD, user_id="bob")  # different user, must survive

    with web.app.test_client() as client:
        resp = client.post(
            "/inventory/delete",
            json={"match": {"q": "", "price_mode": "any", "price_value": ""}},
        )
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "count": 2}
    assert inv.list_all() == []
    assert len(inv.list_all("bob")) == 1


# ---------------------------------------------------------------------------
# partial=tbody contract — JSON envelope with table_html + summary_html
#
# Both /inventory and /market expose a `?partial=tbody` endpoint that
# paginatedtable.js hits on filter/sort/page changes. The response must
# carry both fragments so the page can update the summary stats in
# lockstep with the table — without summary_html the cost-basis /
# market-value totals would stay stale, which is the bug fix this
# regression test pins.
# ---------------------------------------------------------------------------


def test_inventory_partial_tbody_returns_json_with_both_fragments(test_db):
    web.app.config["WTF_CSRF_ENABLED"] = False
    inv.add_one({
        "card_name": "Sol Ring", "set_code": "C21", "quantity": 2,
        "condition": "NM", "price_bought": 1.50, "language": "EN",
        "printing": "normal", "date_added": "2026-05-14",
    })
    with web.app.test_client() as client:
        resp = client.get("/inventory?partial=tbody")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("application/json")
    body = resp.get_json()
    assert set(body.keys()) == {"table_html", "summary_html"}
    # Table HTML carries at least one row reference; summary HTML
    # carries the unfiltered totals.
    assert "Sol Ring" in body["table_html"]
    assert "2</strong> copies" in body["summary_html"]
    assert "cost basis" in body["summary_html"]


def test_market_partial_tbody_returns_empty_fragments_when_no_price_cache(test_db):
    """No price cache → full page renders an alt UI; the partial
    endpoint short-circuits to empty fragments so a filter keystroke
    after a cache flush doesn't 500."""
    web.app.config["WTF_CSRF_ENABLED"] = False
    inv.add_one({
        "card_name": "Sol Ring", "set_code": "C21", "quantity": 1,
        "condition": "NM", "price_bought": 1.50, "language": "EN",
        "printing": "normal", "date_added": "2026-05-14",
    })
    with web.app.test_client() as client:
        resp = client.get("/market?partial=tbody")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("application/json")
    assert resp.get_json() == {"table_html": "", "summary_html": ""}


_GS_CARD = {
    "card_name": "Sol Ring", "set_code": "C21", "set_name": "Commander 2021",
    "card_number": "263", "quantity": 1, "condition": "NM",
    "printing": "Normal", "language": "English",
    "price_bought": 1.5, "date_bought": "2026-01-01",
}


@pytest.mark.parametrize("path", ["/inventory", "/market"])
def test_empty_inventory_shows_getting_started(test_db, path):
    pricing.market_cache_clear()
    with web.app.test_client() as client:
        resp = client.get(path)
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'class="getting-started"' in body
    assert "No inventory yet." in body
    # SQLite (desktop) mode fetches prices on demand, not via the daily job.
    assert "Update prices" in body


def test_market_empty_lead_link_renders_as_html(test_db):
    pricing.market_cache_clear()
    with web.app.test_client() as client:
        body = client.get("/market").data.decode()
    assert '<a href="/inventory">Add cards</a>' in body


def test_getting_started_hosted_mode_mentions_daily_refresh():
    with web.app.test_request_context():
        html = web.app.jinja_env.get_template("_getting_started.html").render(
            lead="x", allow_price_update=False,
        )
    assert "refresh automatically once a day" in html
    assert "Update prices" not in html


def test_nonempty_inventory_hides_getting_started(test_db):
    inv.add_one(_GS_CARD)
    pricing.market_cache_clear()
    with web.app.test_client() as client:
        for path in ("/inventory", "/market"):
            body = client.get(path).data.decode()
            assert 'class="getting-started"' not in body, path


def test_market_filter_matching_nothing_is_not_treated_as_empty(test_db):
    """No rows because of a filter must not show the new-user explainer to
    someone who has a collection."""
    inv.add_one(_GS_CARD)
    pricing.market_cache_clear()
    with web.app.test_client() as client:
        body = client.get("/market?q=zzz-no-such-card").data.decode()
    assert 'class="getting-started"' not in body
    assert "No lots match these filters." in body
