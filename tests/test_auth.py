"""Tests for `mtgcompare.auth` — focused on the paths not already exercised
by `test_web_helpers.py`: the refresh-token flow inside `_auth_gate`, the
`/auth/callback` code-exchange + state validation, the webhook event-type
dispatch, and the security attributes on session cookies.

JWKS / WorkOS HTTP calls are stubbed at module-attribute level — these are
unit tests of our wiring, not of the WorkOS SDK.
"""
from __future__ import annotations

from types import SimpleNamespace

import jwt as pyjwt
import pytest
from sqlalchemy import text

import mtgcompare.auth as auth_module
import mtgcompare.db as db_module
from mtgcompare import web


@pytest.fixture(autouse=True)
def _enable_workos(monkeypatch):
    """Most tests in this file pretend WorkOS is configured.

    Individual tests can flip it off when they want to assert the disabled
    fallback path.
    """
    monkeypatch.setattr(auth_module, "WORKOS_ENABLED", True)
    monkeypatch.setattr(auth_module, "WORKOS_CLIENT_ID", "client_TEST")
    yield


def _stub_authorization_url(monkeypatch):
    monkeypatch.setattr(
        auth_module, "authorization_url",
        lambda *, state: f"https://example.authkit.com/start?state={state}",
    )


# ---------------------------------------------------------------------------
# _auth_gate — refresh-token flow
# ---------------------------------------------------------------------------

def test_auth_gate_refreshes_when_access_token_expired(monkeypatch):
    """When the access-token JWT fails verification but a refresh-token
    cookie is present, the gate must mint a new pair, set both cookies on
    the response, and proceed with the original request — the user must
    not see a redirect or a logout."""
    db_module.init_schema()
    auth_module._upsert_user({
        "id": "user_REFRESH",
        "email": "refresh@example.com",
        "first_name": "Re",
        "last_name": "Fresh",
    })

    calls = {"verify_count": 0, "refresh_arg": None}

    def fake_verify(token: str) -> dict:
        calls["verify_count"] += 1
        if token == "expired-jwt":
            raise pyjwt.InvalidTokenError("expired")
        return {"sub": "user_REFRESH", "sid": "sess_new"}

    def fake_refresh(refresh_token: str) -> dict:
        calls["refresh_arg"] = refresh_token
        return {
            "user": {"id": "user_REFRESH", "email": "refresh@example.com"},
            "access_token": "new-access",
            "refresh_token": "new-refresh",
        }

    monkeypatch.setattr(auth_module, "verify_access_token", fake_verify)
    monkeypatch.setattr(auth_module, "refresh", fake_refresh)

    with web.app.test_client() as client:
        client.set_cookie(auth_module.ACCESS_TOKEN_COOKIE, "expired-jwt")
        client.set_cookie(auth_module.REFRESH_TOKEN_COOKIE, "rt-current")
        resp = client.get("/")

    assert resp.status_code == 200
    assert calls["refresh_arg"] == "rt-current"
    cookies = resp.headers.get_all("Set-Cookie")
    assert any(c.startswith(f"{auth_module.ACCESS_TOKEN_COOKIE}=new-access") for c in cookies)
    assert any(c.startswith(f"{auth_module.REFRESH_TOKEN_COOKIE}=new-refresh") for c in cookies)


