"""App-wide web behaviour: /healthz, webhook signature checks, CSRF, security headers."""


import mtgcompare.auth as auth_module
from mtgcompare import web

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
