import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

import mtgcompare.auth as auth_module
import mtgcompare.db as db_module
from mtgcompare import web


def test_decklist_search_rejects_oversized_lists():
    """Total card count above MAX_DECKLIST_CARDS should short-circuit
    with a clear error rather than fanning out shop scrapes."""
    web.app.config["WTF_CSRF_ENABLED"] = False
    over = web.MAX_DECKLIST_CARDS + 1
    body = f"{over} Sol Ring\n"  # one line, but qty exceeds the cap
    with web.app.test_client() as client:
        resp = client.post("/decklist", data={"decklist": body})
    assert resp.status_code == 200
    page = resp.data.decode()
    assert f"{over} cards" in page
    assert "limit is 100" in page


def test_decklist_search_accepts_at_cap():
    """Exactly MAX_DECKLIST_CARDS is allowed; the request reaches the FX
    fetch path (which we stub to return None to skip the actual scrape)."""
    web.app.config["WTF_CSRF_ENABLED"] = False
    body = f"{web.MAX_DECKLIST_CARDS} Sol Ring\n"
    # Stubbing FX out short-circuits with a different error; the point is
    # that the size cap doesn't fire.
    original = web._get_fx
    web._get_fx = lambda: None
    try:
        with web.app.test_client() as client:
            resp = client.post("/decklist", data={"decklist": body})
        assert resp.status_code == 200
        page = resp.data.decode()
        assert "limit is 100" not in page
    finally:
        web._get_fx = original


def test_parse_decklist_skips_headers_and_comments():
    text = """
    // comment
    Commander:
    1 Sol Ring
    4x Force of Will (ALL)
    Sideboard:
    # another comment
    2 Rhystic Study (C21) 79
    """

    assert web._parse_decklist(text) == [
        (1, "Sol Ring"),
        (4, "Force of Will"),
        (2, "Rhystic Study"),
    ]


def test_parse_shipping_overrides_clamps_and_falls_back_to_defaults():
    source = {
        "ship_hareruya": "500",
        "ship_singlestar": "-5",
        "ship_tcgplayer_scryfall": "oops",
    }

    overrides = web._parse_shipping_overrides(source)

    assert overrides["Hareruya"] == 500
    assert overrides["SingleStar"] == 0
    assert overrides["TokyoMTG"] == web.SHIPPING_JPY["TokyoMTG"]
    assert overrides["TCGPlayer (Scryfall)"] == web.SHIPPING_JPY["TCGPlayer (Scryfall)"]


def test_normalize_set_code_and_foil_helpers():
    assert web._normalize_set_code("neo_123") == "neo"
    assert web._normalize_set_code("neo_123", upper=True) == "NEO"
    assert web._normalize_set_code(None) == ""
    assert web._is_foil("Foil") is True
    assert web._is_foil("Normal") is False


def test_deduct_inventory_empty_inventory():
    name_qty = {"sol ring": 4, "force of will": 2}
    inv_qty, needed = web._deduct_inventory(name_qty, {})
    assert inv_qty == {"sol ring": 0, "force of will": 0}
    assert needed == {"sol ring": 4, "force of will": 2}


def test_deduct_inventory_full_coverage():
    name_qty = {"sol ring": 2, "rhystic study": 1}
    inv_map = {"sol ring": 5, "rhystic study": 3}
    inv_qty, needed = web._deduct_inventory(name_qty, inv_map)
    assert inv_qty == {"sol ring": 2, "rhystic study": 1}
    assert needed == {"sol ring": 0, "rhystic study": 0}


def test_deduct_inventory_partial_coverage():
    name_qty = {"force of will": 4}
    inv_map = {"force of will": 2}
    inv_qty, needed = web._deduct_inventory(name_qty, inv_map)
    assert inv_qty == {"force of will": 2}
    assert needed == {"force of will": 2}


def test_deduct_inventory_excess_inventory_is_capped():
    # Having more copies than requested should never produce negative need
    name_qty = {"lightning bolt": 1}
    inv_map = {"lightning bolt": 99}
    inv_qty, needed = web._deduct_inventory(name_qty, inv_map)
    assert inv_qty["lightning bolt"] == 1
    assert needed["lightning bolt"] == 0


