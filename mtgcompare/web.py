"""Flask web UI for mtgcompare.

Run: uv run python -m mtgcompare.web
Visit: http://127.0.0.1:5000
"""
import hmac
import json
import logging.config
import lzma
import os
import re
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Lock, Thread
from time import monotonic
from uuid import uuid4

import duckdb
import requests
from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import text

from . import auth, db, history_import, run_log
from . import inventory as inv
from .log_context import REQUEST_ID_HEADER, bind_request_id, install_record_factory
from .shops import ACTIVE_SHOPS, SHIPPING_JPY, SHOP_FLAGS, collect_prices, shop_slug
from .utils import get_fx

ROOT_DIR = Path(__file__).resolve().parent.parent
LOGGING_CONF = ROOT_DIR / "logging.conf"

# Install the LogRecord factory before fileConfig so every record carries
# request_id/user_id defaults — the formatter references those fields and
# would KeyError on any record that lacks them.
install_record_factory()

# Apply file-based logging config at import time so it takes effect under
# gunicorn (which imports `mtgcompare.web:app` and never calls main()).
# disable_existing_loggers=False keeps gunicorn's own loggers intact.
logging.config.fileConfig(LOGGING_CONF, disable_existing_loggers=False)

app = Flask(__name__)

# Production refuses to boot with the dev fallback secret key — it signs
# CSRF tokens and flask sessions, and the fallback is in the public repo.
_SECRET_KEY = os.environ.get("SECRET_KEY", "")
if db.IS_POSTGRES and not _SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY must be set when DATABASE_URL is set "
        "(production must not use the public dev fallback)."
    )
app.secret_key = _SECRET_KEY or "mtgcompare-local-dev"


# Stamp a per-request id BEFORE the auth blueprint's gate runs, so even
# the kick-to-login redirect carries a correlatable id in its log line.
# Honors an upstream X-Request-Id when the proxy injects one.
@app.before_request
def _bind_log_context():
    bind_request_id()


@app.after_request
def _echo_request_id(response):
    rid = getattr(g, "request_id", None)
    if rid:
        response.headers.setdefault(REQUEST_ID_HEADER, rid)
    return response


app.register_blueprint(auth.bp)

# CSRF protection for state-changing POSTs from same-origin templates.
# /webhooks/workos is exempt because it's machine-to-machine and validated
# via HMAC. /internal/cron/update-prices is exempted at the route level
# below (bearer-token auth). /auth/login, /auth/callback, /auth/me are GET
# and never trigger CSRF; /auth/logout is POST and IS protected.
from flask_wtf.csrf import CSRFProtect  # noqa: E402

csrf = CSRFProtect(app)
csrf.exempt(auth.webhook)


# Inline <style>/<script> blocks throughout the templates require
# 'unsafe-inline' until those are extracted to /static. Scryfall is the
# only third-party origin (card image previews + named-card lookup).
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data: https://cards.scryfall.io; "
    "connect-src 'self' https://api.scryfall.com; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self' https://api.workos.com"
)


@app.after_request
def _security_headers(response):
    response.headers.setdefault("Strict-Transport-Security",
                                "max-age=31536000; includeSubDomains; preload")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Content-Security-Policy", _CSP)
    return response

_USER_ID_HEADER = os.environ.get("USER_ID_HEADER", "X-User-ID")
_USER_DISPLAY_HEADER = os.environ.get("USER_DISPLAY_HEADER", "")
_CRON_SECRET = os.environ.get("CRON_SECRET", "")

# The legacy `USER_ID_HEADER` path trusts an upstream auth proxy to inject
# the user identity. Now that mtgcompare is publicly reachable without
# Cloudflare Access, that path must never silently activate — production
# (PostgreSQL) requires either WorkOS or an explicit opt-in.
_TRUST_USER_HEADER = os.environ.get("TRUST_USER_HEADER", "") == "1"
if db.IS_POSTGRES and not auth.WORKOS_ENABLED and not _TRUST_USER_HEADER:
    raise RuntimeError(
        "Authentication is unconfigured: set WORKOS_API_KEY/WORKOS_CLIENT_ID/"
        "WORKOS_REDIRECT_URI to enable WorkOS, or set TRUST_USER_HEADER=1 to "
        "explicitly opt into the legacy USER_ID_HEADER fallback."
    )

# Production refuses to boot without a CRON_SECRET — without it the
# `/internal/cron/update-prices` endpoint is open to the internet.
if db.IS_POSTGRES and not _CRON_SECRET:
    raise RuntimeError(
        "CRON_SECRET must be set when DATABASE_URL is set "
        "(otherwise /internal/cron/update-prices has no authentication)."
    )


def _get_user_id() -> str:
    """Return the stable user identity used as a DB key.

    Three modes, in priority order:
    - WorkOS active: the verified JWT subject (set on `g.user_id` by the
      auth middleware).
    - Postgres without WorkOS: legacy header-trust path so docker-compose
      dev stacks keep working without WorkOS env vars. The header is
      required: a missing or empty value aborts the request rather than
      falling back to a shared bucket, so a misconfigured proxy can't
      cross-contaminate inventories.
    - SQLite: always 'local'.
    """
    if auth.WORKOS_ENABLED:
        return getattr(g, "user_id", "anonymous")
    if not db.IS_POSTGRES:
        g.user_id = "local"
        return "local"
    header_value = request.headers.get(_USER_ID_HEADER, "").strip()
    if not header_value:
        abort(403)
    g.user_id = header_value
    return header_value


def _get_display_name() -> str:
    if auth.WORKOS_ENABLED:
        user = getattr(g, "user", None)
        if user:
            name = " ".join(
                p for p in (user.get("first_name"), user.get("last_name")) if p
            ).strip()
            return name or user.get("email") or user.get("id") or "anonymous"
        return "anonymous"
    if not db.IS_POSTGRES:
        return "local"
    if _USER_DISPLAY_HEADER:
        name = request.headers.get(_USER_DISPLAY_HEADER, "").strip()
        if name:
            return name
    return _get_user_id()


@app.context_processor
def _inject_current_user():
    return {
        "current_user": _get_display_name(),
        "workos_enabled": auth.WORKOS_ENABLED,
    }


@app.route("/healthz")
def healthz():
    return {"ok": True}, 200


_CONDITION_ABBR = {
    "nearmint": "NM", "nm": "NM",
    "lightlyplayed": "LP", "lightplay": "LP", "lp": "LP",
    "moderatelyplayed": "MP", "moderateplay": "MP", "mp": "MP",
    "heavilyplayed": "HP", "heavyplay": "HP", "hp": "HP",
    "damaged": "DMG", "dmg": "DMG",
}

@app.template_filter("condition_abbr")
def _condition_abbr(value: str) -> str:
    key = re.sub(r"[^a-z]", "", (value or "").lower())
    return _CONDITION_ABBR.get(key, value)

_fx: float | None = None
_fx_lock = Lock()
_download_jobs: dict[str, dict] = {}
_download_jobs_lock = Lock()


def _get_fx() -> float | None:
    """Fetch the JPY/USD rate once per process."""
    global _fx
    with _fx_lock:
        if _fx is None:
            try:
                _fx = get_fx("jpy")
            except Exception as exc:
                app.logger.error("FX lookup failed: %s", exc)
                return None
        return _fx


