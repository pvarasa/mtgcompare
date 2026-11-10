import json
import threading
import time
from pathlib import Path

import orjson
import pytest

from mtgcompare.scrapers import mythic
from mtgcompare.scrapers.html_base import RateLimitedError, ScraperFetchError
from mtgcompare.scrapers.mythic import (
    MythicStoreScrapper,
    build_query,
    extract_access_token,
    parse_products,
    search_query,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FX = 150.0
CAD = 1.4


@pytest.fixture(autouse=True)
def _fresh_state():
    mythic._reset_for_tests()
    yield
    mythic._reset_for_tests()


@pytest.fixture
def force_of_will() -> dict:
    return json.loads((FIXTURES / "mythic_force_of_will.json").read_text(encoding="utf-8"))


def _variant(title: str, amount: str, available: bool = True) -> dict:
    return {"node": {
        "title": title, "availableForSale": available,
        "price": {"amount": amount, "currencyCode": "CAD"},
    }}


def _product(title: str, *variants: dict, handle: str = "x") -> dict:
    return {"node": {"title": title, "handle": handle, "variants": {"edges": list(variants)}}}


def _conn(*products: dict, next_cursor: str | None = None) -> dict:
    return {
        "pageInfo": {"hasNextPage": next_cursor is not None, "endCursor": next_cursor},
        "edges": list(products),
    }


def _parse(conn: dict, card: str) -> list[dict]:
    return parse_products(conn, card, fx_jpy_per_usd=FX, cad_per_usd=CAD)


# --- parser -----------------------------------------------------------------

def test_fixture_yields_near_mint_english_records(force_of_will):
    records = _parse(force_of_will, "Force of Will")
    assert records
    for r in records:
        assert r["shop"] == "The Mythic Store"
        assert r["card"] == "Force of Will"
        assert r["condition"] == "NM"
        assert r["stock"] is None
        assert r["price_jpy"] > 0 and r["price_usd"] > 0
        assert r["link"].startswith("https://themythicstore.com/products/")
    # The Japanese Silver Scroll printing and the foil-only Retro one are
    # out; the borderless one is tagged.
    titles = {(r["set"], r["variant"]) for r in records}
    assert ("Dominaria Remastered", "borderless") in titles
    assert all("Japanese" not in r["link"] and "retro" not in r["link"] for r in records)


def test_price_is_converted_from_cad():
    conn = _conn(_product("Force of Will [Alliances]", _variant("Near Mint", "70.00")))
    (r,) = _parse(conn, "Force of Will")
    assert r["price_usd"] == 50.0
    assert r["price_jpy"] == 7500.0


def test_only_an_available_non_foil_near_mint_counts():
    conn = _conn(
        _product("Sol Ring [A]", _variant("Near Mint", "5.00", available=False),
                 _variant("Lightly Played", "4.00"), _variant("Near Mint Foil", "9.00")),
        _product("Sol Ring [B]", _variant("Lightly Played", "4.00"),
                 _variant("Near Mint", "6.00")),
        # Stray duplicate variants the store carries at 0.00.
        _product("Sol Ring [C]", _variant("Near Mint", "0.0")),
    )
    records = _parse(conn, "Sol Ring")
    assert [(r["set"], r["price_usd"]) for r in records] == [("B", round(6 / CAD, 2))]


@pytest.mark.parametrize("title", [
    "Opt (Japanese) [Strixhaven: Mystical Archive]",
    "Lightning Bolt (Japanese Foil Etched) [Strixhaven: Mystical Archive]",
    "Lightning Bolt (1638) (Rainbow Foil) [Secret Lair Drop Series]",
    "Lightning Bolt (M10) (Oversized) [Oversize Cards]",
    "Lightning Bolt [Foreign Black Border]",
    "Lightning Bolt Art Card [Strixhaven: School of Mages Art Series]",
    "Chain Lightning [Legends]",
    "Mystical Archive Playmat - Lightning Bolt",
])
def test_non_matching_printings_are_skipped(title):
    card = "Opt" if title.startswith("Opt") else "Lightning Bolt"
    assert _parse(_conn(_product(title, _variant("Near Mint", "2.00"))), card) == []


@pytest.mark.parametrize("title,number,variant", [
    ("Lightning Bolt [Magic 2011]", None, ""),
    ("Lightning Bolt (042) [Secret Lair Drop Series]", "42", ""),
    ("Lightning Bolt (001/006) [The List]", "1", ""),
    ("Lightning Bolt (Borderless Alternate Art) [Double Masters 2022]", None, "borderless"),
    ("Lightning Bolt (Retro) [30th Anniversary Edition]", None, "oldframe"),
    # A marker we have no tag for: unknown, for Scryfall to settle.
    ("Lightning Bolt (Textless) [MagicFest 2019]", None, None),
])
def test_number_and_variant_from_markers(title, number, variant):
    (r,) = _parse(_conn(_product(title, _variant("Near Mint", "2.00"))), "Lightning Bolt")
    assert (r["number"], r["variant"]) == (number, variant)
    assert r["set"] == title.rsplit("[", 1)[1].rstrip("]")


def test_multi_face_and_alternate_names_match():
    conn = _conn(
        _product("Bonecrusher Giant // Stomp (Showcase) [Throne of Eldraine]",
                 _variant("Near Mint", "1.00")),
    )
    (full,) = _parse(conn, "Bonecrusher Giant // Stomp")
    assert full["card"] == "Bonecrusher Giant // Stomp"
    assert full["variant"] == "showcase"
    assert len(_parse(conn, "Bonecrusher Giant")) == 1


def test_accented_names_match_the_ascii_titles():
    conn = _conn(_product("Lim-Dul's Vault [Commander 2013]", _variant("Near Mint", "1.00")))
    (r,) = _parse(conn, "Lim-Dûl's Vault")
    assert r["card"] == "Lim-Dûl's Vault"


# --- query building -----------------------------------------------------------

def test_search_query_folds_accents_and_filters_server_side():
    q = search_query("Lim-Dûl's Vault")
    assert q == 'title:"Lim-Dul\'s Vault" product_type:"MTG Single" available_for_sale:true'


def test_build_query_aliases_each_search_and_escapes_quotes():
    doc = build_query([search_query("Sol Ring"), search_query('Kongming, "Sleeping Dragon"')])
    assert "c0: products(first: 50, query: " in doc
    assert "c1: products(" in doc
    # JSON-escaped twice: once in the search syntax, once as a GraphQL string.
    assert r'\\\"Sleeping Dragon\\\"' in doc
    assert "after:" not in doc
    assert 'after: "abc"' in build_query(["q"], after="abc")


def test_extract_access_token():
    html = (b'<script id="shopify-features" type="application/json">'
            b'{"accessToken":"tok123","betas":[]}</script>')
    assert extract_access_token(html) == "tok123"
    assert extract_access_token(b"<html></html>") is None
    assert extract_access_token(
        b'<script id="shopify-features" type="application/json">nope</script>') is None


# --- batching -----------------------------------------------------------------

def test_batcher_merges_concurrent_searches():
    calls: list[list[str]] = []

    def run(queries):
        calls.append(queries)
        return {q: q.upper() for q in queries}

    batcher = mythic._Batcher(run, window_s=0.2, max_batch=10)
    queries = ["a", "b", "c", "a", "d"]
    results: dict[int, str] = {}

    def go(i):
        results[i] = batcher.submit(queries[i])

    threads = [threading.Thread(target=go, args=(i,)) for i in range(len(queries))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(calls) == 1
    assert sorted(calls[0]) == ["a", "b", "c", "d"]  # the duplicate shares a slot
    assert [results[i] for i in range(len(queries))] == ["A", "B", "C", "A", "D"]


def test_batcher_closes_a_full_batch_early_and_opens_another():
    calls: list[list[str]] = []
    batcher = mythic._Batcher(lambda qs: calls.append(qs) or {q: q for q in qs},
                              window_s=1.0, max_batch=2)
    t0 = time.monotonic()
    threads = [threading.Thread(target=batcher.submit, args=(q,)) for q in "abc"]
    for t in threads:
        t.start()
        time.sleep(0.02)
    for t in threads:
        t.join(5)
    assert sorted(len(c) for c in calls) == [1, 2]
    # The full batch didn't wait its window out (the second one does).
    assert time.monotonic() - t0 < 1.9


def test_batcher_failure_reaches_every_caller():
    def run(queries):
        raise ScraperFetchError("boom")

    batcher = mythic._Batcher(run, window_s=0.1, max_batch=10)
    errors = []

    def go(q):
        try:
            batcher.submit(q)
        except ScraperFetchError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(q,)) for q in "ab"]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(errors) == 2


