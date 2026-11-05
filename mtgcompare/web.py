"""Flask web UI for mtgcompare.

Run: uv run python -m mtgcompare.web
Visit: http://127.0.0.1:5000
"""
import hmac
import logging.config
import math
import os
import queue
import re
import tempfile
from collections.abc import Collection, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from uuid import uuid4

import orjson
import requests
from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    stream_with_context,
    url_for,
)
from flask.json.provider import JSONProvider

from . import auth, db, decklist, jobs, pricing, search
from . import inventory as inv
from .log_context import (
    REQUEST_ID_HEADER,
    bind_request_id,
    install_healthz_access_filter,
    install_record_factory,
)
from .pricing import run_log
from .scrapers import variants
from .scrapers.registry import (
    ACTIVE_SHOPS,
    MARKETPLACE_SHOPS,
    SHIPPING_JPY,
    SHOP_FLAGS,
    collect_prices,
    shop_slug,
)
from .sealed import repo as sealed_repo
from .sealed import service as sealed_service
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

# Drop kube-probe /healthz hits from the gunicorn access log; otherwise
# every pod emits ~8.6k pointless lines/day from readiness+liveness probes.
install_healthz_access_filter()

app = Flask(__name__)

# Swap Flask's stdlib JSON for orjson — faster, ~half the memory.
class _OrjsonProvider(JSONProvider):
    def dumps(self, obj, **_):
        return orjson.dumps(obj).decode()
    def loads(self, s, **_):
        if isinstance(s, str):
            s = s.encode()
        return orjson.loads(s)
app.json = _OrjsonProvider(app)

# Production refuses to boot with the dev fallback secret key — it signs
# CSRF tokens and flask sessions, and the fallback is in the public repo.
_SECRET_KEY = os.environ.get("SECRET_KEY", "")
if db.IS_POSTGRES and not _SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY must be set when DATABASE_URL is set "
        "(production must not use the public dev fallback)."
    )
app.secret_key = _SECRET_KEY or "mtgcompare-local-dev"

# Cap every request body. Werkzeug bounds non-file form fields
# (MAX_FORM_MEMORY_SIZE, 500 kB) but not uploads or JSON bodies, so without
# this one signed-in user could stream a multi-GB CSV to /inventory/import or
# a JSON body to /inventory/add-bulk and exhaust the pod's memory/disk. A
# Deckbox export of a few thousand lots is well under 1 MB.
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024


# Stamp a per-request id BEFORE the auth blueprint's gate runs, so even
# the kick-to-login redirect carries a correlatable id in its log line.
# Honors an upstream X-Request-Id when the proxy injects one.
@app.before_request
def _bind_log_context():
    bind_request_id()
    g._req_start_monotonic = monotonic()


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
#
# Disabled in the loadtest sidecar (TRUST_USER_HEADER=1) so k6 doesn't
# need to scrape a token off the search page before every POST. The
# sidecar is internal-only, has no public Ingress, and only accepts
# traffic from labeled loadtest pods — CSRF would be defending against
# a threat model that doesn't apply.
from flask_wtf.csrf import CSRFProtect  # noqa: E402

if os.environ.get("TRUST_USER_HEADER") == "1":
    app.config["WTF_CSRF_ENABLED"] = False
csrf = CSRFProtect(app)
csrf.exempt(auth.webhook)


# Scripts are all files under /static (page values ride in non-executable
# <script type="application/json"> blocks), so script-src needs no
# 'unsafe-inline'. Inline <style> blocks and style attributes remain.
# Scryfall is the only third-party origin (card images + named-card lookup).
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
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

# The webhook's HMAC signature is its only authentication (the route is
# public-prefixed and CSRF-exempt). With an empty secret the signature is
# computable by anyone, and a forged user.deleted event deletes that
# user's inventory rows.
if auth.WORKOS_ENABLED and not auth.WORKOS_WEBHOOK_SECRET:
    raise RuntimeError(
        "WORKOS_WEBHOOK_SECRET must be set when WorkOS is enabled "
        "(otherwise /webhooks/workos accepts forged events)."
    )


inv.init_schema()


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


_REQUEST_LOG_SKIP_PREFIXES = (
    "/healthz", "/static/", "/favicon", "/robots.txt", "/internal/",
)


@app.after_request
def _log_request_access(response):
    path = request.path or "-"
    for skip in _REQUEST_LOG_SKIP_PREFIXES:
        if path.startswith(skip):
            return response
    # Surface anon traffic explicitly so user-aggregations bucket it cleanly
    # rather than collapsing it with records that legitimately lack identity.
    if not getattr(g, "user_id", None) and auth.WORKOS_ENABLED:
        g.user_id = "anonymous"
    start = getattr(g, "_req_start_monotonic", None)
    duration_ms = int((monotonic() - start) * 1000) if start is not None else -1
    app.logger.info(
        "event=request method=%s path=%s status=%s duration_ms=%s",
        request.method, path, response.status_code, duration_ms,
    )
    return response


@app.context_processor
def _inject_current_user():
    return {
        "current_user": _get_display_name(),
        "workos_enabled": auth.WORKOS_ENABLED,
        "allow_price_update": _manual_price_updates_allowed(),
    }


