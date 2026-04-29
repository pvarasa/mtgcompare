"""Flask web UI for mtgcompare.

Run: uv run python -m mtgcompare.web
Visit: http://127.0.0.1:5000
"""
import json
import logging.config
import lzma
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, Thread
from uuid import uuid4

import duckdb
import requests
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
from sqlalchemy import text

from . import db, history_import
from . import inventory as inv
from .shops import SHIPPING_JPY, SHOP_FLAGS, collect_prices, shop_slug
from .utils import get_fx

ROOT_DIR = Path(__file__).resolve().parent.parent
LOGGING_CONF = ROOT_DIR / "logging.conf"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "mtgcompare-local-dev")

_USER_ID_HEADER = os.environ.get("USER_ID_HEADER", "X-User-ID")
_CRON_SECRET = os.environ.get("CRON_SECRET", "")


def _get_user_id() -> str:
    """Return the current user identity.

    In local SQLite mode always returns 'local' (no auth required).
    In PostgreSQL mode reads the plain header set by the auth proxy.
    """
    if not db.IS_POSTGRES:
        return "local"
    return request.headers.get(_USER_ID_HEADER, "anonymous").strip() or "anonymous"


@app.context_processor
def _inject_current_user():
    return {"current_user": _get_user_id()}

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

    results: list[dict] = []
    error: str | None = None

    if q:
        fx = _get_fx()
        if fx is None:
            error = "Could not fetch FX rate; try again later."
        else:
            results = collect_prices(q, fx, logger=app.logger)
            if include_shipping:
                for r in results:
                    r["ship_jpy"] = shipping_overrides_jpy.get(r["shop"], 0)
                    r["price_jpy_with_shipping"] = r["price_jpy"] + r["ship_jpy"]
                results.sort(key=lambda r: r["price_jpy_with_shipping"])
            else:
                results.sort(key=lambda r: r["price_jpy"])

    return render_template(
        "index.html",
        q=q,
        results=results,
        fx=_fx,
        error=error,
        shop_flags=SHOP_FLAGS,
        shipping_config=ship_cfg,
        include_shipping=include_shipping,
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


def _fetch_card_prices(card_name: str, fx: float) -> list[dict]:
    return collect_prices(card_name, fx, logger=app.logger)


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


@app.route("/decklist", methods=["POST"])
def decklist_search():
    text = request.form.get("decklist", "").strip()
    shipping_overrides_jpy = _parse_shipping_overrides(request.form)
    ship_cfg = _shipping_config(shipping_overrides_jpy)
    use_inventory = request.form.get("use_inventory") == "1"

    def _early_return(error_msg: str, fx_val=None):
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
            use_inventory=use_inventory,
        )

    card_items = _parse_decklist(text)
    if not card_items:
        return _early_return("No cards parsed. Use format: '1 Card Name' or '4x Card Name (SET)'")

    # Consolidate duplicate names, preserve first-seen casing
    name_qty: dict[str, int] = {}
    name_canonical: dict[str, str] = {}
    for qty, name in card_items:
        key = name.lower()
        name_qty[key] = name_qty.get(key, 0) + qty
        if key not in name_canonical:
            name_canonical[key] = name

    # Build inventory map and compute how many we still need to buy
    inv_map: dict[str, int] = {}
    if use_inventory:
        inv.init_schema()
        user_id = _get_user_id()
        for row in inv.list_all(user_id):
            k = row["card_name"].lower()
            inv_map[k] = inv_map.get(k, 0) + row["quantity"]

    name_inv_qty, name_needed = _deduct_inventory(name_qty, inv_map)

    names_to_search = [n for n in name_qty if name_needed[n] > 0]
    prices_by_name: dict[str, list[dict]] = {n: [] for n in name_qty}

    fx = _get_fx()
    if fx is None and names_to_search:
        return _early_return("Could not fetch FX rate; try again later.")

    if names_to_search and fx is not None:
        with ThreadPoolExecutor(max_workers=min(len(names_to_search), 6)) as executor:
            future_to_name = {
                executor.submit(_fetch_card_prices, name_canonical[n], fx): n
                for n in names_to_search
            }
            for future in as_completed(future_to_name):
                n = future_to_name[future]
                try:
                    prices_by_name[n] = future.result()
                except Exception as exc:
                    app.logger.error("Price fetch failed for %r: %s", name_canonical[n], exc)

    for n in names_to_search:
        prices_by_name[n].sort(key=lambda r: r["price_jpy"])

    card_rows = []
    for n in sorted(name_qty, key=lambda x: name_canonical[x].lower()):
        results = prices_by_name[n]
        qty_needed = name_needed[n]
        card_rows.append({
            "name": name_canonical[n],
            "qty": name_qty[n],
            "qty_inventory": name_inv_qty[n],
            "qty_needed": qty_needed,
            "best": results[0] if (results and qty_needed > 0) else None,
            "all": results,
        })

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
    grand_total_usd_with_shipping = round(grand_total_usd + shipping_total_usd, 2)
    grand_total_jpy_with_shipping = round(grand_total_jpy + shipping_total_jpy, 0)

    return render_template(
        "decklist.html",
        decklist=text,
        card_rows=card_rows,
        shop_list=shop_list,
        grand_total_usd=grand_total_usd,
        grand_total_jpy=grand_total_jpy,
        grand_total_usd_with_shipping=grand_total_usd_with_shipping,
        grand_total_jpy_with_shipping=grand_total_jpy_with_shipping,
        shipping_total_jpy=shipping_total_jpy,
        fx=fx,
        shop_flags=SHOP_FLAGS,
        shipping_config=ship_cfg,
        active="search",
        error=None,
        use_inventory=use_inventory,
    )


