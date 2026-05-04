# CLAUDE.md

Repo-specific guidance for coding sessions.

## Layout

- `mtgcompare/`
  Main application package.
- `mtgcompare/web.py`
  Flask UI entry point for search, decklist, inventory, and market pages.
- `mtgcompare/history_import.py`
  MTGJSON price history pipeline: XZ → NDJSON → DuckDB ETL → DuckDB file (local) or PostgreSQL (remote).
- `mtgcompare/compare.py`
  CLI price comparison entry point.
- `mtgcompare/inventory.py`
  Inventory storage and inventory CLI. All public functions accept a `user_id` parameter.
- `mtgcompare/launcher.py`
  Packaged-app entry point: starts Flask in a daemon thread, opens the browser, runs the system-tray icon loop.
- `mtgcompare/db.py`
  SQLAlchemy engine, schema, and DB path resolution. Supports SQLite (local) and PostgreSQL (remote) via `DATABASE_URL`.
- `mtgcompare/auth.py`
  WorkOS AuthKit integration. Exposes the `auth.bp` Flask Blueprint (login/callback/logout/me + `/webhooks/workos`), the `before_app_request` auth gate, and pure helpers (`verify_access_token`, `verify_webhook`, etc.). Inert when `WORKOS_API_KEY`/`WORKOS_CLIENT_ID`/`WORKOS_REDIRECT_URI` are unset.
- `mtgcompare/scrappers/`
  Shop scraper implementations.
- `mtgcompare/templates/`, `mtgcompare/static/`
  Flask templates and browser assets.
- `scripts/`
  Start/stop helpers for local development; `build.ps1` for packaging.
- `Dockerfile`, `.dockerignore`, `docker-compose.yml`
  Container build and local dev stack (app + postgres).
- root `app.py`, `compare.py`
  Thin compatibility wrappers.

## Import conventions

- Use package-relative imports inside `mtgcompare/`.
- Use `mtgcompare...` imports from tests and external entry points.
- Keep parser functions pure and keep network I/O in scraper classes.

## Runtime paths

- In development: `logging.conf`, `inventory.db`, `app.log`, `app.err.log`, and `app.pid` live at repo root. Resolve them relative to repo root, not the package directory.
- When frozen (PyInstaller): `inventory.db` and `app.log` go to `%APPDATA%\mtgcompare\`; `logging.conf` is bundled alongside the exe and found via `__file__`-relative resolution as normal.
- In Docker/PostgreSQL mode: `inventory.db` is not used; `MTGJSON_CACHE_DIR` (default `/tmp/mtgjson`) holds ephemeral XZ/NDJSON/CSV scratch files during price imports.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | _(absent)_ | SQLAlchemy URL; absent → local SQLite, set → PostgreSQL |
| `SECRET_KEY` | `mtgcompare-local-dev` | Flask session secret; must be set in production |
| `WORKOS_API_KEY` | _(absent)_ | WorkOS server-side API key. Presence enables the full WorkOS auth flow. |
| `WORKOS_CLIENT_ID` | _(absent)_ | WorkOS workspace client ID; required alongside `WORKOS_API_KEY` |
| `WORKOS_REDIRECT_URI` | _(absent)_ | OAuth callback URL registered in the WorkOS dashboard, e.g. `https://mtg.vpablo.dev/auth/callback` |
| `WORKOS_WEBHOOK_SECRET` | _(absent)_ | Signing secret for `/webhooks/workos` HMAC verification |
| `USER_ID_HEADER` | `X-User-ID` | Legacy/docker-compose fallback: HTTP header carrying user UID when WorkOS is disabled but Postgres is enabled |
| `USER_DISPLAY_HEADER` | _(empty)_ | Same fallback path: header for display name; falls back to `USER_ID_HEADER` value if unset |
| `CRON_SECRET` | _(empty)_ | Bearer token protecting `/internal/cron/update-prices`; if empty, no auth check |
| `MTGJSON_CACHE_DIR` | `/tmp/mtgjson` | Scratch directory for price import temp files (PostgreSQL mode only) |

## Database backends

`db.IS_POSTGRES` is `True` when `DATABASE_URL` is set. Both backends share the same SQLAlchemy API:

- `db.get_conn()` — context manager yielding a SQLAlchemy connection (auto-commits on exit).
- `db.upsert(conn, table, conflict_cols, rows)` — dialect-aware upsert (`INSERT OR REPLACE` on SQLite, `ON CONFLICT DO UPDATE` on PostgreSQL).
- `db.init_schema()` — creates all tables if absent and runs `_migrate()` to add columns missing from older schemas.