def test_deduct_inventory_case_insensitive_matching():
    # Inventory names are lowercased before building inv_map; decklist keys
    # are also lowercased — so mixed-case variants must match.
    name_qty = {"counterspell": 3}
    inv_map = {"counterspell": 1}   # already lowercased by the caller
    inv_qty, needed = web._deduct_inventory(name_qty, inv_map)
    assert inv_qty["counterspell"] == 1
    assert needed["counterspell"] == 2


def test_deduct_inventory_unrelated_inventory_cards_ignored():
    name_qty = {"sol ring": 1}
    inv_map = {"black lotus": 10, "mox pearl": 4}
    inv_qty, needed = web._deduct_inventory(name_qty, inv_map)
    assert inv_qty["sol ring"] == 0
    assert needed["sol ring"] == 1


def test_deduct_inventory_multiple_lots_aggregated():
    # The caller sums quantities across lots before passing inv_map; verify
    # the helper handles already-aggregated values correctly.
    name_qty = {"dark ritual": 4}
    inv_map = {"dark ritual": 3}    # 2 lots of 1 + 1 lot of 2, pre-summed
    inv_qty, needed = web._deduct_inventory(name_qty, inv_map)
    assert inv_qty["dark ritual"] == 3
    assert needed["dark ritual"] == 1


def test_fetch_card_prices_uses_shared_collector(monkeypatch):
    expected = [{"shop": "Test Shop", "price_jpy": 100}]

    def fake_collect_prices(card_name, fx, logger=None):
        assert card_name == "Force of Will"
        assert fx == 150.0
        assert logger is web.app.logger
        return expected

    monkeypatch.setattr(web, "collect_prices", fake_collect_prices)

    assert web._fetch_card_prices("Force of Will", 150.0) == expected


def test_history_cutoff_for_known_period():
    now = datetime(2026, 4, 22, tzinfo=timezone.utc)
    assert web._history_cutoff("1m", now=now) == datetime(2026, 3, 23, tzinfo=timezone.utc)
    assert web._history_cutoff("all", now=now) is None


def test_slice_history_filters_by_period():
    now = datetime(2026, 4, 22, tzinfo=timezone.utc)
    points = [
        {"price_usd": 2.0, "fetched_at": "2026-03-01T00:00:00+00:00"},
        {"price_usd": 3.0, "fetched_at": "2026-03-25T00:00:00+00:00"},
        {"price_usd": 4.0, "fetched_at": "2026-04-20T00:00:00+00:00"},
    ]

    assert web._slice_history(points, "1m", now=now) == points[1:]
    assert web._slice_history(points, "all", now=now) == points


def test_densify_daily_points_fills_gaps():
    points = {
        "2026-04-20": 3.0,
        "2026-04-22": 5.0,
    }

    assert web._densify_daily_points(points) == [
        {"market_date": "2026-04-20", "price_usd": 3.0},
        {"market_date": "2026-04-21", "price_usd": None},
        {"market_date": "2026-04-22", "price_usd": 5.0},
    ]


def test_mtgjson_set_candidates_include_trimmed_variants():
    assert web._mtgjson_set_candidates("FMB1")[:2] == ["FMB1", "FMB"]


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


def test_get_user_id_defaults_to_anonymous_when_header_absent(monkeypatch):
    monkeypatch.setattr(db_module, "IS_POSTGRES", True)
    with web.app.test_request_context("/"):
        assert web._get_user_id() == "anonymous"


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


# ---------------------------------------------------------------------------
# /healthz + webhook handler
# ---------------------------------------------------------------------------

def test_healthz_returns_200_without_auth(monkeypatch):
    monkeypatch.setattr(auth_module, "WORKOS_ENABLED", True)
    with web.app.test_client() as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}


def test_webhook_rejects_missing_signature(monkeypatch):
    monkeypatch.setattr(auth_module, "WORKOS_ENABLED", True)
    with web.app.test_client() as client:
        resp = client.post("/webhooks/workos", data=b"{}", content_type="application/json")
        assert resp.status_code == 400


def test_webhook_rejects_bad_signature(monkeypatch):
    monkeypatch.setattr(auth_module, "WORKOS_ENABLED", True)

    def fake_verify(raw_body, sig):
        raise ValueError("signature mismatch")

    monkeypatch.setattr(auth_module, "verify_webhook", fake_verify)
    with web.app.test_client() as client:
        resp = client.post(
            "/webhooks/workos", data=b"{}",
            content_type="application/json",
            headers={"WorkOS-Signature": "t=1,v1=bogus"},
        )
        assert resp.status_code == 401


