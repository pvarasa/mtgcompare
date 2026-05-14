# mtgcompare — Performance improvement backlog

Originally captured 2026-05-04 from a 32-card / 76 s decklist search
that was flapping K8s liveness probes. The list below tracks what's
shipped, what's still open, and why — in priority order for the open
items.

Reconciled with shipped code at **v1.8.0**.

---

## Done

### gunicorn workers + timeout *(v1.5.11)*
`--workers=4 --threads=8 --timeout=120` in the Dockerfile (raised from
the original `--workers=2 --threads=4` in v1.6.7). Up to 32 concurrent
requests per pod; long decklist requests no longer trip gunicorn's
default 30s worker timeout.

### Per-shop wall-clock cap *(v1.6.7)*
`SHOP_QUERY_TIMEOUT_S` (default 30s) bounds the slowest shop's
contribution to any single `(card × shop)` query inside `collect_prices`.
Stragglers keep running in the background so their result lands in the
cache for next time, but the caller is unblocked at the cap. Surfaced
in the UI via the `Partial results: ... timed out` warning banner.

### Basic-land filter *(v1.6.7)*
`/decklist` strips Plains/Island/Swamp/Mountain/Forest/Wastes (and
Snow-Covered variants) before the fan-out. Avoids the multi-page
Scryfall responses for popular lands and the ~600-row noise from JP
shops that would otherwise inflate the per-card scrape time.

### Per-card fan-out cap *(v1.6.7)*
`DECKLIST_FAN_OUT_WORKERS` (default 12, configmap-tuned to 18) caps
the outer fan-out. 30 was tried and OOM'd; 18 is the empirical ceiling
at the current pod memory budget (2 GiB).

### Replace bs4+html.parser with selectolax + bytes *(v1.6.8)*
The single biggest per-fetch memory win. All 7 HTML scrapers now parse
with selectolax (Modest engine, C-backed) directly from `resp.content`,
skipping the `resp.text` Python str copy. Measured on a 3-deck cold
probe:

| | v1.6.7 (bs4) | v1.6.8 |
|---|---|---|
| Kenrith 78-name | 277 s, 6 shops timed out | 103 s, 0 timeouts |
| Edgar 91-name | 328 s, 5 shops timed out | 124 s, 0 timeouts |
| Atraxa 90-name | 320 s, 5 shops timed out | 106 s, 0 timeouts |
| Peak memory (3-deck run) | 1.98 GiB | 1.48 GiB |

This is what the original "#5 lxml + iterparse" entry recommended.
We landed on selectolax instead — same parsing model (C tree, drop
after extraction), better CSS selector ergonomics, no iterparse
hand-rolling per shop.

### Scryfall page-by-page + orjson + smaller session pools *(v1.6.9)*
Scryfall's `_iter_pages` is a generator now; each page is parsed and
dropped. JSON parse via `orjson` from bytes. `HTTPAdapter(pool_maxsize=2)`
on every shop session — was 10, which mattered when many scraper
instances spin up per /decklist. Measured impact on top of v1.6.8 is
within run-to-run variance; the change is mostly code hygiene against
the rare popular-card-with-many-printings case.

---

### SSE-streamed /decklist *(v1.8.0)*
`POST /decklist/stream` returns `text/event-stream` directly — one
HTTP request from form-submit to `done`. Bytes start flowing within a
few hundred ms so Cloudflare never trips its idle timer; 524s on
Edgar-class 90+ name decks are gone. Browser consumes the stream via
`fetch()` + `ReadableStream` (not `EventSource`), which means the
search is served end-to-end by the gunicorn worker that received the
POST — no pod affinity / sticky sessions needed for horizontal
scale-out.

Per-user in-flight cap (`_MAX_IN_FLIGHT_PER_USER=3`, counter +
`Lock`) replaces the previous OOM stampede risk. Counter is
per-process; effective cap with N workers is 3×N. A cluster-wide cap
would need a Postgres advisory lock — defer until concurrent-user
load justifies it.

The previous two-request shape (`POST /decklist/jobs` →
`{job_id} 202`, then `GET /decklist/jobs/<id>/stream`) was removed in
the same change. Its in-process `_search_jobs` dict was the reason
`--workers=1` was pinned in the Dockerfile; that constraint is gone.

---

## Open

### Bump gunicorn workers + replicas *(next scale-out step)*

`--workers=1 --threads=32` is unchanged from the SSE migration but
the shape constraint that forced it is gone. The remaining concern
is memory: one cold-cache 100-card search peaks ~1.5 GiB and the pod
limit is 3 GiB. Bumping to `--workers=2` doubles the effective
per-user in-flight cap (3 → 6) and the worst-case memory ceiling
(1.5 GiB → 3 GiB) without a parallel pod-memory bump. Sequence the
work as: bump pod memory first, then workers, then `replicas: 2+`.

### Bump DECKLIST_FAN_OUT_WORKERS *(brute-force interim)*

Workers 18 → 26–30 would drop the Edgar cold-cache fan-out time
further, but with the SSE migration there's no 524 to chase — the
client sees rows fill in regardless of total wall-clock. Trade-off:
more concurrent shop HTTPS requests means higher rate-limit pushback
(Scryfall starts 429ing above ~20 concurrent in our measurements).
Not currently worth doing.

---

## What NOT to do (decisions worth preserving)

- **Don't add a Redis cache layer.** The Postgres `shop_query_log` /
  `shop_listings` cache is serving sub-millisecond hot reads; Redis
  would add a network hop and ops surface for no real win.
- **Don't pre-warm by crawling all of Hareruya nightly.** See
  `docs/shop_integration_plan.md` for the full discussion — bulk
  indexing is the right move past ~15 shops, premature today.
- **Don't switch the bs4 builder to lxml without going further.** The
  selectolax switch in v1.6.8 already achieved the streaming-parser
  win; rebuilding on bs4+lxml would be regression in both speed and
  memory.
- **Don't async-rewrite to httpx + asyncio.** Threads × Python's GIL
  was the original concern but parsing dropped out of the hot path in
  v1.6.8. The remaining wall-time is shop-side I/O latency, which an
  async rewrite wouldn't change. Effort would be ~2 days and would
  require migrating SQLAlchemy + Flask to async too. Not worth it.

---

## Recommended next move

Bump pod memory and gunicorn workers (the open item above). The SSE
migration removed the shape constraint that pinned `--workers=1`, so
the path is finally open — but raising workers without a parallel
memory bump multiplies the OOM risk on cold 100-card searches.
