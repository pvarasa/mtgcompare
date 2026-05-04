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

# Two workers so a long decklist search saturating one worker leaves the
# other free for /healthz and other UI traffic. --timeout=120 covers the
# realistic worst-case decklist (76s measured for a 32-card cold search);
# the gunicorn default of 30s would silently kill those requests.
CMD ["gunicorn", "--workers=2", "--threads=4", "--timeout=120", "--bind=0.0.0.0:5000", "--access-logfile=-", "--error-logfile=-", "mtgcompare.web:app"]
