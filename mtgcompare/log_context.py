"""Per-request log enrichment.

Every emitted ``LogRecord`` gets a ``request_id`` and ``user_id`` attribute
so app logs can be joined to gunicorn's access log and grouped by user.
The ``logging.conf`` formatter references these fields, so they must
exist on *every* record — including ones emitted from worker threads,
import-time config, and library code that has no Flask context. Records
outside a request context get '-' so the formatter never KeyErrors.

Wired up in ``mtgcompare.web`` at import time:

  * ``install_record_factory`` — registers a ``LogRecord`` factory that
    pulls ``request_id``/``user_id`` from ``flask.g`` when available,
    falling back to '-'. Must run before the first record is emitted.

  * ``bind_request_id`` — call from a ``before_request`` to stamp
    ``g.request_id`` early in the request cycle, honoring an upstream
    ``X-Request-Id`` header when present.
"""
from __future__ import annotations

import logging
import re
import uuid

from flask import g, has_request_context, request

REQUEST_ID_HEADER = "X-Request-Id"
_NO_VALUE = "-"
_VALID_RID = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")


def gen_request_id() -> str:
    return uuid.uuid4().hex[:12]


def bind_request_id() -> str:
    """Stamp ``g.request_id`` from the upstream header or a fresh uuid."""
    incoming = request.headers.get(REQUEST_ID_HEADER, "").strip()
    rid = incoming if _VALID_RID.match(incoming) else gen_request_id()
    g.request_id = rid
    return rid


def install_record_factory() -> None:
    """Wrap the active ``LogRecord`` factory so every record has request_id/user_id."""
    base = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = base(*args, **kwargs)
        record.request_id = _NO_VALUE
        record.user_id = _NO_VALUE
        if has_request_context():
            rid = getattr(g, "request_id", None)
            if rid:
                record.request_id = rid
            uid = getattr(g, "user_id", None)
            if uid:
                record.user_id = uid
        return record

    logging.setLogRecordFactory(factory)


class _HealthzAccessFilter(logging.Filter):
    """Drop gunicorn access-log lines for ``GET /healthz``.

    The k8s readiness/liveness probe hits ``/healthz`` every ~10s per pod,
    which would otherwise flood the access log with thousands of lines/day
    that carry no operational signal. Gunicorn's access log emits one
    record per request with the URL path in ``record.args['U']``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            path = record.args.get("U", "") if isinstance(record.args, dict) else ""
        except (AttributeError, TypeError):
            return True
        return path != "/healthz"


def install_healthz_access_filter() -> None:
    """Suppress gunicorn access-log lines for the health endpoint.

    No-op outside gunicorn (Flask's dev server doesn't use ``gunicorn.access``).
    """
    logging.getLogger("gunicorn.access").addFilter(_HealthzAccessFilter())