def test_csrf_protection_blocks_post_without_token():
    """flask-wtf is disabled in conftest for ergonomics; flip it on for
    one test to verify the protection actually fires."""
    web.app.config["WTF_CSRF_ENABLED"] = True
    try:
        with web.app.test_client() as client:
            resp = client.post("/decklist", data={"decklist": "1 Sol Ring"})
        assert resp.status_code == 400
    finally:
        web.app.config["WTF_CSRF_ENABLED"] = False


def test_csrf_exempt_for_cron_and_webhook(monkeypatch):
    """The cron-trigger endpoint and the WorkOS webhook must remain reachable
    without a CSRF token — they have their own auth (bearer + HMAC)."""
    web.app.config["WTF_CSRF_ENABLED"] = True
    try:
        with web.app.test_client() as client:
            # cron rejects on auth, NOT on CSRF (would be 400 if CSRF blocked it)
            resp = client.post("/internal/cron/update-prices")
            assert resp.status_code in (401, 200)  # 401 if CRON_SECRET set, 200 otherwise
            # webhook rejects on missing signature, NOT on CSRF
            monkeypatch.setattr(auth_module, "WORKOS_ENABLED", True)
            resp = client.post("/webhooks/workos", data=b"{}",
                               content_type="application/json")
            assert resp.status_code == 400  # missing signature, not CSRF
    finally:
        web.app.config["WTF_CSRF_ENABLED"] = False


def test_security_headers_set_on_responses():
    """Every response must carry the hardening headers — including the
    public /healthz, which renders without authentication."""
    with web.app.test_client() as client:
        resp = client.get("/healthz")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "max-age=" in resp.headers["Strict-Transport-Security"]
    csp = resp.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp
    assert "default-src 'self'" in csp
    assert "https://api.workos.com" in csp


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


def test_upsert_user_advances_updated_at_on_repeat():
    """`updated_at` must move forward when `_upsert_user` is called for an
    existing row (e.g. via a `user.updated` webhook). The column's PG
    `server_default=func.now()` only fires on INSERT, so the helper has
    to pass the timestamp itself.
    """
    db_module.init_schema()
    auth_module._upsert_user({"id": "user_TS", "email": "a@example.com"})
    with db_module.get_conn() as conn:
        first = conn.execute(text(
            "SELECT updated_at FROM users WHERE workos_user_id = 'user_TS'"
        )).scalar_one()
    time.sleep(0.05)
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


# ---------------------------------------------------------------------------
# _populate_market_prices_from_history — UUID type normalisation
# ---------------------------------------------------------------------------

def test_populate_market_prices_handles_postgres_types(monkeypatch):
    # Simulates psycopg2 behaviour on first run:
    #   - price_rows.uuid is UUID type  → psycopg2 returns uuid.UUID objects
    #   - price_rows.price_usd is NUMERIC → psycopg2 returns decimal.Decimal
    #   - card_maps UUID is a plain str   → from MTGJSON JSON on first load
    # Both mismatches must be resolved so price_usd is stored as a plain float.
    raw_uuid = "df0e1ede-b627-5b95-8eed-0ce7d08a897b"
    card_maps = [("Sol Ring", "C21", "", 0, raw_uuid, "2026-04-29T00:00:00+00:00")]

    captured = {}

    def fake_get_conn():
        import contextlib

        class FakeConn:
            def execute(self, stmt, params=None):
                class FakeResult:
                    def fetchall(self_):
                        return [(uuid.UUID(raw_uuid), "normal", Decimal("1.23"))]
                return FakeResult()

        @contextlib.contextmanager
        def _ctx():
            yield FakeConn()

        return _ctx()

    import mtgcompare.db as db_mod

    monkeypatch.setattr(db_mod, "IS_POSTGRES", True)
    monkeypatch.setattr(db_mod, "get_conn", fake_get_conn)
    monkeypatch.setattr(db_mod, "upsert", lambda conn, table, cols, rows: captured.update({"rows": rows}))

    web._populate_market_prices_from_history(card_maps, None, "2026-04-29T00:00:00+00:00")

    assert captured["rows"][0]["price_usd"] == pytest.approx(1.23)
    assert isinstance(captured["rows"][0]["price_usd"], float)

