"""Lazy/on-demand caching wrapper for shop scrapers.

Wraps any ``MtgScrapper`` so that repeated searches for the same card within
``ttl`` are served from the local DB (``shop_listings`` + ``shop_query_log``)
instead of re-hitting the shop. Designed so that a future nightly bulk crawler
can write into the same ``shop_listings`` table — the read path here doesn't
care whether rows came from a user-triggered scrape or a background job.

Negative results are cached too: if the shop returns zero rows, that fact is
stored in ``shop_query_log`` and respected on subsequent lookups, so cards a
shop genuinely doesn't carry don't keep triggering scrapes.

Concurrent searches for the same ``(shop, card_name)`` coalesce into a single
HTTP fetch via an in-process singleflight, so traffic spikes don't fan out.
"""
import logging
import os
import threading
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text

from . import db
from .scrapper import MtgScrapper
from .scrappers._base import RateLimitedError, ScraperFetchError


def _ttl_from_env() -> timedelta:
    """Read MTGCOMPARE_CACHE_TTL_HOURS at import time; default 24h."""
    raw = os.environ.get("MTGCOMPARE_CACHE_TTL_HOURS")
    if raw:
        try:
            return timedelta(hours=float(raw))
        except ValueError:
            pass
    return timedelta(hours=24)


DEFAULT_TTL = _ttl_from_env()


# The Flask search endpoint reaches the cache before any inventory operation
# does, and the inventory module is what historically called init_schema().
# Guard the first cache access so the new tables exist on a fresh standalone
# DB; subsequent calls are a single bool check.
_schema_ready = False
_schema_lock = threading.Lock()


def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        db.init_schema()
        _schema_ready = True


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize(card_name: str) -> str:
    return " ".join(card_name.strip().lower().split())


def _coerce_aware(value) -> Optional[datetime]:
    """SQLite returns naive datetimes (UTC by convention); Postgres returns aware."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def read_log(conn, shop: str, card_name: str) -> Optional[dict]:
    row = conn.execute(
        text(
            "SELECT queried_at, result_count, status"
            " FROM shop_query_log"
            " WHERE shop = :shop AND card_name = :card"
        ),
        {"shop": shop, "card": card_name},
    ).fetchone()
    if row is None:
        return None
    return {
        "queried_at": _coerce_aware(row[0]),
        "result_count": int(row[1]),
        "status": row[2],
    }


def upsert_log(
    conn,
    shop: str,
    card_name: str,
    result_count: int,
    status: str = "ok",
    now: Optional[datetime] = None,
) -> None:
    db.upsert(
        conn,
        "shop_query_log",
        ["shop", "card_name"],
        [{
            "shop": shop,
            "card_name": card_name,
            "queried_at": now or _now(),
            "result_count": result_count,
            "status": status,
        }],
    )


def read_listings(conn, shop: str, card_name: str) -> list[dict]:
    """Return cached rows in the same shape a fresh scraper would return.

    The schema carries language/finish columns for forward-compatibility, but
    they're intentionally not surfaced here so that ``CachedScrapper`` is a
    transparent wrapper — callers can't tell whether a result came from the
    cache or a fresh fetch.
    """
    rows = conn.execute(
        text(
            "SELECT card_display, set_code, condition,"
            "       price_jpy, price_usd, stock, url"
            " FROM shop_listings"
            " WHERE shop = :shop AND card_name = :card"
        ),
        {"shop": shop, "card": card_name},
    ).fetchall()
    return [{
        "shop": shop,
        "card": r[0],
        "set": r[1],
        "condition": r[2],
        "price_jpy": float(r[3]),
        "price_usd": float(r[4]) if r[4] is not None else None,
        "stock": int(r[5]) if r[5] is not None else None,
        "link": r[6],
    } for r in rows]


def replace_listings(
    conn,
    shop: str,
    card_name: str,
    records: list[dict],
    source: str = "search",
    now: Optional[datetime] = None,
) -> None:
    """Atomically replace every cached listing for (shop, card_name).

    Old rows that aren't in ``records`` disappear — that's how stock that's
    been sold out / delisted gets evicted from the cache.
    """
    timestamp = now or _now()
    conn.execute(
        text("DELETE FROM shop_listings WHERE shop = :shop AND card_name = :card"),
        {"shop": shop, "card": card_name},
    )
    if not records:
        return
    rows = [{
        "shop": shop,
        "card_name": card_name,
        "card_display": r.get("card") or r.get("card_display") or "",
        "set_code": (r.get("set") or "").upper(),
        "language": r.get("language") or "EN",
        "finish": r.get("finish") or "normal",
        "condition": r.get("condition") or "NM",
        "price_jpy": r["price_jpy"],
        "price_usd": r.get("price_usd"),
        "stock": r.get("stock"),
        "url": r.get("link"),
        "last_checked": timestamp,
        "source": source,
    } for r in records]
    cols = list(rows[0].keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    conn.execute(
        text(f"INSERT INTO shop_listings ({', '.join(cols)}) VALUES ({placeholders})"),
        rows,
    )


class _Singleflight:
    """Coalesces concurrent calls with the same key into a single execution.

    Followers block on the leader's Future and receive the same result (or
    exception) without re-running the work.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._inflight: dict[str, Future] = {}

    def do(self, key: str, fn):
        with self._lock:
            existing = self._inflight.get(key)
            if existing is not None:
                fut = existing
                is_leader = False
            else:
                fut = Future()
                self._inflight[key] = fut
                is_leader = True

        if not is_leader:
            return fut.result()

        try:
            result = fn()
        except BaseException as exc:
            fut.set_exception(exc)
            with self._lock:
                self._inflight.pop(key, None)
            raise
        else:
            fut.set_result(result)
            with self._lock:
                self._inflight.pop(key, None)
            return result