def _manual_price_updates_allowed() -> bool:
    """Only the local app triggers price downloads by hand. The hosted site
    has the daily cron, and a user-triggered MTGJSON import or shop crawl
    is heavy, shared-DB work no visitor should be able to start (nor two
    replicas run at once: the jobs registry is per process)."""
    return not db.IS_POSTGRES


def _manual_price_update_refused():
    return jsonify({"ok": False, "error": "Prices update daily on the hosted site."}), 403


# Marketplace shops are skipped by decklist pricing (see registry), so the
# decklist form hides their shop-filter checkbox. (Their shipping-override
# rows are already absent everywhere via _shipping_config.)
app.jinja_env.globals["marketplace_shops"] = MARKETPLACE_SHOPS
app.jinja_env.filters["variant_label"] = variants.label


def _internal_error_message(action: str) -> str:
    """What a user sees when an unexpected exception was caught: never the
    exception text (it can carry SQL or paths), but the request id that
    finds the logged traceback."""
    rid = getattr(g, "request_id", None)
    return f"{action} failed unexpectedly." + (f" Reference: {rid}." if rid else "")


def _internal_error_json(action: str):
    return {"ok": False, "error": _internal_error_message(action)}, 500


def _render_error(code: int, title: str, message: str):
    """Render the branded error template. Falls back to plain text if the
    template itself somehow fails (e.g. base.html context processor blew
    up alongside the original request)."""
    try:
        return render_template(
            "error.html",
            code=code, title=title, message=message,
            request_id=getattr(g, "request_id", None),
            active=None,
        ), code
    except Exception:
        app.logger.exception("event=error_template_render_failed code=%d", code)
        return f"{code} {title}\n\n{message}\n", code


@app.errorhandler(404)
def _handle_404(_err):
    return _render_error(
        404, "Not found",
        "We couldn't find what you were looking for. Check the URL or head back to the search page.",
    )


@app.errorhandler(500)
def _handle_500(_err):
    return _render_error(
        500, "Something went wrong",
        "An unexpected error happened on our side. The details have been logged — "
        "if you can share the request id below, that helps us track it down.",
    )


def _compute_static_token() -> str:
    """Cache-bust token for ``<script src=".../foo.js?v=TOKEN">`` URLs.

    Max mtime under ``static/`` — coarse but stable within a deploy; one
    image rebuild bumps every file's mtime.
    """
    static_dir = Path(__file__).resolve().parent / "static"
    return str(int(max(
        p.stat().st_mtime for p in static_dir.rglob("*") if p.is_file()
    )))


_STATIC_TOKEN = _compute_static_token()


@app.context_processor
def _inject_static_token():
    return {"static_token": _STATIC_TOKEN}


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
_fx_at: float = 0.0          # monotonic time _fx was fetched
_fx_refreshing = False       # one refresh in flight; others keep the old rate
_fx_lock = Lock()
_fx_failed_at: float | None = None
_FX_TTL_S = 6 * 3600.0  # a daily ECB fixing; pods run for weeks, so re-fetch
_FX_RETRY_S = 60.0    # back off this long after a failed fetch
_FX_TIMEOUT_S = 10.0  # wall-clock cap on one FX fetch


def _get_fx() -> float | None:
    """The JPY/USD rate, re-fetched every ``_FX_TTL_S``.

    It used to be fetched once per process, so a pod that ran for weeks
    priced in a weeks-old rate. The network call runs outside the lock and
    under a wall-clock cap, so a hung upstream can't serialize every request
    behind one socket. While a refresh is in flight, or after it failed,
    callers keep the last good rate; only a process that never got one
    returns None. Failures are remembered for ``_FX_RETRY_S`` so an outage
    costs one bounded fetch per window instead of one per request.
    """
    global _fx, _fx_at, _fx_failed_at, _fx_refreshing
    with _fx_lock:
        now = monotonic()
        if _fx is not None and now - _fx_at < _FX_TTL_S:
            return _fx
        if _fx_refreshing or (_fx_failed_at is not None and now - _fx_failed_at < _FX_RETRY_S):
            return _fx
        _fx_refreshing = True
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fx")
    try:
        rate = executor.submit(get_fx, "jpy").result(timeout=_FX_TIMEOUT_S)
    except Exception as exc:
        # TimeoutError stringifies to "" — log the class so the reason shows.
        app.logger.error("FX lookup failed: %s: %s", type(exc).__name__, exc)
        with _fx_lock:
            _fx_failed_at = monotonic()
            _fx_refreshing = False
            return _fx
    finally:
        # Never wait on a hung fetch; the stray worker thread exits on its
        # own once the underlying socket finally gives up.
        executor.shutdown(wait=False)
    with _fx_lock:
        _fx, _fx_at, _fx_refreshing = rate, monotonic(), False
    return rate


