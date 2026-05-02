# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder

RUN pip install uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-group desktop

# ── runtime ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY . .

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 5000

CMD ["gunicorn", "--workers=1", "--threads=4", "--bind=0.0.0.0:5000", "--access-logfile=-", "--error-logfile=-", "mtgcompare.web:app"]
