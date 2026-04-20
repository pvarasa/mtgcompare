"""Capture live fixtures for parser tests.

Usage: uv run python tests/capture_fixtures.py

Regenerates the snapshot files under tests/fixtures/ from live sources.
Re-run this when you suspect the scrapers' parsers have drifted out of
date with the real upstream APIs.
"""
from pathlib import Path

from mtgcompare.scrappers.hareruya import (
    UNISEARCH_API,
    UNISEARCH_LAZY,
    make_session as hareruya_session,
)
from mtgcompare.scrappers.scryfall import (
    SEARCH_URL as SCRYFALL_SEARCH_URL,
    make_session as scryfall_session,
)
from mtgcompare.scrappers.singlestar import (
    SEARCH_URL as SINGLESTAR_SEARCH_URL,
    make_session as singlestar_session,
)
from mtgcompare.scrappers.tokyomtg import (
    SEARCH_URL as TOKYOMTG_SEARCH_URL,
    make_session as tokyomtg_session,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURES.mkdir(exist_ok=True)

CARD = "Force of Will"


def capture_hareruya() -> None:
    session = hareruya_session()
    params = {
        "kw": CARD,
        "fq.price": "1~*",
        "fq.foil_flg": "0",
        "fq.language": "2",
        "fq.stock": "1~*",
        "rows": "60",
        "page": "1",
    }
    r = session.get(UNISEARCH_API, params=params, timeout=20)
    r.raise_for_status()
    (FIXTURES / "hareruya_force_of_will_unisearch.json").write_text(r.text, encoding="utf-8")
    docs = r.json()["response"]["docs"]
    print(f"  unisearch_api: {len(docs)} docs -> hareruya_force_of_will_unisearch.json")

    payload = [("css", "itemList")]
    for i, d in enumerate(docs):
        for key, val in d.items():
            payload.append((f"docs[{i}][{key}]", str(val)))
    r = session.post(UNISEARCH_LAZY, data=payload, timeout=20)
    r.raise_for_status()
    (FIXTURES / "hareruya_force_of_will_lazy.html").write_text(r.text, encoding="utf-8")
    print(f"  unisearch/lazy: {len(r.text)} bytes -> hareruya_force_of_will_lazy.html")


def capture_scryfall() -> None:
    session = scryfall_session()
    r = session.get(
        SCRYFALL_SEARCH_URL,
        params={"q": f'!"{CARD}"', "unique": "prints"},
        timeout=20,
    )
    r.raise_for_status()
    (FIXTURES / "scryfall_force_of_will.json").write_text(r.text, encoding="utf-8")
    n = len(r.json().get("data", []))
    print(f"  /cards/search: {n} printings -> scryfall_force_of_will.json")


def capture_singlestar() -> None:
    session = singlestar_session()
    r = session.get(SINGLESTAR_SEARCH_URL, params={"keyword": CARD}, timeout=20)
    r.raise_for_status()
    (FIXTURES / "singlestar_force_of_will.html").write_text(r.text, encoding="utf-8")
    print(f"  /product-list: {len(r.text)} bytes -> singlestar_force_of_will.html")


def capture_tokyomtg() -> None:
    session = tokyomtg_session()
    r = session.get(
        TOKYOMTG_SEARCH_URL,
        params={"query": CARD, "p": "q"},
        timeout=20,
    )
    r.raise_for_status()
    (FIXTURES / "tokyomtg_force_of_will.html").write_text(r.text, encoding="utf-8")
    print(f"  /cardpage.html: {len(r.text)} bytes -> tokyomtg_force_of_will.html")


def main() -> None:
    print("hareruya:")
    capture_hareruya()
    print("scryfall:")
    capture_scryfall()
    print("singlestar:")
    capture_singlestar()
    print("tokyomtg:")
    capture_tokyomtg()


if __name__ == "__main__":
    main()
