"""Flask web UI for mtgcompare.

Run: uv run python -m mtgcompare.web
Visit: http://127.0.0.1:5000
"""
import logging.config
import tempfile
from pathlib import Path
from threading import Lock

from flask import Flask, flash, redirect, render_template, request, url_for

from . import inventory as inv
from .shops import SHOP_FLAGS, build_scrapers
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


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    results: list[dict] = []
    error: str | None = None

    if q:
        fx = _get_fx()
        if fx is None:
            error = "Could not fetch FX rate; try again later."
        else:
            errors = []
            for scraper in build_scrapers(fx):
                try:
                    results.extend(scraper.get_prices(q))
                except Exception as exc:
                    errors.append(f"{scraper.__class__.__name__}: {exc}")
            if errors:
                error = "; ".join(errors)
            results.sort(key=lambda result: result["price_jpy"])

    return render_template(
        "index.html",
        q=q,
        results=results,
        fx=_fx,
        error=error,
        shop_flags=SHOP_FLAGS,
        active="search",
    )


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
