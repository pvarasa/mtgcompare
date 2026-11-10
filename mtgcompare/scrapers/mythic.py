"""The Mythic Store (themythicstore.com) scraper — Quebec, Canada.

A Shopify store (inventory synced from BinderPOS), priced in CAD. It ships
to Japan: ``/meta.json`` lists ``JP`` in ``ships_to_countries``, and a cart
quote for a Tokyo address on 2026-11-10 offered ChitChats Select at CA$8.95
(10–30 days) for a single card.

**Why the Storefront GraphQL API.** Probed 2026-11-10, every option:

- ``/search`` HTML, ``/products/<handle>.js`` and ``/products.json`` sit
  behind a Cloudflare rule that 429s scripted clients (``Retry-After: 60``)
  after a handful of requests and keeps the IP blocked for 30+ minutes;
  ``/search`` refused the very first request from the dev box.
- ``/search/suggest.json`` stays reachable but only carries a product's
  min/max price across conditions, not the NM price.
- ``/api/<version>/graphql.json`` answered 140 requests (60 back-to-back,
  60 at 8 concurrent, 20 from the cluster) without a single 429, returns
  every printing with each condition's price and sale state, filters
  server-side (singles only, in stock), and takes many searches per request
  as aliased ``products`` fields: a 30-card decklist was one 2.2 s request.

The API needs a Storefront access token. There is no token issued to us:
this is the public one Shopify embeds in every page of the store (the
``accessToken`` in the ``shopify-features`` JSON block) for its own
client-side features. It is read from the homepage and kept for a day,
with the last value seen as the fallback when the homepage can't be read;
a rotated token then fails as a clean ``ScraperFetchError`` (and in the
canary), not as silent empty results. Its scope doesn't include inventory
counts (``quantityAvailable`` → ACCESS_DENIED), so ``stock`` is None.

**Batching.** The decklist fan-out asks every shop about one card at a
time, many cards at once. ``_Batcher`` merges the searches that arrive
within ``BATCH_WINDOW_S`` into one aliased request (at most
``MAX_BATCH`` cards), so a decklist costs Mythic a handful of requests
rather than one per card. Only cache misses reach it: ``CachedScrapper``
answers the rest. A single-card search waits the window out alone.

**Listings.** One product per printing, titled like TCGPlayer::

    Lightning Bolt [Magic 2011]
    Lightning Bolt (Borderless Alternate Art) [Double Masters 2022]
    Lightning Bolt (042) [Secret Lair Drop Series]
    Bonecrusher Giant // Stomp (Showcase) [Throne of Eldraine]
    Opt (Japanese) [Strixhaven: Mystical Archive]

Variants are the condition × finish: "Near Mint", "Lightly Played", …,
"Near Mint Foil", …. Only an available "Near Mint" (non-foil) variant of an
English printing becomes a record, matching the other shops. The bracket is
the set *name*; the Scryfall enrichment maps it to a code.

The ``parse_products`` / ``build_query`` / ``extract_access_token``
functions are pure and are what tests exercise.
"""
import json
import logging
import re
import threading
import unicodedata
from concurrent.futures import Future
from time import monotonic

import orjson
import requests

from ..utils import get_fx
from .base import MtgScrapper
from .html_base import (
    RateLimitedError,
    ScraperFetchError,
    ShopThrottle,
    decode_json_response,
    make_session,
    raise_for_response,
    retry_after_s,
    to_jpy,
)
from .names import matched_name
from .variants import encode, normalize_number, tags_from_markers

SHOP_NAME = "The Mythic Store"

BASE_URL = "https://themythicstore.com"
API_VERSION = "2025-07"
GRAPHQL_URL = f"{BASE_URL}/api/{API_VERSION}/graphql.json"

# Seen in the homepage's shopify-features block on 2026-11-10.
FALLBACK_ACCESS_TOKEN = "b1b14ed105b8a49a05ffd489a0d2b774"  # noqa: S105 — public, embedded in every page
_TOKEN_TTL_S = 24 * 3600.0

PRODUCT_TYPE = "MTG Single"
# 50 is enough for every staple probed in stock (Sol Ring: 34). A card
# that overflows gets follow-up pages on its own, up to MAX_PAGES.
PAGE_SIZE = 50
MAX_PAGES = 3
# Each product has ten variants (5 conditions × foil/non-foil); a few
# carry stray duplicates ("NM", "LP" at 0.00) on top.
_VARIANTS_PER_PRODUCT = 20

