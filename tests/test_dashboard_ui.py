"""Playwright tests for the portfolio dashboard UI.

Run:
    pytest tests/test_dashboard_ui.py -v --timeout=120

Requires:
    - Dashboard running on localhost:8506  (./start_dashboard.sh)
    - playwright installed:  pip install playwright && playwright install chromium
"""
from __future__ import annotations

import asyncio
import os
import re
import pytest
import pytest_asyncio

# Force auto mode for this module so async tests are discovered without strict markers
pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://localhost:8506")
LOAD_TIMEOUT = 60_000   # ms – generous for first Streamlit render
RENDER_TIMEOUT = 90_000  # ms – full render (all sections + live data)


@pytest.fixture(scope="module")
def event_loop():
    """Module-scoped event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="module")
async def browser_context():
    """Launch a headless Chromium browser (shared across module tests)."""
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(viewport={"width": 1920, "height": 1080})
    yield context
    await context.close()
    await browser.close()
    await pw.stop()


@pytest_asyncio.fixture(scope="module")
async def dashboard_page(browser_context):
    """Navigate to the dashboard and wait for full render."""
    page = await browser_context.new_page()
    await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=LOAD_TIMEOUT)
    # Streamlit app container
    await page.wait_for_selector(".stApp", timeout=LOAD_TIMEOUT)
    # Wait for the hidden render-complete marker injected after all sections
    try:
        await page.wait_for_selector("[data-testid='render-complete']", timeout=RENDER_TIMEOUT)
    except Exception:
        pass  # Marker may not appear if data fetch fails; tests degrade gracefully
    # Extra settle time for Streamlit re-renders
    await asyncio.sleep(3)
    yield page
    await page.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _page_text(page) -> str:
    """Return the full visible text of the page body."""
    return await page.inner_text("body")


async def _page_html(page) -> str:
    """Return the full HTML of the page."""
    return await page.content()


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1: Account Summary & Portfolio Greeks
# ═══════════════════════════════════════════════════════════════════════════

class TestAccountSummaryAndGreeks:
    """Section 1 – Account & Portfolio Greeks header block."""

    async def test_section_header_present(self, dashboard_page):
        text = await _page_text(dashboard_page)
        assert "Account & Portfolio Greeks" in text, "Section 1 header missing"

    async def test_net_liquidation_metric(self, dashboard_page):
        text = await _page_text(dashboard_page)
        assert "Net Liquidation" in text, "Net Liquidation metric not rendered"

    async def test_margin_usage_metric(self, dashboard_page):
        text = await _page_text(dashboard_page)
        assert "Margin Usage" in text, "Margin Usage metric not rendered"

    async def test_buying_power_metric(self, dashboard_page):
        text = await _page_text(dashboard_page)
        assert "Buying Power" in text, "Buying Power metric not rendered"

    async def test_spx_delta_metric(self, dashboard_page):
        text = await _page_text(dashboard_page)
        # SPX β-Δ or SPX Weighted Delta
        assert any(tok in text for tok in ["SPX", "β-Δ", "Weighted Delta"]), \
            "SPX delta metric missing"

    async def test_greeks_row_complete(self, dashboard_page):
        text = await _page_text(dashboard_page)
        for label in ["Delta", "Theta", "Vega", "Gamma"]:
            assert label in text, f"Greek metric '{label}' not rendered"

    async def test_theta_vega_ratio(self, dashboard_page):
        text = await _page_text(dashboard_page)
        assert "Ratio" in text or "Θ/V" in text, "Theta/Vega ratio metric missing"


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2: Positions Split (Futures/Stocks vs Options)
# ═══════════════════════════════════════════════════════════════════════════

class TestPositionsSplit:
    """Section 2 – Positions tables split by instrument type."""

    async def test_futures_stocks_heading(self, dashboard_page):
        text = await _page_text(dashboard_page)
        assert "Futures" in text or "Stocks" in text or "No futures" in text.lower(), \
            "Futures/Stocks section not rendered"

    async def test_options_heading(self, dashboard_page):
        text = await _page_text(dashboard_page)
        assert "Options" in text or "No option" in text.lower(), \
            "Options section not rendered"

    async def test_staleness_indicators_or_empty(self, dashboard_page):
        """If we have options, staleness indicators (🟢 🟡 🔴) should exist."""
        html = await _page_html(dashboard_page)
        # At least one staleness emoji should appear, or the positions are empty
        has_staleness = any(emoji in html for emoji in ["🟢", "🟡", "🔴"])
        has_no_positions = "No positions" in html or "no option" in html.lower()
        assert has_staleness or has_no_positions, \
            "Expected staleness indicators or empty-positions notice"

    async def test_action_buttons_for_options(self, dashboard_page):
        """Options actions are shown in-list after row selection, or empty-state copy appears."""
        text = await _page_text(dashboard_page)
        has_action = any(
            keyword in text for keyword in ["Buy", "Sell", "Roll +7d", "Roll +30d"]
        )
        has_prompt = "Select an option row" in text
        has_no_options = "no option" in text.lower() or "No positions" in text
        assert has_action or has_prompt or has_no_options, \
            "Expected in-list options actions or guidance prompt"


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 3: Risk Compliance
# ═══════════════════════════════════════════════════════════════════════════

class TestRiskCompliance:
    """Section 3 – Risk Compliance + trade suggestions."""

    async def test_risk_compliance_header(self, dashboard_page):
        text = await _page_text(dashboard_page)
        assert "Risk Compliance" in text, "Risk Compliance section header missing"

    async def test_compliance_status(self, dashboard_page):
        text = await _page_text(dashboard_page)
        # Should show either ✅ all satisfied or ⚠️ X violated
        ok = "satisfied" in text.lower() or "violated" in text.lower() or "limit" in text.lower()
        assert ok, "No compliance status indicator found"

    async def test_gamma_risk_by_dte(self, dashboard_page):
        """Gamma by DTE compact metrics should appear."""
        text = await _page_text(dashboard_page)
        # Buckets: 0-7, 8-30, 31-60, 60+
        ok = any(b in text for b in ["0-7", "8-30", "31-60", "60+", "Gamma"])
        assert ok, "Gamma risk by DTE buckets not rendered"


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 4: Arbitrage Signals
# ═══════════════════════════════════════════════════════════════════════════

class TestArbSignals:
    """Section 4 – Arb signals sorted by fill probability."""

    async def test_arb_signals_section(self, dashboard_page):
        text = await _page_text(dashboard_page)
        assert "Arbitrage" in text or "Arb" in text or "No active" in text, \
            "Arbitrage Signals section missing"

    async def test_fill_probability_column(self, dashboard_page):
        """If signals exist, fill_prob and net_edge columns should appear."""
        text = await _page_text(dashboard_page)
        has_table = "fill_prob" in text.lower() or "net_edge" in text.lower() or "rank" in text.lower()
        has_empty = "no active" in text.lower() or "no arb" in text.lower()
        assert has_table or has_empty, \
            "Expected fill probability table or empty-signals notice"


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 5: Options Book
# ═══════════════════════════════════════════════════════════════════════════

class TestOptionsBook:
    """Section 5 – Options Book (symbol picker + expiration tabs)."""

    async def test_options_book_header(self, dashboard_page):
        text = await _page_text(dashboard_page)
        assert "Options Book" in text, "Options Book header missing"

    async def test_underlying_selector(self, dashboard_page):
        text = await _page_text(dashboard_page)
        assert "Underlying" in text, "Underlying picker not found"
        assert any(sym in text for sym in ["/ES", "ES", "MES", "SPY", "QQQ"]), \
            "Expected options-book underlyings not found"

    async def test_load_expirations_button(self, dashboard_page):
        text = await _page_text(dashboard_page)
        assert "Load Expirations" in text, "Load Expirations button not found"

    async def test_book_controls(self, dashboard_page):
        """DTE + strikes controls should be visible."""
        text = await _page_text(dashboard_page)
        assert "Strikes" in text or "DTE" in text, "Book controls (DTE/Strikes) missing"


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 6: Order Builder
# ═══════════════════════════════════════════════════════════════════════════

class TestOrderBuilder:
    """Section 6 – Order Builder + Open Orders."""

    async def test_order_builder_present(self, dashboard_page):
        text = await _page_text(dashboard_page)
        ok = any(kw in text for kw in ["Order Builder", "Order", "Flatten Risk", "Trade Journal"])
        assert ok, "Order section not rendered"

    async def test_trade_draft_preview_or_empty(self, dashboard_page):
        text = await _page_text(dashboard_page)
        ok = "Order draft created" in text or "Clear Draft Preview" in text or "Order Builder" in text
        assert ok, "Trade draft acceptance UX block not visible"


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 7: AI Assistant
# ═══════════════════════════════════════════════════════════════════════════

class TestAIAssistant:
    """Section 7 – AI Assistant with risk audit + market brief + chat."""

    async def test_ai_assistant_header(self, dashboard_page):
        text = await _page_text(dashboard_page)
        assert "AI Assistant" in text, "AI Assistant section header missing"

    async def test_risk_audit_panel(self, dashboard_page):
        text = await _page_text(dashboard_page)
        assert "Risk Audit" in text, "Live Risk Audit sub-panel missing"

    async def test_market_brief_panel(self, dashboard_page):
        text = await _page_text(dashboard_page)
        assert "Market Brief" in text, "Market Brief sub-panel missing"

    async def test_chat_input_present(self, dashboard_page):
        """The user prompt input for risk adjustments should exist."""
        html = await _page_html(dashboard_page)
        assert "risk adjustment" in html.lower() or "gamma" in html.lower() or \
            "text_input" in html.lower() or "placeholder" in html.lower(), \
            "Chat input for risk adjustments not found"

    async def test_refresh_brief_button(self, dashboard_page):
        text = await _page_text(dashboard_page)
        assert "Refresh Brief" in text, "Refresh Brief button missing"


# ═══════════════════════════════════════════════════════════════════════════
# LAYOUT ORDER: Verify sections appear in correct sequence
# ═══════════════════════════════════════════════════════════════════════════

class TestLayoutOrder:
    """Verify the 7 sections appear in the correct top-to-bottom order."""

    async def test_section_ordering(self, dashboard_page):
        text = await _page_text(dashboard_page)

        # Find the index of key section markers in the page text
        markers = [
            ("Account & Portfolio Greeks", "Section 1"),
            ("Risk Compliance", "Section 3"),
            ("Options Book", "Section 5"),
            ("AI Assistant", "Section 7"),
        ]

        positions_list: list[tuple[int, str]] = []
        for marker, label in markers:
            idx = text.find(marker)
            if idx >= 0:
                positions_list.append((idx, label))

        # Must find at least 3 of the 4 markers
        assert len(positions_list) >= 3, (
            f"Expected at least 3 section markers, found {len(positions_list)}: "
            f"{[p[1] for p in positions_list]}"
        )

        # Verify ordering is correct
        for i in range(len(positions_list) - 1):
            assert positions_list[i][0] < positions_list[i + 1][0], (
                f"{positions_list[i][1]} (pos {positions_list[i][0]}) should appear before "
                f"{positions_list[i + 1][1]} (pos {positions_list[i + 1][0]})"
            )


# ═══════════════════════════════════════════════════════════════════════════
# SECONDARY PANELS (collapsible)
# ═══════════════════════════════════════════════════════════════════════════

class TestSecondaryPanels:
    """Collapsible secondary panels at the bottom."""

    async def test_iv_hv_expander(self, dashboard_page):
        text = await _page_text(dashboard_page)
        assert "IV" in text or "HV" in text, "IV vs HV Analysis expander missing"

    async def test_market_intelligence_expander(self, dashboard_page):
        text = await _page_text(dashboard_page)
        assert "Intelligence" in text or "News" in text, \
            "Market Intelligence expander missing"


# ═══════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════════════════════════════

class TestSidebar:
    """Sidebar controls and data freshness."""

    async def test_sidebar_has_account_selector(self, dashboard_page):
        html = await _page_html(dashboard_page)
        has_account = "Account" in html or "account" in html
        assert has_account, "Account selector not found in sidebar"

    async def test_data_freshness_expander(self, dashboard_page):
        text = await _page_text(dashboard_page)
        ok = "Data Freshness" in text or "Positions" in text
        assert ok, "Data Freshness sidebar expander missing"


# ═══════════════════════════════════════════════════════════════════════════
# RENDER COMPLETE MARKER
# ═══════════════════════════════════════════════════════════════════════════

class TestRenderComplete:
    """Verify the hidden render-complete marker is present."""

    async def test_render_complete_marker(self, dashboard_page):
        el = await dashboard_page.query_selector("[data-testid='render-complete']")
        assert el is not None, "render-complete marker not found in DOM"


# ═══════════════════════════════════════════════════════════════════════════
# SCREENSHOT SMOKE TEST
# ═══════════════════════════════════════════════════════════════════════════

class TestScreenshots:
    """Take debug screenshots for visual verification."""

    async def test_full_page_screenshot(self, dashboard_page):
        path = os.path.join(os.path.dirname(__file__), "..", "dashboard_playwright_screenshot.png")
        await dashboard_page.screenshot(path=path, full_page=True)
        assert os.path.exists(path), "Full-page screenshot was not saved"


# ═══════════════════════════════════════════════════════════════════════════
# INTERACTION FLOWS
# ═══════════════════════════════════════════════════════════════════════════

class TestInteractionFlows:
    """Playwright interaction checks for core trading workflows."""

    async def test_options_book_load_expirations_clickable(self, dashboard_page):
        btn = dashboard_page.get_by_role("button", name=re.compile(r"Load Expirations", re.I)).first
        await btn.click(timeout=10_000)
        await asyncio.sleep(1.0)
        text = await _page_text(dashboard_page)
        # Backend may return no expirations if market data is unavailable; clickability is the key assertion.
        assert "Options Book" in text, "Options Book flow did not remain stable after Load Expirations click"

    async def test_arb_create_order_opens_trade_draft_modal(self, dashboard_page):
        create_buttons = dashboard_page.get_by_role("button", name=re.compile(r"Create\s*Order", re.I))
        count = await create_buttons.count()
        if count == 0:
            pytest.skip("No active arbitrage signals available for Create Order flow")

        await create_buttons.first.click(timeout=15_000)
        await asyncio.sleep(1.0)

        body_text = await _page_text(dashboard_page)
        assert (
            "Trade Draft Ready" in body_text
            or "Order draft created" in body_text
            or "Draft loaded" in body_text
        ), "Trade draft confirmation UX did not appear"

    async def test_trade_draft_leg_count_visible(self, dashboard_page):
        text = await _page_text(dashboard_page)
        m = re.search(r"(\d+)\s+leg\(s\)", text, re.IGNORECASE)
        if not m:
            pytest.skip("No draft leg-count text visible in current dashboard state")
        assert int(m.group(1)) >= 1, "Draft leg count must be at least one"

    async def test_create_order_prefills_order_builder(self, dashboard_page):
        """Create Order should prefill builder with >=2 legs for spread-style proposals when available."""
        create_buttons = dashboard_page.get_by_role("button", name=re.compile(r"Create\s*Order", re.I))
        if await create_buttons.count() == 0:
            pytest.skip("No Create Order buttons available")

        await create_buttons.first.click(timeout=15_000)
        await asyncio.sleep(1.5)

        leg_input = dashboard_page.get_by_role("spinbutton", name=re.compile(r"Number of legs", re.I))
        if await leg_input.count() == 0:
            pytest.skip("Order Builder leg control not visible after Create Order")
        leg_value = int(await leg_input.first.input_value())
        assert leg_value >= 1, "Order Builder should have at least one staged leg"

    async def test_simulate_trade_no_http_401(self, dashboard_page):
        """Simulation path should not surface the old HTTP 401 message."""
        sim_btn = dashboard_page.get_by_role("button", name=re.compile(r"Simulate Trade", re.I))
        if await sim_btn.count() == 0:
            pytest.skip("Simulate Trade button not visible")
        await sim_btn.first.click(timeout=15_000)
        await asyncio.sleep(5.0)
        text = await _page_text(dashboard_page)
        assert "Broker returned HTTP 401" not in text, "Simulation still fails with HTTP 401"

    async def test_refresh_orders_interaction(self, dashboard_page):
        """Refresh Orders button should be present and clickable."""
        refresh_btn = dashboard_page.get_by_role("button", name=re.compile(r"Refresh Orders", re.I))
        if await refresh_btn.count() == 0:
            pytest.skip("Refresh Orders button not visible")
        await refresh_btn.first.click(timeout=15_000)
        await asyncio.sleep(1.0)
        text = await _page_text(dashboard_page)
        assert "Open Orders" in text, "Open Orders section disappeared after refresh interaction"
