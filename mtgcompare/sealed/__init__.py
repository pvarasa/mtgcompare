"""Sealed-product (booster box / pack / precon / Secret Lair) price tracking.

Kept apart from the card stack because nothing in it maps onto a card's
(name, set, collector number, foil) identity:

- ``catalog``   — MTGJSON SetList.json ``sealedProduct`` → catalog rows,
  deck products linked to their MTGJSON deck files.
- ``decks``     — MTGJSON deck files → inventory card records ("opening" a
  precon or Secret Lair into the card inventory).
- ``tcgplayer`` — TCGPlayer market price + price-history endpoints, keyed
  by the product id MTGJSON already carries (no name matching needed).
- ``repo``      — data access for the ``sealed_*`` tables.
- ``service``   — orchestration (catalog refresh, daily price refresh,
  backfill) and the /sealed page computation.

MTGJSON's AllPrices feed carries no sealed prices (checked 2026-11), which
is why prices come from TCGPlayer directly.
"""