def _parse_shipping_overrides(source) -> dict[str, int]:
    overrides: dict[str, int] = {}
    for shop, default_cost in SHIPPING_JPY.items():
        raw = source.get(f"ship_{shop_slug(shop)}", "").strip()
        try:
            overrides[shop] = max(0, int(float(raw))) if raw else default_cost
        except ValueError:
            overrides[shop] = default_cost
    return overrides


def _parse_enabled_shops(source) -> set[str] | None:
    """Return the set of enabled shop *display names*, or None for "all on".

    The UI submits ``shop_filter=1`` whenever the filter panel is open, plus
    one ``shop_<slug>=1`` per checkbox the user kept. With the flag absent
    we treat the search as default (all shops). With the flag present we
    honor the explicit selection — including the empty-set case where the
    user has deselected everything.
    """
    if source.get("shop_filter") != "1":
        return None
    return {
        name for name in ACTIVE_SHOPS
        if source.get(f"shop_{shop_slug(name)}") == "1"
    }


def _shop_filter_config(enabled: set[str] | None) -> list[dict]:
    """Per-shop checkbox state for the filter panel template."""
    return [
        {
            "shop": name,
            "slug": shop_slug(name),
            "enabled": enabled is None or name in enabled,
        }
        for name in ACTIVE_SHOPS
    ]


def _normalize_set_code(code: str | None, *, upper: bool = False) -> str:
    normalized = code.split("_")[0] if code else ""
    return normalized.upper() if upper else normalized.lower()


def _is_foil(printing: str | None) -> bool:
    return (printing or "").lower() == "foil"


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    include_shipping = request.args.get("shipping") == "1"
    shipping_overrides_jpy = _parse_shipping_overrides(request.args)
    ship_cfg = _shipping_config(shipping_overrides_jpy)
    enabled_shops = _parse_enabled_shops(request.args)
    shop_filter_active = enabled_shops is not None
    shop_filter_cfg = _shop_filter_config(enabled_shops)

    results: list[dict] = []
    error: str | None = None

    if q:
        t0 = monotonic()
        fx = _get_fx()
        if fx is None:
            error = "Could not fetch FX rate; try again later."
        else:
            results = collect_prices(q, fx, enabled=enabled_shops, logger=app.logger)
            if include_shipping:
                for r in results:
                    r["ship_jpy"] = shipping_overrides_jpy.get(r["shop"], 0)
                    r["price_jpy_with_shipping"] = r["price_jpy"] + r["ship_jpy"]
                results.sort(key=lambda r: r["price_jpy_with_shipping"])
            else:
                results.sort(key=lambda r: r["price_jpy"])
        app.logger.info(
            "event=search_query q=%r shops_enabled=%s include_shipping=%d "
            "result_count=%d duration_ms=%d",
            q,
            len(enabled_shops) if enabled_shops is not None else "all",
            int(include_shipping), len(results),
            int((monotonic() - t0) * 1000),
        )

    return render_template(
        "index.html",
        q=q,
        results=results,
        fx=_fx,
        error=error,
        shop_flags=SHOP_FLAGS,
        shipping_config=ship_cfg,
        include_shipping=include_shipping,
        shop_filter_config=shop_filter_cfg,
        shop_filter_active=shop_filter_active,
        active="search",
    )


def _shipping_config(overrides_jpy: dict | None = None) -> list[dict]:
    """Build the per-shop shipping config list passed to templates."""
    return [
        {
            "shop": shop,
            "slug": shop_slug(shop),
            "cost_jpy": int((overrides_jpy or {}).get(shop, SHIPPING_JPY.get(shop, 0))),
        }
        for shop in SHIPPING_JPY
    ]


_DECK_LINE_RE = re.compile(
    r'^(\d+)x?\s+(.+?)(?:\s+\([A-Za-z0-9]+\)(?:\s+\d+[a-z]?)?)?\s*$'
)

# Hard cap on the total card count of a single decklist search. Sized to
# fit a full Commander deck (99 + commander = 100). Beyond this the
# parallel fan-out across cards × shops gets large enough to look like
# an attack to upstream sites and to lock up the worker pool.
MAX_DECKLIST_CARDS = 100


def _parse_decklist(text: str) -> list[tuple[int, str]]:
    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue
        if re.match(r'^(commander|sideboard|deck|maybeboard):?$', line, re.IGNORECASE):
            continue
        m = _DECK_LINE_RE.match(line)
        if m:
            qty = int(m.group(1))
            name = m.group(2).strip()
            if qty > 0 and name:
                result.append((qty, name))
    return result


def _deduct_inventory(
    name_qty: dict[str, int],
    inv_map: dict[str, int],
) -> tuple[dict[str, int], dict[str, int]]:
    """Return (qty_from_inventory, qty_still_needed) per lowercase card key.

    Inventory quantities are capped at the requested amount — extra copies
    don't produce negative need.
    """
    name_inv_qty: dict[str, int] = {}
    name_needed: dict[str, int] = {}
    for key, wanted in name_qty.items():
        have = min(inv_map.get(key, 0), wanted)
        name_inv_qty[key] = have
        name_needed[key] = wanted - have
    return name_inv_qty, name_needed


def _consolidate_decklist(
    card_items: list[tuple[int, str]],
) -> tuple[dict[str, int], dict[str, str]]:
    """Sum duplicate lines and remember the first-seen casing of each name."""
    name_qty: dict[str, int] = {}
    name_canonical: dict[str, str] = {}
    for qty, name in card_items:
        key = name.lower()
        name_qty[key] = name_qty.get(key, 0) + qty
        if key not in name_canonical:
            name_canonical[key] = name
    return name_qty, name_canonical


def _fetch_decklist_prices(
    names_to_search: list[str],
    name_canonical: dict[str, str],
    fx: float,
    enabled_shops: set[str] | None,
) -> dict[str, list[dict]]:
    """Fan out one shop search per distinct name and collect price rows.

    Returns a mapping ``{lower_name: sorted_rows}``. Per-name failures are
    logged and produce an empty list rather than aborting the whole batch.
    """
    prices_by_name: dict[str, list[dict]] = {n: [] for n in names_to_search}
    if not names_to_search:
        return prices_by_name
    shops_count = len(enabled_shops) if enabled_shops is not None else "all"
    with ThreadPoolExecutor(max_workers=min(len(names_to_search), 6)) as executor:
        future_to_name = {
            executor.submit(
                collect_prices, name_canonical[n], fx,
                enabled=enabled_shops, logger=app.logger,
            ): n
            for n in names_to_search
        }
        for future in as_completed(future_to_name):
            n = future_to_name[future]
            try:
                prices_by_name[n] = future.result()
            except Exception as exc:
                app.logger.error(
                    "event=price_fetch_failed card=%r decklist_size=%d shops_enabled=%s detail=%s",
                    name_canonical[n], len(names_to_search), shops_count, exc,
                )
    for n in names_to_search:
        prices_by_name[n].sort(key=lambda r: r["price_jpy"])
    return prices_by_name


