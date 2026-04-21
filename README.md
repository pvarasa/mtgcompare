# mtgcompare

Compares *Magic: The Gathering* card prices across several shops, normalizes
them to JPY and USD, and provides both a CLI and a local Flask web UI for
searching, decklist estimation, and inventory tracking.

## Shops

| Source                   | Market | Data                                 | How                                          |
|--------------------------|--------|--------------------------------------|----------------------------------------------|
| Hareruya                 | JP     | per-printing price + stock + condition | internal JSON search + lazy-render endpoints |
| TCGPlayer (via Scryfall) | US     | per-printing market price            | Scryfall public REST API                     |
| SingleStar               | JP     | per-printing price + stock           | server-rendered search HTML                  |
| TokyoMTG                 | JP     | per-printing price + stock           | server-rendered search HTML                  |

Search results are scoped to English printings. Shop scrapers return only
in-stock results where that data is available. TCGPlayer/Scryfall does not
expose live stock, so those records have `stock: null`.

## Requirements

- Python 3.12+
- [uv](https://github.com/astral-sh/uv)

## Install

```bash
uv sync
```

This creates `.venv/` and installs all dependencies locked in `uv.lock`.

Repo layout:

- `mtgcompare/` holds the application package, Flask assets, and scraper code.
- `scripts/` holds the start/stop helper scripts.
- root-level `app.py` and `compare.py` are thin compatibility wrappers.

## Usage

### CLI

```bash
uv run mtgcompare -c "Force of Will"
uv run python -m mtgcompare.compare -c "Force of Will"
uv run python -m mtgcompare.compare -f examples/card_list.txt
uv run python -m mtgcompare.compare -f examples/card_list.txt -e prices.json
```

### Web UI

```bash
uv run mtgcompare-web
uv run python -m mtgcompare.web
```

Then visit <http://127.0.0.1:5000>.

### Search

- The Search page has two modes: `Single card` and `Decklist`.
- Single-card search shows per-printing results across all shops.
- The cheapest row is highlighted.
- You can optionally include per-shop shipping and sort by card price plus shipping.
- FX is fetched once per process and reused.
- Click the card icon to preview the card art via Scryfall.

### Decklist search

- Paste lines like `1 Sol Ring`, `4x Force of Will (ALL)`, or `1 Rhystic Study (C21) 79`.
- Duplicate card names are consolidated before pricing.
- The app fetches each unique card in parallel, then picks the cheapest result per card.
- The result page shows a deck total, a per-shop cost breakdown, and any cards that were not found.
- Shipping defaults can be adjusted per shop and are included in the totals.

### Inventory

Your owned cards are stored locally in `inventory.db`.

There are three add flows:

1. Single card
   A form with Scryfall-powered name autocomplete and a set dropdown populated from that card's prints.
2. Paste decklist
   Accepts formats like `1 Sol Ring`, `1x Rhystic Study (C21)`, and `1 Cyclonic Rift (CMR) 79`.
   Lines are batch-resolved through Scryfall's `/cards/collection` endpoint, then shown in an editable preview table before commit.
3. Import CSV
   Imports a Deckbox/CardCastle-style CSV in either replace or append mode.

Decklist preview behavior:

- `Add to inventory` stays disabled until you click `Resolve & preview` and at least one card resolves.
- Resolved rows default the date to today.
- The preview `Set` field is editable, but only as the 3-character set code.

CLI equivalents:

```bash
uv run python -m mtgcompare.inventory import binder.csv
uv run python -m mtgcompare.inventory import extras.csv --append
uv run python -m mtgcompare.inventory stats
```

### Market

- The Market page values your inventory using cached Scryfall collection lookups.
- The first visit after adding cards shows inventory rows but no prices until you click `Refresh prices`.
- Cached rows are keyed by `(card_name, normalized set_code, foil/non-foil)`.
- The page shows total cost basis, total market value, unrealized PnL, and per-lot rows.
- Cost basis is whatever USD value you stored in inventory; market value is refreshed from Scryfall.

Startup helpers:

```bash
./scripts/start.sh
./scripts/stop.sh
```

## CLI arguments

```text
-c, --card CARD       name of the card to search for
-f, --file FILE       file with one card name per line
-e, --export EXPORT   file name to export all prices found as JSON
```

`-c` and `-f` are mutually exclusive; one is required. `-e` writes every
record returned, not just the cheapest one, with a timestamp.

## Output

For each card, the CLI prints the cheapest record as JSON:

```json
{
  "shop": "TCGPlayer (Scryfall)",
  "card": "Force of Will",
  "set": "SOA",
  "price_jpy": 10605.15,
  "price_usd": 66.65,
  "stock": null,
  "condition": "NM",
  "link": "https://partner.tcgplayer.com/..."
}
```

## Testing

```bash
uv run pytest
uv run pytest -m live
```

Offline tests use captured fixtures under `tests/fixtures/`. Live tests hit
the real sources and are the contract check for upstream drift.

### Refresh fixtures

```bash
uv run python tests/capture_fixtures.py
```

Inspect the diff before accepting fixture changes.

## Limitations

- FX comes from `yfinance` and is cached once per process in the web app.
- Hareruya pagination is not followed; results are capped to page 1.
- Card matching is case-insensitive exact match.
- Scryfall's USD price is TCGPlayer market price, not the cheapest listing.
- Shipping is modeled as a per-order estimate, not a live checkout quote.
- Tax, fees, and cross-shop order splitting are not modeled.
