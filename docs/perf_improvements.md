# mtgcompare — Performance improvement backlog

Originally captured 2026-05-04 from a 32-card / 76 s decklist search
that was flapping K8s liveness probes. The list below tracks what's
shipped, what's still open, and why — in priority order for the open
items.

Reconciled with shipped code at **v1.8.3**.

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

### Workers 2× + pod-memory bump *(v1.8.3)*
`--workers=1 --threads=32` → `--workers=2 --threads=16`, pod memory
limit 3 GiB → 4 GiB. Same total in-flight cap (32) but split across
two Python processes, doubling effective CPU throughput by working
around the GIL. Measured on the realistic-mix saturation curve at
the 200-VU phase: per-pod throughput **91 → 137 r/s** (+50 %), p95
**2.5 s → 1.5 s**, p99 **7.2 s → 2.1 s**. Knee moved out from ~100 VUs
to past 200 VUs.

Memory limit had to move because the worst-case-2-concurrent-cold-
decklist peak goes from ~1.5 GiB (one worker) to ~3 GiB (two
workers); 4 GiB gives a comfortable margin on the 16 GiB node
alongside Postgres + cloudflared + monitoring.

The detour worth recording: bumping the SQLAlchemy connection pool
first (5+10 → 20+20) made things *worse* — the GIL was already
saturated at 100 VUs, so more in-flight queries just gave the worker
more contention to schedule. The pool change is reverted; the env
knobs (`DB_POOL_SIZE`, `DB_POOL_OVERFLOW`) remain in case future
worker bumps need pool growth too.

---

## Open

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

Run the saturation curve (`server_admin/loadtest/saturate_realistic.js`)
once more under workers=2 with the realistic mix to confirm the knee
sits past 200 VUs in steady state, then decide on `replicas: 3` if
prod's actual concurrent-user count justifies it. Today the
2-replica × 2-worker stack should clear ~270 r/s aggregate before
the knee — well past plausible load.
