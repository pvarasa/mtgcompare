import pytest
import requests

from mtgcompare.scrapers.html_base import HtmlSearchScrapper
from mtgcompare.scrapers.names import faces, matched_name, query_variants


@pytest.mark.parametrize("listing", [
    "Bonecrusher Giant // Stomp",                 # TokyoMTG, MINT MALL
    "Bonecrusher Giant",                          # front face only
    "Bonecrusher Giant - 踏みつけ / Stomp",        # Cardshop Serra
])
def test_multi_face_query_matches_every_shop_rendering(listing):
    assert matched_name(listing, "Bonecrusher Giant // Stomp") == "Bonecrusher Giant // Stomp"


@pytest.mark.parametrize("listing", ["Fire+Ice", "Fire + Ice", "Fire // Ice", "Fire＋Ice"])
def test_split_card_renderings(listing):
    assert matched_name(listing, "Fire // Ice") == "Fire // Ice"


def test_alternate_name_printing_matches_plain_name():
    # Secret Lair "Vivi's Thunder Magic" is a Lightning Bolt printing.
    assert matched_name("Vivi's Thunder Magic // Lightning Bolt", "Lightning Bolt") == "Lightning Bolt"


def test_exact_match_keeps_the_listing_spelling():
    assert matched_name("Force of Will", "force of will") == "Force of Will"


def test_flavour_suffix_reports_the_searched_name():
    assert matched_name("Sol Ring (The Fantastic Four)", "Sol Ring") == "Sol Ring"


@pytest.mark.parametrize("listing,card", [
    ("Blightning", "Lightning Bolt"),
    ("Sword of Fire and Ice", "Fire // Ice"),
    ("Delver of Secrets by Nils Hamm from Innistrad (Backorder)", "Delver of Secrets"),
    ("Insectile Aberration", "Delver of Secrets // Insectile Aberration"),
    ("Mirror-Breaker", "Fable of the Mirror-Breaker"),
])
def test_other_cards_do_not_match(listing, card):
    assert matched_name(listing, card) is None


def test_hyphenated_names_stay_one_face():
    assert faces("Fable of the Mirror-Breaker") == ["fable of the mirror-breaker"]


def test_query_variants():
    assert query_variants("Sol Ring") == ["Sol Ring"]
    assert query_variants("Bonecrusher Giant // Stomp") == ["Bonecrusher Giant // Stomp", "Bonecrusher Giant"]


class _FakeResponse:
    def __init__(self, body: str):
        self.status_code = 200
        self.content = body.encode()
        self.headers: dict = {}


class _FakeSession:
    """Serves canned bodies keyed by (query, page) and records each call."""

    def __init__(self, pages: dict[tuple[str, int], str]):
        self.pages = pages
        self.calls: list[tuple[str, int]] = []

    def get(self, url, params=None, timeout=None):
        key = (params["keyword"], int(params.get("page", 1)))
        self.calls.append(key)
        return _FakeResponse(self.pages.get(key, ""))


class _PagedShop(HtmlSearchScrapper):
    SHOP_NAME = "Paged"
    SEARCH_URL = "https://shop.example/search"
    LOGGER_NAME = "test.paged"
    PAGE_PARAM = "page"
    MAX_PAGES = 3

    def parse_html(self, html, card_name):
        body = html.decode() if isinstance(html, bytes) else html
        return [{"shop": self.SHOP_NAME, "card": card_name, "row": row}
                for row in body.split("|")[0].split(",") if row]


def _shop(pages):
    session = _FakeSession(pages)
    return _PagedShop(fx=150.0, session=session), session


def test_follows_next_page_links_until_the_last_page():
    shop, session = _shop({
        ("Sol Ring", 1): "a,b|<a href='?keyword=x&amp;page=2'>2</a>",
        ("Sol Ring", 2): "c|<a href='?page=1'>1</a>",
    })
    assert [r["row"] for r in shop.get_prices("Sol Ring")] == ["a", "b", "c"]
    assert session.calls == [("Sol Ring", 1), ("Sol Ring", 2)]


def test_stops_at_max_pages():
    shop, session = _shop({("X", n): f"r{n}|?page={n + 1}" for n in range(1, 10)})
    assert len(shop.get_prices("X")) == 3
    assert len(session.calls) == 3


def test_page_two_link_is_not_mistaken_for_page_twenty():
    shop, session = _shop({("X", 1): "r1|?page=20"})
    shop.get_prices("X")
    assert session.calls == [("X", 1)]


def test_multi_face_name_falls_back_to_the_front_face():
    shop, session = _shop({("Bonecrusher Giant", 1): "hit"})
    records = shop.get_prices("Bonecrusher Giant // Stomp")
    assert [r["row"] for r in records] == ["hit"]
    assert session.calls == [("Bonecrusher Giant // Stomp", 1), ("Bonecrusher Giant", 1)]


def test_no_fallback_when_the_full_name_finds_rows():
    shop, session = _shop({("Fire // Ice", 1): "hit"})
    shop.get_prices("Fire // Ice")
    assert session.calls == [("Fire // Ice", 1)]


def test_transport_errors_still_propagate():
    class _Boom(_FakeSession):
        def get(self, *a, **k):
            raise requests.ConnectionError("down")

    shop = _PagedShop(fx=150.0, session=_Boom({}))
    with pytest.raises(Exception, match="fetch failed"):
        shop.get_prices("X")
