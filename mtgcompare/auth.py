"""WorkOS authentication for the Flask app.

The whole module is gated by `WORKOS_API_KEY` / `WORKOS_CLIENT_ID` /
`WORKOS_REDIRECT_URI`: when any are unset `WORKOS_ENABLED` is False, the
Blueprint's lifecycle hooks short-circuit, and the rest of the app falls
back to its pre-WorkOS local behavior (no auth, user_id="local" in
SQLite, or `X-User-ID` header in PG-without-WorkOS).

Session model: a raw access-token JWT in an HttpOnly cookie, verified
against WorkOS's JWKS on every request. A separate refresh-token cookie
mints a new pair when the access token expires.
"""
from __future__ import annotations

import os
import secrets
from typing import Optional

import jwt
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    make_response,
    redirect,
    request,
    url_for,
)
from jwt import PyJWKClient
from sqlalchemy import text

from . import db
from . import inventory as inv

WORKOS_API_KEY        = os.environ.get("WORKOS_API_KEY", "")
WORKOS_CLIENT_ID      = os.environ.get("WORKOS_CLIENT_ID", "")
WORKOS_REDIRECT_URI   = os.environ.get("WORKOS_REDIRECT_URI", "")
WORKOS_WEBHOOK_SECRET = os.environ.get("WORKOS_WEBHOOK_SECRET", "")

WORKOS_ENABLED = bool(WORKOS_API_KEY and WORKOS_CLIENT_ID and WORKOS_REDIRECT_URI)

ACCESS_TOKEN_COOKIE  = "mtgc_at"
REFRESH_TOKEN_COOKIE = "mtgc_rt"
RETURN_TO_COOKIE     = "mtgc_return_to"
STATE_COOKIE         = "mtgc_oauth_state"

REFRESH_COOKIE_MAX_AGE = 30 * 24 * 60 * 60
TRANSIENT_COOKIE_MAX_AGE = 600

# Routes the auth gate must let through unauthenticated. Webhooks and the
# cron endpoint have their own auth (HMAC, bearer); /auth/* needs to be
# open so unauthenticated users can start the login flow.
_PUBLIC_PATH_PREFIXES = (
    "/auth/",
    "/static/",
    "/webhooks/",
    "/internal/cron/",
)
_PUBLIC_EXACT_PATHS = frozenset({"/healthz"})


# ---------------------------------------------------------------------------
# WorkOS SDK + JWKS — lazily constructed so bare imports stay cheap
# ---------------------------------------------------------------------------

_workos_client = None
_jwks_client: Optional[PyJWKClient] = None


def _get_client():
    global _workos_client
    if _workos_client is None:
        from workos import WorkOSClient
        _workos_client = WorkOSClient(api_key=WORKOS_API_KEY, client_id=WORKOS_CLIENT_ID)
    return _workos_client


def _get_jwks() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(
            f"https://api.workos.com/sso/jwks/{WORKOS_CLIENT_ID}",
            cache_keys=True, lifespan=3600,
        )
    return _jwks_client


def random_state() -> str:
    return secrets.token_urlsafe(32)


def authorization_url(*, state: str) -> str:
    return _get_client().user_management.get_authorization_url(
        provider="authkit",
        redirect_uri=WORKOS_REDIRECT_URI,
        state=state,
    )


def logout_url(*, session_id: str | None = None) -> str:
    """Build the WorkOS hosted-logout URL.

    Uses `api.workos.com` directly. A tenant-specific `<team>.authkit.com`
    URL only exists when the paid custom-domain feature is enabled, which
    is deliberately skipped — see docs/workos-setup.md.

    Without a `session_id` WorkOS logs the user out of its own session
    but can't tell which device's session is being revoked.
    """
    base = "https://api.workos.com/user_management/sessions/logout"
    return f"{base}?session_id={session_id}" if session_id else base


def exchange_code(code: str) -> dict:
    return _to_session(_get_client().user_management.authenticate_with_code(code=code))


def refresh(refresh_token: str) -> dict:
    return _to_session(_get_client().user_management.authenticate_with_refresh_token(
        refresh_token=refresh_token,
    ))


def _to_session(response) -> dict:
    user = response.user
    return {
        "user": {
            "id": user.id,
            "email": user.email,
            "first_name": getattr(user, "first_name", None),
            "last_name": getattr(user, "last_name", None),
        },
        "access_token": response.access_token,
        "refresh_token": response.refresh_token,
    }


