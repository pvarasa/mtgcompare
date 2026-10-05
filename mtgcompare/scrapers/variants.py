"""Printing-variant vocabulary shared by every shop.

Every record carries two optional printing fields next to ``set``:

- ``variant`` — the printing's treatment as a comma-joined list of tags
  from ``TAGS`` (``"borderless,fullart"``). The empty string means the
  regular printing: the shop marks variants and this listing has none.
  ``None`` means unknown — the shop gives no way to tell (TokyoMTG's plain
  set names cover the regular and retro-frame cards alike).
- ``number`` — the collector number, normalised by ``normalize_number``
  (``"050"`` → ``"50"``), or ``None`` when the shop doesn't print one.

Shops spell the same treatment many ways — ``(全面アート版)``,
``(フルアート)``, ``■Full Art■`` — so each parser hands its raw marker
strings to ``tags_from_markers`` instead of matching keywords itself.
Markers are the bracketed tags only, never the whole title: card names
like "Retrofitter Foundry" must not read as a retro frame.

``enrich_records`` then fills what the shop left out from the card's
Scryfall printings (memoized per search, so it costs no extra requests):
the collector number, tags the shop didn't state (Serra flags extended
art but not retro frames), and set codes for shops that print set names.
"""
import re
from collections.abc import Iterable

FULL_ART = "fullart"
BORDERLESS = "borderless"
SHOWCASE = "showcase"
EXTENDED = "extended"
OLD_FRAME = "oldframe"
# A variant the shop marks but the vocabulary has no tag for (Japanese
# alt art, Hareruya's "Alternate Frame" box toppers, TokyoMTG "Variants").
OTHER = "other"

# Display order for labels and filter chips.
TAGS: tuple[str, ...] = (FULL_ART, BORDERLESS, SHOWCASE, EXTENDED, OLD_FRAME, OTHER)

LABELS: dict[str, str] = {
    FULL_ART: "Full art",
    BORDERLESS: "Borderless",
    SHOWCASE: "Showcase",
    EXTENDED: "Extended art",
    OLD_FRAME: "Old frame",
    OTHER: "Other variant",
}
REGULAR_LABEL = "Regular"

# Lower-cased substrings per tag. Japanese spellings come from the shops'
# listing titles; English ones from Hareruya's ■…■ markers and Scryfall.
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (FULL_ART, ("全面アート", "フルアート", "full art", "fullart")),
    (BORDERLESS, ("ボーダーレス", "フレームレス", "borderless", "frameless")),
    (SHOWCASE, ("ショーケース", "showcase")),
    (EXTENDED, ("拡張アート", "拡張枠", "extended")),
    (OLD_FRAME, ("旧枠", "retro", "old frame")),
    (OTHER, ("日本画", "日限定", "alternate frame", "alt art", "variants")),
)


def tags_from_markers(markers: Iterable[str]) -> set[str]:
    """Tags named by a listing's variant markers. Unrecognised markers
    (collector numbers, "海外産ブースター版", "アーカイブ") add nothing."""
    tags: set[str] = set()
    for marker in markers:
        text = marker.lower()
        for tag, words in _KEYWORDS:
            if any(w in text for w in words):
                tags.add(tag)
    # OTHER only stands in when nothing more specific was named.
    if len(tags) > 1:
        tags.discard(OTHER)
    return tags


def encode(tags: Iterable[str]) -> str:
    """Canonical ``variant`` string: known tags in display order."""
    present = set(tags)
    return ",".join(t for t in TAGS if t in present)


def decode(variant: str | None) -> set[str] | None:
    if variant is None:
        return None
    return {t for t in variant.split(",") if t}


def label(variant: str | None) -> str:
    """Human label: "Full art · Borderless", "Regular", or "?" for unknown."""
    tags = decode(variant)
    if tags is None:
        return "?"
    if not tags:
        return REGULAR_LABEL
    return " · ".join(LABELS[t] for t in TAGS if t in tags)


_NUMBER_RE = re.compile(r"0*(\d+)(.*)")


def normalize_number(raw: str | None) -> str | None:
    """Strip leading zeros so "050", "50" and "0050" compare equal."""
    if raw is None:
        return None
    text = raw.strip().lstrip("#").strip()
    if not text:
        return None
    m = _NUMBER_RE.fullmatch(text)
    if m:
        return m.group(1) + m.group(2).strip().lower()
    return text.lower()


