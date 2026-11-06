# mtgcompare

[![Latest Release](https://img.shields.io/github/v/release/pvarasa/mtgcompare?label=download)](https://github.com/pvarasa/mtgcompare/releases/latest)
[![Build](https://github.com/pvarasa/mtgcompare/actions/workflows/release.yml/badge.svg)](https://github.com/pvarasa/mtgcompare/actions/workflows/release.yml)

Compare *Magic: The Gathering* card prices across Japanese and US shops, track your collection, and estimate decklist costs. Runs as a local desktop app on Windows/macOS or as a self-hosted server with PostgreSQL.

**New to the app?** Read the illustrated [user manual](docs/user-manual.md).

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
- Add cards one at a time (Scryfall autocomplete for name and set), paste a decklist, open a Commander precon or Secret Lair (every card at its exact printing, priced at today's market price), or import a Deckbox/CardCastle CSV.
- Filter by name (substring match), set, condition or purchase price; sort by any column header; paginated for fast loads on large collections.
- Page-scoped selection for `Delete selected`, `Export decklist`, `Export CSV`; a "Select all NN matching" banner lets you bulk-delete every row matching the current filter across all pages.
- In server mode, each user's inventory is isolated by the identity provided by your auth proxy.

### Market valuation
- Click **Update prices** to download MTGJSON/TCGPlayer price history for your whole collection in one go.
- See total cost basis, total market value, and unrealized PnL per lot and in aggregate (computed across the whole filter, not just the visible page).
- Sort by any column — including derived ones like PnL or market value — and paginate at 25 / 50 / 100 / 200 rows per page.
- Price history charts show daily price movements per card.
- Prices are shared across all users; inventory is per-user.

### Sealed products
- Track booster boxes, packs, Commander precons and Secret Lairs (English or Japanese) separately from your cards, plus a watchlist.
- English lots are valued at TCGPlayer's market price; Japanese lots at the cheapest in-stock Japanese-language offer at Hareruya, SingleStar or Cardshop Serra, with Hareruya's buylist shown alongside.
- **Open** a precon or Secret Lair you hold to add its cards to your inventory.
- Prices refresh daily, with about a year of TCGPlayer history backfilled for each new product.

### Shops covered

| Shop | Market | Data |
|---|---|---|
| Hareruya | 🇯🇵 JP | Price + stock + condition |
| Cardshop Serra | 🇯🇵 JP | Price + stock |
| BLACK FROG | 🇯🇵 JP | Price |
| Card Rush | 🇯🇵 JP | Price + stock |
| MINT MALL | 🇯🇵 JP | Price + stock (multi-tenant marketplace). **Paused** since 2026-10-22 while the site is down for a security review |
| SingleStar | 🇯🇵 JP | Price + stock |
| TokyoMTG | 🇯🇵 JP | Price + stock |
| TCGPlayer market (via Scryfall) | 🇺🇸 US | Market price (stock not available) |
| TCGPlayer → JP | 🇺🇸 US | Cheapest NM/LP offers that ship to Japan — item price shown; each offer's real shipping is applied when *Include shipping* is on (single-card search only) |

Prices are shown in both ¥ and $ using a live FX rate fetched at startup. Results are scoped to English printings.

---

## For developers

### Requirements

- Python 3.14+
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
  "shop": "TCGPlayer market",
  "card": "Force of Will",
  "set": "SOA",
  "price_jpy": 10605.15,
  "price_usd": 66.65,
  "stock": null,
  "condition": "NM",
  "link": "https://www.tcgplayer.com/product/...?Language=English&Printing=Normal"
}
```

### Testing

```bash
uv run pytest                                              # offline tests (default)
uv run pytest -m canary                                    # live scraper tests — hits real sites
DATABASE_URL=postgresql+psycopg2://... uv run pytest -m pg # PostgreSQL bulk-load tests
uv run --group e2e pytest -m e2e                           # Playwright UI tests
```

### Building the Windows app

```powershell
.\scripts\build.ps1
```

Produces `dist/mtgcompare-windows.zip`. To publish a release, push a `v*` tag:

```bash
git tag vX.Y.Z && git push --tags
```

GitHub Actions builds the Windows and macOS zips, attaches them to the GitHub Release, and publishes the Docker image to GHCR.

### Project layout

```
mtgcompare/
  web.py          Flask layer: routes, middleware, SSE transport
  decklist.py     Decklist pricing domain (parse → deduct → fan-out → totals)
  search.py       Single-card result post-processing (marketplace collapse, shipping sort)
  jobs.py         In-memory background-job registry
  meta.py         Data access for the app_meta key/value table
  pricing/        Market + MTGJSON price-history package
    common.py         shared card-identity helpers, price-store factory, MTGJSON fetch
    import_service.py MTGJSON download, card→UUID mapping, import + daily cron update
    market_service.py the /market computation + price-history series
    history_store.py  PriceHistoryStore (DuckDB-file vs PostgreSQL backends)
    history_import.py MTGJSON price history ETL (DuckDB → DuckDB file or PostgreSQL)
    market_repo.py    data access for market_prices + mtgjson_card_map
    run_log.py        daily price-update run bookkeeping
  scrapers/       Scraper stack
    base.py           MtgScrapper ABC
    html_base.py      HtmlSearchScrapper convention base + shared session/error/JSON/FX helpers
    names.py          multi-face-aware card-name matching shared by the shop parsers
    variants.py       printing-variant tags (borderless, showcase, ...)
    enrich.py         resolves each listing's printing against Scryfall
    ocnk.py           listing parser shared by the ocnk.net shops (Card Rush, SingleStar)
    cache.py          CachedScrapper DB-cache wrapper
    registry.py       shop registry (incl. marketplace flag) + collect_prices()
    <shop>.py         per-shop implementations
  sealed/         Sealed products: MTGJSON catalog, precon decks, TCGPlayer prices,
                  Japanese shop crawlers, /sealed page computation
  inventory.py    Inventory storage + CSV import (per-user scoping)
  auth.py         WorkOS AuthKit Blueprint (auth gate + login/callback/logout/me + webhook)
  db.py           SQLAlchemy engine + schema (SQLite and PostgreSQL backends)
  compare.py      CLI entry point
  launcher.py     Packaged-app entry point (tray icon + browser open)
  templates/      Jinja2 HTML templates
  static/         CSS, JS, images
scripts/          start/stop helpers + build.ps1
tests/            Pytest suite
Dockerfile        Multi-stage container build
docker-compose.yml  Local dev stack (app + postgres)
```

### Limitations

- FX is the ECB reference rate (Frankfurter, with open.er-api.com as
  fallback), re-fetched every 6 hours.
- Card matching is case-insensitive exact match — fuzzy matching is not supported.
- Scryfall's USD price reflects TCGPlayer market price, not the cheapest individual listing; the **TCGPlayer → JP** shop covers the listing side, but only for single-card search — marketplace per-card shipping doesn't sum into decklist totals.
- Shipping is a per-order flat estimate, not a live checkout quote.
- Tax, import duties, and cross-shop order splitting are not modeled.
- Cached prices age out after 24 h, but FX drift inside that window can introduce 1–2 % error on the USD column. The JPY column is always exactly what the shop charged at scrape time.
- Decklist search is capped at 100 cards per request (sized to fit a Commander deck).