def _parse_shipping_overrides(source) -> dict[str, int]:
    overrides: dict[str, int] = {}
    for shop, default_cost in SHIPPING_JPY.items():
        raw = source.get(f"ship_{shop_slug(shop)}", "").strip()
        try:
            overrides[shop] = max(0, int(float(raw))) if raw else default_cost
        except (ValueError, OverflowError):  # "oops" / "nan" / "inf"
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
    timed_out_shops: set[str] = set()
    query = search.parse_query(q)

    if q and not query.name:
        error = "Add a card name to the filters, e.g. “Force of Will set:DMR”."
    elif q:
        t0 = monotonic()
        fx = _get_fx()
        if fx is None:
            error = "Could not fetch FX rate; try again later."
        else:
            results = collect_prices(
                query.name, fx, enabled=enabled_shops, logger=app.logger,
                timeouts_out=timed_out_shops,
            )
            results = search.collapse_marketplace_offers(results, include_shipping)
            if include_shipping:
                search.apply_shipping(results, shipping_overrides_jpy)
            else:
                results.sort(key=lambda r: r["price_jpy"])
        app.logger.info(
            "event=search_query q=%r name=%r filter_set=%s filter_number=%s "
            "filter_variants=%s shops_enabled=%s include_shipping=%d "
            "result_count=%d timed_out_shops=%s duration_ms=%d",
            q, query.name, query.set_code or "-", query.number or "-",
            ",".join(query.variants) or "-",
            len(enabled_shops) if enabled_shops is not None else "all",
            int(include_shipping), len(results),
            ",".join(sorted(timed_out_shops)) or "none",
            int((monotonic() - t0) * 1000),
        )

    return render_template(
        "index.html",
        q=q,
        card_name=query.name,
        initial_filters={
            "set": query.set_code,
            "number": query.number,
            "variants": query.variants,
        },
        unknown_filter_tokens=query.unknown_tokens,
        filter_options=search.filter_options(results),
        results=results,
        fx=_fx,
        error=error,
        shop_flags=SHOP_FLAGS,
        shipping_config=ship_cfg,
        include_shipping=include_shipping,
        shop_filter_config=shop_filter_cfg,
        shop_filter_active=shop_filter_active,
        timed_out_shops=sorted(timed_out_shops),
        active="search",
    )


def _shipping_config(overrides_jpy: dict | None = None) -> list[dict]:
    """Build the per-shop shipping config list passed to templates.

    Marketplace shops never appear: their seller shipping is already in
    the price, so an editable flat override would double count. Disabled
    shops (MINT MALL while paused) don't either — they are never searched.
    """
    return [
        {
            "shop": shop,
            "slug": shop_slug(shop),
            "cost_jpy": int((overrides_jpy or {}).get(shop, SHIPPING_JPY.get(shop, 0))),
        }
        for shop in SHIPPING_JPY
        if shop not in MARKETPLACE_SHOPS and shop in ACTIVE_SHOPS
    ]


def _load_inventory_qty_map(use_inventory: bool) -> dict[str, int]:
    """Return ``{lower_card_name: total_quantity}`` for the current user.

    Returns an empty dict when ``use_inventory`` is False so callers can
    treat the "not deducting" case the same as "deducting from nothing".
    """
    if not use_inventory:
        return {}
    user_id = _get_user_id()
    inv_map: dict[str, int] = {}
    for row in inv.list_all(user_id):
        k = row["card_name"].lower()
        inv_map[k] = inv_map.get(k, 0) + row["quantity"]
    return inv_map


def _parse_decklist_form_basics(form) -> decklist.DecklistFormBasics:
    return decklist.DecklistFormBasics(
        decklist_text=form.get("decklist", "").strip(),
        shipping_overrides_jpy=_parse_shipping_overrides(form),
        use_inventory=form.get("use_inventory") == "1",
        enabled_shops=_parse_enabled_shops(form),
        match_printings=form.get("match_printings") == "1",
    )


@app.route("/decklist", methods=["POST"])
def decklist_search():
    """No-JS fallback for the streaming search: one blocking request."""
    t0 = monotonic()
    basics = _parse_decklist_form_basics(request.form)
    page = {
        "decklist": basics.decklist_text,
        "error": None,
        "card_rows": [], "shop_list": [], **decklist.EMPTY_TOTALS,
        "fx": None,
        "shop_flags": SHOP_FLAGS,
        "shipping_config": _shipping_config(basics.shipping_overrides_jpy),
        "shop_filter_config": _shop_filter_config(basics.enabled_shops),
        "shop_filter_active": basics.enabled_shops is not None,
        "use_inventory": basics.use_inventory,
        "match_printings": basics.match_printings,
        "skipped_basics": 0, "unparsed_lines": [], "skipped_maybeboard": 0,
        "timed_out_shops": [],
        "active": "search",
    }
    shops_enabled = len(basics.enabled_shops) if basics.enabled_shops is not None else "all"

    def rejected(message: str, reason: str):
        app.logger.info(
            "event=decklist_search status=rejected reason=%s shops_enabled=%s "
            "use_inventory=%d duration_ms=%d",
            reason, shops_enabled, int(basics.use_inventory), int((monotonic() - t0) * 1000),
        )
        return render_template("decklist.html", **{**page, "error": message})

    prep = decklist.prepare_decklist_search(
        basics,
        load_inv_map=lambda: _load_inventory_qty_map(basics.use_inventory),
        get_fx=_get_fx,
    )
    if isinstance(prep, decklist.DecklistReject):
        return rejected(prep.message, prep.reason)

    user_id = _get_user_id()
    active = _acquire_search_slot(user_id)
    if active is not None:
        return rejected(_too_many_searches_msg(active), "rate_limited"), 429
    try:
        result = decklist.price_decklist(prep, collect=collect_prices, logger=app.logger)
    finally:
        _release_search_slot(user_id)

    app.logger.info(
        "event=decklist_search status=ok size=%d distinct_names=%d "
        "names_searched=%d inventory_hits=%d shops_enabled=%s use_inventory=%d "
        "rows_with_match=%d skipped_basics=%d timed_out_shops=%s duration_ms=%d",
        prep.total_cards, len(prep.name_qty), len(prep.names_to_search), prep.inventory_hits,
        len(decklist.effective_search_shops(basics.enabled_shops)),
        int(basics.use_inventory), result.rows_with_match, prep.skipped_basics,
        ",".join(result.timed_out_shops) or "none",
        int((monotonic() - t0) * 1000),
    )
    return render_template("decklist.html", **{
        **page,
        "decklist": prep.decklist_text,
        "card_rows": result.card_rows,
        "shop_list": result.shop_list,
        **result.totals,
        "fx": prep.fx,
        "skipped_basics": prep.skipped_basics,
        "unparsed_lines": prep.unparsed_lines,
        "skipped_maybeboard": prep.skipped_maybeboard,
        "timed_out_shops": result.timed_out_shops,
    })


