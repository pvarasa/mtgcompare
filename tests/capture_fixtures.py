"""Capture live fixtures for parser tests.

Usage: uv run python tests/capture_fixtures.py

Regenerates the snapshot files under ``tests/fixtures/`` from live
sources. Re-run when scraper canaries (``pytest -m canary``) fail or
when a shop's HTML/JSON structure is suspected to have drifted.

For the convention-based shops (the ones built on ``HtmlSearchScrapper``)
we instantiate the real scraper class so the captured fixture goes
through exactly the same headers, session, and ``search_params`` the
production scraper sends. Hareruya (two-step JSON+HTML flow) and
Scryfall (JSON, pagination) have their own custom functions.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import requests

from mtgcompare.scrappers._base import HtmlSearchScrapper
from mtgcompare.scrappers.blackfrog import BlackFrogScrapper
from mtgcompare.scrappers.cardrush import CardRushScrapper
from mtgcompare.scrappers.enndalgames import EnndalGamesScrapper
from mtgcompare.scrappers.hareruya import UNISEARCH_API, UNISEARCH_LAZY
from mtgcompare.scrappers.hareruya import make_session as hareruya_session
from mtgcompare.scrappers.mintmall import MintMallScrapper
from mtgcompare.scrappers.scryfall import SEARCH_URL as SCRYFALL_SEARCH_URL
from mtgcompare.scrappers.scryfall import make_session as scryfall_session
from mtgcompare.scrappers.serra import CardshopSerraScrapper
from mtgcompare.scrappers.singlestar import SingleStarScrapper
from mtgcompare.scrappers.tokyomtg import TokyoMtgScrapper

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURES.mkdir(exist_ok=True)

CARD = "Force of Will"


def _capture_html_shop(scraper: HtmlSearchScrapper, fixture_name: str) -> None:
    """Re-run the scraper's own GET against the live endpoint and write
    the body to ``tests/fixtures/<fixture_name>``."""
    resp = scraper.session.get(
        scraper.SEARCH_URL,
        params=scraper.search_params(CARD),
        timeout=20,
    )
    resp.raise_for_status()
    # decode_response returns bytes by default but BLACK FROG overrides
    # it to return EUC-JP decoded str — both write fine to a text file
    # since test fixtures are read with explicit encoding by the tests.
    body: Any = scraper.decode_response(resp)
    path = FIXTURES / fixture_name
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body, encoding="utf-8")
    print(f"  {scraper.SHOP_NAME}: {len(body)} bytes -> {path.name}")


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
    print(f"  Hareruya unisearch_api: {len(docs)} docs")

    payload: list[tuple[str, str]] = [("css", "itemList")]
    for i, d in enumerate(docs):
        for key, val in d.items():
            payload.append((f"docs[{i}][{key}]", str(val)))
    r = session.post(UNISEARCH_LAZY, data=payload, timeout=20)
    r.raise_for_status()
    (FIXTURES / "hareruya_force_of_will_lazy.html").write_text(r.text, encoding="utf-8")
    print(f"  Hareruya unisearch/lazy: {len(r.text)} bytes")


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
    print(f"  Scryfall /cards/search: {n} printings")


def main() -> int:
    """Captures each shop and skips (with a warning) on transport failures
    — a single down shop shouldn't block the rest of the refresh."""
    convention_shops: list[tuple[type[HtmlSearchScrapper], str]] = [
        (BlackFrogScrapper,       "blackfrog_force_of_will.html"),
        (CardRushScrapper,        "cardrush_force_of_will.html"),
        (EnndalGamesScrapper,     "enndalgames_force_of_will.html"),
        (MintMallScrapper,        "mintmall_force_of_will.html"),
        (CardshopSerraScrapper,   "serra_force_of_will.html"),
        (SingleStarScrapper,      "singlestar_force_of_will.html"),
        (TokyoMtgScrapper,        "tokyomtg_force_of_will.html"),
    ]
    print("convention-based HTML shops:")
    for cls, fname in convention_shops:
        try:
            _capture_html_shop(cls(fx=150.0), fname)
        except requests.RequestException as exc:
            print(f"  {cls.SHOP_NAME}: SKIPPED — {exc}")

    print("Hareruya:")
    try:
        capture_hareruya()
    except requests.RequestException as exc:
        print(f"  Hareruya: SKIPPED — {exc}")

    print("Scryfall:")
    try:
        capture_scryfall()
    except requests.RequestException as exc:
        print(f"  Scryfall: SKIPPED — {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
