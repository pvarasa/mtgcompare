"""Every page runs under a script-src without 'unsafe-inline': any inline
script or handler left in a template would be blocked and break the page
silently, so fail on any CSP violation the browser reports."""
from __future__ import annotations

import pytest

import mtgcompare.web as web_module

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("path", ["/", "/?q=", "/inventory", "/market", "/sealed", "/no-such-page"])
def test_page_has_no_csp_violations(e2e_base_url, clean_inventory, monkeypatch, page, path):
    monkeypatch.setattr(web_module, "_get_fx", lambda: 150.0)
    violations: list[str] = []
    page.on("console", lambda msg: violations.append(msg.text)
            if "Content Security Policy" in msg.text else None)
    page.goto(f"{e2e_base_url}{path}")
    page.wait_for_load_state("networkidle")
    assert violations == []


def test_error_page_back_button(e2e_base_url, page):
    page.goto(f"{e2e_base_url}/sealed")
    page.goto(f"{e2e_base_url}/no-such-page")
    page.click("button[data-action=back]")
    page.wait_for_url(f"{e2e_base_url}/sealed", timeout=3000)