# --- the scraper over a fake session ---------------------------------------------

class _Resp:
    def __init__(self, status: int = 200, body=None, headers=None, content: bytes | None = None):
        self.status_code = status
        self.ok = status < 400
        self.headers = headers or {}
        self.content = content if content is not None else orjson.dumps(body)


class _Session:
    def __init__(self, *posts: _Resp, homepage: _Resp | None = None):
        self.posts = list(posts)
        self.homepage = homepage or _Resp(content=(
            b'<script id="shopify-features" type="application/json">'
            b'{"accessToken":"live-token"}</script>'))
        self.sent: list[dict] = []

    def get(self, url, timeout=None):
        return self.homepage

    def post(self, url, data=None, headers=None, timeout=None):
        self.sent.append({"url": url, "query": orjson.loads(data)["query"], "headers": headers})
        return self.posts.pop(0)


def _data(*conns: dict) -> _Resp:
    return _Resp(body={"data": {f"c{i}": c for i, c in enumerate(conns)}})


def test_scraper_posts_with_the_homepage_token(force_of_will):
    session = _Session(_data(force_of_will))
    records = MythicStoreScrapper(fx=FX, session=session, cad_per_usd=CAD).get_prices("Force of Will")
    assert records
    (sent,) = session.sent
    assert sent["url"] == mythic.GRAPHQL_URL
    assert sent["headers"] == {"X-Shopify-Storefront-Access-Token": "live-token"}
    assert 'title:\\"Force of Will\\"' in sent["query"]