def _build_card_rows(
    name_qty: dict[str, int],
    name_canonical: dict[str, str],
    name_inv_qty: dict[str, int],
    name_needed: dict[str, int],
    prices_by_name: dict[str, list[dict]],
) -> list[dict]:
    """Project the per-name state into the row shape the template expects."""
    rows = []
    for n in sorted(name_qty, key=lambda x: name_canonical[x].lower()):
        results = prices_by_name.get(n, [])
        qty_needed = name_needed[n]
        rows.append({
            "name": name_canonical[n],
            "qty": name_qty[n],
            "qty_inventory": name_inv_qty[n],
            "qty_needed": qty_needed,
            "best": results[0] if (results and qty_needed > 0) else None,
            "all": results,
        })
    return rows


def _compute_shop_totals(
    card_rows: list[dict],
    shipping_overrides_jpy: dict[str, int],
    fx: float | None,
) -> tuple[list[dict], dict[str, float]]:
    """Aggregate per-shop totals and grand totals from already-built card rows.

    Returns ``(shop_list_sorted_by_total_desc, grand_totals)`` where
    ``grand_totals`` carries USD/JPY raw + with-shipping figures plus
    ``shipping_total_jpy`` for the template.
    """
    shop_totals: dict[str, dict] = {}
    grand_total_usd = 0.0
    grand_total_jpy = 0.0

    for row in card_rows:
        if row["best"] is None:
            continue
        shop = row["best"]["shop"]
        qty = row["qty_needed"]
        unit_usd = row["best"]["price_usd"]
        unit_jpy = row["best"]["price_jpy"]
        grand_total_usd += unit_usd * qty
        grand_total_jpy += unit_jpy * qty
        if shop not in shop_totals:
            ship_jpy = shipping_overrides_jpy.get(shop, SHIPPING_JPY.get(shop, 0))
            shop_totals[shop] = {
                "shop": shop,
                "unique_cards": 0,
                "total_copies": 0,
                "total_usd": 0.0,
                "total_jpy": 0.0,
                "shipping_jpy": ship_jpy,
                "shipping_usd": round(ship_jpy / fx, 2) if fx else 0.0,
            }
        shop_totals[shop]["unique_cards"] += 1
        shop_totals[shop]["total_copies"] += qty
        shop_totals[shop]["total_usd"] += unit_usd * qty
        shop_totals[shop]["total_jpy"] += unit_jpy * qty

    for s in shop_totals.values():
        s["total_usd_with_shipping"] = round(s["total_usd"] + s["shipping_usd"], 2)
        s["total_jpy_with_shipping"] = round(s["total_jpy"] + s["shipping_jpy"], 0)

    shop_list = sorted(shop_totals.values(), key=lambda s: -s["total_usd_with_shipping"])

    shipping_total_jpy = sum(s["shipping_jpy"] for s in shop_totals.values())
    shipping_total_usd = round(shipping_total_jpy / fx, 2) if fx else 0.0
    grand_totals = {
        "grand_total_usd": grand_total_usd,
        "grand_total_jpy": grand_total_jpy,
        "grand_total_usd_with_shipping": round(grand_total_usd + shipping_total_usd, 2),
        "grand_total_jpy_with_shipping": round(grand_total_jpy + shipping_total_jpy, 0),
        "shipping_total_jpy": shipping_total_jpy,
    }
    return shop_list, grand_totals


def _load_inventory_qty_map(use_inventory: bool) -> dict[str, int]:
    """Return ``{lower_card_name: total_quantity}`` for the current user.

    Returns an empty dict when ``use_inventory`` is False so callers can
    treat the "not deducting" case the same as "deducting from nothing".
    """
    if not use_inventory:
        return {}
    inv.init_schema()
    user_id = _get_user_id()
    inv_map: dict[str, int] = {}
    for row in inv.list_all(user_id):
        k = row["card_name"].lower()
        inv_map[k] = inv_map.get(k, 0) + row["quantity"]
    return inv_map


@app.route("/decklist", methods=["POST"])
def decklist_search():
    t0 = monotonic()
    text = request.form.get("decklist", "").strip()
    shipping_overrides_jpy = _parse_shipping_overrides(request.form)
    ship_cfg = _shipping_config(shipping_overrides_jpy)
    use_inventory = request.form.get("use_inventory") == "1"
    enabled_shops = _parse_enabled_shops(request.form)
    shop_filter_active = enabled_shops is not None
    shop_filter_cfg = _shop_filter_config(enabled_shops)

    def _early_return(error_msg: str, fx_val=None, *, reason: str):
        app.logger.info(
            "event=decklist_search status=rejected reason=%s shops_enabled=%s "
            "use_inventory=%d duration_ms=%d",
            reason,
            len(enabled_shops) if enabled_shops is not None else "all",
            int(use_inventory), int((monotonic() - t0) * 1000),
        )
        return render_template(
            "decklist.html",
            decklist=text,
            error=error_msg,
            card_rows=[], shop_list=[],
            grand_total_usd=0.0, grand_total_jpy=0.0,
            grand_total_usd_with_shipping=0.0, grand_total_jpy_with_shipping=0.0,
            shipping_total_jpy=0,
            fx=fx_val, shop_flags=SHOP_FLAGS,
            shipping_config=ship_cfg, active="search",
            shop_filter_config=shop_filter_cfg,
            shop_filter_active=shop_filter_active,
            use_inventory=use_inventory,
        )

    card_items = _parse_decklist(text)
    if not card_items:
        return _early_return(
            "No cards parsed. Use format: '1 Card Name' or '4x Card Name (SET)'",
            reason="parse_empty",
        )

    total_cards = sum(qty for qty, _ in card_items)
    if total_cards > MAX_DECKLIST_CARDS:
        return _early_return(
            f"Decklist is {total_cards} cards — the limit is {MAX_DECKLIST_CARDS}. "
            "Trim it or split into multiple searches.",
            reason="too_large",
        )

    name_qty, name_canonical = _consolidate_decklist(card_items)
    inv_map = _load_inventory_qty_map(use_inventory)
    name_inv_qty, name_needed = _deduct_inventory(name_qty, inv_map)
    names_to_search = [n for n in name_qty if name_needed[n] > 0]
    inventory_hits = sum(1 for n in name_qty if name_inv_qty[n] > 0)

    fx = _get_fx()
    if fx is None and names_to_search:
        return _early_return("Could not fetch FX rate; try again later.", reason="fx_unavailable")

    prices_by_name = (
        _fetch_decklist_prices(names_to_search, name_canonical, fx, enabled_shops)
        if fx is not None else {n: [] for n in names_to_search}
    )
    # Names without unmet need still need an empty entry for the template.
    for n in name_qty:
        prices_by_name.setdefault(n, [])

    card_rows = _build_card_rows(name_qty, name_canonical, name_inv_qty, name_needed, prices_by_name)
    shop_list, totals = _compute_shop_totals(card_rows, shipping_overrides_jpy, fx)

    rows_with_match = sum(1 for r in card_rows if r["best"] is not None)
    app.logger.info(
        "event=decklist_search status=ok size=%d distinct_names=%d "
        "names_searched=%d inventory_hits=%d shops_enabled=%s use_inventory=%d "
        "rows_with_match=%d duration_ms=%d",
        total_cards, len(name_qty), len(names_to_search), inventory_hits,
        len(enabled_shops) if enabled_shops is not None else "all",
        int(use_inventory), rows_with_match, int((monotonic() - t0) * 1000),
    )

    return render_template(
        "decklist.html",
        decklist=text,
        card_rows=card_rows,
        shop_list=shop_list,
        fx=fx,
        shop_flags=SHOP_FLAGS,
        shipping_config=ship_cfg,
        shop_filter_config=shop_filter_cfg,
        shop_filter_active=shop_filter_active,
        active="search",
        error=None,
        use_inventory=use_inventory,
        **totals,
    )


