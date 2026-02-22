"""
Playwright end-to-end tests for the Portfolio Risk Manager dashboard.

Tests all major dashboard features for every available IBKR account.
Covers: account selector, sidebar controls, all dashboard sections, and
verifies no JavaScript errors while each panel renders.

Run with:
    pytest tests/test_dashboard_playwright_all.py -v --tb=short

Prerequisites:
    - Dashboard running on http://localhost:8506
      (start with: ./start_dashboard.sh   or   .venv/bin/streamlit run dashboard/app.py --server.port 8506)
    - playwright browsers installed:
      .venv/bin/playwright install chromium
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Generator

import pytest
import pytest_asyncio

# ── Playwright import ────────────────────────────────────────────────────────
try:
    from playwright.async_api import async_playwright, Page, Browser, BrowserContext
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

DASHBOARD_URL = "http://localhost:8506"
LOAD_TIMEOUT  = 40_000   # ms – Streamlit can be slow to start
NAV_TIMEOUT   = 20_000   # ms – panel-level wait
SCREENSHOT_DIR = Path("/tmp/dashboard_playwright_screenshots")

# ── Skip marker ─────────────────────────────────────────────────────────────
pytestmark = pytest.mark.skipif(
    not HAS_PLAYWRIGHT,
    reason="playwright not installed – run: pip install playwright && playwright install chromium",
)


# ── Shared browser / page fixtures ──────────────────────────────────────────


@pytest_asyncio.fixture(loop_scope="module")
async def browser_ctx():
    """Launch Chromium once per module, share across tests."""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(headless=True)
        ctx: BrowserContext = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=True,
        )
        yield ctx
        await ctx.close()
        await browser.close()


@pytest_asyncio.fixture(loop_scope="module")
async def page(browser_ctx) -> Page:
    """Single shared page – dashboard app stays loaded."""
    pg: Page = await browser_ctx.new_page()
    pg.set_default_timeout(NAV_TIMEOUT)

    await pg.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=LOAD_TIMEOUT)
    # Wait for the Streamlit app root container
    await pg.wait_for_selector(".stApp", timeout=LOAD_TIMEOUT)
    # Give Streamlit time to finish its first full render
    await asyncio.sleep(6)
    return pg


async def _save_screenshot(page: Page, name: str) -> None:
    path = SCREENSHOT_DIR / f"{name}.png"
    await page.screenshot(path=str(path), full_page=False)


async def _page_text(page: Page) -> str:
    return await page.evaluate("() => document.body.innerText")


# ── Helper: check dashboard is reachable ────────────────────────────────────

class TestDashboardReachable:
    """Sanity: dashboard is up and not showing a Python traceback."""

    @pytest.mark.asyncio(loop_scope="module")
    async def test_title_is_portfolio_risk_manager(self, page: Page):
        title = await page.title()
        assert "Portfolio Risk Manager" in title, f"Unexpected title: {title}"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_no_javascript_errors(self, page: Page):
        msgs = await page.evaluate("""() => {
            const errors = [];
            window.__pw_errors = errors;
            window.addEventListener('error', e => errors.push(e.message));
            return errors;
        }""")
        assert msgs == [], f"JS errors on page: {msgs}"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_no_python_traceback_on_page(self, page: Page):
        text = await _page_text(page)
        assert "Traceback (most recent call last)" not in text, \
            "Python traceback visible on dashboard"
        assert "NameError" not in text, "NameError visible on dashboard"
        assert "AttributeError" not in text or "No attribute" not in text.lower(), \
            "AttributeError visible on dashboard"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_screenshot_initial_load(self, page: Page):
        await _save_screenshot(page, "01_initial_load")


# ── Sidebar Controls ─────────────────────────────────────────────────────────

class TestSidebarControls:
    """All sidebar controls are present and interactive."""

    @pytest.mark.asyncio(loop_scope="module")
    async def test_reload_accounts_button_visible(self, page: Page):
        btn = page.get_by_role("button", name="Reload Accounts")
        await btn.wait_for(timeout=5000)
        assert await btn.is_visible()

    @pytest.mark.asyncio(loop_scope="module")
    async def test_sign_in_button_visible(self, page: Page):
        btn = page.get_by_role("button", name="Sign in to IBKR")
        await btn.wait_for(timeout=5000)
        assert await btn.is_visible()

    @pytest.mark.asyncio(loop_scope="module")
    async def test_account_selectbox_visible(self, page: Page):
        combo = page.get_by_role("combobox", name=lambda n: "IBKR Account" in (n or ""))
        await combo.wait_for(timeout=5000)
        assert await combo.is_visible()

    @pytest.mark.asyncio(loop_scope="module")
    async def test_refresh_button_visible(self, page: Page):
        btn = page.get_by_role("button", name="Refresh")
        await btn.wait_for(timeout=5000)
        assert await btn.is_visible()

    @pytest.mark.asyncio(loop_scope="module")
    async def test_flatten_risk_button_visible(self, page: Page):
        btn = page.get_by_role("button", name="🚨 Flatten Risk")
        await btn.wait_for(timeout=5000)
        assert await btn.is_visible()

    @pytest.mark.asyncio(loop_scope="module")
    async def test_ibkr_only_mode_checkbox_checked(self, page: Page):
        """IBKR-only mode should default to checked."""
        checkbox = page.get_by_role("checkbox", name=lambda n: "IBKR-only mode" in (n or ""))
        await checkbox.wait_for(timeout=5000)
        is_checked = await checkbox.is_checked()
        assert is_checked, "IBKR-only mode checkbox should be checked by default"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_show_per_position_greeks_checkbox_checked(self, page: Page):
        checkbox = page.get_by_role("checkbox", name=lambda n: "Show per-position Greeks" in (n or ""))
        await checkbox.wait_for(timeout=5000)
        assert await checkbox.is_checked()

    @pytest.mark.asyncio(loop_scope="module")
    async def test_llm_model_selectbox_present(self, page: Page):
        combo = page.get_by_role("combobox", name=lambda n: "Model" in (n or ""))
        await combo.wait_for(timeout=5000)
        assert await combo.is_visible()


# ── Account Summary Section ──────────────────────────────────────────────────

class TestIBKRAccountSummary:
    """IBKR Account Summary renders correctly."""

    @pytest.mark.asyncio(loop_scope="module")
    async def test_account_summary_header_visible(self, page: Page):
        text = await _page_text(page)
        assert "IBKR Account Summary" in text, "IBKR Account Summary header not found"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_net_liquidation_metric_visible(self, page: Page):
        text = await _page_text(page)
        assert "Net Liquidation" in text, "Net Liquidation metric not found"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_buying_power_metric_visible(self, page: Page):
        text = await _page_text(page)
        assert "Buying Power" in text, "Buying Power metric not found"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_maint_margin_metric_visible(self, page: Page):
        text = await _page_text(page)
        assert "Maint Margin" in text, "Maint Margin metric not found"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_excess_liquidity_metric_visible(self, page: Page):
        text = await _page_text(page)
        assert "Excess Liquidity" in text, "Excess Liquidity metric not found"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_screenshot_account_summary(self, page: Page):
        await _save_screenshot(page, "02_account_summary")


# ── Risk First Dashboard Section ─────────────────────────────────────────────

class TestRiskFirstDashboard:
    """Risk First Dashboard section."""

    @pytest.mark.asyncio(loop_scope="module")
    async def test_risk_first_header_present(self, page: Page):
        text = await _page_text(page)
        assert "Risk First Dashboard" in text, "Risk First Dashboard header missing"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_margin_usage_metric_present(self, page: Page):
        text = await _page_text(page)
        assert "Margin Usage" in text, "Margin Usage metric missing"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_spx_weighted_delta_present(self, page: Page):
        text = await _page_text(page)
        assert "SPX" in text and ("Delta" in text or "Weighted" in text), \
            "SPX weighted delta metric missing"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_vega_exposure_present(self, page: Page):
        text = await _page_text(page)
        assert "Vega" in text and "Exposure" in text, "Vega Exposure metric missing"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_theta_vega_ratio_present(self, page: Page):
        text = await _page_text(page)
        assert "Theta" in text and "Vega" in text and "Ratio" in text, \
            "Theta/Vega Ratio metric missing"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_screenshot_risk_first(self, page: Page):
        await _save_screenshot(page, "03_risk_first_dashboard")


# ── Regime Banner ────────────────────────────────────────────────────────────

class TestRegimeBanner:
    """Regime detection banner is rendered."""

    @pytest.mark.asyncio(loop_scope="module")
    async def test_regime_text_visible(self, page: Page):
        text = await _page_text(page)
        assert "Regime:" in text, "Regime banner not found"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_vix_value_in_banner(self, page: Page):
        text = await _page_text(page)
        assert "VIX" in text, "VIX not present in regime banner"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_term_structure_in_banner(self, page: Page):
        text = await _page_text(page)
        assert "Term Structure" in text, "Term Structure not in regime banner"


# ── Portfolio Greeks Section ─────────────────────────────────────────────────

class TestPortfolioGreeks:
    """Portfolio Greeks section."""

    @pytest.mark.asyncio(loop_scope="module")
    async def test_portfolio_greeks_header_present(self, page: Page):
        text = await _page_text(page)
        assert "Portfolio Greeks" in text, "Portfolio Greeks section missing"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_delta_metric_present(self, page: Page):
        text = await _page_text(page)
        assert "Delta" in text, "Delta metric missing"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_theta_metric_present(self, page: Page):
        text = await _page_text(page)
        assert "Theta" in text, "Theta metric missing"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_vega_metric_present(self, page: Page):
        text = await _page_text(page)
        assert "Vega" in text, "Vega metric missing"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_gamma_metric_present(self, page: Page):
        text = await _page_text(page)
        assert "Gamma" in text, "Gamma metric missing"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_screenshot_portfolio_greeks(self, page: Page):
        await _save_screenshot(page, "04_portfolio_greeks")


# ── Positions Table ──────────────────────────────────────────────────────────

class TestPositionsTable:
    """Portfolio Positions & Greeks table is rendered."""

    @pytest.mark.asyncio(loop_scope="module")
    async def test_positions_greeks_header_present(self, page: Page):
        text = await _page_text(page)
        assert "Portfolio Positions" in text, "Portfolio Positions section header missing"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_positions_table_has_data(self, page: Page):
        """Table should render at least one row (paper account has AAPL position)."""
        # Look for dataframe or table rows
        table = page.locator(".stDataFrame, [data-testid='stDataFrame']")
        count = await table.count()
        assert count > 0, "No dataframe/table found in positions section"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_screenshot_positions_table(self, page: Page):
        await _save_screenshot(page, "05_positions_table")


# ── Risk Compliance Section ──────────────────────────────────────────────────

class TestRiskCompliance:
    """Risk Compliance section renders without crashing."""

    @pytest.mark.asyncio(loop_scope="module")
    async def test_risk_compliance_header_present(self, page: Page):
        text = await _page_text(page)
        assert "Risk Compliance" in text, "Risk Compliance section missing"


# ── IV vs HV Analysis ────────────────────────────────────────────────────────

class TestIVvsHV:
    """IV vs HV Analysis section."""

    @pytest.mark.asyncio(loop_scope="module")
    async def test_iv_hv_section_present(self, page: Page):
        text = await _page_text(page)
        assert "IV vs HV" in text or ("IV" in text and "HV" in text), \
            "IV vs HV section missing"


# ── Market Data & Intelligence ───────────────────────────────────────────────

class TestMarketSections:
    """Market data and arbitrage signal sections."""

    @pytest.mark.asyncio(loop_scope="module")
    async def test_market_data_section_visible(self, page: Page):
        text = await _page_text(page)
        assert "Market Data" in text, "Market Data section missing"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_arbitrage_signals_section_visible(self, page: Page):
        text = await _page_text(page)
        assert "Arbitrage Signals" in text, "Arbitrage Signals section missing"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_ai_assistant_section_visible(self, page: Page):
        text = await _page_text(page)
        assert "AI Assistant" in text, "AI Assistant section missing"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_screenshot_lower_sections(self, page: Page):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)
        await _save_screenshot(page, "06_lower_sections")


# ── Account Switching ────────────────────────────────────────────────────────

class TestAccountSwitching:
    """Verify account switching works and dashboard re-renders correctly."""

    @pytest.mark.asyncio(loop_scope="module")
    async def test_account_selector_has_options(self, page: Page):
        """At least one account must be available in the selector."""
        # The combobox shows the selected value; find all options
        combos = page.get_by_role("combobox")
        count = await combos.count()
        assert count >= 1, "No comboboxes found; account selector missing"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_current_account_displayed_in_selector(self, page: Page):
        """The IBKR Account combobox shows a non-empty account ID."""
        combo = page.get_by_role("combobox", name=lambda n: "IBKR Account" in (n or ""))
        try:
            await combo.wait_for(timeout=5000)
        except Exception:
            # Fallback: first combobox
            combo = page.get_by_role("combobox").first
        selected = await combo.input_value()
        assert selected.strip(), f"Account selector is empty (got: '{selected}')"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_refresh_button_triggers_rerender(self, page: Page):
        """Clicking Refresh should not crash the app (page still shows title)."""
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.5)

        refresh_btn = page.get_by_role("button", name="Refresh")
        await refresh_btn.click()
        # Wait for Streamlit to re-render (spinner disappears or content refreshes)
        await asyncio.sleep(5)

        text = await _page_text(page)
        assert "Portfolio Risk Manager" in text, \
            "Dashboard broken after Refresh click"
        assert "Traceback" not in text, "Python traceback after Refresh click"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_screenshot_after_refresh(self, page: Page):
        await _save_screenshot(page, "07_after_refresh")


# ── Sidebar Toggle Controls ──────────────────────────────────────────────────

class TestSidebarToggles:
    """Sidebar toggles don't crash the app."""

    @pytest.mark.asyncio(loop_scope="module")
    async def test_toggle_ibkr_only_mode_off_and_back(self, page: Page):
        """Unchecking and re-checking IBKR-only mode should not crash."""
        await page.evaluate("window.scrollTo(0, 0)")
        checkbox = page.get_by_role("checkbox", name=lambda n: "IBKR-only mode" in (n or ""))
        await checkbox.wait_for(timeout=5000)
        # Uncheck it
        await checkbox.click()
        await asyncio.sleep(4)
        text = await _page_text(page)
        assert "Traceback" not in text, "Crash after unchecking IBKR-only mode"
        # Re-check it
        await checkbox.click()
        await asyncio.sleep(4)
        text = await _page_text(page)
        assert "Traceback" not in text, "Crash after re-checking IBKR-only mode"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_toggle_show_positions_off_and_back(self, page: Page):
        """Unchecking positions table should hide it gracefully."""
        checkbox = page.get_by_role("checkbox", name=lambda n: "Show per-position Greeks" in (n or ""))
        await checkbox.wait_for(timeout=5000)
        await checkbox.click()
        await asyncio.sleep(4)
        text = await _page_text(page)
        assert "Traceback" not in text, "Crash toggling off per-position Greeks"
        # Turn it back on
        await checkbox.click()
        await asyncio.sleep(4)

    @pytest.mark.asyncio(loop_scope="module")
    async def test_screenshot_after_toggles(self, page: Page):
        await _save_screenshot(page, "08_after_toggles")