BATCH_WINDOW_S = 0.25
# A 30-card request reported a query cost of 3,390 and was accepted; 25
# keeps a margin under whatever the ceiling is.
MAX_BATCH = 25
REQUEST_TIMEOUT_S = 20.0

_CONDITION = "Near Mint"

_TITLE_RE = re.compile(
    r"^(?P<name>.+?)\s*(?P<markers>(?:\([^)]*\)\s*)*)\[(?P<set>[^\]]+)\]\s*$"
)
_MARKER_RE = re.compile(r"\(([^)]*)\)")
# "(042)", "(0901)", "(001/006)" (The List), "(141/249)".
_NUMBER_MARKER_RE = re.compile(r"^(\d+[a-z]?)(?:/\d+)?$", re.IGNORECASE)
# Printings that aren't the plain English card: other languages, foil-only
# treatments, oversized cards.
_SKIP_MARKER_WORDS = (
    "japanese", "phyrexian", "chinese", "korean", "russian", "german",
    "french", "italian", "spanish", "portuguese", "foil", "oversized",
)
_SKIP_SETS = frozenset({"foreign black border", "oversize cards"})


def fold_accents(text: str) -> str:
    """"Lim-Dûl's Vault" → "Lim-Dul's Vault". The store's titles are plain
    ASCII and its search doesn't fold accents itself (the accented query
    finds nothing)."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def search_query(card_name: str) -> str:
    """The ``products(query:)`` string for one card. Titles carry the full
    ``Front // Back`` name, and the title search finds them from either
    the full name or the front face, so no retry with the front face is
    needed."""
    name = fold_accents(card_name).strip()
    return (
        f"title:{json.dumps(name)} product_type:{json.dumps(PRODUCT_TYPE)}"
        " available_for_sale:true"
    )


_PRODUCT_FIELDS = (
    "pageInfo { hasNextPage endCursor } "
    "edges { node { title handle "
    f"variants(first: {_VARIANTS_PER_PRODUCT}) "
    "{ edges { node { title availableForSale price { amount currencyCode } } } } } }"
)


def build_query(queries: list[str], after: str | None = None) -> str:
    """One GraphQL document searching every query, aliased ``c0``…``cN``.

    Values are embedded as JSON string literals (valid GraphQL strings)
    rather than variables, because the alias count varies per batch.
    """
    cursor = f", after: {json.dumps(after)}" if after else ""
    fields = "\n".join(
        f"c{i}: products(first: {PAGE_SIZE}, query: {json.dumps(q)}{cursor}) {{ {_PRODUCT_FIELDS} }}"
        for i, q in enumerate(queries)
    )
    return "query {\n" + fields + "\n}"


_FEATURES_RE = re.compile(
    rb'<script[^>]+id="shopify-features"[^>]*>(?P<json>.*?)</script>', re.DOTALL
)


def extract_access_token(html: bytes) -> str | None:
    """The Storefront token from the homepage's ``shopify-features`` block."""
    m = _FEATURES_RE.search(html)
    if not m:
        return None
    try:
        token = orjson.loads(m.group("json")).get("accessToken")
    except (orjson.JSONDecodeError, AttributeError):
        return None
    return token if isinstance(token, str) and token else None


def _markers(raw: str) -> list[str]:
    return [m.strip() for m in _MARKER_RE.findall(raw)]


def parse_products(
    connection: dict,
    card_name: str,
    *,
    fx_jpy_per_usd: float,
    cad_per_usd: float,
) -> list[dict]:
    """Records for one aliased ``products`` connection."""
    records: list[dict] = []
    folded_card = fold_accents(card_name)
    for edge in connection.get("edges") or []:
        node = edge.get("node") or {}
        m = _TITLE_RE.match((node.get("title") or "").strip())
        if not m:
            continue
        set_name = m.group("set").strip()
        if set_name.lower() in _SKIP_SETS:
            continue
        markers = _markers(m.group("markers"))
        if any(w in mk.lower() for mk in markers for w in _SKIP_MARKER_WORDS):
            continue
        listing = m.group("name").strip()
        card = matched_name(listing, card_name)
        if card is None and matched_name(fold_accents(listing), folded_card) is not None:
            card = card_name
        if card is None:
            continue

        price_cad = _near_mint_price(node)
        if price_cad is None:
            continue

        number = None
        treatment = []
        for mk in markers:
            nm = _NUMBER_MARKER_RE.match(mk)
            if nm and number is None:
                number = normalize_number(nm.group(1))
            else:
                treatment.append(mk)
        tags = tags_from_markers(treatment)
        # A marker we can't name ("Textless", "Toppers", a player's name)
        # still says it isn't the regular printing: leave it to Scryfall.
        variant = encode(tags) if tags or not treatment else None

        price_usd = round(price_cad / cad_per_usd, 2)
        records.append({
            "shop": SHOP_NAME,
            "card": card,
            "set": set_name,
            "number": number,
            "variant": variant,
            "price_jpy": to_jpy(price_cad / cad_per_usd, fx_jpy_per_usd),
            "price_usd": price_usd,
            "stock": None,
            "condition": "NM",
            "link": f"{BASE_URL}/products/{node.get('handle')}",
        })
    return records


