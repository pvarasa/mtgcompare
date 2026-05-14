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
# Workers > 1 is now safe shape-wise: the streaming /decklist/stream
# endpoint is a single HTTP request (POST that emits text/event-stream),
# so it doesn't depend on shared in-process state across requests. The
# per-user in-flight cap (_in_flight_by_user) is still per-process, so
# bumping workers also multiplies the effective cap — fine as long as
# pod memory headroom can absorb cap × N_workers concurrent searches
# at ~1.5 GiB peak each. Today's pod is 3 GiB and one search occupies
# ~half of it; raising workers needs a parallel memory bump.
#
# --timeout=120 covers the synchronous /decklist fallback (cold-cache
# 100-card searches measured ~90 s post-timeout-hardening). The
# streaming /decklist/stream path doesn't depend on this — its
# long-lived response body isn't a single request from gunicorn's
# perspective once bytes start flowing.
CMD ["gunicorn", "--workers=1", "--threads=32", "--timeout=120", "--bind=0.0.0.0:5000", "--access-logfile=-", "--error-logfile=-", "mtgcompare.web:app"]
