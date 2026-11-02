"""MTGJSON SetList.json → sealed-product catalog rows.

SetList.json (~12 MB) lists every set's ``sealedProduct`` entries with a
stable UUID, category/subtype and the TCGPlayer product id, so one download
gives the whole catalog without fetching per-set files.
"""
from __future__ import annotations

import requests

SETLIST_URL = "https://mtgjson.com/api/v5/SetList.json"
_HEADERS = {"User-Agent": "mtgcompare/0.1", "Accept": "application/json"}

# Only boosters are tracked: boxes are the point, packs are cheap to add.
# Decks, bundles, Secret Lairs etc. are deliberately out of scope.
TRACKED_CATEGORIES = ("booster_box", "booster_pack")


def parse_setlist(payload: dict, *, updated_at: str) -> list[dict]:
    """Extract tracked sealed products from a SetList.json document."""
    rows: list[dict] = []
    seen: set[str] = set()
    for s in payload.get("data") or []:
        for p in s.get("sealedProduct") or []:
            if p.get("category") not in TRACKED_CATEGORIES:
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
                "updated_at": updated_at,
            })
    return rows


def fetch_setlist() -> dict:
    resp = requests.get(SETLIST_URL, headers=_HEADERS, timeout=(20, 120))
    resp.raise_for_status()
    return resp.json()
