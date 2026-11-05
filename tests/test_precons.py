"""Opening precons / Secret Lairs into the inventory: catalog linking, deck
parsing, the preview + commit service, and the routes.

All offline: deck files come from a trimmed real fixture through an
injected loader (or a monkeypatched ``decks.fetch_deck``).
"""
import copy
import json
from pathlib import Path

import pytest

from mtgcompare import inventory as inv
from mtgcompare.pricing import market_repo
from mtgcompare.sealed import catalog, decks, repo, service

FIXTURES = Path(__file__).parent / "fixtures"
VV_DECK = json.loads((FIXTURES / "mtgjson_deck_virtue_and_valor.json").read_text(encoding="utf-8"))

ROCKS_DECK = {"data": {"code": "SLD", "name": "A Box of Rocks", "mainBoard": [
    {"name": "Arcane Signet", "setCode": "SLD", "number": "201", "count": 1, "isFoil": False,
     "uuid": "rock-1", "supertypes": [], "types": ["Artifact"]},
    {"name": "Gilded Lotus", "setCode": "SLD", "number": "205", "count": 1, "isFoil": False,
     "uuid": "rock-5", "supertypes": [], "types": ["Artifact"]},
]}}
DECK_FILES = {"VirtueAndValor_WOC": VV_DECK, "ABoxOfRocks_SLD": ROCKS_DECK}

DECK_LIST = {"data": [
    {"code": "WOC", "fileName": "VirtueAndValor_WOC", "name": "Virtue and Valor", "type": "Commander Deck"},
    {"code": "SLD", "fileName": "ABoxOfRocks_SLD", "name": "A Box of Rocks", "type": "Secret Lair Drop"},
]}

SETLIST = {"data": [
    {"code": "WOC", "name": "Wilds of Eldraine Commander", "releaseDate": "2023-09-08", "sealedProduct": [
        {"uuid": "precon-vv", "name": "Wilds of Eldraine Commander Deck Virtue and Valor",
         "category": "deck", "subtype": "commander", "identifiers": {"tcgplayerProductId": "496044"},
         "contents": {"deck": [{"name": "Virtue and Valor", "set": "woc"}],
                      "sealed": [{"name": "Collector Sample Pack", "set": "woe", "uuid": "x"}]}},
        # References a deck DeckList.json doesn't have: tracked, not openable.
        {"uuid": "precon-missing", "name": "Wilds of Eldraine Commander Deck Fae Dominion",
         "category": "deck", "subtype": "commander", "identifiers": {},
         "contents": {"deck": [{"name": "Fae Dominion", "set": "woc"}]}},
        {"uuid": "bundle-1", "name": "Some Bundle", "category": "bundle", "subtype": "default",
         "contents": {"deck": [{"name": "Virtue and Valor", "set": "woc"}]}},
    ]},
    {"code": "WOE", "name": "Wilds of Eldraine", "releaseDate": "2023-09-08", "sealedProduct": [
        {"uuid": "box-woe", "name": "Wilds of Eldraine Draft Booster Box", "category": "booster_box",
         "subtype": "draft", "identifiers": {"tcgplayerProductId": "1"}},
    ]},
    {"code": "SLD", "name": "Secret Lair Drop", "releaseDate": "2019-12-02", "sealedProduct": [
        {"uuid": "sl-rocks", "name": "Secret Lair Drop A Box of Rocks", "category": "box_set",
         "subtype": "secret_lair", "releaseDate": "2021-03-15",
         "contents": {"deck": [{"name": "A Box of Rocks", "set": "sld"}],
                      "card": [{"name": "Lotus Field", "number": "2023", "set": "slc",
                                "uuid": "bonus-lotus", "foil": True, "finishes": ["foil"]}]}},
        {"uuid": "mtgo-1", "name": "Some MTGO Redemption", "category": "box_set",
         "subtype": "mtgo_redemption", "contents": {"deck": [{"name": "A Box of Rocks", "set": "sld"}]}},
    ]},
]}


def _load(file_name):
    return DECK_FILES[file_name]


# --- catalog -----------------------------------------------------------------

