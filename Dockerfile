# syntax=docker/dockerfile:1

# Base image is digest-pinned so a given git tag always builds on the same
# layers — a floating tag meant two builds of the same commit could differ.
# The tag is kept alongside the digest for readability; the digest is what
# actually resolves. This is the multi-arch index digest, not a per-platform
# one, so it stays portable.
#
# To refresh (picks up Debian security updates, which is the only way they
# reach this image — there is no apt-get upgrade below):
#   docker manifest inspect python:3.14-slim | head
# then update BOTH FROM lines together.
FROM python:3.14-slim@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966 AS builder

# uv from its official image, pinned like the base: `pip install uv` took
# whatever PyPI served at build time.
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-group desktop

# ── runtime ──────────────────────────────────────────────────────────────────
# Same digest as the builder stage above — keep them in lockstep.
FROM python:3.14-slim@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966

# Run as a non-root user. UID matches the deployment manifest's
# securityContext.runAsUser so volume permissions line up.
# The base image's pip is never used at runtime (deps live in /app/.venv),
# but it carries vendored msgpack / urllib3 / setuptools that trivy flags
# HIGH. Remove it rather than scan around it.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin mtgc \
    && python -m pip uninstall -y -q pip \
    && rm -rf /root/.cache

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
