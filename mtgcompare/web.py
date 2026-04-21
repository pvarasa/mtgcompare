"""Flask web UI for mtgcompare.

Run: uv run python -m mtgcompare.web
Visit: http://127.0.0.1:5000
"""
import logging.config
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

import requests

from flask import Flask, flash, redirect, render_template, request, url_for

from . import db
from . import inventory as inv
from .shops import SHIPPING_JPY, SHOP_FLAGS, collect_prices, shop_slug
from .utils import get_fx

ROOT_DIR = Path(__file__).resolve().parent.parent
LOGGING_CONF = ROOT_DIR / "logging.conf"

app = Flask(__name__)
# `flash()` needs a session cookie; the value only needs to be stable per process.
app.secret_key = "mtgcompare-local"

_fx: float | None = None
_fx_lock = Lock()


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


@app.route("/decklist", methods=["POST"])
def decklist_search():
    text = request.form.get("decklist", "").strip()
    shipping_overrides_jpy = _parse_shipping_overrides(request.form)
    ship_cfg = _shipping_config(shipping_overrides_jpy)

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
        )

    card_items = _parse_decklist(text)
    if not card_items:
        return _early_return("No cards parsed. Use format: '1 Card Name' or '4x Card Name (SET)'")

    fx = _get_fx()
    if fx is None:
        return _early_return("Could not fetch FX rate; try again later.")

    # Consolidate duplicate names, preserve first-seen casing
    name_qty: dict[str, int] = {}
    name_canonical: dict[str, str] = {}
    for qty, name in card_items:
        key = name.lower()
        name_qty[key] = name_qty.get(key, 0) + qty
        if key not in name_canonical:
            name_canonical[key] = name

    names = list(name_qty.keys())
    prices_by_name: dict[str, list[dict]] = {n: [] for n in names}

    with ThreadPoolExecutor(max_workers=min(len(names), 6)) as executor:
        future_to_name = {
            executor.submit(_fetch_card_prices, name_canonical[n], fx): n
            for n in names
        }
        for future in as_completed(future_to_name):
            n = future_to_name[future]
            try:
                prices_by_name[n] = future.result()
            except Exception as exc:
                app.logger.error("Price fetch failed for %r: %s", name_canonical[n], exc)

    for n in names:
        prices_by_name[n].sort(key=lambda r: r["price_jpy"])

    card_rows = []
    for n in sorted(names, key=lambda x: name_canonical[x].lower()):
        results = prices_by_name[n]
        card_rows.append({
            "name": name_canonical[n],
            "qty": name_qty[n],
            "best": results[0] if results else None,
            "all": results,
        })

    shop_totals: dict[str, dict] = {}
    grand_total_usd = 0.0
    grand_total_jpy = 0.0

    for row in card_rows:
        if row["best"] is None:
            continue
        shop = row["best"]["shop"]
        qty = row["qty"]
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
                "shipping_usd": round(ship_jpy / fx, 2),
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
    shipping_total_usd = round(shipping_total_jpy / fx, 2)
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