def _near_mint_price(node: dict) -> float | None:
    for edge in (node.get("variants") or {}).get("edges") or []:
        v = edge.get("node") or {}
        if v.get("title") != _CONDITION or not v.get("availableForSale"):
            continue
        price = v.get("price") or {}
        if price.get("currencyCode") not in (None, "CAD"):
            continue
        try:
            amount = float(price.get("amount") or "")
        except ValueError:
            continue
        if amount > 0:
            return amount
    return None


class _Batcher:
    """Merges concurrent searches into one request.

    The first caller with no open batch leads: it waits ``window_s`` for
    others to join, closes the batch and runs it; joiners block on their
    Future. A full batch closes early. Identical queries share one slot.
    """

    def __init__(self, run, window_s: float, max_batch: int):
        self._run = run
        self._window_s = window_s
        self._max_batch = max_batch
        self._lock = threading.Lock()
        self._open: dict[str, Future] | None = None
        self._full = threading.Event()

    def submit(self, query: str) -> dict:
        with self._lock:
            batch = self._open
            if batch is not None and (query in batch or len(batch) < self._max_batch):
                fut = batch.setdefault(query, Future())
                if len(batch) >= self._max_batch:
                    self._full.set()
                lead = False
            else:
                fut = Future()
                batch = {query: fut}
                self._open = batch
                self._full = full = threading.Event()
                lead = True
        if not lead:
            return fut.result()

        full.wait(self._window_s)
        with self._lock:
            if self._open is batch:
                self._open = None
        try:
            results = self._run(list(batch))
        except BaseException as exc:
            for f in batch.values():
                f.set_exception(exc)
        else:
            for q, f in batch.items():
                f.set_result(results[q])
        return fut.result()


class _CadRate:
    """CAD per USD, re-fetched every few hours like the web layer's JPY rate."""

    TTL_S = 6 * 3600.0
    RETRY_S = 60.0

    def __init__(self):
        self._lock = threading.Lock()
        self._rate: float | None = None
        self._at = 0.0
        self._failed_at: float | None = None

    def get(self) -> float:
        with self._lock:
            now = monotonic()
            if self._rate is not None and now - self._at < self.TTL_S:
                return self._rate
            if self._failed_at is not None and now - self._failed_at < self.RETRY_S:
                if self._rate is not None:
                    return self._rate
                raise ScraperFetchError(f"{SHOP_NAME}: no CAD rate (recent FX failure)")
            try:
                rate = get_fx("cad")
            except Exception as exc:  # noqa: BLE001 — any FX failure is a fetch failure here
                self._failed_at = now
                if self._rate is not None:
                    return self._rate
                raise ScraperFetchError(f"{SHOP_NAME}: CAD rate unavailable: {exc}") from exc
            self._rate, self._at, self._failed_at = rate, now, None
            return rate


_CAD_RATE = _CadRate()


class _Token:
    def __init__(self):
        self._lock = threading.Lock()
        self._value = FALLBACK_ACCESS_TOKEN
        self._at: float | None = None

    def get(self, session: requests.Session, logger: logging.Logger) -> str:
        with self._lock:
            if self._at is not None and monotonic() - self._at < _TOKEN_TTL_S:
                return self._value
            self._at = monotonic()
            try:
                resp = session.get(BASE_URL + "/", timeout=REQUEST_TIMEOUT_S)
                token = extract_access_token(resp.content) if resp.ok else None
            except requests.RequestException as exc:
                logger.warning("event=token_refresh_failed shop=%r detail=%s", SHOP_NAME, exc)
                return self._value
            if token is None:
                logger.warning(
                    "event=token_refresh_failed shop=%r status=%s detail=no token on homepage",
                    SHOP_NAME, resp.status_code,
                )
            elif token != self._value:
                logger.info("event=token_rotated shop=%r", SHOP_NAME)
                self._value = token
            return self._value