def test_parse_setlist_links_deck_products_to_deck_files():
    rows = {r["uuid"]: r for r in catalog.parse_setlist(
        SETLIST, updated_at="t", deck_files=catalog.deck_file_index(DECK_LIST))}
    # Bundles (mostly boosters) and MTGO redemptions stay out.
    assert set(rows) == {"precon-vv", "precon-missing", "box-woe", "sl-rocks"}
    assert json.loads(rows["precon-vv"]["contents"]) == {"decks": ["VirtueAndValor_WOC"], "cards": []}
    assert rows["precon-missing"]["contents"] is None
    assert rows["box-woe"]["contents"] is None
    sl = json.loads(rows["sl-rocks"]["contents"])
    assert sl["decks"] == ["ABoxOfRocks_SLD"]
    assert sl["cards"] == [{"name": "Lotus Field", "set": "SLC", "number": "2023",
                            "uuid": "bonus-lotus", "foil": True}]


def test_parse_setlist_without_deck_index_lists_decks_unopenable():
    rows = {r["uuid"]: r for r in catalog.parse_setlist(SETLIST, updated_at="t")}
    assert rows["precon-vv"]["contents"] is None


# --- deck parsing ------------------------------------------------------------

def test_parse_deck_takes_commander_and_main_not_display_copy():
    cards = decks.parse_deck(VV_DECK)
    names = [c["card_name"] for c in cards]
    assert names.count("Ellivere of the Wild Court") == 1   # displayCommander skipped
    commander = cards[0]
    assert commander["printing"] == "Foil" and commander["set_code"] == "WOC" and commander["card_number"] == "2"
    giant = next(c for c in cards if c["card_number"] == "71")
    assert giant["card_name"] == "Realm-Cloaked Giant // Cast Off" and giant["printing"] == "Normal"
    forests = [c for c in cards if c["card_name"] == "Forest"]
    assert [(c["card_number"], c["quantity"], c["is_basic"]) for c in forests] == [("275", 8, True), ("276", 7, True)]
    assert not giant["is_basic"]


def test_parse_deck_sideboard_is_opt_in_and_etched_is_foil():
    deck = copy.deepcopy(VV_DECK)
    deck["data"]["sideBoard"] = [{"name": "Sol Ring", "setCode": "WOC", "number": "99", "count": 1,
                                  "isFoil": False, "isEtched": True, "uuid": "sb-1", "types": ["Artifact"]}]
    assert "Sol Ring" not in [c["card_name"] for c in decks.parse_deck(deck)]
    side = [c for c in decks.parse_deck(deck, include_sideboard=True) if c["card_name"] == "Sol Ring"]
    assert side[0]["printing"] == "Foil"


def test_merge_cards_sums_one_printing_and_keeps_finishes_apart():
    a = {"card_name": "X", "set_code": "S", "card_number": "1", "quantity": 1, "printing": "Normal",
         "uuid": "u", "is_basic": False}
    merged = decks.merge_cards([a, {**a, "quantity": 2}, {**a, "printing": "Foil"}])
    assert [(c["printing"], c["quantity"]) for c in merged] == [("Normal", 3), ("Foil", 1)]


def test_fetch_deck_refuses_odd_file_names():
    with pytest.raises(ValueError):
        decks.fetch_deck("../AllPrintings")


def test_load_deck_caches(monkeypatch):
    decks.cache_clear()
    calls = []
    try:
        fetch = lambda name: calls.append(name) or VV_DECK  # noqa: E731
        decks.load_deck("VirtueAndValor_WOC", fetch=fetch)
        decks.load_deck("VirtueAndValor_WOC", fetch=fetch)
        assert calls == ["VirtueAndValor_WOC"]
    finally:
        decks.cache_clear()


# --- service -----------------------------------------------------------------

@pytest.fixture
def seeded(test_db):
    service.refresh_catalog(force=True, fetch=lambda: SETLIST, fetch_decks=lambda: DECK_LIST)
    return test_db


def _add_lot(db, user, uuid, qty):
    with db.get_conn() as conn:
        repo.add_lot(conn, user, {"product_uuid": uuid, "language": "EN", "quantity": qty,
                                  "price_bought": None, "currency": "USD", "date_bought": None})
        return [lot for lot in repo.list_lots(conn, user) if lot["uuid"] == uuid][0]["id"]


def test_refresh_catalog_reads_both_files_before_writing(test_db):
    def boom():
        raise RuntimeError("DeckList down")
    with pytest.raises(RuntimeError):
        service.refresh_catalog(force=True, fetch=lambda: SETLIST, fetch_decks=boom)
    with test_db.get_conn() as conn:
        assert repo.count_products(conn) == 0