# Suffixes shops append to a set's name for its special printings
# (TokyoMTG: "Dominaria Remastered Variants").
_SET_NAME_SUFFIX_RE = re.compile(r"\s+(variants|alt art)$")
_PUNCT_RE = re.compile(r"[^\w\s]")


def _set_name_key(name: str) -> str:
    """"Secrets of Strixhaven: Mystical Archive" (TokyoMTG) and Scryfall's
    colon-less spelling compare equal."""
    return " ".join(_PUNCT_RE.sub(" ", name.lower()).split())


def _set_code_for(set_label: str, codes: set[str], names: dict[str, str]) -> str | None:
    if set_label.upper() in codes:
        return set_label.upper()
    key = _set_name_key(set_label)
    if key in names:
        return names[key]
    return names.get(_SET_NAME_SUFFIX_RE.sub("", key))


def _pick(candidates: list[dict], shop_tags: set[str] | None) -> list[dict]:
    """Printings in the record's set that fit what the shop said.

    Shops name the same printing differently (Double Masters' box-topper
    Force of Will is "extended art", "full art", "borderless" or
    "Alternate Frame" depending on the shop). So when the exact tags match
    nothing, the art-forward treatments — which shops use interchangeably —
    match each other, and failing that the set's only variant printing
    matches. Old frame and showcase stay distinct: they're never confused.
    """
    def tags(c: dict) -> set[str]:
        return decode(c["variant"]) or set()

    if shop_tags is None:
        return candidates
    if not shop_tags:
        return [c for c in candidates if not tags(c)]
    variants = [c for c in candidates if tags(c)]
    wanted = shop_tags - {OTHER}
    exact = [c for c in variants if wanted <= tags(c)]
    if exact:
        return exact
    if wanted and wanted <= _ART_FAMILY:
        family = [c for c in variants if tags(c) & _ART_FAMILY]
        if family:
            return family
    return variants


# Treatments shops conflate for the same frameless-art printing.
_ART_FAMILY = frozenset({FULL_ART, BORDERLESS, EXTENDED})


def enrich_records(records: list[dict], printings: list[dict]) -> list[dict]:
    """Fill ``number`` / ``variant`` / set codes in place from Scryfall.

    ``printings`` are ``scryfall.summarize_page`` summaries (``set``,
    ``set_name``, ``number``, ``variant``). A record is matched to one
    printing by set + collector number, or — without a number — by being
    the only printing in its set that fits the shop's own tags. A match
    supplies the number and *replaces* the shop's tags with Scryfall's,
    which are consistent across shops where the shops' own are not;
    several candidates that agree on the treatment still settle the tags.
    """
    if not printings:
        return records
    codes = {p["set"] for p in printings}
    names = {_set_name_key(p["set_name"]): p["set"] for p in printings if p.get("set_name")}
    by_set: dict[str, list[dict]] = {}
    for p in printings:
        by_set.setdefault(p["set"], []).append(p)

    for r in records:
        code = _set_code_for(r.get("set") or "", codes, names)
        if code is None:
            continue
        r["set"] = code
        candidates = by_set[code]
        shop_tags = decode(r.get("variant"))
        number = r.get("number")
        if number is not None:
            matches = [c for c in candidates if c["number"] == number]
        else:
            matches = _pick(candidates, shop_tags)
        if len(matches) == 1:
            r["number"] = matches[0]["number"]
            r["variant"] = matches[0]["variant"]
        elif len(matches) > 1:
            treatments = {match["variant"] for match in matches}
            if len(treatments) == 1 and shop_tags is None:
                r["variant"] = treatments.pop()
        elif shop_tags == set() and number is None:
            # Shops leave a set's own treatment unmarked: every Mystical
            # Archive card is a borderless showcase, so an unmarked one is
            # that set's base printing — its lowest collector number. The
            # number itself stays unknown; it's an inference, not a match.
            base = min(candidates, key=lambda c: _number_sort_key(c["number"]))
            r["variant"] = base["variant"]
    return records


def _number_sort_key(number: str | None) -> tuple[int, str]:
    m = re.match(r"\d+", number or "")
    return (int(m.group(0)) if m else 10**9, number or "")