# Streaming /decklist via SSE.
#
# Cold 100-card searches can exceed Cloudflare's ~100 s edge timeout, so
# we stream a text/event-stream response (meta → row* → shop_timeout* →
# totals* → done | error) with a ": keepalive" every 15 s. One HTTP
# request from submit to done — no job_id, no sticky sessions required.


# Per-user cap on concurrent decklist fan-outs, shared by the sync and SSE
# endpoints. Per-process — with N gunicorn workers the cluster-wide cap is
# 3×N. A slot is held until the fan-out itself ends, not the HTTP response:
# releasing on client disconnect let a connect-and-drop loop pile up
# fan-outs without bound.
_MAX_IN_FLIGHT_PER_USER = 3
_in_flight_by_user: dict[str, int] = {}
_in_flight_lock = Lock()


def _acquire_search_slot(user_id: str) -> int | None:
    """Take a slot; returns None on success, else the user's active count."""
    with _in_flight_lock:
        active = _in_flight_by_user.get(user_id, 0)
        if active >= _MAX_IN_FLIGHT_PER_USER:
            return active
        _in_flight_by_user[user_id] = active + 1
        return None


def _release_search_slot(user_id: str) -> None:
    # Drop the entry at zero so the dict doesn't accumulate one row per
    # distinct user_id across the worker's lifetime.
    with _in_flight_lock:
        remaining = max(0, _in_flight_by_user.get(user_id, 0) - 1)
        if remaining == 0:
            _in_flight_by_user.pop(user_id, None)
        else:
            _in_flight_by_user[user_id] = remaining


def _too_many_searches_msg(active: int) -> str:
    return (
        f"You already have {active} searches in flight. "
        "Wait for one to finish before starting another."
    )


def _format_sse(event_type: str, payload: dict) -> str:
    return f"event: {event_type}\ndata: {orjson.dumps(payload).decode()}\n\n"


@app.route("/decklist/stream", methods=["POST"])
def decklist_stream():
    """Single-request SSE search. Validates the form, then streams the
    text/event-stream response directly — no job_id, no follow-up GET.

    The producer thread drives the fan-out and writes events to a Queue;
    the response generator drains the queue, emitting each event as an
    SSE frame and a ": keepalive" comment every 15 s of silence. The
    keepalive matters for cold 100-card searches where individual shop
    timeouts can run ~30 s with no new ``row`` event in between.
    """
    basics = _parse_decklist_form_basics(request.form)
    prep = decklist.prepare_decklist_search(
        basics,
        load_inv_map=lambda: _load_inventory_qty_map(basics.use_inventory),
        get_fx=_get_fx,
    )
    if isinstance(prep, decklist.DecklistReject):
        return jsonify({"error": prep.message, "reason": prep.reason}), 400

    user_id = _get_user_id()
    active = _acquire_search_slot(user_id)
    if active is not None:
        return jsonify({"error": _too_many_searches_msg(active), "reason": "rate_limited"}), 429

    # Pre-load the row template once per search (render() runs per card,
    # ~up to 100/decklist) while we still hold the Flask app context; the
    # producer thread can't reach app.jinja_env. collect_prices is read
    # here so test monkeypatches on web.collect_prices still apply.
    row_template = app.jinja_env.get_template("_decklist_row.html")

    cancel = Event()

    def produce(q: queue.Queue) -> None:
        try:
            decklist.produce_decklist_events(
                prep=prep, q=q, row_template=row_template,
                collect=collect_prices, logger=app.logger, cancel=cancel,
            )
        finally:
            _release_search_slot(user_id)

    def generate() -> Iterator[str]:
        q: queue.Queue = queue.Queue()
        Thread(target=produce, args=(q,), daemon=True).start()
        try:
            while True:
                try:
                    item = q.get(timeout=15.0)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                if item is None:
                    return
                evt_type, payload = item
                yield _format_sse(evt_type, payload)
        finally:
            # Client gone (or stream done): stop the fan-out from starting
            # any more cards. The producer releases the slot when it exits.
            cancel.set()

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            # Defence-in-depth in case any intermediate proxy is configured
            # to buffer; nginx and cloudflared both honour this hint.
            "X-Accel-Buffering": "no",
        },
    )


