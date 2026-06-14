"""Single-card search result post-processing (Flask-free).

The /search (index) route fetches per-shop offers via ``collect_prices``,
then post-processes them here before rendering: collapse each marketplace
shop's multiple offers down to the active sort mode's winner, and — when
the shipping toggle is on — fill per-row shipping and re-sort by landed
total. Kept out of ``web.py`` so it's unit-testable without the app, and so
the route is left with request parsing + rendering only.
"""
from .scrapers.registry import MARKETPLACE_SHOPS


def landed_jpy(r: dict) -> float:
    """Item price plus the row's own shipping (0 when unknown/absent)."""
    return r["price_jpy"] + (r.get("ship_jpy") or 0)


def collapse_marketplace_offers(
    results: list[dict],
    include_shipping: bool,
) -> list[dict]:
    """Show one marketplace row per (shop, card, set): the active sort
    mode's winner.

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
        key = (r["shop"], r["card"], r["set"])
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
