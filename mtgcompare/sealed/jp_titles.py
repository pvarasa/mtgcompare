"""Parse Japanese shop titles for sealed boosters and match them to the catalog.

Japanese shops name sealed product in one regular shape:

    Hareruya:   (36パック)《ブルームバロウ プレイ・ブースターBOX》《○日本語版》[BLB]
    SingleStar: ブルームバロウ 日本語版 プレイブースター 1BOX

``parse_*_title`` turns a title into a ``JpListing`` (set, booster type,
box/pack, language) or None for anything that isn't a single, sound,
English/Japanese booster box or pack (bundles, multi-box lots, damaged
"訳あり" stock, single cards). ``match_product`` then picks the catalog row.

SingleStar titles carry no set code, so ``set_name_key`` normalises the
Japanese set name for a name → code map built from Hareruya's titles (which
carry both); MTGJSON has no Japanese set names.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Booster-type word immediately before ブースター → MTGJSON sealedProduct
# subtypes it can be. No type word means the set's one main booster, which
# MTGJSON files as "draft" (modern sets) or "default" (older ones).
# MTGJSON files one-off booster kinds (Beyond, Epilogue) as "other", so
# those also require an English word in the product name.
_TYPE_SUBTYPES: dict[str | None, tuple[tuple[str, ...], str | None]] = {
    "プレイ": (("play",), None),
    "ドラフト": (("draft",), None),
    "セット": (("set",), None),
    "コレクター": (("collector",), None),
    "ジャンプスタート": (("jumpstart",), None),
    "ビヨンド": (("other",), "Beyond"),
    "エピローグ": (("other",), "Epilogue"),
    None: (("draft", "default", "jumpstart", "premium"), None),
}
_TYPE_WORDS = "プレイ|ドラフト|セット|コレクター|ジャンプスタート|ビヨンド|エピローグ"
_TYPE_RE = re.compile(rf"({_TYPE_WORDS})?・?\s*ブースター")
# Hareruya set codes that differ from MTGJSON's.
_SET_CODE_ALIASES = {"10ED": "10E", "4EDBB": "4BB", "PO2": "P02"}
_LANG_RE = re.compile(r"(日本語|英語)版")
_LANG = {"日本語": "JP", "英語": "EN"}
_PRODUCT_PREFIX_RE = re.compile(r"^マジック\s*[:：]\s*ザ・ギャザリング\s*[|｜\-－]\s*")
# Words that disqualify a listing: damaged stock, multi-unit lots,
# special editions the catalog files separately.
_REJECT = ("訳あり", "パックセット", "セット販売", "シュリンク破れ", "外箱破損")


@dataclass(frozen=True)
class JpListing:
    set_code: str | None
    set_name: str            # Japanese set name as the shop wrote it
    booster_type: str | None  # key of _TYPE_SUBTYPES
    category: str            # booster_box / booster_pack
    language: str            # EN / JP


def set_name_key(name: str) -> str:
    """Normalise a Japanese set name for cross-shop lookup.

    NFKC folds full/half-width (：/:, ２/2); then the "Magic: The
    Gathering | " crossover prefix, spaces and punctuation are dropped.
    """
    s = unicodedata.normalize("NFKC", name).strip()
    s = _PRODUCT_PREFIX_RE.sub("", s)
    return re.sub(r"[\s・:：\-－|｜'’\"“”!！]", "", s).lower()


def _language(text: str) -> str | None:
    m = _LANG_RE.search(text)
    return _LANG[m.group(1)] if m else None


def parse_hareruya_title(title: str) -> JpListing | None:
    """Parse a Hareruya sealed title; None if it isn't a booster box/pack."""
    if any(w in title for w in _REJECT):
        return None
    m = re.match(r"^(?:【[^】]*】)*\((\d+)パック\)《([^》]+)》(.*)$", title)
    if not m:
        return None
    packs, inner, rest = int(m.group(1)), m.group(2), m.group(3)
    code = re.search(r"\[([A-Z0-9]{2,6})\]", rest)
    lang = _language(inner + rest)
    tm = _TYPE_RE.search(inner)
    if not (code and lang and tm):
        return None
    after = inner[tm.end():]
    if after.startswith("BOX"):
        category = "booster_box"
    elif after.startswith("パック") and packs == 1:
        category = "booster_pack"
    else:
        return None
    return JpListing(
        set_code=_SET_CODE_ALIASES.get(code.group(1), code.group(1)),
        set_name=inner[:tm.start()].strip(),
        booster_type=tm.group(1),
        category=category,
        language=lang,
    )


def parse_singlestar_title(title: str) -> JpListing | None:
    """Parse a SingleStar sealed title (no set code; resolve via set_name)."""
    if any(w in title for w in _REJECT):
        return None
    m = re.match(r"^(.+?)\s*(日本語|英語)版\s*(.+)$", title)
    if not m:
        return None
    set_name, lang, rest = m.group(1).strip(), _LANG[m.group(2)], m.group(3)
    tm = re.match(rf"^({_TYPE_WORDS})?・?\s*ブースター\s*1\s*(BOX|パック)", rest)
    if not tm:
        return None
    return JpListing(
        set_code=None,
        set_name=set_name,
        booster_type=tm.group(1),
        category="booster_box" if tm.group(2) == "BOX" else "booster_pack",
        language=lang,
    )


def resolve_set_code(set_name: str, name_map: dict[str, str]) -> str | None:
    """Set code for a Japanese set name via ``name_map`` (``set_name_key`` →
    code). Falls back to a unique prefix match either way, since shops
    shorten names ("ストリクスヘイヴン" vs "ストリクスヘイヴン：魔法学院")."""
    key = set_name_key(set_name)
    if not key:
        return None
    if key in name_map:
        return name_map[key]
    codes = {code for k, code in name_map.items() if k.startswith(key) or key.startswith(k)}
    return codes.pop() if len(codes) == 1 else None


def _ascii_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def match_product(listing: JpListing, set_code: str, catalog_by_set: dict[str, list[dict]]) -> dict | None:
    """Pick the catalog product a listing is for.

    Candidates share set, category and an allowed subtype. Several can
    remain (e.g. "Collector Booster Pack" and "Collector Booster Sample
    Pack"); the canonical one has the shortest name. Japan-only editions
    ("War of the Spark Japanese Booster Box") win for JP listings and are
    excluded for EN ones. Jumpstart boosters MTGJSON files under their own
    set (Foundations → J25) are found by English set-name prefix.
    """
    subtypes, name_word = _TYPE_SUBTYPES[listing.booster_type]

    def fits(p: dict) -> bool:
        return (
            p["category"] == listing.category
            and p.get("subtype") in subtypes
            and (name_word is None or name_word in p["name"])
        )

    in_set = catalog_by_set.get(set_code, [])
    candidates = [p for p in in_set if fits(p)]
    if not candidates and listing.booster_type == "ジャンプスタート" and in_set:
        prefix = _ascii_key(in_set[0].get("set_name") or "")
        candidates = [
            p for rows in catalog_by_set.values() for p in rows
            if fits(p) and prefix and _ascii_key(p["name"]).startswith(prefix)
        ]
    if not candidates:
        return None
    japanese = [p for p in candidates if "Japanese" in p["name"]]
    if listing.language == "JP" and japanese:
        candidates = japanese
    elif japanese and len(japanese) < len(candidates):
        candidates = [p for p in candidates if "Japanese" not in p["name"]]
    # Prefer the subtype listed first (draft before default), then shortest.
    return min(candidates, key=lambda p: (subtypes.index(p["subtype"]), len(p["name"]), p["name"]))
