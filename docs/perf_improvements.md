# mtgcompare — Performance improvement backlog

Captured 2026-05-04 from the analysis of a 32-unique-card / 76-card
Legacy decklist search that took **76.5 s wall-clock** (cold cache),
peaked at **+480 MB transient RSS**, and was causing K8s liveness probes
to flap on the single-worker gunicorn config.

v1.5.11 already shipped #1 and #2 (gunicorn `--workers=2 --timeout=120`)
to fix the immediate liveness-probe problem. Items below are the next
levels of optimisation, ranked roughly by ROI.

---

## #3 — Stream results to the browser as cards complete *(highest UX win)*

**Problem.** With 76 s end-to-end decklist time, the user sees a blank
loading spinner for ~76 s. Total time is fundamentally bounded by the
slowest shop on the slowest card, so we can't reduce wall-clock easily.
But we *can* dramatically reduce **perceived** time.

**Idea.** Replace the final `render_template("decklist.html", ...)` with
a streaming response that emits HTML fragments (or JSON-Lines events)
as each card's `as_completed()` future resolves. The browser renders
each row as it arrives — first card visible in ~3 s instead of 76 s.

**Implementation sketch.**

- New endpoint `POST /decklist/stream` that returns
  `Content-Type: text/event-stream`.
- Generator function:
  ```python
  def stream_decklist(text, fx, enabled, ...):
      # ... existing parsing/inventory deduction ...
      with ThreadPoolExecutor(max_workers=6) as ex:
          futs = {ex.submit(_fetch_card_prices, n, fx, enabled): n for n in names}
          for fut in as_completed(futs):
              row = build_row(fut.result(), ...)
              yield f"event: card\ndata: {json.dumps(row)}\n\n"
      yield f"event: complete\ndata: {json.dumps(grand_totals)}\n\n"
  ```
- Frontend uses `EventSource` (or fetch+ReadableStream for POSTed
  bodies) to inject rows as they arrive.
- Keep the existing non-streaming endpoint as a fallback for clients
  that don't speak SSE (and for the canary).

**Effort.** ~80 lines of code + a small frontend rework. ~4 hours.

**Watch out for.** SSE keeps the gunicorn worker thread busy for the
duration; with 2 workers and 4 threads each (8 total) plus health
probes, we still have plenty of headroom for normal traffic. If the
service grows, revisit whether async (#6) is worth it.

---

## #4 — Cap inner concurrency to halve peak memory

**Problem.** Today: 6 cards × 8 shops = **48 concurrent HTTPS
connections** in one process. Peak memory ~480 MB during BeautifulSoup
parsing of overlapping responses.

**Idea.** Reduce one of the two fan-out levels:

| Card-level | Shop-level | Peak inflight | Wall-clock impact |
|---|---|---|---|
| 6 | 8 (today) | 48 | baseline |
| 3 | 8 | 24 | +30 % wall-clock |
| 6 | 4 | 24 | +25 % wall-clock |
| 3 | 4 | 12 | +50 % wall-clock |

**Implementation.** Two-line change:
- `web.py`: `max_workers=min(len(names_to_search), 6)` → `3`
- `shops.py`: `max_workers=len(scrapers)` → `min(len(scrapers), 4)`

**Effort.** 5 min. Might be worth doing pre-emptively before traffic
grows; halves memory peak with manageable latency cost.

**Skip if** #3 (streaming) lands first — the latency increase is more
forgiving when results arrive incrementally.

---

## #5 — lxml + iterparse for the heavyweight HTML shops

**Problem.** Cardshop Serra returns a 2.5 MB HTML page per card.
BeautifulSoup builds the full DOM (~25 MB of Python objects) and holds
the GIL for hundreds of ms while doing it. With 6 such parses
overlapping, the GIL is contested almost continuously, which is the
proximate reason `/healthz` slows down during searches.

**Idea.** For the largest-response shops (Cardshop Serra, MINT MALL,
SingleStar — all >1 MB), parse with `lxml.etree.iterparse`. It
streams the document, fires events on tag-close, and lets us free
nodes as we go. Memory drops ~70 %, GIL holds shorten to single-digit
ms each.

**Implementation.** Per shop, replace `BeautifulSoup(html)` and
`soup.select(...)` with an `iterparse(io.BytesIO(html), events=("end",), tag="li")`-style loop. The pure parser functions are well-isolated;
each scraper rewrite is ~30–50 lines. Tests stay fixture-driven so
the behaviour is verifiable.

**Effort.** 1–2 hours per shop. Three shops to do = half a day total.

**Worth it when.** Memory or GIL becomes a pain on a more crowded host
(more workers, more concurrent users), or before the bulk-crawl path
in the cache plan lands.

---

## #6 — Async I/O via httpx + asyncio *(largest refactor, eliminates GIL contention)*

**Problem.** Threads × Python = GIL serialization on CPU-bound work
(parsing). Adding more threads doesn't actually run more parsers
simultaneously — it just adds context-switch overhead.

**Idea.** Replace ThreadPoolExecutor with `asyncio.gather()` over
`httpx.AsyncClient`. Single-threaded cooperative I/O; one parse at a
time but 8+ HTTPS calls in flight. Memory drops further (no thread
stacks). gunicorn still uses sync workers but the inside of each
request is async.

**Implementation sketch.**

- New scraper base: `AsyncMtgScrapper` with `async def get_prices(...)`.
- All 7 HTML scrapers reimplemented with `httpx.AsyncClient` + the
  same `parse_search_html` pure functions.
- Cache layer becomes `async`-aware (singleflight via
  `asyncio.Future`).
- `collect_prices` becomes `async def` with `asyncio.gather`.
- `web.py` runs the async coroutine via `asyncio.run()` or, better,
  switches to `gunicorn -k uvicorn.workers.UvicornWorker` and
  declares the search routes `async`.

**Effort.** ~2 days. Cache layer is the trickiest piece because it
currently uses sync SQLAlchemy.

**Watch out for.** Mixing sync (SQLAlchemy, Flask routing) with async
(scrapers, httpx) is surprisingly easy to get wrong. If pursued, do
it as a clean migration to FastAPI + AsyncEngine, not a hybrid.

---

## #7 — Bump probe tolerances *(band-aid, last resort)*

```yaml
livenessProbe:
  periodSeconds: 60
  timeoutSeconds: 10
  failureThreshold: 5
```

Doesn't fix anything; just makes the symptom less visible. Only use
this if 1, 2, and one of 3/4/5 are insufficient. Currently NOT needed
after v1.5.11.

---

## What NOT to do

- **Don't add a Redis cache layer.** The local SQLite/Postgres cache
  is already serving sub-millisecond hot reads; Redis would add a
  network hop and ops surface for no real win.
- **Don't pre-warm the cache by crawling all of Hareruya nightly.**
  See `docs/shop_integration_plan.md` for the full discussion of why
  bulk indexing is a bigger project than it appears (and why it's the
  right move once we hit ~15+ shops, but premature today).
- **Don't switch to BeautifulSoup's `lxml` builder without iterparse.**
  Just changing the parser backend is a 5–15 % win; the big savings
  come from streaming, which requires a structural rewrite.

---

## Recommended next move

When the v1.5.11 gunicorn fix has had a few days to prove itself, do
**#3 (streaming results)** as the next big improvement. It's the
single change that most transforms how the app feels for a Legacy /
Modern decklist user, and it doesn't require any of the larger
refactors below it.