def verify_access_token(token: str) -> dict:
    """Verify a WorkOS access-token JWT and return its claims.

    Raises `jwt.InvalidTokenError` on any failure (signature, expiry,
    client_id mismatch). Caller decides whether to refresh or reject.
    """
    signing_key = _get_jwks().get_signing_key_from_jwt(token).key
    claims = jwt.decode(
        token, signing_key, algorithms=["RS256"],
        # The `iss` value has churned across WorkOS API versions; rely on
        # signature + the explicit client_id check below instead.
        options={"verify_aud": False, "verify_iss": False},
    )
    if claims.get("client_id") and claims["client_id"] != WORKOS_CLIENT_ID:
        raise jwt.InvalidTokenError("client_id claim mismatch")
    return claims


def verify_webhook(raw_body: bytes, signature_header: str):
    """Verify a WorkOS webhook signature; return the typed event payload."""
    return _get_client().webhooks.verify_event(
        event_body=raw_body,
        event_signature=signature_header,
        secret=WORKOS_WEBHOOK_SECRET,
    )


# ---------------------------------------------------------------------------
# Cookie + DB helpers
# ---------------------------------------------------------------------------

def _set_session_cookies(response, *, access_token: str, refresh_token: str) -> None:
    common = dict(httponly=True, secure=True, samesite="Lax", path="/")
    response.set_cookie(ACCESS_TOKEN_COOKIE,  access_token,  max_age=REFRESH_COOKIE_MAX_AGE, **common)
    response.set_cookie(REFRESH_TOKEN_COOKIE, refresh_token, max_age=REFRESH_COOKIE_MAX_AGE, **common)


def _clear_session_cookies(response) -> None:
    common = dict(path="/", samesite="Lax", secure=True)
    for name in (ACCESS_TOKEN_COOKIE, REFRESH_TOKEN_COOKIE, STATE_COOKIE, RETURN_TO_COOKIE):
        response.delete_cookie(name, **common)


def _set_transient_cookie(response, name: str, value: str) -> None:
    response.set_cookie(
        name, value,
        max_age=TRANSIENT_COOKIE_MAX_AGE,
        httponly=True, secure=True, samesite="Lax", path="/",
    )


def _upsert_user(user: dict) -> None:
    """Insert or update a row in the local `users` table from JWT claims.

    Webhooks are eventually consistent — we can't rely on `user.created`
    having arrived before the user's first authenticated request.
    """
    with db.get_conn() as conn:
        db.upsert(conn, "users", ["workos_user_id"], [{
            "workos_user_id": user["id"],
            "email": user.get("email") or "",
            "first_name": user.get("first_name"),
            "last_name": user.get("last_name"),
        }])


def _is_public_path(path: str) -> bool:
    return path in _PUBLIC_EXACT_PATHS or any(path.startswith(p) for p in _PUBLIC_PATH_PREFIXES)


def _load_user_record(workos_user_id: str) -> dict | None:
    """Read the cached user fields (email, name) from the local users table.

    WorkOS access-token JWTs only carry `sub` + `sid`; profile fields
    (email, first/last name) live in the API response from the OAuth
    code-exchange and the webhook payload, both of which we mirror into
    `users` via `_upsert_user`. The auth gate consults that mirror so
    every authenticated request has access to the email + name without
    a round-trip to WorkOS.
    """
    with db.get_conn() as conn:
        row = conn.execute(
            text(
                "SELECT email, first_name, last_name FROM users"
                " WHERE workos_user_id = :uid"
            ),
            {"uid": workos_user_id},
        ).mappings().first()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Flask blueprint — middleware + routes
# ---------------------------------------------------------------------------

bp = Blueprint("auth", __name__)


@bp.before_app_request
def _auth_gate():
    if not WORKOS_ENABLED or _is_public_path(request.path):
        return None

    access_token = request.cookies.get(ACCESS_TOKEN_COOKIE)
    refresh_token = request.cookies.get(REFRESH_TOKEN_COOKIE)
    refreshed: dict | None = None
    claims: dict | None = None

    if access_token:
        try:
            claims = verify_access_token(access_token)
        except jwt.InvalidTokenError:
            claims = None

    if claims is None and refresh_token:
        try:
            refreshed = refresh(refresh_token)
            claims = verify_access_token(refreshed["access_token"])
        except Exception as exc:
            current_app.logger.info("WorkOS refresh failed: %s", exc)
            refreshed = None
            claims = None

    if claims is None:
        # Kick the user to AuthKit. Stash where they were heading so the
        # callback can land them back there.
        state = random_state()
        resp = make_response(redirect(authorization_url(state=state)))
        _set_transient_cookie(resp, STATE_COOKIE, state)
        if request.method == "GET":
            _set_transient_cookie(resp, RETURN_TO_COOKIE, request.full_path)
        return resp

    g.user_id = claims["sub"]
    record = _load_user_record(claims["sub"]) or {}
    g.user = {
        "id": claims["sub"],
        "email": record.get("email") or "",
        "first_name": record.get("first_name"),
        "last_name": record.get("last_name"),
        "session_id": claims.get("sid"),
    }
    if refreshed is not None:
        g._refresh_pair = refreshed