def test_older_catalog_version_is_stale_even_when_fresh(seeded):
    from mtgcompare import meta
    assert not service.catalog_is_stale()
    with seeded.get_conn() as conn:
        meta.write(conn, service.CATALOG_VERSION_META_KEY, "1")
    assert service.catalog_is_stale()


def test_search_openable_matches_every_word(seeded):
    names = [r["name"] for r in service.search_openable("virtue valor")]
    assert names == ["Wilds of Eldraine Commander Deck Virtue and Valor"]
    sl = service.search_openable("box rocks")
    assert sl[0]["kind"] == "Secret Lair" and sl[0]["year"] == "2021"
    assert service.search_openable("fae dominion") == []    # not openable
    assert service.search_openable("draft booster") == []   # boosters never


def test_precon_preview_skips_basics_and_names_sets(seeded):
    out = service.precon_preview("precon-vv", load=_load)
    names = [r["card_name"] for r in out["records"]]
    assert "Forest" not in names and "Plains" not in names
    assert out["basics_skipped"] == 29
    assert out["cards_total"] == 4
    first = out["records"][0]
    assert first["set_name"] == "Wilds of Eldraine Commander"
    assert first["language"] == "English" and first["condition"] == "NM" and first["price_bought"] is None

    full = service.precon_preview("precon-vv", skip_basics=False, language="JP", load=_load)
    assert full["cards_total"] == 33 and full["basics_skipped"] == 0
    assert {r["language"] for r in full["records"]} == {"Japanese"}


def test_precon_preview_includes_bonus_cards(seeded):
    out = service.precon_preview("sl-rocks", load=_load)
    lotus = [r for r in out["records"] if r["card_name"] == "Lotus Field"]
    assert lotus and lotus[0]["printing"] == "Foil" and lotus[0]["set_code"] == "SLC"


@pytest.mark.parametrize("uuid", ["nope", "precon-missing", "box-woe"])
def test_precon_preview_rejects_unopenable(seeded, uuid):
    with pytest.raises(service.PreconError):
        service.precon_preview(uuid, load=_load)


def test_add_precon_cards_maps_only_unedited_printings(seeded):
    records = service.precon_preview("precon-vv", load=_load)["records"]
    giant = next(r for r in records if r["card_number"] == "71")
    giant["printing"] = "Foil"   # user edit: not the deck's printing any more
    records.append({**records[0], "card_name": "Forged Uuid", "set_code": "LEA", "card_number": "1"})

    added = service.add_precon_cards("u1", records, product_uuid="precon-vv", load=_load)
    assert added == len(records)
    assert len(inv.list_all("u1")) == len(records)
    with seeded.get_conn() as conn:
        mapped = {(r["card_name"], r["set_code"], r["card_number"], r["is_foil"]): r["uuid"]
                  for r in market_repo.load_card_map(conn)}
    assert mapped[("Ellivere of the Wild Court", "WOC", "2", 1)] == "f2347475-c4ba-55b8-a691-74423976e518"
    assert ("Realm-Cloaked Giant // Cast Off", "WOC", "71", 1) not in mapped
    assert not any(k[0] == "Forged Uuid" for k in mapped)


def test_add_precon_cards_consumes_one_unit_then_the_lot(seeded):
    lot_id = _add_lot(seeded, "u1", "precon-vv", 2)
    records = service.precon_preview("precon-vv", load=_load)["records"]
    service.add_precon_cards("u1", records, product_uuid="precon-vv", lot_id=lot_id, load=_load)
    with seeded.get_conn() as conn:
        assert repo.get_lot(conn, "u1", lot_id)["quantity"] == 1
    service.add_precon_cards("u1", records, product_uuid="precon-vv", lot_id=lot_id, load=_load)
    with seeded.get_conn() as conn:
        assert repo.get_lot(conn, "u1", lot_id) is None
    with pytest.raises(service.PreconError):
        service.add_precon_cards("u1", records, product_uuid="precon-vv", lot_id=lot_id, load=_load)
    assert len(inv.list_all("u1")) == 2 * len(records)   # the failed third open added nothing


