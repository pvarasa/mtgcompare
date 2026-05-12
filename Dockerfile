# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder

RUN pip install uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-group desktop

# ── runtime ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Run as a non-root user. UID matches the deployment manifest's
# securityContext.runAsUser so volume permissions line up.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin mtgc

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY . .

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 5000

USER mtgc

# One worker, 32 threads = up to 32 concurrent requests per pod.
#
# Why one worker instead of the previous workers=4/threads=8: the SSE
# /decklist/jobs flow keeps job state in a process-local dict
# (mtgcompare/web.py::_search_jobs). gunicorn workers are separate
# processes with private address spaces, so a POST that creates a job
# in worker 2 would 404 on a follow-up GET that the kernel routes to
# worker 1. Threads inside one worker share memory, so collapsing to
# workers=1 fixes the visibility problem without changing total
# request concurrency (still 32 in-flight requests per pod).
#
# This is a deployment-shape constraint, not an SSE limitation. The
# common scale-out patterns for SSE with multiple workers are Redis
# pub/sub or Postgres LISTEN/NOTIFY for the event bus, with workers
# treated as interchangeable consumers. We don't need that today
# (replicas: 1 and workload is I/O-bound, so the GIL isn't binding),
# but it's the natural growth path — see docs/perf_improvements.md.
#
# --timeout=120 covers the synchronous /decklist (cold-cache 100-card
# searches measured ~90 s post-timeout-hardening). The streaming
# /decklist/jobs path doesn't depend on this — its long-lived SSE
# connection isn't a single request from gunicorn's perspective once
# bytes start flowing.
CMD ["gunicorn", "--workers=1", "--threads=32", "--timeout=120", "--bind=0.0.0.0:5000", "--access-logfile=-", "--error-logfile=-", "mtgcompare.web:app"]