class CachedScrapper(MtgScrapper):
    """DB-backed cache around any MtgScrapper.

    The wrapped scrapper is unaware of the cache; tests that exercise parsers
    directly continue to work without DB setup.
    """

    def __init__(
        self,
        scrapper: MtgScrapper,
        shop_name: str,
        ttl: timedelta = DEFAULT_TTL,
    ):
        super().__init__()
        self.scrapper = scrapper
        self.shop_name = shop_name
        self.ttl = ttl
        self._sf = _Singleflight()
        self.logger = logging.getLogger(f"cache.{shop_name}")

    def get_prices(self, card_name: str) -> list[dict]:
        norm = _normalize(card_name)
        return self._sf.do(f"{self.shop_name}::{norm}",
                           lambda: self._fetch_or_cache(card_name, norm))

    def _fetch_or_cache(self, card_name: str, norm: str) -> list[dict]:
        _ensure_schema()
        with db.get_conn() as conn:
            log = read_log(conn, self.shop_name, norm)
            if log and log["status"] == "ok" and self._is_fresh(log["queried_at"]):
                self.logger.debug(
                    "cache hit %s %r (%d rows, %s old)",
                    self.shop_name, card_name, log["result_count"],
                    _now() - log["queried_at"],
                )
                return read_listings(conn, self.shop_name, norm)

        # Miss — go to the network. Transport failures (ScraperFetchError)
        # propagate without writing to cache so the next request retries
        # rather than serving a 24h-stale "0 results, ok" entry. Other
        # exceptions (e.g. parser bugs) are also not cached.
        # logfmt-style key=value pairs so dashboards can extract `event` and
        # `shop` as labels without having to parse the prose. The card name
        # is included to help track which queries trip per-shop limits.
        try:
            records = self.scrapper.get_prices(card_name)
        except RateLimitedError as exc:
            self.logger.warning(
                "event=rate_limited shop=%s card=%r detail=%s",
                self.shop_name, card_name, exc,
            )
            raise
        except ScraperFetchError as exc:
            self.logger.warning(
                "event=fetch_failed shop=%s card=%r detail=%s",
                self.shop_name, card_name, exc,
            )
            raise

        with db.get_conn() as conn:
            replace_listings(conn, self.shop_name, norm, records)
            upsert_log(conn, self.shop_name, norm, len(records), status="ok")
        self.logger.debug(
            "cache miss %s %r → %d rows", self.shop_name, card_name, len(records),
        )
        return records

    def _is_fresh(self, queried_at: Optional[datetime]) -> bool:
        if queried_at is None:
            return False
        return (_now() - queried_at) < self.ttl