def _format_ago(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        dt  = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        sec = int((datetime.now(UTC) - dt).total_seconds())
        if sec <    60: return "just now"
        if sec <  3600: return f"{sec // 60} min ago"
        if sec < 86400: return f"{sec // 3600} hr ago"
        return f"{sec // 86400} days ago"
    except Exception:
        return iso


_MARKET_HISTORY_PERIODS = {
    "1w": 7,
    "1m": 30,
    "all": None,
}

_MTGJSON_BASE_URL = "https://mtgjson.com/api/v5"
_MTGJSON_HEADERS = {"User-Agent": "mtgcompare/0.1", "Accept": "application/json"}


def _history_cutoff(period: str, *, now: datetime | None = None) -> datetime | None:
    days = _MARKET_HISTORY_PERIODS.get(period)
    if days is None:
        return None
    anchor = now or datetime.now(UTC)
    return anchor - timedelta(days=days)


def _slice_history(points: list[dict], period: str, *, now: datetime | None = None) -> list[dict]:
    cutoff = _history_cutoff(period, now=now)
    if cutoff is None:
        return points
    return [
        point for point in points
        if datetime.fromisoformat(point["fetched_at"].replace("Z", "+00:00")) >= cutoff
    ]


def _mtgjson_cache_dir() -> Path:
    if db.IS_POSTGRES:
        # Linux containers only; overridable via env var. CLAUDE.md documents the default.
        cache_dir = Path(os.environ.get("MTGJSON_CACHE_DIR", "/tmp/mtgjson"))  # noqa: S108
    else:
        cache_dir = db.DB_PATH.parent / "mtgjson"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _mtgjson_history_path() -> Path:
    return _mtgjson_cache_dir() / "AllPrices.json.xz"


def _mtgjson_history_duckdb_path() -> Path:
    return _mtgjson_cache_dir() / "AllPricesHistory.duckdb"



def _mtgjson_set_path(set_code: str) -> Path:
    return _mtgjson_cache_dir() / f"{_normalize_set_code(set_code, upper=True)}.json.xz"


def _mtgjson_set_candidates(set_code: str) -> list[str]:
    normalized = _normalize_set_code(set_code, upper=True)
    candidates: list[str] = []
    for value in (
        normalized,
        normalized.split("_")[0],
        normalized.split("-")[0],
        re.sub(r"\d+$", "", normalized),
    ):
        value = value.strip()
        if value and value not in candidates:
            candidates.append(value)
    trimmed = normalized
    while len(trimmed) > 3:
        trimmed = trimmed[:-1]
        if trimmed and trimmed not in candidates:
            candidates.append(trimmed)
    return candidates


def _download_file(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with requests.get(url, headers=_MTGJSON_HEADERS, stream=True, timeout=(20, 300)) as resp:
        resp.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if chunk:
                    fh.write(chunk)
    tmp.replace(target)


def _download_mtgjson_set_file(set_code: str) -> tuple[str, Path] | None:
    for candidate in _mtgjson_set_candidates(set_code):
        path = _mtgjson_set_path(candidate)
        if path.exists():
            return candidate, path
        try:
            _download_file(f"{_MTGJSON_BASE_URL}/{candidate}.json.xz", path)
            return candidate, path
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                continue
            raise
    return None


def _read_meta(conn, key: str) -> str | None:
    row = conn.execute(
        text("SELECT value FROM app_meta WHERE key = :key"), {"key": key}
    ).mappings().first()
    return row["value"] if row else None


def _write_meta(conn, key: str, value: str) -> None:
    db.upsert(conn, "app_meta", ["key"], [{"key": key, "value": value}])


def _init_download_job(job_id: str) -> None:
    with _download_jobs_lock:
        _download_jobs[job_id] = {
            "id": job_id,
            "state": "running",
            "phase": "Queued",
            "detail": "Waiting to start...",
            "progress": 0,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "error": None,
        }


def _set_download_job(job_id: str, **updates) -> None:
    with _download_jobs_lock:
        job = _download_jobs.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")


def _get_download_job(job_id: str) -> dict | None:
    with _download_jobs_lock:
        job = _download_jobs.get(job_id)
        return dict(job) if job else None


_history_duckdb_lock = Lock()


def _has_price_history() -> bool:
    if db.IS_POSTGRES:
        with db.get_conn() as conn:
            return conn.execute(text("SELECT 1 FROM price_rows LIMIT 1")).fetchone() is not None
    return _mtgjson_history_duckdb_path().exists()


def _query_history(uuid: str, finish: str) -> dict[str, float]:
    if db.IS_POSTGRES:
        with db.get_conn() as conn:
            rows = conn.execute(
                text("SELECT market_date, price_usd FROM price_rows"
                     " WHERE uuid = :uuid AND finish = :finish ORDER BY market_date ASC"),
                {"uuid": uuid, "finish": finish},
            ).fetchall()
        return {
            (r[0].isoformat() if isinstance(r[0], date) else str(r[0])): float(r[1])
            for r in rows if r[1] is not None
        }

    duckdb_path = _mtgjson_history_duckdb_path()
    if not duckdb_path.exists():
        return {}
    with _history_duckdb_lock:
        conn = duckdb.connect(str(duckdb_path), read_only=True)
        try:
            rows = conn.execute(
                "SELECT market_date, price_usd FROM price_rows "
                "WHERE uuid = ? AND finish = ? ORDER BY market_date ASC",
                [uuid, finish],
            ).fetchall()
        finally:
            conn.close()
    return {row[0]: row[1] for row in rows if row[1] is not None}


def _candidate_uuid_map(cards: list[dict], set_code: str) -> dict[tuple[str, str, str], dict[str, str]]:
    candidates: dict[tuple[str, str, str], dict[str, str]] = {}
    normalized_set = _normalize_set_code(set_code, upper=True)
    for card in cards:
        name = (card.get("name") or "").strip()
        if not name:
            continue
        card_number = (card.get("number") or "").strip()
        identifiers = card.get("identifiers") or {}
        finishes = {finish.lower() for finish in (card.get("finishes") or [])}
        normal_uuid = identifiers.get("mtgjsonNonFoilVersionId")
        foil_uuid = identifiers.get("mtgjsonFoilVersionId")
        if not normal_uuid and "nonfoil" in finishes:
            normal_uuid = card.get("uuid")
        if not foil_uuid and "foil" in finishes:
            foil_uuid = card.get("uuid")
        key = (name.lower(), normalized_set, card_number)
        bucket = candidates.setdefault(key, {})
        if normal_uuid and "normal" not in bucket:
            bucket["normal"] = normal_uuid
        if foil_uuid and "foil" not in bucket:
            bucket["foil"] = foil_uuid
    return candidates


def _resolve_candidate_uuid(row: dict, candidates: dict[tuple[str, str, str], dict[str, str]]) -> str | None:
    name_key = row["card_name"].lower()
    set_key = _normalize_set_code(row["set_code"], upper=True)
    card_number = (row.get("card_number") or "").strip()
    finish_key = "foil" if _is_foil(row.get("printing")) else "normal"
    search_keys = [(name_key, set_key, card_number)]
    if card_number:
        search_keys.append((name_key, set_key, ""))
    for key in search_keys:
        bucket = candidates.get(key)
        if bucket and bucket.get(finish_key):
            return bucket[finish_key]
    return None


def _load_set_cards(path: Path) -> list[dict]:
    with lzma.open(path, "rt", encoding="utf-8") as fh:
        payload = json.load(fh)
    return ((payload.get("data") or {}).get("cards")) or []


def _densify_daily_points(
    price_points: dict[str, float],
    *,
    start_day: date | None = None,
    end_day: date | None = None,
) -> list[dict]:
    if not price_points:
        return []
    normalized = {
        datetime.fromisoformat(stamp).date(): value
        for stamp, value in price_points.items()
    }
    lo = start_day or min(normalized)
    hi = end_day or max(normalized)
    points: list[dict] = []
    current = lo
    while current <= hi:
        value = normalized.get(current)
        points.append({
            "market_date": current.isoformat(),
            "price_usd": value,
        })
        current += timedelta(days=1)
    return points




def _populate_market_prices_from_history(
    card_maps: list[tuple],
    duckdb_path: Path | None,
    fetched_at: str,
) -> None:
    """Write the latest price for each mapped inventory lot into market_prices.

    card_maps: list of (card_name, set_code, card_number, is_foil, uuid, updated_at)
    """
    if not card_maps:
        return

    # Deduplicate to one market_prices row per (card_name, set_code, is_foil).
    uuid_to_db_key: dict[tuple[str, str], tuple[str, str, int]] = {}
    seen_db_keys: set[tuple[str, str, int]] = set()
    for card_name, set_code, _card_number, is_foil, uuid, _ in card_maps:
        finish = "foil" if is_foil else "normal"
        db_key = (card_name, set_code, is_foil)
        if db_key not in seen_db_keys:
            seen_db_keys.add(db_key)
            uuid_to_db_key[(str(uuid), finish)] = db_key

    if not uuid_to_db_key:
        return

    if db.IS_POSTGRES:
        uuid_list = list({u for (u, _) in uuid_to_db_key})
        params = {f"u{i}": u for i, u in enumerate(uuid_list)}
        placeholders = ", ".join(f":u{i}" for i in range(len(uuid_list)))
        with db.get_conn() as conn:
            rows = conn.execute(
                # placeholders are :u0, :u1, …; user values bound via `params`.
                text(f"""
                    SELECT DISTINCT ON (uuid, finish) uuid, finish, price_usd
                    FROM price_rows
                    WHERE uuid IN ({placeholders})
                    ORDER BY uuid, finish, market_date DESC
                """),  # noqa: S608
                params,
            ).fetchall()
        latest: dict[tuple[str, str], float | None] = {
            (str(r[0]), r[1]): float(r[2]) if r[2] is not None else None
            for r in rows
        }
    else:
        if not duckdb_path or not duckdb_path.exists():
            return
        uuid_list = list({u for (u, _) in uuid_to_db_key})
        placeholders = ", ".join("?" for _ in uuid_list)
        with _history_duckdb_lock:
            conn_duck = duckdb.connect(str(duckdb_path), read_only=True)
            try:
                rows = conn_duck.execute(
                    # placeholders are positional `?`; user values bound via `uuid_list`.
                    f"""
                    SELECT uuid, finish, price_usd
                    FROM price_rows
                    WHERE uuid IN ({placeholders})
                    QUALIFY ROW_NUMBER() OVER (PARTITION BY uuid, finish ORDER BY market_date DESC) = 1
                    """,  # noqa: S608
                    uuid_list,
                ).fetchall()
            finally:
                conn_duck.close()
        latest = {(r[0], r[1]): r[2] for r in rows}

    inserts = [
        {
            "card_name": card_name,
            "set_code":  set_code,
            "is_foil":   is_foil,
            "price_usd": latest.get((uuid, finish)),
            "fetched_at": fetched_at,
        }
        for (uuid, finish), (card_name, set_code, is_foil) in uuid_to_db_key.items()
    ]
    with db.get_conn() as conn:
        db.upsert(conn, "market_prices", ["card_name", "set_code", "is_foil"], inserts)


def _row_key_for_mapping(row: dict) -> tuple[str, str, str, int]:
    """Identity key for an inventory row in the MTGJSON map table."""
    return (
        row["card_name"].lower(),
        _normalize_set_code(row["set_code"], upper=True),
        (row.get("card_number") or "").strip(),
        int(_is_foil(row.get("printing"))),
    )


def _load_existing_card_map() -> dict[tuple[str, str, str, int], str]:
    """Read mtgjson_card_map keyed by row identity for fast lookup."""
    with db.get_conn() as conn:
        existing_rows = conn.execute(
            text("SELECT card_name, set_code, card_number, is_foil, uuid FROM mtgjson_card_map")
        ).mappings().all()
    return {
        (r["card_name"].lower(), r["set_code"], r["card_number"], r["is_foil"]): r["uuid"]
        for r in existing_rows
    }


def _resolve_inventory_uuids(
    inventory_rows: list[dict],
    downloaded_at: str,
    progress: Callable[[int, str, str], None],
) -> tuple[list[tuple[str, str, str, int, str, str]], set[str]]:
    """Map every inventory lot to an MTGJSON UUID.

    Downloads MTGJSON set files only for sets that have at least one
    unmapped lot — already-resolved sets are taken from
    ``mtgjson_card_map`` directly.

    Returns ``(card_maps, sets_needing_load)`` where ``card_maps`` is the
    full list of resolvable lots with their UUIDs, and ``sets_needing_load``
    is the set of normalized set codes whose mapping rows should be
    refreshed (used by the caller to evict stale rows before upsert).
    """
    existing_uuid = _load_existing_card_map()

    sets_needing_load: set[str] = {
        _normalize_set_code(row["set_code"], upper=True)
        for row in inventory_rows
        if row.get("set_code") and _row_key_for_mapping(row) not in existing_uuid
    }

    candidates_by_set: dict[str, dict[tuple[str, str, str], dict[str, str]]] = {}
    sets_to_load = sorted(sets_needing_load)
    if sets_to_load:
        total_to_load = len(sets_to_load)
        for index, set_code in enumerate(sets_to_load, start=1):
            progress(
                5 + round(index / total_to_load * 20),
                "Downloading set data",
                f"Downloading MTGJSON set file for {set_code} ({index}/{total_to_load})...",
            )
            resolved = _download_mtgjson_set_file(set_code)
            if not resolved:
                app.logger.warning("No MTGJSON set file found for inventory set %s", set_code)
                candidates_by_set[set_code] = {}
                continue
            resolved_set_code, set_path = resolved
            candidates_by_set[set_code] = _candidate_uuid_map(_load_set_cards(set_path), resolved_set_code)
    else:
        progress(25, "Set data", "All sets already mapped — skipping set file load.")

    progress(28, "Mapping inventory", "Resolving MTGJSON card UUIDs for inventory lots...")
    card_maps: list[tuple[str, str, str, int, str, str]] = []
    for row in inventory_rows:
        key = _row_key_for_mapping(row)
        set_code = _normalize_set_code(row["set_code"], upper=True)
        if set_code in sets_needing_load:
            uuid = _resolve_candidate_uuid(row, candidates_by_set.get(set_code, {}))
        else:
            uuid = existing_uuid.get(key)
        if not uuid:
            continue
        is_foil = int(_is_foil(row.get("printing")))
        card_number = (row.get("card_number") or "").strip()
        card_maps.append((row["card_name"], set_code, card_number, is_foil, uuid, downloaded_at))

    return card_maps, sets_needing_load


def _ensure_history_loaded(
    history_duckdb_path: Path,
    progress: Callable[[int, str, str], None],
) -> int:
    """Ensure MTGJSON price history is loaded into the active backend.

    Downloads AllPrices.json.xz and runs the rebuild pipeline (DuckDB or
    PostgreSQL depending on ``db.IS_POSTGRES``) only when the local store
    is empty. Returns the row count written, or 0 if the existing store
    was reused (caller can fall back to the meta table for the count).
    """
    needs_history = (
        (db.IS_POSTGRES and not _has_price_history())
        or (not db.IS_POSTGRES and not history_duckdb_path.exists())
    )
    if not needs_history:
        progress(40, "History ready", "Using existing price history.")
        return 0

    history_path = _mtgjson_history_path()
    progress(32, "Downloading history", "Downloading MTGJSON AllPrices history...")
    try:
        _download_file(f"{_MTGJSON_BASE_URL}/AllPrices.json.xz", history_path)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            raise RuntimeError(
                "MTGJSON price files are temporarily unavailable. Please try again later."
            ) from exc
        raise
    try:
        if db.IS_POSTGRES:
            return history_import.rebuild_history_pg(
                history_path, db.engine, progress_cb=progress,
            )
        with _history_duckdb_lock:
            return history_import.rebuild_history_db(
                history_path, history_duckdb_path, progress_cb=progress,
            )
    finally:
        history_path.unlink(missing_ok=True)


def _persist_card_map_and_meta(
    card_maps: list[tuple[str, str, str, int, str, str]],
    sets_needing_load: set[str],
    downloaded_at: str,
    history_row_count: int,
) -> int:
    """Write fresh mtgjson_card_map rows + history meta. Returns effective row count.

    If ``history_row_count`` is 0 (existing history was reused), reads the
    last persisted count from the meta table so callers can report a
    consistent number.
    """
    with db.get_conn() as conn:
        if sets_needing_load:
            params = {f"s{i}": s for i, s in enumerate(sets_needing_load)}
            placeholders = ", ".join(f":s{i}" for i in range(len(sets_needing_load)))
            conn.execute(
                # placeholders are :s0, :s1, …; user values bound via `params`.
                text(f"DELETE FROM mtgjson_card_map WHERE set_code IN ({placeholders})"),  # noqa: S608
                params,
            )
        if card_maps:
            card_map_dicts = [
                {"card_name": m[0], "set_code": m[1], "card_number": m[2],
                 "is_foil": m[3], "uuid": m[4], "updated_at": m[5]}
                for m in card_maps
            ]
            db.upsert(conn, "mtgjson_card_map",
                      ["card_name", "set_code", "card_number", "is_foil"],
                      card_map_dicts)
        _write_meta(conn, "mtgjson_history_downloaded_at", downloaded_at)
        if history_row_count:
            _write_meta(conn, "mtgjson_history_db_built_at", downloaded_at)
            _write_meta(conn, "mtgjson_history_db_row_count", str(history_row_count))

    if history_row_count:
        return history_row_count
    with db.get_conn() as conn:
        row_count = _read_meta(conn, "mtgjson_history_db_row_count")
    return int(row_count) if row_count else 0


def _import_mtgjson_history(rows: list[dict], *, progress_cb=None) -> tuple[int, int]:
    def _progress(progress: int, phase: str, detail: str) -> None:
        if progress_cb:
            progress_cb(progress, phase, detail)

    inventory_rows = [dict(row) for row in rows]
    downloaded_at = datetime.now(UTC).isoformat(timespec="seconds")

    card_maps, sets_needing_load = _resolve_inventory_uuids(
        inventory_rows, downloaded_at, _progress,
    )

    history_duckdb_path = _mtgjson_history_duckdb_path()
    history_row_count = _ensure_history_loaded(history_duckdb_path, _progress)

    _progress(96, "Saving mappings", "Updating local card-to-MTGJSON mappings...")
    history_row_count = _persist_card_map_and_meta(
        card_maps, sets_needing_load, downloaded_at, history_row_count,
    )

    _progress(98, "Updating prices", "Writing latest prices to market table...")
    _populate_market_prices_from_history(
        card_maps,
        None if db.IS_POSTGRES else history_duckdb_path,
        downloaded_at,
    )

    _progress(100, "Done", f"Indexed {history_row_count:,} MTGJSON price points and mapped {len(card_maps)} lot(s).")
    return len(card_maps), history_row_count


@app.route("/market")
def market():
    inv.init_schema()
    user_id = _get_user_id()
    inventory_rows = inv.list_all(user_id)

    # Load cached prices — no live fetch on GET.
    with db.get_conn() as conn:
        cache_rows = [db.row_to_dict(r) for r in conn.execute(
            text("SELECT card_name, set_code, is_foil, price_usd, fetched_at FROM market_prices")
        ).mappings().all()]
        mtgjson_downloaded_at = _read_meta(conn, "mtgjson_history_downloaded_at")

    price_cache: dict[tuple, float | None] = {}
    last_fetched_at: str | None = None
    for cr in cache_rows:
        key = (cr["card_name"].lower(), cr["set_code"].lower(), cr["is_foil"])
        price_cache[key] = cr["price_usd"]
        if last_fetched_at is None or cr["fetched_at"] > last_fetched_at:
            last_fetched_at = cr["fetched_at"]

    has_cache = bool(price_cache)
    last_refreshed = _format_ago(last_fetched_at)
    history_db_exists = _has_price_history()
    mtgjson_last_downloaded = _format_ago(mtgjson_downloaded_at) if history_db_exists else None

    if not inventory_rows:
        return render_template(
            "market.html", rows=[], summary=None, fx=None, error=None,
            has_cache=has_cache,
            last_refreshed=last_refreshed,
            mtgjson_last_downloaded=mtgjson_last_downloaded,
            history_db_exists=history_db_exists,
            allow_price_update=not db.IS_POSTGRES,
            active="market",
        )

    fx = _get_fx() if has_cache else None

    priced = []
    for row in inventory_rows:
        is_foil = int(_is_foil(row.get("printing")))
        key = (row["card_name"].lower(), _normalize_set_code(row["set_code"]), is_foil)
        price_usd = price_cache.get(key) if has_cache else None
        priced.append({
            **row,
            "market_price_usd": price_usd,
            "market_price_jpy": round(price_usd * fx) if (price_usd is not None and fx) else None,
        })

    if not has_cache:
        return render_template(
            "market.html", rows=priced, summary=None, fx=None, error=None,
            has_cache=False,
            last_refreshed=None,
            mtgjson_last_downloaded=mtgjson_last_downloaded,
            history_db_exists=history_db_exists,
            allow_price_update=not db.IS_POSTGRES,
            active="market",
        )

    for row in priced:
        pb  = row.get("price_bought")
        mp  = row.get("market_price_usd")
        qty = row["quantity"]
        row["cost_basis_usd"]   = round(pb * qty, 2) if pb is not None else None
        row["market_value_usd"] = round(mp * qty, 2) if mp is not None else None
        row["market_value_jpy"] = round(row["market_price_jpy"] * qty) if row["market_price_jpy"] is not None else None
        if pb is not None and mp is not None:
            row["pnl_usd"] = round((mp - pb) * qty, 2)
            row["pnl_pct"] = round((mp / pb - 1) * 100, 1) if pb > 0 else 0.0
        else:
            row["pnl_usd"] = None
            row["pnl_pct"] = None

    priced.sort(key=lambda r: (r["pnl_usd"] is None, -(r["pnl_usd"] or 0)))

    pnl_rows    = [r for r in priced if r["pnl_usd"]          is not None]
    cost_rows   = [r for r in priced if r["cost_basis_usd"]   is not None]
    market_rows = [r for r in priced if r["market_value_usd"] is not None]

    total_cost       = sum(r["cost_basis_usd"]   for r in cost_rows)
    total_pnl        = sum(r["pnl_usd"]          for r in pnl_rows)
    total_market     = sum(r["market_value_usd"]  for r in market_rows)
    total_market_jpy = sum(r["market_value_jpy"]  for r in market_rows if r["market_value_jpy"] is not None)

    summary = {
        "total_cost_usd":   round(total_cost,   2),
        "total_pnl_usd":    round(total_pnl,    2),
        "pnl_pct":          round(total_pnl / total_cost * 100, 1) if total_cost > 0 else None,
        "total_market_usd": round(total_market, 2),
        "total_market_jpy": round(total_market_jpy),
        "lots_total":       len(priced),
        "lots_no_cost":     len(priced) - len(cost_rows),
        "lots_no_market":   len(priced) - len(market_rows),
        "lots_in_pnl":      len(pnl_rows),
    }

    return render_template(
        "market.html",
        rows=priced, summary=summary, fx=fx, error=None,
        has_cache=True, last_refreshed=last_refreshed,
        mtgjson_last_downloaded=mtgjson_last_downloaded,
        history_db_exists=history_db_exists,
        allow_price_update=not db.IS_POSTGRES,
        active="market",
    )


@app.route("/market/history/download", methods=["POST"])
def market_history_download():
    inv.init_schema()
    # Use global inventory so all users' cards get UUID-mapped and priced.
    inventory_rows = inv.list_all_global()

    with _download_jobs_lock:
        running = next((job for job in _download_jobs.values() if job["state"] == "running"), None)
    if running:
        return jsonify({"ok": True, "job_id": running["id"], "already_running": True})

    job_id = uuid4().hex
    _init_download_job(job_id)

    def _progress(progress: int, phase: str, detail: str) -> None:
        _set_download_job(job_id, progress=progress, phase=phase, detail=detail)

    def _worker(snapshot_rows: list[dict]) -> None:
        try:
            mapped_count, point_count = _import_mtgjson_history(snapshot_rows, progress_cb=_progress)
            _set_download_job(
                job_id,
                state="done",
                progress=100,
                phase="Done",
                detail=f"Downloaded history for {mapped_count} lot(s) and imported {point_count} daily price points.",
            )
        except Exception as exc:
            app.logger.exception(
                "event=history_download_failed job_id=%s class=%s",
                job_id, type(exc).__name__,
            )
            _set_download_job(
                job_id,
                state="error",
                phase="Failed",
                detail="MTGJSON history download failed.",
                error=str(exc),
            )

    Thread(target=_worker, args=([dict(row) for row in inventory_rows],), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id, "already_running": False})


@app.route("/market/history/download/status")
def market_history_download_status():
    job_id = request.args.get("job_id", "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    job = _get_download_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    return jsonify({"ok": True, **job})


@app.route("/market/history")
def market_history():
    card_name = request.args.get("card_name", "").strip()
    set_code = _normalize_set_code(request.args.get("set_code", ""), upper=True)
    card_number = request.args.get("card_number", "").strip()
    is_foil = 1 if request.args.get("printing", "").strip().lower() == "foil" else 0
    period = request.args.get("period", "1m").strip().lower()
    if period not in _MARKET_HISTORY_PERIODS:
        period = "1m"

    if not card_name or not set_code:
        return jsonify({"ok": False, "error": "card_name and set_code are required"}), 400

    inv.init_schema()
    with db.get_conn() as conn:
        downloaded_at = _read_meta(conn, "mtgjson_history_downloaded_at")
        mapped = conn.execute(
            text("""SELECT uuid
                    FROM mtgjson_card_map
                    WHERE lower(card_name) = lower(:card_name)
                      AND set_code = :set_code
                      AND card_number = :card_number
                      AND is_foil = :is_foil
                    LIMIT 1"""),
            {"card_name": card_name, "set_code": set_code,
             "card_number": card_number, "is_foil": is_foil},
        ).mappings().first()

    finish = "foil" if is_foil else "normal"
    history = _query_history(mapped["uuid"], finish) if mapped else {}
    dense_points = _densify_daily_points(
        history,
        end_day=datetime.now(UTC).date(),
    ) if history else []
    if period != "all" and dense_points:
        cutoff = _history_cutoff(period)
        if cutoff is None:
            raise ValueError(f"Unknown history period: {period!r}")
        dense_points = [
            point for point in dense_points
            if datetime.fromisoformat(point["market_date"]).replace(tzinfo=UTC) >= cutoff
        ]
    available_since = next((point["market_date"] for point in dense_points if point["price_usd"] is not None), None)

    return jsonify({
        "ok": True,
        "card_name": card_name,
        "set_code": set_code,
        "card_number": card_number,
        "is_foil": bool(is_foil),
        "default_period": "1m",
        "period": period,
        "available_periods": list(_MARKET_HISTORY_PERIODS),
        "period_days": _MARKET_HISTORY_PERIODS,
        "available_since": available_since,
        "downloaded_at": downloaded_at,
        "has_history": bool(history),
        "source": {
            "label": "MTGJSON / TCGplayer retail",
            "detail": (
                "Imported from MTGJSON price history. Blank days mean MTGJSON has no value"
                " for that day or the local download is behind."
            ),
        },
        "points": dense_points,
        "all_points_count": len(dense_points),
    })


@app.route("/inventory")
def inventory():
    inv.init_schema()
    user_id = _get_user_id()
    return render_template(
        "inventory.html",
        rows=inv.list_all(user_id),
        stats=inv.stats(user_id),
        active="inventory",
    )


def _opt_float(value: str) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


@app.route("/inventory/add", methods=["POST"])
def inventory_add():
    user_id = _get_user_id()
    record = {}
    try:
        record = {
            "card_name": request.form["card_name"].strip(),
            "set_code": request.form["set_code"].strip().upper(),
            "set_name": request.form.get("set_name", "").strip(),
            "card_number": request.form.get("card_number", "").strip(),
            "quantity": int(request.form.get("quantity", "1")),
            "condition": request.form.get("condition", "NM").strip(),
            "printing": request.form.get("printing", "Normal").strip(),
            "language": request.form.get("language", "English").strip(),
            "price_bought": _opt_float(request.form.get("price_bought", "")),
            "date_bought": request.form.get("date_bought", "").strip() or None,
        }
        if not record["card_name"] or not record["set_code"]:
            flash("Card name and set are required.")
            return redirect(url_for("inventory"))
        inv.add_one(record, user_id)
    except Exception as exc:
        app.logger.exception(
            "event=inventory_add_failed source=manual card=%r set_code=%r",
            record.get("card_name"), record.get("set_code"),
        )
        flash(f"Add failed: {exc}")
        return redirect(url_for("inventory"))

    app.logger.info(
        "event=inventory_add source=manual card=%r set_code=%r quantity=%d",
        record["card_name"], record["set_code"], record["quantity"],
    )
    flash(f"Added {record['quantity']}x {record['card_name']} [{record['set_code']}].")
    return redirect(url_for("inventory"))


@app.route("/inventory/add-bulk", methods=["POST"])
def inventory_add_bulk():
    user_id = _get_user_id()
    payload = request.get_json(silent=True) or {}
    records = payload.get("records") or []
    if not records:
        return {"ok": False, "error": "No records"}, 400
    try:
        count = inv.add_many(records, user_id)
    except Exception as exc:
        app.logger.exception(
            "event=inventory_add_failed source=decklist record_count=%d",
            len(records),
        )
        return {"ok": False, "error": str(exc)}, 500
    app.logger.info(
        "event=inventory_add source=decklist record_count=%d added=%d",
        len(records), count,
    )
    flash(f"Added {count} card(s) from decklist.")
    return {"ok": True, "count": count}


@app.route("/inventory/import", methods=["POST"])
def inventory_import():
    user_id = _get_user_id()
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        flash("No file selected.")
        return redirect(url_for("inventory"))

    replace = request.form.get("mode", "replace") != "append"

    # csv.DictReader needs a real file path (we open with utf-8-sig).
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        uploaded.save(tmp.name)
        tmp_path = tmp.name
    try:
        count = inv.import_csv(tmp_path, replace=replace, user_id=user_id)
    except Exception as exc:
        app.logger.exception(
            "event=inventory_import_failed filename=%r replace_mode=%d",
            uploaded.filename, int(replace),
        )
        flash(f"Import failed: {exc}")
        return redirect(url_for("inventory"))
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    app.logger.info(
        "event=inventory_import filename=%r replace_mode=%d rows=%d",
        uploaded.filename, int(replace), count,
    )
    verb = "Replaced inventory with" if replace else "Appended"
    flash(f"{verb} {count} rows from {uploaded.filename}.")
    return redirect(url_for("inventory"))


def _run_daily_price_update(
    progress_cb=None,
) -> tuple[int, int, int, "date | None"]:
    """Download today's prices for all cards and update UUID mappings for inventory.

    Used by the cron endpoint. Returns
    (mapped_count, rows_inserted, uuids_streamed, market_date).
    """
    def _progress(progress: int, phase: str, detail: str) -> None:
        if progress_cb:
            progress_cb(progress, phase, detail)

    inventory_rows = inv.list_all_global()
    cache_dir = _mtgjson_cache_dir()

    today_xz = cache_dir / "AllPricesToday.json.xz"
    _progress(10, "Downloading today's prices", "Downloading AllPricesToday.json.xz...")
    try:
        _download_file(f"{_MTGJSON_BASE_URL}/AllPricesToday.json.xz", today_xz)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            raise RuntimeError("MTGJSON AllPricesToday not available yet.") from exc
        raise

    market_date = history_import.read_meta_date(today_xz)

    if db.IS_POSTGRES:
        uuids_streamed, rows_inserted = history_import.merge_today_prices_pg(
            today_xz, db.engine, progress_cb=_progress,
        )
    else:
        duckdb_path = _mtgjson_history_duckdb_path()
        with _history_duckdb_lock:
            uuids_streamed, rows_inserted = history_import.merge_today_prices(
                today_xz, duckdb_path, progress_cb=_progress,
            )
    today_xz.unlink(missing_ok=True)

    mapped_count, _ = _import_mtgjson_history(inventory_rows, progress_cb=_progress)
    return mapped_count, rows_inserted, uuids_streamed, market_date


@app.route("/internal/cron/update-prices", methods=["POST"])
@csrf.exempt
def cron_update_prices():
    """Protected endpoint for the daily K8s CronJob.

    Requires Authorization: Bearer <CRON_SECRET> header.
    Pass `X-Trigger-Source: manual` to mark a hand-fired run.
    """
    triggered_at = datetime.now(UTC)
    trigger_source = request.headers.get("X-Trigger-Source", "cron")

    if _CRON_SECRET:
        provided = request.headers.get("Authorization", "")
        expected = f"Bearer {_CRON_SECRET}"
        if not hmac.compare_digest(provided, expected):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401

    inv.init_schema()

    with _download_jobs_lock:
        running = next((j for j in _download_jobs.values() if j["state"] == "running"), None)
    if running:
        return jsonify({"ok": True, "job_id": running["id"], "already_running": True})

    job_id = uuid4().hex
    _init_download_job(job_id)
    run_id = run_log.record_start(triggered_at, trigger_source, job_id)
    app.logger.info(
        "Daily price update started run_id=%s job_id=%s source=%s",
        run_id, job_id, trigger_source,
    )

    def _progress(progress: int, phase: str, detail: str) -> None:
        _set_download_job(job_id, progress=progress, phase=phase, detail=detail)

    def _worker() -> None:
        t0 = monotonic()
        try:
            mapped_count, rows_inserted, uuids_streamed, market_date = (
                _run_daily_price_update(progress_cb=_progress)
            )
            duration_ms = int((monotonic() - t0) * 1000)
            _set_download_job(
                job_id, state="done", progress=100, phase="Done",
                detail=f"Updated {rows_inserted:,} price points for {mapped_count} lot(s).",
            )
            run_log.record_finish(
                run_id=run_id, status="success", duration_ms=duration_ms,
                uuids_streamed=uuids_streamed, rows_inserted=rows_inserted,
                market_date=market_date,
            )
            app.logger.info(
                "Daily price update done run_id=%s rows_inserted=%s "
                "uuids_streamed=%s market_date=%s duration_ms=%s",
                run_id, rows_inserted, uuids_streamed, market_date, duration_ms,
            )
        except Exception as exc:
            duration_ms = int((monotonic() - t0) * 1000)
            app.logger.exception("Daily price update failed run_id=%s", run_id)
            _set_download_job(job_id, state="error", phase="Failed",
                              detail="Daily price update failed.", error=str(exc))
            run_log.record_finish(
                run_id=run_id, status="failed", duration_ms=duration_ms,
                error_message=str(exc),
            )

    Thread(target=_worker, daemon=True).start()
    return jsonify({
        "ok": True, "job_id": job_id, "run_id": run_id, "already_running": False,
    })


def main() -> None:
    host = os.environ.get("FLASK_RUN_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_RUN_PORT", "5000"))
    # use_reloader=False so start/stop scripts have a single PID to manage;
    # debug features (interactive tracebacks) are still active.
    # Local dev only — production serves via gunicorn (see Dockerfile).
    app.run(host=host, port=port, debug=True, use_reloader=False)  # noqa: S201


if __name__ == "__main__":
    main()
