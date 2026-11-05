"""MTGJSON deck files → inventory card records ("opening" a precon).

A fixed-content sealed product (a Commander precon, a Secret Lair drop)
carries in its catalog ``contents`` the MTGJSON deck file names it is made
of, plus any individually listed bonus cards (see ``sealed.catalog``).
``decks/<fileName>.json`` lists every card with its exact printing (set,
collector number, foil) and MTGJSON uuid, so the cards map to market prices
without the name matching the price import otherwise needs.

Parsing is pure (``parse_deck`` / ``merge_cards``); ``load_deck`` is the
only network call, cached in-process because a printed deck never changes.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from threading import Lock

import requests

DECK_URL = "https://mtgjson.com/api/v5/decks/{file_name}.json"
_HEADERS = {"User-Agent": "mtgcompare/0.1", "Accept": "application/json"}
# Deck file names are CamelCase plus "_SETCODE" (``VirtueAndValor_WOC``).
# They come from our own catalog, never the request, but the URL is still
# only built from a name of that shape.
_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,120}$")

# Boards that are part of the physical product. ``displayCommander`` is the
# oversized/display copy of the commander (already in ``commander``) and
# ``tokens`` aren't inventory cards.
MAIN_BOARDS = ("commander", "mainBoard", "planes", "schemes")
SIDE_BOARD = "sideBoard"

_CACHE_TTL_S = 24 * 3600
_CACHE_MAX = 128
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = Lock()


def _printing(card: dict) -> str:
    # The inventory knows Normal / Foil only; an etched card is a foil-class
    # finish, priced under MTGJSON's foil-side history like the rest.
    return "Foil" if card.get("isFoil") or card.get("isEtched") else "Normal"


def _is_basic(card: dict) -> bool:
    return "Basic" in (card.get("supertypes") or []) and "Land" in (card.get("types") or [])


def parse_deck(payload: dict, *, include_sideboard: bool = False) -> list[dict]:
    """One deck file → card dicts (unmerged, one per deck entry).

    Keys: ``card_name, set_code, card_number, quantity, printing, uuid,
    is_basic``.
    """
    deck = payload.get("data") or {}
    boards = MAIN_BOARDS + ((SIDE_BOARD,) if include_sideboard else ())
    cards: list[dict] = []
    for board in boards:
        for c in deck.get(board) or []:
            name, set_code, uuid = c.get("name"), c.get("setCode"), c.get("uuid")
            if not name or not set_code or not uuid:
                continue
            cards.append({
                "card_name": name,
                "set_code": set_code.upper(),
                "card_number": str(c.get("number") or ""),
                "quantity": max(1, int(c.get("count") or 1)),
                "printing": _printing(c),
                "uuid": uuid,
                "is_basic": _is_basic(c),
            })
    return cards


def bonus_card_records(bonus: list[dict]) -> list[dict]:
    """The catalog's bonus cards (``contents.cards``) as card dicts. Their
    type line isn't listed, so they never count as basics."""
    return [
        {"card_name": b["name"], "set_code": b["set"], "card_number": b["number"],
         "quantity": 1, "printing": "Foil" if b.get("foil") else "Normal",
         "uuid": b["uuid"], "is_basic": False}
        for b in bonus
    ]


def merge_cards(cards: list[dict]) -> list[dict]:
    """Collapse repeats of one printing (same uuid + finish), summing counts.
    A deck can list a card in two boards, or two decks of one product can
    share a printing. Order of first appearance is kept."""
    merged: dict[tuple[str, str], dict] = {}
    for c in cards:
        key = (c["uuid"], c["printing"])
        if key in merged:
            merged[key]["quantity"] += c["quantity"]
        else:
            merged[key] = dict(c)
    return list(merged.values())


def parse_contents(raw: str | None) -> dict | None:
    """The catalog row's ``contents`` JSON, or None when it isn't openable."""
    if not raw:
        return None
    try:
        contents = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(contents, dict) or not contents.get("decks"):
        return None
    return contents


def fetch_deck(file_name: str) -> dict:
    if not _FILE_NAME_RE.match(file_name):
        raise ValueError(f"bad deck file name {file_name!r}")
    resp = requests.get(DECK_URL.format(file_name=file_name), headers=_HEADERS, timeout=(10, 60))
    resp.raise_for_status()
    return resp.json()


# The only card fields ``parse_deck`` reads. A deck file carries ~50 per
# card (rulings, legalities, foreign data...), ~500 KB per deck: caching it
# whole would let a stream of preview requests pin hundreds of MB.
_CARD_FIELDS = ("name", "setCode", "number", "count", "isFoil", "isEtched", "uuid", "supertypes", "types")


def _trim(payload: dict) -> dict:
    deck = payload.get("data") or {}
    return {"data": {
        board: [{k: c[k] for k in _CARD_FIELDS if k in c} for c in deck.get(board) or []]
        for board in (*MAIN_BOARDS, SIDE_BOARD)
    }}


def load_deck(file_name: str, *, fetch: Callable[[str], dict] | None = None) -> dict:
    """``fetch_deck`` (trimmed to what ``parse_deck`` reads) behind a small
    TTL cache: preview, then commit, each need the deck, and the second
    read shouldn't cost another 500 KB download."""
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(file_name)
        if hit and now - hit[0] < _CACHE_TTL_S:
            return hit[1]
    payload = _trim((fetch or fetch_deck)(file_name))
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
        _cache[file_name] = (now, payload)
    return payload


def cache_clear() -> None:
    with _cache_lock:
        _cache.clear()


def product_cards(
    contents: dict, *, include_sideboard: bool,
    load: Callable[[str], dict],
) -> list[dict]:
    """Every card of a product (all its decks plus bonus cards), merged."""
    cards: list[dict] = []
    for file_name in contents["decks"]:
        cards.extend(parse_deck(load(file_name), include_sideboard=include_sideboard))
    cards.extend(bonus_card_records(contents.get("cards") or []))
    return merge_cards(cards)