def test_auth_gate_redirects_when_refresh_fails(monkeypatch):
    """Both tokens broken → kick the user to AuthKit, do NOT serve a 500."""
    _stub_authorization_url(monkeypatch)

    def fake_verify(_token):
        raise pyjwt.InvalidTokenError("bad")

    def fake_refresh(_rt):
        raise RuntimeError("refresh endpoint rejected the token")

    monkeypatch.setattr(auth_module, "verify_access_token", fake_verify)
    monkeypatch.setattr(auth_module, "refresh", fake_refresh)

    with web.app.test_client() as client:
        client.set_cookie(auth_module.ACCESS_TOKEN_COOKIE, "bad-jwt")
        client.set_cookie(auth_module.REFRESH_TOKEN_COOKIE, "bad-rt")
        resp = client.get("/inventory", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("https://example.authkit.com/start")


# ---------------------------------------------------------------------------
# /auth/callback — state validation + happy-path
# ---------------------------------------------------------------------------

def test_callback_rejects_state_mismatch(monkeypatch):
    """The OAuth state-cookie check is the CSRF defense for the callback —
    a request whose querystring `state` doesn't match the cookie value
    must NOT exchange the code, even if the code itself looks fine."""
    exchanges = {"called": False}

    def fake_exchange(_code):
        exchanges["called"] = True
        return {}

    monkeypatch.setattr(auth_module, "exchange_code", fake_exchange)

    with web.app.test_client() as client:
        client.set_cookie(auth_module.STATE_COOKIE, "expected-state")
        resp = client.get(
            "/auth/callback?code=abc&state=attacker-state",
            follow_redirects=False,
        )

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/auth/login")
    assert exchanges["called"] is False, "code-exchange must not run on state mismatch"


def test_callback_happy_path_sets_cookies_and_redirects(monkeypatch):
    """Matching state + working code-exchange must set both session cookies
    with HttpOnly+Secure+SameSite=Lax, then redirect to the saved return-to."""
    db_module.init_schema()

    def fake_exchange(code: str) -> dict:
        assert code == "valid-code"
        return {
            "user": {
                "id": "user_CALLBACK",
                "email": "cb@example.com",
                "first_name": "Cal",
                "last_name": "Back",
            },
            "access_token": "fresh-access",
            "refresh_token": "fresh-refresh",
        }

    monkeypatch.setattr(auth_module, "exchange_code", fake_exchange)

    with web.app.test_client() as client:
        client.set_cookie(auth_module.STATE_COOKIE, "matching-state")
        client.set_cookie(auth_module.RETURN_TO_COOKIE, "/inventory")
        resp = client.get(
            "/auth/callback?code=valid-code&state=matching-state",
            follow_redirects=False,
        )

    assert resp.status_code == 302
    assert resp.headers["Location"] == "/inventory"
    cookies = resp.headers.get_all("Set-Cookie")

    access = next(c for c in cookies if c.startswith(f"{auth_module.ACCESS_TOKEN_COOKIE}="))
    refresh = next(c for c in cookies if c.startswith(f"{auth_module.REFRESH_TOKEN_COOKIE}="))
    for cookie_str in (access, refresh):
        assert "HttpOnly" in cookie_str
        assert "Secure" in cookie_str
        assert "SameSite=Lax" in cookie_str

    # The user record must be mirrored locally so subsequent gate calls
    # can populate g.user without round-tripping to WorkOS.
    with db_module.get_conn() as conn:
        row = conn.execute(text(
            "SELECT email, first_name FROM users WHERE workos_user_id = 'user_CALLBACK'"
        )).mappings().first()
    assert row["email"] == "cb@example.com"
    assert row["first_name"] == "Cal"


def test_callback_unsafe_return_to_falls_back_to_root(monkeypatch):
    """A protocol-relative or off-origin `return_to` cookie value must be
    discarded so attackers can't seed a post-login open redirect."""
    db_module.init_schema()
    monkeypatch.setattr(auth_module, "exchange_code", lambda _c: {
        "user": {"id": "user_X", "email": "x@example.com"},
        "access_token": "a", "refresh_token": "r",
    })

    with web.app.test_client() as client:
        client.set_cookie(auth_module.STATE_COOKIE, "ok")
        client.set_cookie(auth_module.RETURN_TO_COOKIE, "//evil.com/steal")
        resp = client.get("/auth/callback?code=c&state=ok", follow_redirects=False)

    assert resp.headers["Location"] == "/"


# ---------------------------------------------------------------------------
# Webhook dispatch
# ---------------------------------------------------------------------------

def _fake_event(event_type: str, **fields):
    """Build the duck-typed object the webhook handler reads from."""
    return SimpleNamespace(event=event_type, data=SimpleNamespace(**fields))


def test_webhook_user_created_upserts_row(monkeypatch):
    db_module.init_schema()
    monkeypatch.setattr(
        auth_module, "verify_webhook",
        lambda _body, _sig: _fake_event(
            "user.created", id="user_NEW",
            email="new@example.com", first_name="Nu", last_name="Wave",
        ),
    )
    with web.app.test_client() as client:
        resp = client.post(
            "/webhooks/workos", data=b"{}",
            content_type="application/json",
            headers={"WorkOS-Signature": "t=1,v1=stub"},
        )
    assert resp.status_code == 200
    with db_module.get_conn() as conn:
        row = conn.execute(text(
            "SELECT email, first_name FROM users WHERE workos_user_id = 'user_NEW'"
        )).mappings().first()
    assert row["email"] == "new@example.com"
    assert row["first_name"] == "Nu"


def test_webhook_user_deleted_clears_user_and_inventory(monkeypatch):
    """A `user.deleted` event must remove both the user row and any inventory
    they owned — keeping orphaned rows around would re-attach to a future
    sub-collision."""
    db_module.init_schema()
    auth_module._upsert_user({"id": "user_GONE", "email": "g@example.com"})
    with db_module.get_conn() as conn:
        conn.execute(text(
            "INSERT INTO inventory (user_id, card_name, set_code, quantity,"
            " condition, printing, language)"
            " VALUES ('user_GONE', 'Sol Ring', 'CMM', 1, 'NM', 'normal', 'EN')"
        ))

    monkeypatch.setattr(
        auth_module, "verify_webhook",
        lambda _body, _sig: _fake_event("user.deleted", id="user_GONE"),
    )

    with web.app.test_client() as client:
        resp = client.post(
            "/webhooks/workos", data=b"{}",
            content_type="application/json",
            headers={"WorkOS-Signature": "t=1,v1=stub"},
        )
    assert resp.status_code == 200

    with db_module.get_conn() as conn:
        user_row = conn.execute(text(
            "SELECT 1 FROM users WHERE workos_user_id = 'user_GONE'"
        )).first()
        inv_row = conn.execute(text(
            "SELECT 1 FROM inventory WHERE user_id = 'user_GONE'"
        )).first()
    assert user_row is None
    assert inv_row is None


def test_webhook_unknown_event_type_is_ignored(monkeypatch):
    """An event type the handler doesn't know about must succeed (so WorkOS
    doesn't retry it) without mutating local tables."""
    db_module.init_schema()
    monkeypatch.setattr(
        auth_module, "verify_webhook",
        lambda _body, _sig: _fake_event(
            "organization.created", id="org_X", email=None,
        ),
    )
    with web.app.test_client() as client:
        resp = client.post(
            "/webhooks/workos", data=b"{}",
            content_type="application/json",
            headers={"WorkOS-Signature": "t=1,v1=stub"},
        )
    assert resp.status_code == 200
    with db_module.get_conn() as conn:
        row = conn.execute(text(
            "SELECT 1 FROM users WHERE workos_user_id = 'org_X'"
        )).first()
    assert row is None