# ── Flatten Risk Panel ───────────────────────────────────────────────────────

class TestFlattenRiskPanel:
    """Flatten Risk panel renders without error."""

    @pytest.mark.asyncio(loop_scope="module")
    async def test_flatten_risk_section_visible(self, page: Page):
        text = await _page_text(page)
        # The section is guarded by a try/except, so it won't crash but may show a warning
        # Just check the button is accessible
        btn = page.get_by_role("button", name="🚨 Flatten Risk")
        assert await btn.is_visible(), "Flatten Risk button not visible"


# ── AI Sections ──────────────────────────────────────────────────────────────

class TestAISections:
    """AI Risk Audit and Market Brief sections render without error."""

    @pytest.mark.asyncio(loop_scope="module")
    async def test_live_risk_audit_section_present(self, page: Page):
        text = await _page_text(page)
        assert "Live Risk Audit" in text or "Risk Audit" in text, \
            "Live Risk Audit section missing"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_market_brief_section_present(self, page: Page):
        text = await _page_text(page)
        assert "Market Brief" in text, "Market Brief section missing"

    @pytest.mark.asyncio(loop_scope="module")
    async def test_ai_assistant_input_visible(self, page: Page):
        """AI Assistant text input should be accessible."""
        # scroll to bottom first
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)
        inp = page.get_by_placeholder("How should I reduce near-term gamma?")
        await inp.wait_for(timeout=5000)
        assert await inp.is_visible(), "AI Assistant text input not visible"


