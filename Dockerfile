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

# Four workers / eight threads each = up to 32 concurrent requests per
# pod. The work is I/O-bound (parallel shop scrapes), so we trade more
# RAM for substantially higher concurrent-search capacity. The pod's
# CPU limit is sized accordingly in the k8s deployment.
# --timeout=120 covers the realistic worst-case decklist (cold-cache
# 100-card searches measured ~90s post-timeout-hardening); the
# gunicorn default of 30s would silently kill those requests.
CMD ["gunicorn", "--workers=4", "--threads=8", "--timeout=120", "--bind=0.0.0.0:5000", "--access-logfile=-", "--error-logfile=-", "mtgcompare.web:app"]