_TOKEN = _Token()
_SESSION = make_session({"Accept": "application/json", "Content-Type": "application/json"})
_THROTTLE = ShopThrottle(SHOP_NAME, max_concurrent=4)
_LOGGER = logging.getLogger("mtgcompare.scrapers.mythic")


def _post_graphql(document: str, session: requests.Session) -> dict:
    token = _TOKEN.get(session, _LOGGER)
    with _THROTTLE.slot():
        try:
            resp = session.post(
                GRAPHQL_URL,
                data=orjson.dumps({"query": document}),
                headers={"X-Shopify-Storefront-Access-Token": token},
                timeout=REQUEST_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            raise ScraperFetchError(f"{SHOP_NAME} fetch failed: {exc}") from exc
    if resp.status_code == 429:
        _THROTTLE.back_off(retry_after_s(resp))
    if resp.status_code in (401, 403):
        raise ScraperFetchError(
            f"{SHOP_NAME} HTTP {resp.status_code} — the storefront token may have rotated"
        )
    raise_for_response(resp, SHOP_NAME)
    body = decode_json_response(resp, SHOP_NAME)
    errors = body.get("errors")
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else errors
        message = first.get("message") if isinstance(first, dict) else first
        code = (first.get("extensions") or {}).get("code") if isinstance(first, dict) else None
        if code == "THROTTLED":
            _THROTTLE.back_off(60.0)
            raise RateLimitedError(f"{SHOP_NAME} GraphQL throttled")
        raise ScraperFetchError(f"{SHOP_NAME} GraphQL error: {message}")
    data = body.get("data")
    if not isinstance(data, dict):
        raise ScraperFetchError(f"{SHOP_NAME} GraphQL response has no data")
    return data


def _search_many(queries: list[str], session: requests.Session = _SESSION) -> dict[str, dict]:
    """One aliased request → {query: products connection}."""
    data = _post_graphql(build_query(queries), session)
    out = {}
    for i, q in enumerate(queries):
        conn = data.get(f"c{i}")
        if not isinstance(conn, dict):
            raise ScraperFetchError(f"{SHOP_NAME} GraphQL response is missing c{i}")
        out[q] = conn
    return out


_BATCHER = _Batcher(_search_many, BATCH_WINDOW_S, MAX_BATCH)


class MythicStoreScrapper(MtgScrapper):
    def __init__(
        self,
        fx: float | None = None,
        session: requests.Session | None = None,
        cad_per_usd: float | None = None,
    ):
        super().__init__()
        self.fx = fx if fx is not None else get_fx("jpy")
        self.cad_per_usd = cad_per_usd
        # A custom session (tests) bypasses the shared batcher.
        self.session = session
        self.logger = _LOGGER

    def get_prices(self, card_name: str) -> list[dict]:
        t0 = monotonic()
        cad = self.cad_per_usd if self.cad_per_usd is not None else _CAD_RATE.get()
        query = search_query(card_name)
        if self.session is None:
            conn = _BATCHER.submit(query)
            session = _SESSION
        else:
            session = self.session
            conn = _search_many([query], session)[query]
        connections = [conn]
        pages = 1
        while (
            pages < MAX_PAGES
            and (conn.get("pageInfo") or {}).get("hasNextPage")
            and (cursor := (conn.get("pageInfo") or {}).get("endCursor"))
        ):
            conn = _post_graphql(build_query([query], after=cursor), session)["c0"]
            connections.append(conn)
            pages += 1
        records = [
            r for c in connections
            for r in parse_products(c, card_name, fx_jpy_per_usd=self.fx, cad_per_usd=cad)
        ]
        self.logger.info(
            "event=shop_query shop=%r card=%r rows=%d pages=%d duration_ms=%d",
            SHOP_NAME, card_name, len(records), pages, int((monotonic() - t0) * 1000),
        )
        return records


def _reset_for_tests() -> None:
    """Forget process-wide state (token freshness, CAD rate, back-off)."""
    global _TOKEN, _CAD_RATE
    _TOKEN = _Token()
    _CAD_RATE = _CadRate()
    _THROTTLE.reset()


__all__ = [
    "MythicStoreScrapper",
    "SHOP_NAME",
    "build_query",
    "extract_access_token",
    "fold_accents",
    "parse_products",
    "search_query",
]
