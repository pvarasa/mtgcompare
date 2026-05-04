# mtgcompare

[![Latest Release](https://img.shields.io/github/v/release/pvarasa/mtgcompare?label=download)](https://github.com/pvarasa/mtgcompare/releases/latest)
[![Build](https://github.com/pvarasa/mtgcompare/actions/workflows/release.yml/badge.svg)](https://github.com/pvarasa/mtgcompare/actions/workflows/release.yml)

Compare *Magic: The Gathering* card prices across Japanese and US shops, track your collection, and estimate decklist costs. Runs as a local desktop app on Windows/macOS or as a self-hosted server with PostgreSQL.

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

## Download & run (macOS)

> **Requires Apple Silicon (M1 or later).** The pre-built app is compiled for arm64 and will not run on Intel Macs.

1. Go to the [latest release](https://github.com/pvarasa/mtgcompare/releases/latest) and download `mtgcompare-macos.zip`.
2. Extract the zip — you get **`MTG Compare.app`**.
3. Drag it to `/Applications` (optional but recommended).
4. **First launch only:** because the app is unsigned, macOS will block it. Right-click (or Control-click) the app icon and choose **Open**, then click **Open** again in the dialog.
5. Your browser opens automatically at `http://127.0.0.1:5000`.
6. A menu-bar icon appears — click it to reopen the browser or quit.

Your inventory is stored in `~/Library/Application Support/mtgcompare/inventory.db` and survives app updates.

---

## Features

### Price search
- **Single card** — search by name across all shops in parallel; the cheapest result is highlighted. Cold searches take ~5–10 s (bounded by the slowest shop); already-searched cards return from a 24 h DB cache in ~10–200 ms.
- **Decklist** — paste a list in `1 Sol Ring`, `4x Force of Will (ALL)`, or `1 Rhystic Study (C21) 79` format (max 100 cards per search); the app finds the best price per card and shows a per-shop spending breakdown.
- **Shipping-aware sorting** — enable per-shop shipping estimates to sort and total by true landed cost.
- **Use inventory** — check this on a decklist search and already-owned copies are deducted automatically; only the remaining quantity is priced.

### Inventory
- Add cards one at a time (Scryfall autocomplete for name and set), paste a decklist, or import a Deckbox/CardCastle CSV.
- Filter by name or purchase price; select any subset and export as a decklist `.txt` or CSV.
- In server mode, each user's inventory is isolated by the identity provided by your auth proxy.

### Market valuation
- Click **Update prices** to download MTGJSON/TCGPlayer price history for your whole collection in one go.
- See total cost basis, total market value, and unrealized PnL per lot and in aggregate.
- Price history charts show daily price movements per card.
- Prices are shared across all users; inventory is per-user.

### Shops covered

| Shop | Market | Data |
|---|---|---|
| Hareruya | 🇯🇵 JP | Price + stock + condition |
| Cardshop Serra | 🇯🇵 JP | Price + stock |
| BLACK FROG | 🇯🇵 JP | Price |
| Card Rush | 🇯🇵 JP | Price + stock |
| MINT MALL | 🇯🇵 JP | Price + stock (multi-tenant marketplace) |
| SingleStar | 🇯🇵 JP | Price + stock |
| TokyoMTG | 🇯🇵 JP | Price + stock |
| TCGPlayer (via Scryfall) | 🇺🇸 US | Market price (stock not available) |

Prices are shown in both ¥ and $ using a live FX rate fetched at startup. Results are scoped to English printings.

---

## For developers

### Requirements

- Python 3.12+
- [uv](https://github.com/astral-sh/uv)

### Install & run (local / SQLite)

```bash
uv sync
uv run mtgcompare-web      # starts the web UI at http://127.0.0.1:5000
```

Or use the start/stop helpers:

```bash
./scripts/start.sh
./scripts/stop.sh
```

### Run with Docker (PostgreSQL)

```bash
docker compose up
```

Starts the app at `http://localhost:5000` backed by a local PostgreSQL instance. Data is persisted in the `pgdata` Docker volume.

### Deploy to Kubernetes

The app is a stateless container when `DATABASE_URL` is set. Point it at your PostgreSQL instance and configure the following environment variables:

| Variable | Description |
|---|---|
| `DATABASE_URL` | SQLAlchemy PostgreSQL URL, e.g. `postgresql+psycopg2://user:pass@host/db` |
| `SECRET_KEY` | Flask session secret (required in production) |
| `WORKOS_API_KEY` | WorkOS server-side API key — presence enables the full WorkOS auth flow |
| `WORKOS_CLIENT_ID` | WorkOS workspace client ID |
| `WORKOS_REDIRECT_URI` | OAuth callback URL registered in the WorkOS dashboard |
| `WORKOS_WEBHOOK_SECRET` | Signing secret for HMAC verification on `/webhooks/workos` |
| `USER_ID_HEADER` | Legacy fallback used only when WorkOS env vars are unset: HTTP header carrying the user identity from an upstream auth proxy (default: `X-User-ID`) |
| `CRON_SECRET` | Bearer token protecting the daily price-refresh endpoint |
| `MTGJSON_CACHE_DIR` | Scratch directory for price import temp files (default: `/tmp/mtgjson`) |

When the WorkOS env vars are set, the app validates a session cookie issued by AuthKit on every non-public request; `/auth/login` redirects to AuthKit, `/auth/callback` exchanges the OAuth code, and `/webhooks/workos` syncs user CRUD events. When they are unset, the app falls back to trusting `USER_ID_HEADER` (PostgreSQL mode) or treating the user as `"local"` (SQLite / desktop mode), so local development and the packaged Windows/macOS apps continue to work without any auth configuration.

**Daily price refresh** is triggered by a `POST /internal/cron/update-prices` request with an `Authorization: Bearer <CRON_SECRET>` header. Wire this up as a K8s CronJob hitting the cluster-internal service URL so it is never exposed externally.

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
uv run pytest                                              # offline tests (default)
uv run pytest -m live                                      # live scraper tests — hits real sites
DATABASE_URL=postgresql+psycopg2://... uv run pytest -m pg # PostgreSQL bulk-load tests
```

### Building the Windows app

```powershell
.\scripts\build.ps1
```

Produces `dist/mtgcompare-windows.zip`. To publish a release, push a version tag:

```bash
git tag v1.6.0 && git push --tags
```

GitHub Actions builds and attaches the zip to the GitHub Release automatically.

### Project layout

```
mtgcompare/
  web.py          Flask UI (search, decklist, inventory, market, cron endpoint)
  auth.py         WorkOS AuthKit Blueprint (auth gate + login/callback/logout/me + webhook)
  compare.py      CLI entry point
  inventory.py    Inventory storage + CSV import (per-user scoping)
  history_import.py  MTGJSON price history pipeline (DuckDB ETL → SQLite or PostgreSQL)
  launcher.py     Packaged-app entry point (tray icon + browser open)
  db.py           SQLAlchemy engine + schema (SQLite and PostgreSQL backends)
  shops.py        Shop registry + collect_prices()
  scrappers/      Per-shop scraper implementations
  templates/      Jinja2 HTML templates
  static/         CSS, JS, images
scripts/          start/stop helpers + build.ps1
tests/            Pytest suite
Dockerfile        Multi-stage container build
docker-compose.yml  Local dev stack (app + postgres)
```

### Limitations

- FX is fetched once from `yfinance` and cached for the process lifetime.
- Card matching is case-insensitive exact match — fuzzy matching is not supported.
- Scryfall's USD price reflects TCGPlayer market price, not the cheapest individual listing.
- Shipping is a per-order flat estimate, not a live checkout quote.
- Tax, import duties, and cross-shop order splitting are not modeled.
- Cached prices age out after 24 h, but FX drift inside that window can introduce 1–2 % error on the USD column. The JPY column is always exactly what the shop charged at scrape time.
- Decklist search is capped at 100 cards per request (sized to fit a Commander deck).