def _format_ago(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        dt  = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        sec = int((datetime.now(timezone.utc) - dt).total_seconds())
        if sec <    60: return "just now"
        if sec <  3600: return f"{sec // 60} min ago"
        if sec < 86400: return f"{sec // 3600} hr ago"
        return f"{sec // 86400} days ago"
    except Exception:
        return iso


_SCRYFALL_COLLECTION = "https://api.scryfall.com/cards/collection"
_SCRYFALL_HEADERS = {"User-Agent": "mtgcompare/0.1", "Accept": "application/json"}


def _safe_float(val) -> float | None:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


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
    anchor = now or datetime.now(timezone.utc)
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
        cache_dir = Path(os.environ.get("MTGJSON_CACHE_DIR", "/tmp/mtgjson"))
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
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "error": None,
        }


def _set_download_job(job_id: str, **updates) -> None:
    with _download_jobs_lock:
        job = _download_jobs.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


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
            uuid_to_db_key[(uuid, finish)] = db_key

    if not uuid_to_db_key:
        return

    if db.IS_POSTGRES:
        uuid_list = list({u for (u, _) in uuid_to_db_key})
        params = {f"u{i}": u for i, u in enumerate(uuid_list)}
        placeholders = ", ".join(f":u{i}" for i in range(len(uuid_list)))
        with db.get_conn() as conn:
            rows = conn.execute(
                text(f"""
                    SELECT DISTINCT ON (uuid, finish) uuid, finish, price_usd
                    FROM price_rows
                    WHERE uuid IN ({placeholders})
                    ORDER BY uuid, finish, market_date DESC
                """),
                params,
            ).fetchall()
        latest: dict[tuple[str, str], float | None] = {
            (r[0], r[1]): float(r[2]) if r[2] is not None else None
            for r in rows
        }
    else:
        if not duckdb_path or not duckdb_path.exists():
            return
        uuid_str = ", ".join(f"'{u}'" for u, _ in uuid_to_db_key)
        with _history_duckdb_lock:
            conn_duck = duckdb.connect(str(duckdb_path), read_only=True)
            try:
                rows = conn_duck.execute(f"""
                    SELECT uuid, finish, price_usd
                    FROM price_rows
                    WHERE uuid IN ({uuid_str})
                    QUALIFY ROW_NUMBER() OVER (PARTITION BY uuid, finish ORDER BY market_date DESC) = 1
                """).fetchall()
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


