"""Single-card search result post-processing (Flask-free).

The /search (index) route fetches per-shop offers via ``collect_prices``,
then post-processes them here before rendering: collapse each marketplace
shop's multiple offers down to the active sort mode's winner, and — when
the shipping toggle is on — fill per-row shipping and re-sort by landed
total. Kept out of ``web.py`` so it's unit-testable without the app, and so
the route is left with request parsing + rendering only.

``parse_query`` splits the optional printing filters off the search box
text — ``Force of Will set:DMR is:borderless``, ``Sol Ring #2807``,
``Force of Will (DMR) 418`` — so shops are searched by the bare name and
the filters only preselect the results-table controls. Without filter
tokens a query behaves exactly as before.
"""
import re
from dataclasses import dataclass, field

from .scrapers import variants
from .scrapers.names import has_cjk
from .scrapers.registry import MARKETPLACE_SHOPS

# Filter value meaning "the regular printing" (variant == "").
REGULAR = "regular"

_IS_ALIASES: dict[str, str] = {
    "fullart": variants.FULL_ART,
    "full-art": variants.FULL_ART,
    "borderless": variants.BORDERLESS,
    "showcase": variants.SHOWCASE,
    "extended": variants.EXTENDED,
    "extendedart": variants.EXTENDED,
    "extended-art": variants.EXTENDED,
    "oldframe": variants.OLD_FRAME,
    "old-frame": variants.OLD_FRAME,
    "retro": variants.OLD_FRAME,
    "regular": REGULAR,
}
_SET_TOKEN_RE = re.compile(r"(?:^|\s)(?:set|s|e):(\S+)", re.IGNORECASE)
_NUMBER_TOKEN_RE = re.compile(r"(?:^|\s)(?:(?:cn|number):|#)(\S+)", re.IGNORECASE)
_IS_TOKEN_RE = re.compile(r"(?:^|\s)is:(\S+)", re.IGNORECASE)
# Decklist-export style suffix: "Force of Will (DMR) 418".
_PAREN_GROUP_RE = re.compile(r"[(（]([^)）]*)[)）]")
_PAREN_SET_RE = re.compile(r"\s+\(([A-Za-z0-9]{2,6})\)(?:\s+([0-9]+[a-z★]?))?\s*$")


@dataclass
class SearchQuery:
    name: str
    set_code: str | None = None
    number: str | None = None
    # variants tags, or REGULAR; a row must carry all of them.
    variants: list[str] = field(default_factory=list)
    unknown_tokens: list[str] = field(default_factory=list)

    @property
    def has_filters(self) -> bool:
        return bool(self.set_code or self.number or self.variants)


def parse_query(q: str) -> SearchQuery:
    """Split printing-filter tokens off a search-box query."""
    text = q.strip()
    set_code: str | None = None
    number: str | None = None
    wanted: list[str] = []
    unknown: list[str] = []

    m = _PAREN_SET_RE.search(text)
    if m:
        set_code, number = m.group(1).upper(), m.group(2)
        text = text[:m.start()]
    for m in _SET_TOKEN_RE.finditer(text):
        set_code = m.group(1).upper()
    for m in _NUMBER_TOKEN_RE.finditer(text):
        number = m.group(1)
    for m in _IS_TOKEN_RE.finditer(text):
        tag = _IS_ALIASES.get(m.group(1).lower())
        if tag is None:
            unknown.append(m.group(0).strip())
        elif tag not in wanted:
            wanted.append(tag)
    for pattern in (_SET_TOKEN_RE, _NUMBER_TOKEN_RE, _IS_TOKEN_RE):
        text = pattern.sub(" ", text)
    # A treatment in parentheses, as shops title them: "Island (全面アート)",
    # "Force of Will (borderless)".
    for m in list(_PAREN_GROUP_RE.finditer(text)):
        tags = variants.tags_from_markers([m.group(1)]) - {variants.OTHER}
        if tags:
            wanted += [t for t in variants.TAGS if t in tags and t not in wanted]
            text = text.replace(m.group(0), " ")
    name = " ".join(text.split())
    # Shop-title style "島/Island": search the English half, which every
    # shop (and Scryfall) understands. "Fire // Ice" has no CJK side.
    if "//" not in name and name.count("/") == 1:
        left, right = (part.strip() for part in name.split("/"))
        if has_cjk(left) and right and not has_cjk(right):
            name = right
        elif has_cjk(right) and left and not has_cjk(left):
            name = left
    if REGULAR in wanted:
        wanted = [REGULAR]  # "regular" can't combine with a treatment
    return SearchQuery(
        name=name,
        set_code=set_code,
        number=variants.normalize_number(number),
        variants=wanted,
        unknown_tokens=unknown,
    )


def filter_options(results: list[dict]) -> dict:
    """Sets and treatments present in ``results``, for the filter controls."""
    sets: dict[str, int] = {}
    tags: set[str] = set()
    has_regular = has_unknown = False
    for r in results:
        sets[r["set"]] = sets.get(r["set"], 0) + 1
        row_tags = variants.decode(r.get("variant"))
        if row_tags is None:
            has_unknown = True
        elif not row_tags:
            has_regular = True
        else:
            tags |= row_tags
    chips = [(REGULAR, variants.REGULAR_LABEL)] if has_regular else []
    chips += [(t, variants.LABELS[t]) for t in variants.TAGS if t in tags]
    return {
        "sets": sorted(sets.items()),
        "variant_chips": chips,
        "has_unknown": has_unknown,
    }


def landed_jpy(r: dict) -> float:
    """Item price plus the row's own shipping (0 when unknown/absent)."""
    return r["price_jpy"] + (r.get("ship_jpy") or 0)


def collapse_marketplace_offers(
    results: list[dict],
    include_shipping: bool,
) -> list[dict]:
    """Show one marketplace row per (shop, card, printing): the active
    sort mode's winner.

    Marketplace scrapers emit both the cheapest-by-item-price and the
    cheapest-by-landed-total offer, because each sort mode has a
    different true cheapest. Rendering both at once reads as duplicate
    rows — and with the shipping toggle off, the landed-cost row would
    leak shipping into a view that promised not to consider it.
    """
    metric = landed_jpy if include_shipping else (lambda r: r["price_jpy"])

    kept: list[dict] = []
    winners: dict[tuple, dict] = {}
    for r in results:
        if r["shop"] not in MARKETPLACE_SHOPS:
            kept.append(r)
            continue
        # The collector number keeps a set's variant printings apart
        # (Dominaria Remastered's regular and borderless Force of Will).
        key = (r["shop"], r["card"], r["set"], r.get("number"))
        current = winners.get(key)
        if current is None or metric(r) < metric(current):
            winners[key] = r
    kept.extend(winners.values())
    return kept


def apply_shipping(results: list[dict], overrides_jpy: dict[str, int]) -> None:
    """Fill per-row shipping, compute the landed total, sort by it.

    Marketplace rows arrive with their offer's real ``ship_jpy``;
    every other row gets the flat per-shop estimate.
    """
    for r in results:
        if r.get("ship_jpy") is None:
            r["ship_jpy"] = overrides_jpy.get(r["shop"], 0)
        r["price_jpy_with_shipping"] = landed_jpy(r)
    results.sort(key=lambda r: r["price_jpy_with_shipping"])