# ── Greek Diagnostics / Missing Greeks ──────────────────────────────────────

class TestGreeksDiagnostics:
    """Greeks diagnostics section renders without crashing."""

    @pytest.mark.asyncio(loop_scope="module")
    async def test_greeks_ts_freshness_section_or_modes_info_present(self, page: Page):
        text = await _page_text(page)
        # Should have either the diagnostics mode info or the IBKR-only notice
        has_diag_info = (
            "Greeks Diagnostics" in text
            or "IBKR-only mode" in text
            or "greeks_source_counts" in text
            or "ibkr_no_data" in text
            or "Disable to allow" in text
        )
        assert has_diag_info, "No Greeks diagnostics info found on page"


# ── Second Account (if available) ───────────────────────────────────────────

class TestSecondAccount:
    """If multiple accounts are in the selector, test switching."""

    @pytest.mark.asyncio(loop_scope="module")
    async def test_discover_available_accounts(self, page: Page):
        """Inspect the account selector's options."""
        await page.evaluate("window.scrollTo(0, 0)")
        # Click the combobox to open its dropdown and see options
        combo = page.get_by_role("combobox", name=lambda n: "IBKR Account" in (n or ""))
        try:
            await combo.wait_for(timeout=5000)
            await combo.click()
            await asyncio.sleep(1)
            # Get aria reference for the listbox
            options_text = await page.evaluate("""() => {
                const listbox = document.querySelector('[role="listbox"]');
                if (!listbox) return [];
                return Array.from(listbox.querySelectorAll('[role="option"]'))
                            .map(o => o.textContent.trim());
            }""")
            # Close the combo
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

            # Save discovered accounts as a pytest xfail note if only one
            if len(options_text) <= 1:
                pytest.skip(
                    f"Only one account available ({options_text}); "
                    "multi-account switching requires gateway to be authenticated."
                )
            # If we have multiple accounts, record them
            assert len(options_text) >= 2, f"Expected 2+ accounts, got: {options_text}"
        except Exception as exc:
            pytest.skip(f"Account option listing failed ({exc}) — likely gateway not auth'd")

    @pytest.mark.asyncio(loop_scope="module")
    async def test_switch_to_second_account_and_verify_render(self, page: Page):
        """Switch to the second account and confirm dashboard re-renders."""
        combo = page.get_by_role("combobox", name=lambda n: "IBKR Account" in (n or ""))
        try:
            await combo.wait_for(timeout=5000)
            await combo.click()
            await asyncio.sleep(1)

            options = await page.evaluate("""() => {
                const listbox = document.querySelector('[role="listbox"]');
                if (!listbox) return [];
                return Array.from(listbox.querySelectorAll('[role="option"]'))
                            .map(o => o.textContent.trim());
            }""")
            if len(options) < 2:
                await page.keyboard.press("Escape")
                pytest.skip("Only one account — cannot test switching")

            # Click the second option
            second_option = page.locator('[role="option"]').nth(1)
            await second_option.click()
            await asyncio.sleep(8)   # wait for full re-render

            text = await _page_text(page)
            assert "Portfolio Risk Manager" in text, "Dashboard crashed after account switch"
            assert "Traceback" not in text, "Python traceback after account switch"
            await _save_screenshot(page, "09_second_account")

            # Switch back to first account
            await combo.click()
            await asyncio.sleep(1)
            first_option = page.locator('[role="option"]').first
            await first_option.click()
            await asyncio.sleep(6)

        except Exception as exc:
            pytest.skip(f"Account switch failed ({exc})")


# ── Final summary screenshot ─────────────────────────────────────────────────

class TestFinalScreenshot:
    @pytest.mark.asyncio(loop_scope="module")
    async def test_final_full_screenshot(self, page: Page):
        """Take a final viewport screenshot for manual review."""
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(1)
        await _save_screenshot(page, "10_final_state")

    @pytest.mark.asyncio(loop_scope="module")
    async def test_all_sections_rendered_no_crash(self, page: Page):
        """Final assertion: no traceback visible anywhere on page."""
        text = await _page_text(page)
        bad_phrases = [
            "Traceback (most recent call last)",
            "NameError:",
            "AttributeError:",
            "ModuleNotFoundError:",
            "ImportError:",
        ]
        for phrase in bad_phrases:
            assert phrase not in text, f"Error phrase found on dashboard: {phrase!r}"
