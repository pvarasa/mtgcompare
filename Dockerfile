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

# Two workers × 16 threads = up to 32 concurrent requests per pod,
# split across two Python processes so each gets its own GIL. Measured
# on the realistic-mix loadtest (see server_admin/loadtest/saturate_realistic.js)
# this lifts per-pod throughput from ~91 r/s → ~137 r/s and drops the
# p99 at 200 VUs from 7.2 s → 2.1 s vs the previous workers=1 config —
# the single-worker setup was bottlenecked on the GIL, not on the
# in-flight cap. Same total cap, double effective CPU.
#
# Safe shape-wise because /decklist/stream is a single HTTP request
# (no in-process state shared across requests across worker
# boundaries). Per-user in-flight cap (_in_flight_by_user) is still
# per-process, so the effective cap is 3 × N_workers.
#
# Memory: worst-case is N_workers × 1.5 GiB (one cold 100-card search
# per worker) plus ~150 MiB baseline each. The deployment manifest
# accordingly sets memory limit to 4 GiB; bump alongside any further
# worker count increase.
#
# --timeout=120 covers the synchronous /decklist fallback (cold-cache
# 100-card searches measured ~90 s post-timeout-hardening). The
# streaming /decklist/stream path doesn't depend on this — its
# long-lived response body isn't a single request from gunicorn's
# perspective once bytes start flowing.
CMD ["gunicorn", "--workers=2", "--threads=16", "--timeout=120", "--bind=0.0.0.0:5000", "--access-logfile=-", "--error-logfile=-", "mtgcompare.web:app"]
