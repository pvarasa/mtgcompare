"""Scryfall enrichment wrapper for shop scrapers.

``build_scrapers`` wraps each shop as ``CachedScrapper(EnrichingScrapper(shop))``
so the cache stores rows that already carry the Scryfall-resolved collector
number, treatment tags and set code (see ``variants.enrich_records``). The
printings come from ``scryfall.fetch_card_summaries``, memoized per search
and shared with the two TCGPlayer shops, so the same fan-out costs one
Scryfall pagination however many shops ask.

Enrichment is best-effort: a Scryfall failure returns the shop's rows as
they are rather than failing a shop that answered fine.
"""
import logging

from .base import MtgScrapper
from .scryfall import fetch_card_summaries
from .variants import enrich_records

logger = logging.getLogger("mtgcompare.scrapers.enrich")


def _complete(record: dict) -> bool:
    return record.get("number") is not None and record.get("variant") is not None


class EnrichingScrapper(MtgScrapper):
    def __init__(self, scrapper: MtgScrapper, shop_name: str):
        super().__init__()
        self.scrapper = scrapper
        self.shop_name = shop_name  # read by collect_prices' timeout report

    def get_prices(self, card_name: str) -> list[dict]:
        records = self.scrapper.get_prices(card_name)
        if all(_complete(r) for r in records):
            return records  # nothing to fill (Scryfall-sourced shops, or no rows)
        try:
            printings = fetch_card_summaries(card_name)
        except Exception as exc:  # noqa: BLE001 — enrichment must never cost a shop its rows
            logger.warning("event=enrich_failed card=%r detail=%s", card_name, exc)
            return records
        return enrich_records(records, printings)