def test_add_precon_cards_wont_consume_someone_elses_or_another_products_lot(seeded):
    other = _add_lot(seeded, "u2", "precon-vv", 1)
    rocks = _add_lot(seeded, "u1", "sl-rocks", 1)
    records = service.precon_preview("precon-vv", load=_load)["records"]
    for lot_id in (other, rocks):
        with pytest.raises(service.PreconError):
            service.add_precon_cards("u1", records, product_uuid="precon-vv", lot_id=lot_id, load=_load)
    assert inv.list_all("u1") == []


def test_shop_matching_sees_boosters_only(seeded, monkeypatch):
    from mtgcompare.sealed import jp_shops
    seen = {}

    def fake_best_offers(shop, listings, catalog_by_set, set_names):
        seen.update(catalog_by_set)
        return {}, {}
    monkeypatch.setattr(jp_shops, "best_offers", fake_best_offers)
    service.refresh_shop_prices(crawlers={"hareruya": lambda: []})
    assert {p["uuid"] for rows in seen.values() for p in rows} == {"box-woe"}


# --- routes ------------------------------------------------------------------

@pytest.fixture
def client(seeded, monkeypatch):
    from mtgcompare import web
    monkeypatch.setattr(web, "_get_fx", lambda: 150.0)
    monkeypatch.setattr(decks, "fetch_deck", _load)
    decks.cache_clear()
    with web.app.test_client() as c:
        yield c
    decks.cache_clear()


def _inventory_config(client, url: str) -> dict:
    page = client.get(url).get_data(as_text=True)
    start = page.index('id="inventory-config">') + len('id="inventory-config">')
    return json.loads(page[start:page.index("</script>", start)])


def test_search_route(client):
    assert client.get("/sealed/precons/search?q=v").get_json()["results"] == []
    results = client.get("/sealed/precons/search?q=virtue").get_json()["results"]
    assert [r["uuid"] for r in results] == ["precon-vv"]


def test_cards_route(client):
    body = client.get("/sealed/precons/precon-vv/cards?skip_basics=0").get_json()
    assert body["ok"] and body["cards_total"] == 33
    assert client.get("/sealed/precons/box-woe/cards").status_code == 404


def test_cards_route_reports_mtgjson_outage(client, monkeypatch):
    import requests

    def down(*_a, **_k):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(service, "precon_preview", down)
    resp = client.get("/sealed/precons/precon-vv/cards")
    assert resp.status_code == 502 and "MTGJSON" in resp.get_json()["error"]


def test_open_lot_end_to_end(client, seeded):
    lot_id = _add_lot(seeded, "local", "precon-vv", 1)
    sealed = client.get("/sealed").get_data(as_text=True)
    assert f"/inventory?open_lot={lot_id}" in sealed

    open_lot = _inventory_config(client, f"/inventory?open_lot={lot_id}")["openLot"]
    assert open_lot["lot_id"] == lot_id and open_lot["product_uuid"] == "precon-vv"

    records = client.get("/sealed/precons/precon-vv/cards").get_json()["records"]
    resp = client.post("/inventory/add-bulk", json={
        "records": records, "precon": {"product_uuid": "precon-vv", "lot_id": lot_id},
    })
    assert resp.status_code == 200 and resp.get_json()["count"] == len(records)
    with seeded.get_conn() as conn:
        assert repo.get_lot(conn, "local", lot_id) is None
    again = client.post("/inventory/add-bulk", json={
        "records": records, "precon": {"product_uuid": "precon-vv", "lot_id": lot_id},
    })
    assert again.status_code == 404


def test_open_lot_param_ignores_unopenable_and_foreign_lots(client, seeded):
    box = _add_lot(seeded, "local", "box-woe", 1)
    foreign = _add_lot(seeded, "u2", "precon-vv", 1)
    for lot_id in (box, foreign, "abc"):
        assert _inventory_config(client, f"/inventory?open_lot={lot_id}")["openLot"] is None
    assert "/inventory?open_lot=" not in client.get("/sealed").get_data(as_text=True)


def test_add_bulk_without_precon_is_unchanged(client):
    resp = client.post("/inventory/add-bulk", json={"records": [{
        "card_name": "Sol Ring", "set_code": "C21", "quantity": 1}]})
    assert resp.status_code == 200
    assert client.post("/inventory/add-bulk", json={"records": [{"set_code": "X"}]}).status_code == 400
