"""User identity across the three modes (SQLite, header-trust, WorkOS), WorkOS
token verification, the auth gate and its public-path allowlist, logout."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

import mtgcompare.auth as auth_module
import mtgcompare.db as db_module
from mtgcompare import web

# ---------------------------------------------------------------------------
# _get_user_id
# ---------------------------------------------------------------------------

def test_get_user_id_returns_local_in_sqlite_mode(monkeypatch):
    monkeypatch.setattr(db_module, "IS_POSTGRES", False)
    with web.app.test_request_context("/"):
        assert web._get_user_id() == "local"


def test_get_user_id_reads_header_in_postgres_mode(monkeypatch):
    monkeypatch.setattr(db_module, "IS_POSTGRES", True)
    with web.app.test_request_context("/", headers={"X-User-ID": "alice"}):
        assert web._get_user_id() == "alice"


def test_get_user_id_aborts_when_header_absent(monkeypatch):
    """A missing USER_ID_HEADER in Postgres-without-WorkOS mode must 403,
    not fall back to a shared 'anonymous' bucket — a misconfigured proxy
    that drops the header would otherwise cross-contaminate inventories."""
    from werkzeug.exceptions import Forbidden
    monkeypatch.setattr(db_module, "IS_POSTGRES", True)
    with web.app.test_request_context("/"), pytest.raises(Forbidden):
        web._get_user_id()


def test_get_user_id_respects_custom_header_name(monkeypatch):
    monkeypatch.setattr(db_module, "IS_POSTGRES", True)
    monkeypatch.setattr(web, "_USER_ID_HEADER", "X-Auth-Sub")
    with web.app.test_request_context("/", headers={"X-Auth-Sub": "bob"}):
        assert web._get_user_id() == "bob"


# _get_display_name
# ---------------------------------------------------------------------------

def test_get_display_name_returns_local_in_sqlite_mode(monkeypatch):
    monkeypatch.setattr(db_module, "IS_POSTGRES", False)
    with web.app.test_request_context("/"):
        assert web._get_display_name() == "local"


def test_get_display_name_uses_display_header_when_set(monkeypatch):
    monkeypatch.setattr(db_module, "IS_POSTGRES", True)
    monkeypatch.setattr(web, "_USER_ID_HEADER", "X-UID")
    monkeypatch.setattr(web, "_USER_DISPLAY_HEADER", "X-Username")
    with web.app.test_request_context("/", headers={"X-UID": "uid-123", "X-Username": "pablo"}):
        assert web._get_display_name() == "pablo"


def test_get_display_name_falls_back_to_user_id_when_display_header_absent(monkeypatch):
    monkeypatch.setattr(db_module, "IS_POSTGRES", True)
    monkeypatch.setattr(web, "_USER_ID_HEADER", "X-UID")
    monkeypatch.setattr(web, "_USER_DISPLAY_HEADER", "X-Username")
    with web.app.test_request_context("/", headers={"X-UID": "uid-123"}):
        assert web._get_display_name() == "uid-123"


def test_get_display_name_falls_back_to_user_id_when_display_header_unconfigured(monkeypatch):
    monkeypatch.setattr(db_module, "IS_POSTGRES", True)
    monkeypatch.setattr(web, "_USER_ID_HEADER", "X-UID")
    monkeypatch.setattr(web, "_USER_DISPLAY_HEADER", "")
    with web.app.test_request_context("/", headers={"X-UID": "uid-123", "X-Username": "pablo"}):
        assert web._get_display_name() == "uid-123"


# ---------------------------------------------------------------------------
# WorkOS-enabled identity path
# ---------------------------------------------------------------------------

def test_get_user_id_uses_g_when_workos_enabled(monkeypatch):
    from flask import g
    monkeypatch.setattr(auth_module, "WORKOS_ENABLED", True)
    with web.app.test_request_context("/"):
        g.user_id = "user_01ABC"
        assert web._get_user_id() == "user_01ABC"


def test_get_user_id_anonymous_when_workos_enabled_but_unset(monkeypatch):
    monkeypatch.setattr(auth_module, "WORKOS_ENABLED", True)
    with web.app.test_request_context("/"):
        assert web._get_user_id() == "anonymous"


def test_get_display_name_uses_email_when_workos_enabled(monkeypatch):
    from flask import g
    monkeypatch.setattr(auth_module, "WORKOS_ENABLED", True)
    with web.app.test_request_context("/"):
        g.user = {"id": "user_01", "email": "alice@example.com"}
        assert web._get_display_name() == "alice@example.com"


def test_inject_current_user_exposes_workos_flag(monkeypatch):
    monkeypatch.setattr(auth_module, "WORKOS_ENABLED", False)
    with web.app.test_request_context("/"):
        ctx = web._inject_current_user()
        assert ctx["workos_enabled"] is False
    monkeypatch.setattr(auth_module, "WORKOS_ENABLED", True)
    with web.app.test_request_context("/"):
        ctx = web._inject_current_user()
        assert ctx["workos_enabled"] is True


def _stub_jwks(monkeypatch):
    class _Key: key = "stub"
    class _JWKS:
        def get_signing_key_from_jwt(self, _token): return _Key()
    monkeypatch.setattr(auth_module, "_get_jwks", lambda: _JWKS())


def test_verify_access_token_rejects_missing_client_id(monkeypatch):
    """A token whose payload lacks `client_id` must fail closed even if the
    signature checks out — without this the auth gate would silently accept
    a signature-valid token from a different WorkOS app."""
    _stub_jwks(monkeypatch)
    monkeypatch.setattr(auth_module, "WORKOS_CLIENT_ID", "client_TEST")
    monkeypatch.setattr(auth_module.jwt, "decode",
                        lambda *_a, **_kw: {"sub": "user_X", "sid": "sess_1"})

    import jwt as _jwt
    with pytest.raises(_jwt.InvalidTokenError, match="client_id"):
        auth_module.verify_access_token("fake.jwt")


def test_verify_access_token_rejects_mismatched_client_id(monkeypatch):
    _stub_jwks(monkeypatch)
    monkeypatch.setattr(auth_module, "WORKOS_CLIENT_ID", "client_TEST")
    monkeypatch.setattr(
        auth_module.jwt, "decode",
        lambda *_a, **_kw: {"sub": "user_X", "client_id": "client_OTHER"},
    )

    import jwt as _jwt
    with pytest.raises(_jwt.InvalidTokenError, match="client_id"):
        auth_module.verify_access_token("fake.jwt")


def test_verify_access_token_returns_claims_on_matching_client_id(monkeypatch):
    _stub_jwks(monkeypatch)
    monkeypatch.setattr(auth_module, "WORKOS_CLIENT_ID", "client_TEST")
    monkeypatch.setattr(
        auth_module.jwt, "decode",
        lambda *_a, **_kw: {"sub": "user_X", "client_id": "client_TEST", "sid": "sess_1"},
    )

    claims = auth_module.verify_access_token("fake.jwt")
    assert claims["sub"] == "user_X"
    assert claims["client_id"] == "client_TEST"


def test_upsert_user_advances_updated_at_on_repeat(monkeypatch):
    """`updated_at` must move forward when `_upsert_user` is called for an
    existing row (e.g. via a `user.updated` webhook). The column's PG
    `server_default=func.now()` only fires on INSERT, so the helper has
    to pass the timestamp itself.
    """
    # Two known instants instead of a sleep: the gap no longer depends on
    # the clock's resolution (~16 ms on Windows).
    stamps = iter([datetime(2026, 1, 1, 12, 0, tzinfo=UTC), datetime(2026, 1, 1, 12, 5, tzinfo=UTC)])

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(stamps)

    monkeypatch.setattr(auth_module, "datetime", _Clock)
    auth_module._upsert_user({"id": "user_TS", "email": "a@example.com"})
    with db_module.get_conn() as conn:
        first = conn.execute(text(
            "SELECT updated_at FROM users WHERE workos_user_id = 'user_TS'"
        )).scalar_one()
    auth_module._upsert_user({"id": "user_TS", "email": "b@example.com"})
    with db_module.get_conn() as conn:
        second = conn.execute(text(
            "SELECT updated_at FROM users WHERE workos_user_id = 'user_TS'"
        )).scalar_one()
    assert second > first


def test_auth_routes_404_when_workos_disabled(monkeypatch):
    monkeypatch.setattr(auth_module, "WORKOS_ENABLED", False)
    with web.app.test_client() as client:
        assert client.get("/auth/login").status_code == 404
        assert client.get("/auth/me").status_code == 404
        assert client.post("/webhooks/workos").status_code == 404


def test_is_safe_return_to_rejects_protocol_relative_and_other_origins():
    """`startswith("/")` alone accepts `//evil.com/x` (browsers treat
    that as protocol-relative). Reject those plus the `\\\\` variant."""
    safe = auth_module._is_safe_return_to
    # Same-origin paths — accepted.
    assert safe("/") is True
    assert safe("/inventory") is True
    assert safe("/market/history?card_name=foo") is True
    # Protocol-relative or absolute URLs — rejected.
    assert safe("//evil.com/x") is False
    assert safe("//evil.com") is False
    assert safe("/\\evil.com/x") is False
    assert safe("https://evil.com/x") is False
    assert safe("javascript:alert(1)") is False
    # Empty / missing — rejected.
    assert safe("") is False
    assert safe("foo") is False


def test_logout_rejects_get_method(monkeypatch):
    """GET-CSRF on /auth/logout (e.g. <img src="/auth/logout">) is
    closed by making the route POST-only — Flask returns 405."""
    monkeypatch.setattr(auth_module, "WORKOS_ENABLED", True)
    with web.app.test_client() as client:
        resp = client.get("/auth/logout")
        assert resp.status_code == 405


def test_logout_post_clears_cookies(monkeypatch):
    monkeypatch.setattr(auth_module, "WORKOS_ENABLED", True)
    with web.app.test_client() as client:
        resp = client.post("/auth/logout", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["Location"].startswith(
            "https://api.workos.com/user_management/sessions/logout"
        )


# ---------------------------------------------------------------------------
# Public-path allowlist + auth-gate redirect
# ---------------------------------------------------------------------------

def test_is_public_path_allowlist():
    is_public = auth_module._is_public_path
    # Exact match for /healthz; prefixes (all ending in /) for the rest.
    assert is_public("/healthz") is True
    assert is_public("/auth/login") is True
    assert is_public("/auth/callback") is True
    assert is_public("/static/cardpreview.js") is True
    assert is_public("/webhooks/workos") is True
    assert is_public("/internal/cron/update-prices") is True
    # Non-allowlisted paths must NOT be public.
    assert is_public("/") is False
    assert is_public("/inventory") is False
    assert is_public("/market/history") is False
    # Guard against prefix over-match — /healthzfoo must not be treated as /healthz.
    assert is_public("/healthzfoo") is False


def test_auth_gate_redirects_anonymous_to_authkit(monkeypatch):
    monkeypatch.setattr(auth_module, "WORKOS_ENABLED", True)
    monkeypatch.setattr(
        auth_module, "authorization_url",
        lambda *, state: f"https://vpablo.authkit.com/?state={state}",
    )
    with web.app.test_client() as client:
        resp = client.get("/inventory", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("https://vpablo.authkit.com/")
    cookies = resp.headers.get_all("Set-Cookie")
    assert any(c.startswith(f"{auth_module.STATE_COOKIE}=") for c in cookies)
    assert any(c.startswith(f"{auth_module.RETURN_TO_COOKIE}=") for c in cookies)


def test_index_renders_for_authenticated_user(monkeypatch):
    """Catches stale endpoint names in templates (e.g. `auth_logout` after the
    Blueprint move where the real endpoint is `auth.logout`) AND verifies
    the navbar shows the user's display name (falling back to email).

    Without this test the bug only surfaces in production: the navbar's
    `{% if workos_enabled %}` branch is dead in unit-test contexts, so a
    BuildError can ship undetected.
    """
    monkeypatch.setattr(auth_module, "WORKOS_ENABLED", True)
    monkeypatch.setattr(
        auth_module, "verify_access_token",
        # Note: real WorkOS access tokens do NOT carry an `email` claim;
        # the gate must source profile fields from the local users table.
        lambda token: {"sub": "user_01TEST", "sid": "sess_1"},
    )
    # Seed the users table so the gate's DB lookup finds a profile.
    db_module.init_schema()
    auth_module._upsert_user({
        "id": "user_01TEST",
        "email": "alice@example.com",
        "first_name": "Alice",
        "last_name": "Tester",
    })
    with web.app.test_client() as client:
        client.set_cookie(auth_module.ACCESS_TOKEN_COOKIE, "fake.jwt")
        resp = client.get("/")
    assert resp.status_code == 200
    assert b"Alice Tester" in resp.data
    assert b"user_01TEST" not in resp.data  # must not fall back to user_id
    # The Sign-out form must render for WorkOS users — it silently vanished
    # once before (76bd33a) because nothing asserted its presence.
    assert b"Sign out" in resp.data


def test_index_falls_back_to_email_when_name_missing(monkeypatch):
    monkeypatch.setattr(auth_module, "WORKOS_ENABLED", True)
    monkeypatch.setattr(
        auth_module, "verify_access_token",
        lambda token: {"sub": "user_01NONAME", "sid": "sess_2"},
    )
    db_module.init_schema()
    auth_module._upsert_user({
        "id": "user_01NONAME",
        "email": "bob@example.com",
        "first_name": None,
        "last_name": None,
    })
    with web.app.test_client() as client:
        client.set_cookie(auth_module.ACCESS_TOKEN_COOKIE, "fake.jwt")
        resp = client.get("/")
    assert resp.status_code == 200
    assert b"bob@example.com" in resp.data


def test_auth_gate_skips_public_paths_without_invoking_workos(monkeypatch):
    # /healthz must succeed even when AuthKit is unreachable; if the gate
    # inadvertently called authorization_url for a public path, this test
    # would raise from the lambda.
    monkeypatch.setattr(auth_module, "WORKOS_ENABLED", True)
    def _explode(**_kwargs):
        raise AssertionError("auth gate must not invoke authorization_url for public paths")
    monkeypatch.setattr(auth_module, "authorization_url", _explode)
    with web.app.test_client() as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/static/cardpreview.js").status_code in (200, 404)
