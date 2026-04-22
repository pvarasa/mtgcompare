# mtgcompare

[![Latest Release](https://img.shields.io/github/v/release/pvarasa/mtgcompare?label=download)](https://github.com/pvarasa/mtgcompare/releases/latest)
[![Build](https://github.com/pvarasa/mtgcompare/actions/workflows/release.yml/badge.svg)](https://github.com/pvarasa/mtgcompare/actions/workflows/release.yml)

Compare *Magic: The Gathering* card prices across Japanese and US shops, track your collection, and estimate decklist costs — all running locally on your machine.

---

## Download & run (Windows)

1. Go to the [latest release](https://github.com/pvarasa/mtgcompare/releases/latest) and download `mtgcompare-windows.zip`.
2. Extract the zip anywhere (Desktop, Documents, etc.).
3. Double-click **`mtgcompare.exe`**.
4. Your browser opens automatically at `http://127.0.0.1:5000`.
5. A tray icon appears — right-click it to reopen the browser or quit.

Your inventory is stored in `%APPDATA%\mtgcompare\inventory.db` and survives app updates.

> **Windows SmartScreen warning:** because the exe is unsigned you may see a blue "Windows protected your PC" dialog. Click **More info → Run anyway** to proceed.

---

## Features

### Price search
- **Single card** — search by name across all shops simultaneously; the cheapest result is highlighted.
- **Decklist** — paste a list in `1 Sol Ring`, `4x Force of Will (ALL)`, or `1 Rhystic Study (C21) 79` format; the app finds the best price per card and shows a per-shop spending breakdown.
- **Shipping-aware sorting** — enable per-shop shipping estimates to sort and total by true landed cost.
- **Use inventory** — check this on a decklist search and already-owned copies are deducted automatically; only the remaining quantity is priced.

### Inventory
- Add cards one at a time (Scryfall autocomplete for name and set), paste a decklist, or import a Deckbox/CardCastle CSV.
- Filter by name or purchase price; select any subset and export as a decklist `.txt` or CSV.

### Market valuation
- Click **Refresh prices** to fetch current Scryfall/TCGPlayer market prices for your whole collection in one go.
- See total cost basis, total market value, and unrealized PnL per lot and in aggregate.

### Shops covered

| Shop | Market | Data |
|---|---|---|
| Hareruya | 🇯🇵 JP | Price + stock + condition |
| SingleStar | 🇯🇵 JP | Price + stock |
| TokyoMTG | 🇯🇵 JP | Price + stock |
| TCGPlayer (via Scryfall) | 🇺🇸 US | Market price (stock not available) |

Prices are shown in both ¥ and $ using a live FX rate fetched at startup. Results are scoped to English printings.

---

## For developers

### Requirements

- Python 3.12+
- [uv](https://github.com/astral-sh/uv)

### Install & run

```bash
uv sync
uv run mtgcompare-web      # starts the web UI at http://127.0.0.1:5000
```

Or use the start/stop helpers:

```bash
./scripts/start.sh
./scripts/stop.sh
```

### CLI

```bash
uv run mtgcompare -c "Force of Will"
uv run python -m mtgcompare.compare -f examples/card_list.txt -e prices.json
```

```text
-c, --card CARD       name of the card to search for
-f, --file FILE       file with one card name per line
-e, --export EXPORT   export all results to JSON
```

### Inventory CLI

```bash
uv run python -m mtgcompare.inventory import binder.csv
uv run python -m mtgcompare.inventory import extras.csv --append
uv run python -m mtgcompare.inventory stats
```

### CLI output

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

### Testing

```bash
uv run pytest          # offline tests (default)
uv run pytest -m live  # live scraper tests — hits real sites
```

### Building the Windows app

```powershell
.\scripts\build.ps1
```

Produces `dist/mtgcompare-windows.zip`. To publish a release, push a version tag:

```bash
git tag v1.2.0 && git push --tags
```

GitHub Actions builds and attaches the zip to the GitHub Release automatically.

### Project layout

```
mtgcompare/
  web.py          Flask UI (search, decklist, inventory, market)
  compare.py      CLI entry point
  inventory.py    Inventory storage + CSV import
  launcher.py     Packaged-app entry point (tray icon + browser open)
  db.py           SQLite connection + schema
  shops.py        Shop registry + collect_prices()
  scrappers/      Per-shop scraper implementations
  templates/      Jinja2 HTML templates
  static/         CSS, JS, images
scripts/          start/stop helpers + build.ps1
tests/            Pytest suite
```

### Limitations

- FX is fetched once from `yfinance` and cached for the process lifetime.
- Hareruya results are capped to page 1 (no pagination).
- Card matching is case-insensitive exact match — fuzzy matching is not supported.
- Scryfall's USD price reflects TCGPlayer market price, not the cheapest individual listing.
- Shipping is a per-order flat estimate, not a live checkout quote.
- Tax, import duties, and cross-shop order splitting are not modeled.
