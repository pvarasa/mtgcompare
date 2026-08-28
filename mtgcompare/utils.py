"""Shared helpers that sit at the bottom of the import graph.

The scrapers, the web layer and the CLI all pull ``get_fx`` from here, so
whatever this module imports is paid by every entry point at boot. Keep it
light — this used to import yfinance, which cost ~4.5 s of import time and
~164 MB of wheels (pandas, numpy, curl_cffi, lxml, protobuf, …) to fetch
one number.
"""
import orjson
import requests

# Frankfurter serves the ECB reference rates, keyless. `api.frankfurter.app`
# 301-redirects here, so the .dev host is pinned directly. open.er-api.com is
# a second, independently-operated keyless provider behind it — one free
# endpoint is a single point of failure for a rate every JPY price depends on.
_FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest"
_ER_API_URL = "https://open.er-api.com/v6/latest/USD"

# The web layer caps the whole lookup at 10 s (``web._get_fx``), but that cap
# is on a worker thread it abandons rather than joins. A real socket timeout
# is what actually ends the request, so keep this comfortably under that cap.
_TIMEOUT_S = 5.0


class FxError(Exception):
    """No FX source could supply a usable rate."""


def _from_frankfurter(code: str) -> float:
    resp = requests.get(_FRANKFURTER_URL, params={"base": "USD", "symbols": code}, timeout=_TIMEOUT_S)
    resp.raise_for_status()
    return float(orjson.loads(resp.content)["rates"][code])


def _from_er_api(code: str) -> float:
    resp = requests.get(_ER_API_URL, timeout=_TIMEOUT_S)
    resp.raise_for_status()
    return float(orjson.loads(resp.content)["rates"][code])


def get_fx(ccy: str) -> float:
    """Return units of ``ccy`` per 1 USD — ``get_fx("jpy")`` is ~159.

    Raises ``FxError`` when every source fails rather than returning None:
    the web layer wraps this in its own timeout and 60 s failure backoff
    (``web._get_fx``) and needs the exception to know the fetch failed.

    This is a *daily reference rate*, not a live quote. The ECB fixing is
    published around 16:00 CET on TARGET business days, so a weekend or a
    holiday returns the last business day's value — the same practical
    staleness the previous Yahoo ``previousClose`` source had, and well
    inside tolerance for comparing card prices.
    """
    code = ccy.upper()
    failures = []
    for source in (_from_frankfurter, _from_er_api):
        try:
            return source(code)
        except Exception as exc:  # noqa: BLE001 — any failure means try the next source
            failures.append(f"{source.__name__}: {type(exc).__name__}: {exc}")
    raise FxError(f"no FX source returned a {code} rate — {'; '.join(failures)}")
