"""MTGJSON SetList.json → sealed-product catalog rows.

SetList.json (~12 MB) lists every set's ``sealedProduct`` entries with a
stable UUID, category/subtype and the TCGPlayer product id, so one download
gives the whole catalog without fetching per-set files.

Two kinds of product are kept:

- **Boosters** (boxes and packs): the original scope, priced and matched
  against the Japanese shops.
- **Fixed-content products**: precon decks (Commander, duel, Planeswalker,
  ...) and Secret Lair drops. Their ``contents.deck`` references
  (``{name, set}``) are resolved against MTGJSON's ``DeckList.json`` to the
  deck file names, so a held product can later be "opened" into its cards
  (see ``sealed.decks``). The resolved references live in the row's
  ``contents`` JSON: ``{"decks": [fileName, ...], "cards": [bonus card,
  ...]}``. A deck product whose references don't all resolve is still
  tracked, with ``contents`` NULL (it just can't be opened).
"""
from __future__ import annotations

import json

import requests

SETLIST_URL = "https://mtgjson.com/api/v5/SetList.json"
DECK_LIST_URL = "https://mtgjson.com/api/v5/DeckList.json"
_HEADERS = {"User-Agent": "mtgcompare/0.1", "Accept": "application/json"}

BOOSTER_CATEGORIES = ("booster_box", "booster_pack")
# Products whose contents are fixed and listed by MTGJSON as decks. Bundles
# and kits also reference decks (their land packs) but are mostly boosters,
# so opening one into "its cards" would be wrong; they stay out.
DECK_CATEGORIES = ("deck", "multiple_decks", "deck_box", "box_set")
# box_set subtypes that aren't something you own as a physical product.
_SKIPPED_SUBTYPES = ("mtgo_redemption",)


def deck_file_index(deck_list: dict) -> dict[tuple[str, str], str]:
    """``{(deck name lower, set code lower): fileName}`` from DeckList.json."""
    out: dict[tuple[str, str], str] = {}
    for d in deck_list.get("data") or []:
        name, code, file_name = d.get("name"), d.get("code"), d.get("fileName")
        if name and code and file_name:
            out.setdefault((name.strip().lower(), code.strip().lower()), file_name)
    return out


def _bonus_cards(contents: dict) -> list[dict]:
    """``contents.card``: individually listed extra cards (a Secret Lair
    countdown kit's foil, ...). Each has a uuid, so it maps exactly."""
    cards = []
    for c in contents.get("card") or []:
        if c.get("uuid") and c.get("name") and c.get("set"):
            cards.append({
                "name": c["name"], "set": c["set"].upper(), "number": str(c.get("number") or ""),
                "uuid": c["uuid"], "foil": bool(c.get("foil")),
            })
    return cards


def _deck_contents(product: dict, decks: dict[tuple[str, str], str]) -> str | None:
    refs = (product.get("contents") or {}).get("deck") or []
    files = [decks.get(((r.get("name") or "").strip().lower(), (r.get("set") or "").strip().lower()))
             for r in refs]
    if not files or not all(files):
        return None
    return json.dumps({"decks": files, "cards": _bonus_cards(product["contents"])}, sort_keys=True)


def _kept(product: dict) -> bool:
    category = product.get("category")
    if category in BOOSTER_CATEGORIES:
        return True
    return (
        category in DECK_CATEGORIES
        and product.get("subtype") not in _SKIPPED_SUBTYPES
        and bool((product.get("contents") or {}).get("deck"))
    )


def parse_setlist(
    payload: dict, *, updated_at: str, deck_files: dict[tuple[str, str], str] | None = None,
) -> list[dict]:
    """Extract tracked sealed products from a SetList.json document.

    ``deck_files`` is ``deck_file_index(DeckList.json)``; without it deck
    products are still listed but none of them can be opened.
    """
    decks = deck_files or {}
    rows: list[dict] = []
    seen: set[str] = set()
    for s in payload.get("data") or []:
        for p in s.get("sealedProduct") or []:
            if not _kept(p):
                continue
            uuid = p.get("uuid")
            name = (p.get("name") or "").strip()
            if not uuid or not name or uuid in seen:
                continue
            seen.add(uuid)
            pid = (p.get("identifiers") or {}).get("tcgplayerProductId")
            rows.append({
                "uuid": uuid,
                "set_code": (p.get("setCode") or s.get("code") or "").upper(),
                "set_name": s.get("name"),
                "name": name,
                "category": p["category"],
                "subtype": p.get("subtype"),
                "release_date": p.get("releaseDate") or s.get("releaseDate"),
                "tcgplayer_product_id": int(pid) if pid and str(pid).isdigit() else None,
                "contents": None if p["category"] in BOOSTER_CATEGORIES else _deck_contents(p, decks),
                "updated_at": updated_at,
            })
    return rows


def set_names(payload: dict) -> dict[str, str]:
    """``{SET CODE: set name}`` for every set in SetList.json. Deck cards
    come from several sets (a Commander deck reprints across many), and
    MTGJSON's deck files carry only the code."""
    return {
        s["code"].upper(): s["name"]
        for s in payload.get("data") or []
        if s.get("code") and s.get("name")
    }


def fetch_setlist() -> dict:
    resp = requests.get(SETLIST_URL, headers=_HEADERS, timeout=(20, 120))
    resp.raise_for_status()
    return resp.json()


def fetch_deck_list() -> dict:
    resp = requests.get(DECK_LIST_URL, headers=_HEADERS, timeout=(20, 60))
    resp.raise_for_status()
    return resp.json()
