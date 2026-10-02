"""Card-name matching shared by the shop scrapers.

The app searches with Scryfall's canonical names, and Scryfall (which also
feeds the search autocomplete) names every multi-face card ``Front // Back``
— double-faced, MDFC, adventure and split cards alike. The Japanese shops
each render those differently once the Japanese half of the listing title is
stripped:

    Bonecrusher Giant // Stomp                    TokyoMTG, MINT MALL
    Bonecrusher Giant                             SingleStar, Card Rush, BLACK FROG, Hareruya
    Delver of Secrets // Insectile Aberration     Hareruya (built from 《A》/《B》)
    Bonecrusher Giant - 踏みつけ / Stomp           Cardshop Serra
    Fire+Ice  /  Fire + Ice                       most shops, for split cards

Comparing the raw strings matched none of those for a ``Front // Back``
query, so every multi-face card searched from the autocomplete came back
empty from most shops. ``matched_name`` compares *faces* instead, with the
same rule the Scryfall scraper uses: the full name, the same set of faces,
or any one face. The last case also picks up alternate-name printings such
as Secret Lair's "Vivi's Thunder Magic // Lightning Bolt" for a plain
"Lightning Bolt" search.
"""
import re

# Japanese (and full-width) runs are dropped so a title like Serra's
# "Bonecrusher Giant - 踏みつけ / Stomp" reduces to its English faces.
_CJK_RE = re.compile(r"[　-ヿ㐀-鿿＀-￯]+")
# Full-width separators are mapped to ASCII before the CJK strip eats them.
_FULLWIDTH = str.maketrans({"＋": "+", "／": "/"})
# Face separators: "//", "/", "+" and a spaced " - " (Serra). Hyphens inside
# a name ("Mirror-Breaker") have no surrounding spaces, so they survive.
_FACE_SEP_RE = re.compile(r"\s*(?://|/|\+|\s-\s)\s*")
# Parenthesised flavour text some shops append, e.g. "(The Fantastic Four)".
_PAREN_RE = re.compile(r"\([^)]*\)")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def faces(name: str) -> list[str]:
    """Lower-cased English faces of a card or listing name."""
    text = _CJK_RE.sub(" ", name.translate(_FULLWIDTH))
    text = _PAREN_RE.sub(" ", text)
    return [f for f in (_norm(p) for p in _FACE_SEP_RE.split(text)) if f]


def matched_name(listing_name: str, card_name: str) -> str | None:
    """The name to report if ``listing_name`` is a printing of ``card_name``.

    Returns the listing's own text when it is literally the same name (so
    records keep the shop's canonical casing, as before), ``card_name`` for
    any other match, and None when the listing is another card.
    """
    listing, target = faces(listing_name), faces(card_name)
    if not listing or not target:
        return None
    if listing == target:
        same_text = len(target) == 1 and _norm(listing_name) == target[0]
        return listing_name.strip() if same_text else card_name
    if len(target) == 1:
        # Plain name: any face of the listing, e.g. an alternate-name
        # "Flavour // Lightning Bolt" printing, or one half of a split card.
        return card_name if target[0] in listing else None
    # Multi-face query against a listing that only names the front face.
    if len(listing) == 1 and listing[0] == target[0]:
        return card_name
    return None


def query_variants(card_name: str) -> list[str]:
    """Search terms to try in order: the name as given, then — for a
    ``Front // Back`` name — the front face alone, which the keyword search
    of shops that only list the front face needs to find anything."""
    name = card_name.strip()
    front = name.split("//", 1)[0].strip()
    return [name, front] if front and front != name else [name]