def _compute_market_ctx(user_id: str, params: dict) -> dict:
    """Inject the web-layer FX provider and pagination choices into the pure
    pricing computation. Kept as a thin wrapper so the /market route needs no
    change and ``web._get_fx`` monkeypatches still apply."""
    return pricing.compute_market_ctx(
        user_id, params, get_fx=_get_fx, per_page_choices=_PER_PAGE_CHOICES,
    )


@app.route("/market")
def market():
    user_id = _get_user_id()
    params = _parse_table_query(
        request.args,
        sort_choices=pricing.MKT_SORT_CHOICES,
        default_sort="pnl_usd", default_dir="desc",
    )

    # Server-side cache of the heavy computation. The rendered HTML
    # carries a per-request CSRF token, so we cache the data dict and
    # let render_template build a fresh response per call. Daily price
    # update calls pricing.market_cache_clear() to flush.
    cache_key = (
        user_id,
        params["q"], params["sort"], params["direction"],
        params["page"], params["per_page"],
        params["price_mode"], params["price_value"],
    )
    ctx = pricing.market_cache_get(cache_key)
    if ctx is None:
        ctx = _compute_market_ctx(user_id, params)
        pricing.market_cache_set(cache_key, ctx)

    if request.args.get("partial") == "tbody":
        # Filter / sort / pagination updates need to swap both the
        # table fragment AND the cost-basis / market-value / PnL
        # summary fragment so the stats stay consistent with what's
        # shown. The client (paginatedtable.js) reads both keys.
        #
        # When the price cache is empty the full page shows a
        # "Click Update prices" notice instead of the table; the
        # market_value_* fields are unset on rows, which would crash
        # _market_table.html. Return empty fragments so the client's
        # next filter keystroke is a no-op rather than a 500.
        if not ctx.get("has_cache") or not ctx.get("summary"):
            return jsonify({"table_html": "", "summary_html": ""})
        return jsonify({
            "table_html": render_template("_market_table.html", **ctx),
            "summary_html": render_template("_market_summary.html", **ctx),
        })
    return render_template("market.html", **ctx)