When adding inventory fields, update `mtgcompare/db.py` (Table definition + `_migrate`), `mtgcompare/inventory.py` (`_INSERT_SQL` and `_dict()`), together.

## Inventory invariants

- `inventory.db` is local state and must remain gitignored.
- One inventory row is one lot, not one printing.
- Every row has a `user_id` (TEXT, not a FK). In local SQLite mode `user_id` defaults to `"local"`.
- `inv.list_all(user_id)` and `inv.stats(user_id)` are scoped to the given user.
- `inv.list_all_global()` returns all users' rows; use this for shared operations like price downloads.
- CSV import is replace-by-default and scoped to the requesting user (only that user's rows are deleted).
- Single-card add and decklist add are append-only.

## User identity

Three modes, in priority order, all funnelled through `web._get_user_id()`:

1. **WorkOS active** (production). The `WORKOS_API_KEY` / `WORKOS_CLIENT_ID` / `WORKOS_REDIRECT_URI` env vars are all set. The `auth.bp` Blueprint registers a `before_app_request` gate that validates the access-token JWT cookie against WorkOS's JWKS, transparently refreshes it via the refresh-token cookie when expired, and populates `g.user_id` (= JWT `sub`) plus `g.user` (email + names sourced from the local `users` table — access-token JWTs only carry `sub`/`sid`). The `users` table is upserted on login and via `/webhooks/workos` (`user.created` / `user.updated` / `user.deleted`).
2. **PostgreSQL without WorkOS** (docker-compose dev / legacy). Falls back to trusting `USER_ID_HEADER`. The `users` table exists but is unused on this path.
3. **SQLite** (desktop / local dev). Always `"local"`. No middleware, no users table use.

The `users` table is keyed on `workos_user_id`; inventory rows continue to key on the same TEXT `user_id` column they always have, so the same SQLAlchemy code works across all three modes.

## Decklist preview UI

- `Resolve & preview` must run before `Add to inventory` becomes enabled.
- Resolved preview rows default the date field to today.
- The preview `Set` field is editable as a 3-character set code only.
- Inventory filtering is client-side and supports filtering by `price_bought`, including empty values.
- Decklist search on the main Search page is separate from inventory add: it prices pasted lists across shops and shows per-shop shipping-aware totals.

## Search and market behavior

- The Search page supports both single-card search and decklist search.
- Single-card search can optionally include per-shop shipping overrides in sort order.
- Market prices are cached in the `market_prices` table (global, not per-user).
- The Market page does not fetch live prices on GET. Prices are populated via **Update prices** (`POST /market/history/download`), which downloads MTGJSON history and writes the latest price per mapped lot into `market_prices` as a side effect (`_populate_market_prices_from_history`).
- There is no separate Scryfall refresh; prices come from MTGJSON/TCGPlayer daily data.
- Market cache keys are `(card_name, normalized set_code, is_foil)`.

## MTGJSON price history

### Local mode (SQLite / DuckDB)

- Price history is stored in `mtgjson/AllPricesHistory.duckdb` (DuckDB, single file).
- Full rebuild: `history_import.rebuild_history_db()` — builds to a `.tmp` file and renames atomically. Only runs once; if the DuckDB already exists the download is skipped.
- Incremental update: `history_import.merge_today_prices()` — upserts today's prices into the existing DuckDB.
- Concurrency: all DuckDB access in `web.py` is serialized via `_history_duckdb_lock` (reads use `read_only=True`).

### PostgreSQL mode

- Price history is stored in the `price_rows` table: `(uuid UUID, finish TEXT, market_date DATE, price_usd NUMERIC(10,4))` with PRIMARY KEY `(uuid, finish, market_date)`.
- DuckDB is used as an **ephemeral ETL engine only** — no `.duckdb` file is persisted. The pipeline is: XZ → NDJSON → in-memory DuckDB → CSV → PostgreSQL `COPY FROM STDIN`.
- Full rebuild: `history_import.rebuild_history_pg()` — detects empty table and uses direct COPY (fastest path); subsequent runs use temp-table upsert.
- Incremental update: `history_import.merge_today_prices_pg()` — always uses temp-table upsert.
- `_has_price_history()` in `web.py` checks `price_rows` row count instead of the DuckDB file existence.

### Shared

- Card-to-UUID mapping lives in `mtgjson_card_map` table; only sets with unmapped lots are reprocessed.
- `market_history_download` uses `inv.list_all_global()` so all users' cards get mapped and priced.

## Daily price refresh (production)

- Endpoint: `POST /internal/cron/update-prices` — protected by `Authorization: Bearer <CRON_SECRET>`.
- Downloads `AllPricesToday.json.xz`, merges into price history, refreshes `market_prices`.
- Intended caller: a K8s CronJob in the infra repo hitting the cluster-internal service URL (never exposed externally).
- Job status is trackable via `GET /market/history/download/status?job_id=<id>`.

## Docker

- `Dockerfile`: multi-stage build — `uv` installs deps in a builder stage, `python:3.12-slim` is the runtime.
- `docker-compose.yml`: local dev stack with app + `postgres:16-alpine`. Start with `docker compose up`.
- The app is stateless when `DATABASE_URL` is set; no volumes needed for the app container.

## Scripts

- Use `scripts/start.sh`, `scripts/stop.sh`, `scripts/start.ps1`, and `scripts/stop.ps1`.
- On Windows, the shell scripts delegate to the PowerShell scripts.
- The scripts should start the app via `python -m mtgcompare.web`.

## Packaging (Windows desktop app)

- Entry point: `mtgcompare/launcher.py` — starts Flask in a daemon thread, opens the browser, runs a system-tray icon loop.
- Build locally: `.\scripts\build.ps1` (installs PyInstaller + desktop deps, runs tests, produces `dist/mtgcompare-windows.zip`).
- Release: push a `v*` tag; GitHub Actions builds and attaches the zip to the GitHub Release automatically.
- User data (`inventory.db`, `app.log`) goes to `%APPDATA%\mtgcompare\` when running frozen so it survives app updates.
- Desktop deps (`pystray`, `pillow`) live in the `desktop` dependency group — not installed by default with `uv sync`.

## Testing

- `uv run pytest`
  Offline tests (SQLite mode).
- `uv run pytest -m live`
  Live scraper checks.
- `DATABASE_URL=postgresql+psycopg2://... uv run pytest -m pg`
  PostgreSQL-specific tests (require a real Postgres instance).
- Tests import the package from repo root via `tests/conftest.py`, so `uv run pytest` should work without setting `PYTHONPATH`.
- `.\.venv\Scripts\python -m pytest`
  Fallback if `uv` has cache or permission issues in this environment.
- DB-layer tests use a per-test temporary SQLite engine via `monkeypatch` on `db.engine`/`db.DB_PATH`/`db.IS_POSTGRES` — they never touch `inventory.db`.

## Git workflow

- Always ask for confirmation before running `git commit` or `git push`.

## What "ship" means

When the user says "ship" (or equivalent: "ship it", "ship this", "let's ship"),
they mean run the **full release pipeline end-to-end**, in this order:

1. **Confirm tests pass** — `uv run pytest` clean.
2. **Confirm lint passes** — `uv run ruff check mtgcompare/ tests/` clean.
3. **Commit** with a message that explains the *why*.
4. **Push** to `origin/master`.
5. **Tag** the next semver version (`vX.Y.Z`) and push the tag — this
   triggers the GitHub Release workflow, which builds the Windows zip,
   the macOS zip, and the GHCR Docker image.
6. **Babysit the build**: watch the CI run AND the Release workflow run
   to completion. If either fails, diagnose and fix the underlying issue
   (don't bypass) and re-tag if necessary.
7. **Update the deployment manifest in `../server_admin`** to point at
   the new image tag (`k8s/apps/mtgcompare/02-deployment.yml`),
   commit + push there.
8. **Apply the manifest** with `kubectl apply -f ...` and watch the
   rollout (`kubectl -n apps rollout status deploy/mtgcompare`).
9. **Verify in prod**: confirm the new image is live and a smoke test
   (a sample search, a healthz check, or whatever fits the change)
   succeeds.

If any step fails, fix it before moving on. Don't leave the system in
a half-deployed state.

## Generated files

These should stay ignored:

- `.venv/`
- `.pytest_cache/`
- `__pycache__/`
- `inventory.db`
- `mtgjson/`
- `app.pid`
- `app.log`
- `app.err.log`
- `.tk_home.html`
- `.tk_search.html`
- `binder.csv`
