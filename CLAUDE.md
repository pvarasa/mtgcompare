# CLAUDE.md

Repo-specific guidance for coding sessions.

## Layout

- `mtgcompare/`
  Main application package.
- `mtgcompare/web.py`
  Flask UI entry point for search, decklist, inventory, and market pages.
- `mtgcompare/compare.py`
  CLI price comparison entry point.
- `mtgcompare/inventory.py`
  Inventory storage and inventory CLI.
- `mtgcompare/scrappers/`
  Shop scraper implementations.
- `mtgcompare/templates/`, `mtgcompare/static/`
  Flask templates and browser assets.
- `scripts/`
  Start/stop helpers for local development.
- root `app.py`, `compare.py`
  Thin compatibility wrappers.

## Import conventions

- Use package-relative imports inside `mtgcompare/`.
- Use `mtgcompare...` imports from tests and external entry points.
- Keep parser functions pure and keep network I/O in scraper classes.

## Runtime paths

- `logging.conf`, `inventory.db`, `app.log`, `app.err.log`, and `app.pid` live at repo root.
- The package code should resolve those paths relative to the repo root, not relative to the package directory.

## Inventory invariants

- `inventory.db` is local state and must remain gitignored.
- One inventory row is one lot, not one printing.
- CSV import is replace-by-default.
- Single-card add and decklist add are append-only.
- When adding inventory fields, update `mtgcompare/db.py`, `mtgcompare/inventory.py` `_INSERT_SQL`, and `_tuple()` together.

## Decklist preview UI

- `Resolve & preview` must run before `Add to inventory` becomes enabled.
- Resolved preview rows default the date field to today.
- The preview `Set` field is editable as a 3-character set code only.
- Inventory filtering is client-side and supports filtering by `price_bought`, including empty values.
- Decklist search on the main Search page is separate from inventory add: it prices pasted lists across shops and shows per-shop shipping-aware totals.

## Search and market behavior

- The Search page supports both single-card search and decklist search.
- Single-card search can optionally include per-shop shipping overrides in sort order.
- Market prices are cached in the `market_prices` table inside `inventory.db`.
- The Market page does not fetch live prices on GET; refresh happens only through `POST /market/refresh`.
- Market cache keys are `(card_name, normalized set_code, is_foil)`.

## Scripts

- Use `scripts/start.sh`, `scripts/stop.sh`, `scripts/start.ps1`, and `scripts/stop.ps1`.
- On Windows, the shell scripts delegate to the PowerShell scripts.
- The scripts should start the app via `python -m mtgcompare.web`.

## Testing

- `uv run pytest`
  Offline tests.
- `uv run pytest -m live`
  Live scraper checks.
- Tests import the package from repo root via `tests/conftest.py`, so `uv run pytest` should work without setting `PYTHONPATH`.
- `.\.venv\Scripts\python -m pytest`
  Fallback if `uv` has cache or permission issues in this environment.

## Generated files

These should stay ignored:

- `.venv/`
- `.pytest_cache/`
- `__pycache__/`
- `inventory.db`
- `app.pid`
- `app.log`
- `app.err.log`
- `.tk_home.html`
- `.tk_search.html`
- `binder.csv`