@app.route("/market/history/download", methods=["POST"])
def market_history_download():
    if not _manual_price_updates_allowed():
        return _manual_price_update_refused()
    # Use global inventory so all users' cards get UUID-mapped and priced.
    inventory_rows = inv.list_all_global()

    running = jobs.find_running()
    if running:
        return jsonify({"ok": True, "job_id": running["id"], "already_running": True})

    job_id = uuid4().hex
    jobs.init(job_id)

    def _progress(progress: int, phase: str, detail: str) -> None:
        jobs.update(job_id, progress=progress, phase=phase, detail=detail)

    def _worker(snapshot_rows: list[dict]) -> None:
        try:
            mapped_count, point_count = pricing.import_mtgjson_history(snapshot_rows, progress_cb=_progress)
            jobs.update(
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
            jobs.update(
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
    job = jobs.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    return jsonify({"ok": True, **job})


@app.route("/market/history")
def market_history():
    card_name = request.args.get("card_name", "").strip()
    set_code = pricing.normalize_set_code(request.args.get("set_code", ""), upper=True)
    if not card_name or not set_code:
        return jsonify({"ok": False, "error": "card_name and set_code are required"}), 400
    return jsonify(pricing.card_history_payload(
        card_name=card_name,
        set_code=set_code,
        card_number=request.args.get("card_number", "").strip(),
        foil=request.args.get("printing", "").strip().lower() == "foil",
        period=request.args.get("period", "").strip().lower(),
    ))


@app.route("/market/history/portfolio")
def market_history_portfolio():
    return jsonify(pricing.portfolio_history_payload(inv.list_all(_get_user_id())))


# --- Sealed products ---------------------------------------------------------

@app.route("/sealed")
def sealed_page():
    user_id = _get_user_id()
    ctx = sealed_service.compute_sealed_ctx(user_id, fx=_get_fx())
    return render_template("sealed.html", active="sealed", **ctx)


def _resolve_sealed_product_or_flash() -> dict | None:
    name = request.form.get("product", "").strip()
    if not name:
        flash("Pick a product.")
        return None
    product = sealed_service.resolve_product(name)
    if not product:
        flash(f"Unknown product {name!r}. Pick one from the list.")
    return product


@app.route("/sealed/lots", methods=["POST"])
def sealed_add_lot():
    user_id = _get_user_id()
    product = _resolve_sealed_product_or_flash()
    if not product:
        return redirect(url_for("sealed_page"))
    language = request.form.get("language", "EN").strip().upper()
    currency = request.form.get("currency", "JPY").strip().upper()
    try:
        quantity = int(request.form.get("quantity", "1"))
    except ValueError:
        quantity = 0
    if language not in sealed_service.LANGUAGES or currency not in sealed_service.CURRENCIES or quantity < 1:
        flash("Invalid language, currency or quantity.")
        return redirect(url_for("sealed_page"))
    with db.get_conn() as conn:
        sealed_repo.add_lot(conn, user_id, {
            "product_uuid": product["uuid"],
            "language": language,
            "quantity": quantity,
            "price_bought": _opt_float(request.form.get("price_bought", "")),
            "currency": currency,
            "date_bought": request.form.get("date_bought", "").strip() or None,
        })
    sealed_service.ensure_priced(product)
    app.logger.info("event=sealed_lot_add product=%s quantity=%d", product["uuid"], quantity)
    flash(f"Added {quantity}x {product['name']} ({language}).")
    return redirect(url_for("sealed_page"))


@app.route("/sealed/lots/<int:lot_id>/delete", methods=["POST"])
def sealed_delete_lot(lot_id: int):
    user_id = _get_user_id()
    with db.get_conn() as conn:
        deleted = sealed_repo.delete_lot(conn, user_id, lot_id)
    flash("Lot removed." if deleted else "Lot not found.")
    return redirect(url_for("sealed_page"))


@app.route("/sealed/watch", methods=["POST"])
def sealed_add_watch():
    user_id = _get_user_id()
    product = _resolve_sealed_product_or_flash()
    if not product:
        return redirect(url_for("sealed_page"))
    with db.get_conn() as conn:
        sealed_repo.add_watch(conn, user_id, product["uuid"], datetime.now(UTC).isoformat(timespec="seconds"))
    sealed_service.ensure_priced(product)
    flash(f"Watching {product['name']}.")
    return redirect(url_for("sealed_page"))


@app.route("/sealed/watch/delete", methods=["POST"])
def sealed_delete_watch():
    user_id = _get_user_id()
    with db.get_conn() as conn:
        sealed_repo.delete_watch(conn, user_id, request.form.get("product_uuid", ""))
    return redirect(url_for("sealed_page"))


@app.route("/sealed/refresh", methods=["POST"])
def sealed_refresh():
    """Refresh the catalog (if stale) and every tracked product's price.

    Local mode only (see ``_manual_price_updates_allowed``).
    """
    if not _manual_price_updates_allowed():
        return _manual_price_update_refused()
    running = jobs.find_running("sealed")
    if running:
        return jsonify({"ok": True, "job_id": running["id"], "already_running": True})
    job_id = uuid4().hex
    jobs.init(job_id, kind="sealed")

    def _progress(progress: int, phase: str, detail: str) -> None:
        jobs.update(job_id, progress=progress, phase=phase, detail=detail)

    def _worker() -> None:
        try:
            stats = sealed_service.run_daily_update(progress_cb=_progress)
            detail = f"TCGPlayer: {stats['priced']} of {stats['products']} product(s)."
            if stats["failed"]:
                detail += f" {stats['failed']} failed."
            for shop, shop_stats in stats.get("shops", {}).items():
                detail += (
                    f" {shop}: failed." if "error" in shop_stats
                    else f" {shop}: {shop_stats['recorded']} offers."
                )
            jobs.update(job_id, state="done", progress=100, phase="Done", detail=detail)
        except Exception as exc:
            app.logger.exception("event=sealed_refresh_failed job_id=%s", job_id)
            jobs.update(job_id, state="error", phase="Failed",
                        detail="Sealed price refresh failed.", error=str(exc))

    Thread(target=_worker, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id, "already_running": False})


@app.route("/sealed/precons/search")
def sealed_precon_search():
    """Autocomplete for the inventory's Precon add mode: openable products
    (precons, Secret Lairs) matching every word typed."""
    q = request.args.get("q", "").strip()[:100]
    return jsonify({"ok": True, "results": sealed_service.search_openable(q) if len(q) >= 2 else []})


@app.route("/sealed/precons/<product_uuid>/cards")
def sealed_precon_cards(product_uuid: str):
    """An openable product's cards as inventory add-preview records."""
    language = request.args.get("language", "EN").strip().upper()
    try:
        result = sealed_service.precon_preview(
            product_uuid,
            skip_basics=request.args.get("skip_basics", "1") == "1",
            include_sideboard=request.args.get("sideboard", "0") == "1",
            language=language if language in sealed_service.LANGUAGES else "EN",
        )
    except sealed_service.PreconError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except requests.RequestException as exc:
        app.logger.warning(
            "event=precon_deck_fetch_failed product=%s class=%s err=%s",
            product_uuid, type(exc).__name__, exc,
        )
        return jsonify({"ok": False, "error": "MTGJSON is unreachable right now. Try again later."}), 502
    return jsonify({"ok": True, **result})


@app.route("/sealed/history")
def sealed_history():
    result = sealed_service.product_history(
        request.args.get("uuid", "").strip(),
        sealed_service.parse_series(request.args.get("series")),
    )
    if result is None:
        return jsonify({"ok": False, "error": "unknown product"}), 404
    points = result["points"]
    return jsonify({
        "ok": True,
        "product": result["product"],
        "series": result["series"],
        "series_options": result["series_options"],
        "currency": result["currency"],
        "default_period": "all",
        "period": "all",
        "available_periods": list(pricing.MARKET_HISTORY_PERIODS),
        "period_days": pricing.MARKET_HISTORY_PERIODS,
        "available_since": points[0]["market_date"] if points else None,
        "downloaded_at": points[-1]["market_date"] if points else None,
        "has_history": bool(points),
        "source": {"label": result["series"], "detail": result["source_detail"]},
        "points": points,
        "all_points_count": len(points),
    })


_PER_PAGE_CHOICES = (25, 50, 100, 200)


def _clamp_int(value: str | None, *, default: int, lo: int, hi: int) -> int:
    """Tolerate junk in URL params — never 500 on a malformed ?page=foo."""
    if value is None:
        return default
    try:
        n = int(value)
    except ValueError:
        return default
    return max(lo, min(hi, n))


def _parse_table_query(args, *, sort_choices: Collection[str],
                       default_sort: str, default_dir: str) -> dict:
    """Shared filter/sort/pagination parser for /inventory and /market.

    Caller supplies the page-specific sort whitelist and defaults. The
    filter shape (q + price_mode/price_value) is identical across pages
    so users can carry filter context between them.
    """
    per_page = _clamp_int(args.get("per_page"), default=50, lo=1, hi=200)
    if per_page not in _PER_PAGE_CHOICES:
        per_page = 50
    page = _clamp_int(args.get("page"), default=1, lo=1, hi=10_000)

    sort = args.get("sort") or default_sort
    if sort not in sort_choices:
        sort = default_sort
    direction = (args.get("dir") or default_dir).lower()
    if direction not in ("asc", "desc"):
        direction = default_dir

    return {
        "sort": sort, "direction": direction, "page": page, "per_page": per_page,
        **_parse_inventory_filter(args),
    }


def _parse_inventory_filter(source) -> dict:
    """The inventory filter (name substring, price mode/value, set,
    condition) from query args or a JSON ``match`` object. Shared by the
    /inventory and /market tables and filter-based delete, so "delete all
    matching" removes exactly what the table showed."""
    price_mode = source.get("price_mode") or "any"
    return {
        "q": str(source.get("q") or "").strip(),
        "price_mode": price_mode if price_mode in inv.PRICE_MODES else "any",
        "price_value": _opt_float(str(source.get("price_value") or "")),
        "set_code": str(source.get("set_code") or "").strip().upper() or None,
        "condition": str(source.get("condition") or "").strip() or None,
    }


def _inventory_filter_kwargs(params: dict) -> dict:
    return {
        "q": params["q"] or None,
        "price_mode": params["price_mode"], "price_value": params["price_value"],
        "set_code": params["set_code"], "condition": params["condition"],
    }


def _open_lot_config(user_id: str, lot_id: str | None) -> dict | None:
    """``/inventory?open_lot=<id>`` (the sealed page's Open button): the lot
    the Precon add mode should preload, or None."""
    if not lot_id or not lot_id.isdigit():
        return None
    with db.get_conn() as conn:
        lot = sealed_repo.get_lot(conn, user_id, int(lot_id))
    if not lot or not lot["openable"]:
        return None
    return {
        "lot_id": lot["id"], "product_uuid": lot["uuid"], "name": lot["name"],
        "language": lot["language"], "quantity": lot["quantity"],
    }


@app.route("/inventory")
def inventory():
    user_id = _get_user_id()
    params = _parse_table_query(
        request.args,
        sort_choices=inv.SORT_COLUMNS,
        default_sort="card_name", default_dir="asc",
    )

    # Two server round-trips: aggregate CTE + paginated page query.
    filters = _inventory_filter_kwargs(params)
    rows, matched, stats = inv.page_with_aggregates(
        user_id, sort=params["sort"], direction=params["direction"],
        page=params["page"], per_page=params["per_page"], **filters,
    )
    total = matched["printings"]
    total_pages = max(1, math.ceil(total / params["per_page"])) if total else 1
    # If the user landed on a stale page (e.g. they were on page 5, then
    # filtered down to 2 pages of results), pin them to the last real
    # page and refetch the page rows. Aggregates remain correct (they're
    # computed across the whole filtered set, not the page).
    if params["page"] > total_pages:
        params["page"] = total_pages
        rows = inv.list_paginated(
            user_id, sort=params["sort"], direction=params["direction"],
            page=params["page"], per_page=params["per_page"], **filters,
        )

    ctx = {
        "rows": rows,
        "stats": stats,
        "matched": matched,
        "params": params,
        "total": total,
        "total_pages": total_pages,
        "per_page_choices": _PER_PAGE_CHOICES,
        "set_choices": inv.distinct_sets(user_id),
        "condition_choices": inv.distinct_conditions(user_id),
        "open_lot": _open_lot_config(user_id, request.args.get("open_lot")),
        "active": "inventory",
    }
    if request.args.get("partial") == "tbody":
        # Same shape as /market: swap both the table and the inv-stats
        # "X copies across Y lots · cost basis $Z" line so the totals
        # match the filtered view.
        return jsonify({
            "table_html": render_template("_inventory_table.html", **ctx),
            "summary_html": render_template("_inventory_stats.html", **ctx),
        })
    return render_template("inventory.html", **ctx)


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
    except ValueError:
        flash("Quantity must be a whole number.")
        return redirect(url_for("inventory"))
    except Exception:
        app.logger.exception(
            "event=inventory_add_failed source=manual card=%r set_code=%r",
            record.get("card_name"), record.get("set_code"),
        )
        flash(_internal_error_message("Add"))
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
    if not records or not isinstance(records, list):
        return {"ok": False, "error": "No records"}, 400
    # Cards opened from a precon / Secret Lair: also record their MTGJSON
    # uuids, and consume one unit of the sealed lot when opening one.
    precon = payload.get("precon") if isinstance(payload.get("precon"), dict) else None
    source = "precon" if precon else "decklist"
    try:
        if precon:
            lot_id = precon.get("lot_id")
            count = sealed_service.add_precon_cards(
                user_id, records,
                product_uuid=str(precon.get("product_uuid") or ""),
                lot_id=int(lot_id) if lot_id not in (None, "") else None,
            )
        else:
            count = inv.add_many(records, user_id)
    except sealed_service.PreconError as exc:
        return {"ok": False, "error": str(exc)}, 404
    except (ValueError, TypeError, KeyError):
        return {"ok": False, "error": "Malformed records"}, 400
    except requests.RequestException:
        app.logger.warning("event=inventory_add_failed source=precon reason=mtgjson_unreachable")
        return {"ok": False, "error": "MTGJSON is unreachable right now. Try again later."}, 502
    except Exception:
        app.logger.exception(
            "event=inventory_add_failed source=%s record_count=%d",
            source, len(records),
        )
        return _internal_error_json("Add")
    app.logger.info(
        "event=inventory_add source=%s record_count=%d added=%d",
        source, len(records), count,
    )
    if precon and precon.get("lot_id") not in (None, ""):
        flash(f"Opened the sealed lot: added {count} card(s).")
    else:
        flash(f"Added {count} card(s) from {source}.")
    return {"ok": True, "count": count}


@app.route("/inventory/delete", methods=["POST"])
def inventory_delete():
    """Delete inventory rows for the current user.

    Two payload shapes:
      - {"ids": [1, 2, 3]}                  — id-based, used by per-page
                                              "Delete selected"
      - {"match": {"q": "...", "price_mode": "lte", "price_value": 0.5}}
                                              — filter-based, used by the
                                              virtual "Select all matching"
                                              flow. With an empty/absent
                                              filter, wipes the user's
                                              entire inventory; the client
                                              is responsible for typed
                                              confirmation on large counts.
    """
    user_id = _get_user_id()
    payload = request.get_json(silent=True) or {}

    if "ids" in payload:
        raw_ids = payload.get("ids") or []
        if not isinstance(raw_ids, list) or not raw_ids:
            return {"ok": False, "error": "No ids"}, 400
        try:
            ids = [int(x) for x in raw_ids]
        except (TypeError, ValueError):
            return {"ok": False, "error": "Invalid ids"}, 400
        try:
            count = inv.delete(ids, user_id)
        except Exception:
            app.logger.exception(
                "event=inventory_delete_failed source=ids requested=%d",
                len(ids),
            )
            return _internal_error_json("Delete")
        app.logger.info(
            "event=inventory_delete source=ids requested=%d deleted=%d",
            len(ids), count,
        )
        return {"ok": True, "count": count}

    match = payload.get("match")
    if not isinstance(match, dict):
        return {"ok": False, "error": "Provide 'ids' or 'match'"}, 400
    filters = _inventory_filter_kwargs(_parse_inventory_filter(match))
    q, price_mode = filters["q"], filters["price_mode"]
    try:
        count = inv.delete_matching(user_id, **filters)
    except Exception:
        app.logger.exception(
            "event=inventory_delete_failed source=match q=%r price_mode=%r",
            q, price_mode,
        )
        return _internal_error_json("Delete")
    app.logger.info(
        "event=inventory_delete source=match q=%r price_mode=%r deleted=%d",
        q, price_mode, count,
    )
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
    except KeyError as exc:
        flash(f"Import failed: the file has no {exc} column. Upload a Deckbox CSV export.")
        return redirect(url_for("inventory"))
    except (ValueError, UnicodeDecodeError):
        flash("Import failed: the file couldn't be read as a Deckbox CSV export.")
        return redirect(url_for("inventory"))
    except Exception:
        app.logger.exception(
            "event=inventory_import_failed filename=%r replace_mode=%d",
            uploaded.filename, int(replace),
        )
        flash(_internal_error_message("Import"))
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


    running = jobs.find_running()
    if running:
        return jsonify({"ok": True, "job_id": running["id"], "already_running": True})

    job_id = uuid4().hex
    jobs.init(job_id)
    run_id = run_log.record_start(triggered_at, trigger_source, job_id)
    app.logger.info(
        "Daily price update started run_id=%s job_id=%s source=%s",
        run_id, job_id, trigger_source,
    )

    def _progress(progress: int, phase: str, detail: str) -> None:
        jobs.update(job_id, progress=progress, phase=phase, detail=detail)

    def _worker() -> None:
        t0 = monotonic()
        try:
            mapped_count, rows_inserted, uuids_streamed, market_date = (
                pricing.run_daily_price_update(progress_cb=_progress)
            )
            duration_ms = int((monotonic() - t0) * 1000)
            jobs.update(
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
            jobs.update(job_id, state="error", phase="Failed",
                        detail="Daily price update failed.", error=str(exc))
            run_log.record_finish(
                run_id=run_id, status="failed", duration_ms=duration_ms,
                error_message=str(exc),
            )
        # Sealed prices are a separate source with a separate failure
        # domain: an MTGJSON outage must not skip them, nor they the cards.
        try:
            sealed_service.run_daily_update()
        except Exception:
            app.logger.exception("Daily sealed price update failed run_id=%s", run_id)

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
