# mtgcompare — Performance improvement backlog

Originally captured 2026-05-04 from a 32-card / 76 s decklist search
that was flapping K8s liveness probes. The list below tracks what's
shipped, what's still open, and why — in priority order for the open
items.

Reconciled with shipped code at **v1.6.9**.

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

## Open

### Stream results to the browser (SSE) *(highest remaining UX win)*

**Problem.** Cold-cache 100-card commander searches still take
~100–125 s and one of the three test decks (Edgar Markov, 91 names)
slips past Cloudflare's ~100 s edge timeout, returning a 524 even
though the backend eventually completes successfully. The wall-clock
floor is bounded by `ceil(distinct_names / fan_out_workers) ×
per_shop_timeout` and can't realistically drop further without
either a much bigger pod or accepting upstream rate-limit pushback.

**Idea.** Bytes-flowing connections don't 524. Replace the synchronous
`render_template("decklist.html", ...)` with an SSE-driven flow:

- `POST /decklist` creates an in-process `Job`, returns `{job_id}` + 202.
- `GET /decklist/{job_id}/stream` opens a `text/event-stream` connection
  that emits typed events as the search runs:
  - `started` → skeleton (total cards, basics_skipped, names_to_search)
  - `row` → one per card as `_fetch_decklist_prices` yields
  - `totals` → running shop/grand totals (debounced, e.g. every 1 s)
  - `shop_timeout` → live warning-banner updates
  - `done` → final aggregated state
  - `error` → fatal failure
- Browser uses `EventSource` to append rows and update totals live.

**Why the structural pieces are mostly in place.** `_fetch_decklist_prices`
already uses `as_completed`; converting it to a generator that yields
`(name, prices)` per completion is small. Job state can be an in-process
dict while `replicas: 1` — Redis is a scale-out concern, not a today
concern. Auth on the SSE endpoint piggybacks on the existing WorkOS
session check.

**Effort.** ~1 focused day:
- ~30 min: `_fetch_decklist_prices` → generator
- ~30 min: `Job` dataclass + `_jobs: dict` + cleanup TTL
- ~1 h: SSE endpoint (auth + cleanup)
- ~1 h: tests
- ~2 h: split `decklist.html` into form + skeleton + JS handler
- buffer: live running-totals math on the client

**Also fixes the concurrent-users OOM angle.** The SSE plan creates a
natural place to plug a process-level semaphore that queues
concurrent `/decklist` requests instead of letting them stampede into
the 2 GiB memory ceiling.

### Process-level concurrency cap *(small standalone change)*

If SSE is too big to land soon, a 20-line `threading.Semaphore` (or
`pg_try_advisory_lock` for cross-worker coordination) around the
fan-out gives concurrent users a clean "you're queued" path instead of
the current OOM-prone stampede. See the per-fetch memory analysis
session of 2026-05-12 for the full breakdown.

### Bump pod memory + DECKLIST_FAN_OUT_WORKERS *(brute-force interim)*

2 GiB → 3 GiB on the pod limit + workers 18 → 26–30 would put the
Edgar cold-cache case under 100 s on raw fan-out alone, without the
SSE rewrite. Trade-off: more concurrent shop HTTPS requests means a
higher chance of upstream rate-limit pushback (Scryfall starts 429ing
above ~20 concurrent in our measurements). Worth doing only if SSE
slips and Edgar's 124 s 524 becomes a recurring user complaint.

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

Land the **SSE streaming endpoint** (the "Open #1" item above). It's
the structural fix for the remaining CF-524 cases, and the same
refactor naturally opens the door to the concurrent-users semaphore.