@bp.after_app_request
def _persist_refreshed_tokens(response):
    pair = getattr(g, "_refresh_pair", None)
    if pair:
        _set_session_cookies(
            response,
            access_token=pair["access_token"],
            refresh_token=pair["refresh_token"],
        )
    return response


@bp.route("/auth/login")
def login():
    if not WORKOS_ENABLED:
        abort(404)
    state = random_state()
    resp = make_response(redirect(authorization_url(state=state)))
    _set_transient_cookie(resp, STATE_COOKIE, state)
    return_to = request.args.get("return_to", "").strip()
    if return_to.startswith("/"):
        _set_transient_cookie(resp, RETURN_TO_COOKIE, return_to)
    return resp


@bp.route("/auth/callback")
def callback():
    if not WORKOS_ENABLED:
        abort(404)
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    expected_state = request.cookies.get(STATE_COOKIE, "")
    if not code or not state or state != expected_state:
        flash("Login failed — invalid state. Please try again.")
        return redirect(url_for("auth.login"))

    try:
        session = exchange_code(code)
    except Exception as exc:
        current_app.logger.exception("WorkOS code exchange failed")
        flash(f"Login failed: {exc}")
        return redirect(url_for("auth.login"))

    inv.init_schema()
    _upsert_user(session["user"])

    return_to = request.cookies.get(RETURN_TO_COOKIE, "")
    if not return_to.startswith("/"):
        return_to = "/"

    resp = make_response(redirect(return_to))
    _set_session_cookies(resp,
                         access_token=session["access_token"],
                         refresh_token=session["refresh_token"])
    common = dict(path="/", samesite="Lax", secure=True)
    resp.delete_cookie(STATE_COOKIE, **common)
    resp.delete_cookie(RETURN_TO_COOKIE, **common)
    return resp


@bp.route("/auth/logout", methods=["GET", "POST"])
def logout():
    if not WORKOS_ENABLED:
        abort(404)
    session_id = None
    access_token = request.cookies.get(ACCESS_TOKEN_COOKIE)
    if access_token:
        try:
            session_id = verify_access_token(access_token).get("sid")
        except jwt.InvalidTokenError:
            session_id = None
    resp = make_response(redirect(logout_url(session_id=session_id)))
    _clear_session_cookies(resp)
    return resp


@bp.route("/auth/me")
def me():
    if not WORKOS_ENABLED:
        abort(404)
    user = getattr(g, "user", None)
    if not user:
        return jsonify({"authenticated": False}), 401
    return jsonify({"authenticated": True, "user": user})


@bp.route("/webhooks/workos", methods=["POST"])
def webhook():
    if not WORKOS_ENABLED:
        abort(404)
    sig = request.headers.get("WorkOS-Signature", "")
    if not sig:
        return jsonify({"ok": False, "error": "missing signature"}), 400
    try:
        event = verify_webhook(request.get_data(cache=True), sig)
    except Exception as exc:
        current_app.logger.warning("Webhook verification failed: %s", exc)
        return jsonify({"ok": False, "error": "signature verification failed"}), 401

    inv.init_schema()
    data = event.data
    user_id = data.id
    if event.event == "user.deleted":
        with db.get_conn() as conn:
            conn.execute(text("DELETE FROM inventory WHERE user_id = :uid"), {"uid": user_id})
            conn.execute(text("DELETE FROM users WHERE workos_user_id = :uid"), {"uid": user_id})
    elif event.event in ("user.created", "user.updated"):
        with db.get_conn() as conn:
            db.upsert(conn, "users", ["workos_user_id"], [{
                "workos_user_id": user_id,
                "email": getattr(data, "email", "") or "",
                "first_name": getattr(data, "first_name", None),
                "last_name": getattr(data, "last_name", None),
            }])
    return jsonify({"ok": True}), 200