def _import_mtgjson_history(rows: list[dict], *, progress_cb=None) -> tuple[int, int]:
    def _progress(progress: int, phase: str, detail: str) -> None:
        if progress_cb:
            progress_cb(progress, phase, detail)

    inventory_rows = [dict(row) for row in rows]
    downloaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _row_key(row: dict) -> tuple[str, str, str, int]:
        return (
            row["card_name"].lower(),
            _normalize_set_code(row["set_code"], upper=True),
            (row.get("card_number") or "").strip(),
            int(_is_foil(row.get("printing"))),
        )

    # Load existing mappings so we can skip sets that are already fully resolved.
    with db.get_conn() as conn:
        existing_rows = conn.execute(
            text("SELECT card_name, set_code, card_number, is_foil, uuid FROM mtgjson_card_map")
        ).mappings().all()
    existing_uuid: dict[tuple[str, str, str, int], str] = {
        (r["card_name"].lower(), r["set_code"], r["card_number"], r["is_foil"]): r["uuid"]
        for r in existing_rows
    }

    # Only load XZ files for sets that have at least one unmapped inventory row.
    sets_needing_load: set[str] = {
        _normalize_set_code(row["set_code"], upper=True)
        for row in inventory_rows
        if row.get("set_code") and _row_key(row) not in existing_uuid
    }

    candidates_by_set: dict[str, dict[tuple[str, str, str], dict[str, str]]] = {}
    sets_to_load = sorted(sets_needing_load)
    if sets_to_load:
        total_to_load = len(sets_to_load)
        for index, set_code in enumerate(sets_to_load, start=1):
            _progress(
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
        _progress(25, "Set data", "All sets already mapped — skipping set file load.")

    _progress(28, "Mapping inventory", "Resolving MTGJSON card UUIDs for inventory lots...")
    card_maps: list[tuple[str, str, str, int, str, str]] = []
    for row in inventory_rows:
        key = _row_key(row)
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

    history_duckdb_path = _mtgjson_history_duckdb_path()
    history_row_count = 0

    if db.IS_POSTGRES:
        has_history = _has_price_history()
        if not has_history:
            history_path = _mtgjson_history_path()
            _progress(32, "Downloading history", "Downloading MTGJSON AllPrices history...")
            try:
                _download_file(f"{_MTGJSON_BASE_URL}/AllPrices.json.xz", history_path)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    raise RuntimeError(
                        "MTGJSON price files are temporarily unavailable. Please try again later."
                    ) from exc
                raise
            history_row_count = history_import.rebuild_history_pg(
                history_path, db.engine, progress_cb=_progress,
            )
            history_path.unlink(missing_ok=True)
        else:
            _progress(40, "History ready", "Using existing PostgreSQL price history.")
    elif not history_duckdb_path.exists():
        history_path = _mtgjson_history_path()
        _progress(32, "Downloading history", "Downloading MTGJSON AllPrices history...")
        try:
            _download_file(f"{_MTGJSON_BASE_URL}/AllPrices.json.xz", history_path)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                raise RuntimeError(
                    "MTGJSON price files are temporarily unavailable. Please try again later."
                ) from exc
            raise
        with _history_duckdb_lock:
            history_row_count = history_import.rebuild_history_db(
                history_path, history_duckdb_path, progress_cb=_progress,
            )
        history_path.unlink(missing_ok=True)
    else:
        _progress(40, "History ready", "Using existing local price history database.")

    _progress(96, "Saving mappings", "Updating local card-to-MTGJSON mappings...")
    with db.get_conn() as conn:
        if sets_needing_load:
            params = {f"s{i}": s for i, s in enumerate(sets_needing_load)}
            placeholders = ", ".join(f":s{i}" for i in range(len(sets_needing_load)))
            conn.execute(
                text(f"DELETE FROM mtgjson_card_map WHERE set_code IN ({placeholders})"),
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

    if not history_row_count:
        with db.get_conn() as conn:
            row_count = _read_meta(conn, "mtgjson_history_db_row_count")
            history_row_count = int(row_count) if row_count else 0

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
        cache_rows = conn.execute(
            text("SELECT card_name, set_code, is_foil, price_usd, fetched_at FROM market_prices")
        ).mappings().all()
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
            app.logger.exception("MTGJSON history download failed")
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
        end_day=datetime.now(timezone.utc).date(),
    ) if history else []
    if period != "all" and dense_points:
        cutoff = _history_cutoff(period)
        assert cutoff is not None
        dense_points = [
            point for point in dense_points
            if datetime.fromisoformat(point["market_date"]).replace(tzinfo=timezone.utc) >= cutoff
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
        app.logger.exception("single-card add failed")
        flash(f"Add failed: {exc}")
        return redirect(url_for("inventory"))

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
        app.logger.exception("bulk add failed")
        return {"ok": False, "error": str(exc)}, 500
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
        app.logger.exception("inventory import failed")
        flash(f"Import failed: {exc}")
        return redirect(url_for("inventory"))
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    verb = "Replaced inventory with" if replace else "Appended"
    flash(f"{verb} {count} rows from {uploaded.filename}.")
    return redirect(url_for("inventory"))


def _run_daily_price_update(progress_cb=None) -> tuple[int, int]:
    """Download today's prices for all cards and update UUID mappings for inventory.

    Used by the cron endpoint. Returns (mapped_count, row_count).
    """
    def _progress(progress: int, phase: str, detail: str) -> None:
        if progress_cb:
            progress_cb(progress, phase, detail)

    inventory_rows = inv.list_all_global()
    cache_dir = _mtgjson_cache_dir()

    if db.IS_POSTGRES:
        today_xz = cache_dir / "AllPricesToday.json.xz"
        _progress(10, "Downloading today's prices", "Downloading AllPricesToday.json.xz...")
        try:
            _download_file(f"{_MTGJSON_BASE_URL}/AllPricesToday.json.xz", today_xz)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                raise RuntimeError("MTGJSON AllPricesToday not available yet.") from exc
            raise
        row_count = history_import.merge_today_prices_pg(
            today_xz, db.engine, progress_cb=_progress,
        )
        today_xz.unlink(missing_ok=True)
    else:
        today_xz = cache_dir / "AllPricesToday.json.xz"
        _progress(10, "Downloading today's prices", "Downloading AllPricesToday.json.xz...")
        try:
            _download_file(f"{_MTGJSON_BASE_URL}/AllPricesToday.json.xz", today_xz)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                raise RuntimeError("MTGJSON AllPricesToday not available yet.") from exc
            raise
        duckdb_path = _mtgjson_history_duckdb_path()
        with _history_duckdb_lock:
            row_count = history_import.merge_today_prices(
                today_xz, duckdb_path, progress_cb=_progress,
            )
        today_xz.unlink(missing_ok=True)

    mapped_count, _ = _import_mtgjson_history(inventory_rows, progress_cb=_progress)
    return mapped_count, row_count


@app.route("/internal/cron/update-prices", methods=["POST"])
def cron_update_prices():
    """Protected endpoint for the daily K8s CronJob.

    Requires Authorization: Bearer <CRON_SECRET> header.
    """
    if _CRON_SECRET:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {_CRON_SECRET}":
            return jsonify({"ok": False, "error": "Unauthorized"}), 401

    inv.init_schema()

    with _download_jobs_lock:
        running = next((j for j in _download_jobs.values() if j["state"] == "running"), None)
    if running:
        return jsonify({"ok": True, "job_id": running["id"], "already_running": True})

    job_id = uuid4().hex
    _init_download_job(job_id)

    def _progress(progress: int, phase: str, detail: str) -> None:
        _set_download_job(job_id, progress=progress, phase=phase, detail=detail)

    def _worker() -> None:
        try:
            mapped_count, row_count = _run_daily_price_update(progress_cb=_progress)
            _set_download_job(
                job_id, state="done", progress=100, phase="Done",
                detail=f"Updated {row_count:,} price points for {mapped_count} lot(s).",
            )
        except Exception as exc:
            app.logger.exception("Daily price update failed")
            _set_download_job(job_id, state="error", phase="Failed",
                              detail="Daily price update failed.", error=str(exc))

    Thread(target=_worker, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id, "already_running": False})


def main() -> None:
    logging.config.fileConfig(LOGGING_CONF)
    host = os.environ.get("FLASK_RUN_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_RUN_PORT", "5000"))
    # use_reloader=False so start/stop scripts have a single PID to manage;
    # debug features (interactive tracebacks) are still active.
    app.run(host=host, port=port, debug=True, use_reloader=False)


if __name__ == "__main__":
    main()
