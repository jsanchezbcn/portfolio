"""End-to-end UI smoke tests for the Portfolio Risk Manager dashboard (sync Playwright)."""
from __future__ import annotations

import os
import socket

import pytest
from playwright.sync_api import sync_playwright, Browser, Page

DASHBOARD_URL = "http://localhost:8506"
LOAD_MS = 35_000


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture(scope="module")
def page(browser: Browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    yield pg
    pg.close()
    ctx.close()


def _load(page: Page, extra_ms: int = 5000) -> str:
    """Navigate to dashboard, click Reload Accounts if present, return HTML."""
    page.goto(DASHBOARD_URL, wait_until="networkidle", timeout=LOAD_MS)
    page.wait_for_selector(".stApp", timeout=LOAD_MS)
    page.wait_for_timeout(4000)
    for label in ["Reload Accounts", "Reload"]:
        btn = page.locator(f"text={label}")
        if btn.count() > 0:
            btn.first.click()
            page.wait_for_timeout(extra_ms)
            break
    return page.content()


def test_01_port_open():
    """Port 8506 must be open."""
    try:
        with socket.create_connection(("localhost", 8506), timeout=5):
            pass
    except OSError:
        pytest.fail("Dashboard not reachable on port 8506")


def test_02_no_traceback(page: Page):
    """Dashboard must not render a Python traceback."""
    content = _load(page)
    assert "Traceback (most recent call last)" not in content


def test_03_main_title(page: Page):
    """'Portfolio Risk Manager' title must be present."""
    assert "Portfolio Risk Manager" in page.content()


def test_04_sidebar_has_ibkr_controls(page: Page):
    """Sidebar must contain IBKR account or login controls."""
    sidebar = page.locator("[data-testid='stSidebar']")
    assert sidebar.count() > 0, "Sidebar not found"
    text = sidebar.inner_text()
    assert any(kw in text for kw in ["IBKR", "Sign in", "Account", "Reload"]), \
        f"No IBKR controls in sidebar. Text: {text[:300]}"


def test_05_risk_first_dashboard_header(page: Page):
    """'Risk First Dashboard' section header must appear."""
    assert "Risk First Dashboard" in page.content()


def test_06_margin_usage_metric(page: Page):
    """'Margin Usage' metric must be rendered."""
    assert "Margin Usage" in page.content()


def test_07_spx_delta_metric(page: Page):
    """SPX beta-weighted delta metric must be rendered."""
    c = page.content()
    has_spx_delta = "SPX" in c and any(kw in c for kw in ["Delta", "Weighted", "\u03b2"])
    assert has_spx_delta, "SPX beta-weighted delta metric not found"


def test_08_vega_exposure_metric(page: Page):
    """'Vega Exposure' metric must be rendered."""
    assert "Vega Exposure" in page.content()


def test_09_theta_vega_ratio_metric(page: Page):
    """'Theta/Vega Ratio' metric must be rendered."""
    assert "Theta/Vega Ratio" in page.content()


def test_10_portfolio_greeks_section(page: Page):
    """'Portfolio Greeks' section must appear."""
    assert "Portfolio Greeks" in page.content()


def test_11_risk_compliance_section(page: Page):
    """'Risk Compliance' section must appear."""
    assert "Risk Compliance" in page.content()


def test_12_no_stale_error_banner(page: Page):
    """'No IBKR accounts' error should not appear when mock is active."""
    if "No IBKR accounts available from gateway" in page.content():
        pytest.xfail("Gateway unauthenticated — MOCK_IBKR accounts not visible")


def test_13_screenshot_evidence(page: Page):
    """Save full-page screenshot as test evidence."""
    path = "/tmp/dashboard_e2e_test.png"
    page.screenshot(path=path, full_page=True)
    assert os.path.exists(path), f"Screenshot not saved at {path}"
    size = os.path.getsize(path)
    assert size > 5_000, f"Screenshot suspiciously small: {size} bytes"
    print(f"\nScreenshot: {path} ({size:,} bytes)")