def _fetch_market_prices(rows: list[dict], fx: float) -> list[dict]:
    """Augment inventory rows with market_price_usd / market_price_jpy via Scryfall."""
    # Collect unique (name, set) pairs to minimise requests
    seen: dict[tuple, tuple] = {}
    for row in rows:
        key = (row["card_name"].lower(), _normalize_set_code(row["set_code"]))
        if key not in seen:
            seen[key] = (row["card_name"], row["set_code"])

    # name_price_map: keyed by lowercase name only (fallback pass)
    name_price_map: dict[str, dict] = {}
    # set_price_map: keyed by (lowercase name, lowercase set) (precise pass)
    set_price_map: dict[tuple, dict] = {}
    items = list(seen.values())

    def _do_batch(identifiers: list[dict]) -> tuple[list[dict], list[dict]]:
        """POST one batch to Scryfall; return (found_cards, not_found_identifiers)."""
        try:
            resp = requests.post(
                _SCRYFALL_COLLECTION,
                json={"identifiers": identifiers},
                headers=_SCRYFALL_HEADERS,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", []), data.get("not_found", [])
        except Exception as exc:
            app.logger.error("Scryfall collection batch failed: %s", exc)
            return [], []

    # Pass 1: look up by name + set
    not_found_names: list[str] = []
    for i in range(0, len(items), 75):
        batch = items[i:i + 75]
        identifiers = [{"name": n, "set": _normalize_set_code(s)} for n, s in batch]
        found, not_found = _do_batch(identifiers)
        for card in found:
            key = ((card.get("name") or "").lower(), (card.get("set") or "").lower())
            p = card.get("prices") or {}
            prices = {"usd": _safe_float(p.get("usd")), "usd_foil": _safe_float(p.get("usd_foil"))}
            set_price_map[key] = prices
        for nf in not_found:
            name = (nf.get("name") or "").strip()
            if name:
                not_found_names.append(name)
        if i + 75 < len(items):
            time.sleep(0.1)

    # Pass 2: retry not-found by name only (handles set code mismatches)
    unique_fallback = list({n.lower(): n for n in not_found_names}.values())
    for i in range(0, len(unique_fallback), 75):
        batch = unique_fallback[i:i + 75]
        identifiers = [{"name": n} for n in batch]
        found, _ = _do_batch(identifiers)
        for card in found:
            name_key = (card.get("name") or "").lower()
            p = card.get("prices") or {}
            prices = {"usd": _safe_float(p.get("usd")), "usd_foil": _safe_float(p.get("usd_foil"))}
            name_price_map[name_key] = prices
        if i + 75 < len(unique_fallback):
            time.sleep(0.1)

    result = []
    for row in rows:
        name_lower = row["card_name"].lower()
        set_lower = _normalize_set_code(row["set_code"])
        is_foil = _is_foil(row.get("printing"))
        p = set_price_map.get((name_lower, set_lower)) or name_price_map.get(name_lower) or {}
        price_usd = p.get("usd_foil") if is_foil else p.get("usd")
        result.append({
            **row,
            "market_price_usd": price_usd,
            "market_price_jpy": round(price_usd * fx) if price_usd is not None else None,
        })
    return result


@app.route("/market")
def market():
    inv.init_schema()
    inventory_rows = inv.list_all()

    # Load cached prices — no live fetch on GET.
    with db.get_conn() as conn:
        cache_rows = conn.execute(
            "SELECT card_name, set_code, is_foil, price_usd, fetched_at FROM market_prices"
        ).fetchall()

    price_cache: dict[tuple, float | None] = {}
    last_fetched_at: str | None = None
    for cr in cache_rows:
        key = (cr["card_name"].lower(), cr["set_code"].lower(), cr["is_foil"])
        price_cache[key] = cr["price_usd"]
        if last_fetched_at is None or cr["fetched_at"] > last_fetched_at:
            last_fetched_at = cr["fetched_at"]

    has_cache      = bool(price_cache)
    last_refreshed = _format_ago(last_fetched_at)

    if not inventory_rows:
        return render_template(
            "market.html", rows=[], summary=None, fx=None, error=None,
            has_cache=has_cache, last_refreshed=last_refreshed, active="market",
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
            has_cache=False, last_refreshed=None, active="market",
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
        active="market",
    )


@app.route("/market/refresh", methods=["POST"])
def market_refresh():
    inv.init_schema()
    inventory_rows = inv.list_all()

    if not inventory_rows:
        flash("No inventory to price.")
        return redirect(url_for("market"))

    fx = _get_fx()
    if fx is None:
        flash("Could not fetch FX rate; try again later.")
        return redirect(url_for("market"))

    priced = _fetch_market_prices(inventory_rows, fx)

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    seen: dict[tuple, float | None] = {}
    for row in priced:
        is_foil = int(_is_foil(row.get("printing")))
        key = (row["card_name"], _normalize_set_code(row["set_code"], upper=True), is_foil)
        if key not in seen:
            seen[key] = row.get("market_price_usd")

    with db.get_conn() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO market_prices
               (card_name, set_code, is_foil, price_usd, fetched_at)
               VALUES (?, ?, ?, ?, ?)""",
            [(name, set_code, is_foil, price, fetched_at)
             for (name, set_code, is_foil), price in seen.items()],
        )

    return redirect(url_for("market"))


@app.route("/inventory")
def inventory():
    inv.init_schema()
    return render_template(
        "inventory.html",
        rows=inv.list_all(),
        stats=inv.stats(),
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
        inv.add_one(record)
    except Exception as exc:
        app.logger.exception("single-card add failed")
        flash(f"Add failed: {exc}")
        return redirect(url_for("inventory"))

    flash(f"Added {record['quantity']}x {record['card_name']} [{record['set_code']}].")
    return redirect(url_for("inventory"))


@app.route("/inventory/add-bulk", methods=["POST"])
def inventory_add_bulk():
    payload = request.get_json(silent=True) or {}
    records = payload.get("records") or []
    if not records:
        return {"ok": False, "error": "No records"}, 400
    try:
        count = inv.add_many(records)
    except Exception as exc:
        app.logger.exception("bulk add failed")
        return {"ok": False, "error": str(exc)}, 500
    flash(f"Added {count} card(s) from decklist.")
    return {"ok": True, "count": count}


@app.route("/inventory/import", methods=["POST"])
def inventory_import():
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
        count = inv.import_csv(tmp_path, replace=replace)
    except Exception as exc:
        app.logger.exception("inventory import failed")
        flash(f"Import failed: {exc}")
        return redirect(url_for("inventory"))
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    verb = "Replaced inventory with" if replace else "Appended"
    flash(f"{verb} {count} rows from {uploaded.filename}.")
    return redirect(url_for("inventory"))


def main() -> None:
    logging.config.fileConfig(LOGGING_CONF)
    # use_reloader=False so start/stop scripts have a single PID to manage;
    # debug features (interactive tracebacks) are still active.
    app.run(debug=True, use_reloader=False)


if __name__ == "__main__":
    main()