def test_scraper_falls_back_to_the_known_token_when_the_homepage_fails(force_of_will):
    session = _Session(_data(force_of_will), homepage=_Resp(status=429, content=b""))
    MythicStoreScrapper(fx=FX, session=session, cad_per_usd=CAD).get_prices("Force of Will")
    assert session.sent[0]["headers"]["X-Shopify-Storefront-Access-Token"] == \
        mythic.FALLBACK_ACCESS_TOKEN


def test_scraper_follows_pages_up_to_the_cap():
    page = _conn(_product("Sol Ring [A]", _variant("Near Mint", "2.00")), next_cursor="cur")
    session = _Session(*[_data(page) for _ in range(mythic.MAX_PAGES + 1)])
    records = MythicStoreScrapper(fx=FX, session=session, cad_per_usd=CAD).get_prices("Sol Ring")
    assert len(session.sent) == mythic.MAX_PAGES
    assert 'after: "cur"' in session.sent[1]["query"]
    assert len(records) == mythic.MAX_PAGES


@pytest.mark.parametrize("resp,error", [
    (_Resp(status=429, body={}, headers={"Retry-After": "30"}), RateLimitedError),
    (_Resp(status=401, body={}), ScraperFetchError),
    (_Resp(status=503, body={}), ScraperFetchError),
    (_Resp(body={"errors": [{"message": "Access denied"}]}), ScraperFetchError),
    (_Resp(body={"errors": [{"message": "slow", "extensions": {"code": "THROTTLED"}}]}),
     RateLimitedError),
    (_Resp(body={"data": {}}), ScraperFetchError),
    (_Resp(content=b"<html>"), ScraperFetchError),
])
def test_scraper_failures_raise_fetch_errors(resp, error):
    with pytest.raises(error):
        MythicStoreScrapper(fx=FX, session=_Session(resp), cad_per_usd=CAD).get_prices("Sol Ring")


def test_a_429_backs_the_shop_off():
    scraper = MythicStoreScrapper(fx=FX, session=_Session(
        _Resp(status=429, body={}, headers={"Retry-After": "30"})), cad_per_usd=CAD)
    with pytest.raises(RateLimitedError):
        scraper.get_prices("Sol Ring")
    # The next call refuses without sending anything.
    session = _Session()
    with pytest.raises(RateLimitedError):
        MythicStoreScrapper(fx=FX, session=session, cad_per_usd=CAD).get_prices("Opt")
    assert session.sent == []


def test_cad_rate_failure_is_a_fetch_error(monkeypatch):
    def fail(ccy):
        raise RuntimeError("down")

    monkeypatch.setattr(mythic, "get_fx", fail)
    with pytest.raises(ScraperFetchError):
        MythicStoreScrapper(fx=FX, session=_Session()).get_prices("Sol Ring")


def test_cad_rate_is_fetched_once(monkeypatch, force_of_will):
    calls = []
    monkeypatch.setattr(mythic, "get_fx", lambda ccy: calls.append(ccy) or 1.4)
    session = _Session(_data(force_of_will), _data(force_of_will))
    for _ in range(2):
        MythicStoreScrapper(fx=FX, session=session).get_prices("Force of Will")
    assert calls == ["cad"]
