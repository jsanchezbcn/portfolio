"""desktop/tests/test_ai_risk_tab.py — Basic tests for AI Risk tab."""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from PySide6.QtCore import Qt

from models.order import AITradeSuggestion, OrderAction, OrderLeg

from desktop.ui.ai_risk_tab import AIRiskTab, _default_trades_model, _get_copilot_account


class TestAIRiskTab:
    def test_creates_without_crash(self, qtbot, mock_engine):
        tab = AIRiskTab(mock_engine)
        qtbot.addWidget(tab)

    def test_default_model_is_gpt5_mini(self, qtbot, mock_engine, monkeypatch):
        monkeypatch.delenv("LLM_FAST_MODEL", raising=False)
        monkeypatch.delenv("LLM_MODEL", raising=False)

        tab = AIRiskTab(mock_engine)
        qtbot.addWidget(tab)

        assert tab.current_model == "gpt-5-mini"

    def test_default_model_prefers_fast_model_env(self, monkeypatch):
        monkeypatch.setenv("LLM_FAST_MODEL", "gpt-4.1")
        monkeypatch.setenv("LLM_MODEL", "gpt-5")

        assert _default_trades_model() == "gpt-4.1"

    def test_default_model_picker_has_entries(self, qtbot, mock_engine):
        tab = AIRiskTab(mock_engine)
        qtbot.addWidget(tab)
        assert tab._cmb_model.count() > 0

    def test_default_scenario_is_auto(self, qtbot, mock_engine):
        tab = AIRiskTab(mock_engine)
        qtbot.addWidget(tab)
        assert "Auto" in tab._cmb_scenario.currentText()

    def test_has_whatif_button(self, qtbot, mock_engine):
        tab = AIRiskTab(mock_engine)
        qtbot.addWidget(tab)
        assert "WhatIf" in tab._btn_whatif.text()

    def test_has_clear_suggestions_button(self, qtbot, mock_engine):
        tab = AIRiskTab(mock_engine)
        qtbot.addWidget(tab)
        assert "Clear Suggestions" in tab._btn_clear_suggestions.text()

    def test_has_canned_prompts(self, qtbot, mock_engine):
        tab = AIRiskTab(mock_engine)
        qtbot.addWidget(tab)

        assert tab._cmb_preset_group.count() >= 4
        assert tab._cmb_presets.count() > 1

    def test_changing_prompt_group_updates_prompt_list(self, qtbot, mock_engine):
        tab = AIRiskTab(mock_engine)
        qtbot.addWidget(tab)

        tab._cmb_preset_group.setCurrentText("Execution")

        assert tab._cmb_presets.count() > 1
        assert "slippage" in tab._cmb_presets.itemText(3).lower()

    def test_use_preset_populates_question_box(self, qtbot, mock_engine):
        tab = AIRiskTab(mock_engine)
        qtbot.addWidget(tab)

        tab._cmb_presets.setCurrentIndex(1)
        tab._on_use_preset()

        assert tab._txt_user.toPlainText().strip() == tab._cmb_presets.currentData()

    def test_use_preset_button_click_populates_question_box(self, qtbot, mock_engine):
        tab = AIRiskTab(mock_engine)
        qtbot.addWidget(tab)

        tab._cmb_preset_group.setCurrentText("Hedge Ideas")
        tab._cmb_presets.setCurrentIndex(1)
        qtbot.mouseClick(tab._btn_use_preset, Qt.MouseButton.LeftButton)

        assert tab._txt_user.toPlainText().strip() == tab._cmb_presets.currentData()

    def test_build_tools_context_includes_structured_portfolio_state(self, qtbot, mock_engine, monkeypatch):
        monkeypatch.setenv("GITHUB_COPILOT_ACTIVE_PROFILE", "work")
        tab = AIRiskTab(mock_engine)
        qtbot.addWidget(tab)
        tab._context = {
            "summary": {
                "total_spx_delta": 22.5,
                "total_theta": 110.0,
                "total_vega": -450.0,
                "theta_vega_ratio": -0.24,
            },
            "regime_name": "neutral_volatility",
            "vix": 18.2,
            "nlv": 250000.0,
            "violations": [{"metric": "delta", "current": 22.5, "limit": 15.0}],
            "resolved_limits": {"max_spx_delta_pct_nlv": 0.01},
            "positions": [
                {"symbol": "ES", "sec_type": "FOP", "quantity": -1, "expiry": "20260320", "spx_delta": -30.0, "theta": 75.0, "vega": -220.0},
                {"symbol": "MES", "sec_type": "FOP", "quantity": 2, "expiry": "20260320", "spx_delta": 8.0, "theta": 20.0, "vega": -80.0},
            ],
            "open_orders": [{"symbol": "ES"}],
            "recent_fills": [{"symbol": "MES"}],
            "order_log": [],
            "last_prices": {"ES": 5750.25, "VIX": 18.2},
            "prices": {"ES": {"last": 5750.25}},
            "account": {"net_liquidation": 250000.0},
        }
        tab._suggestions = [
            AITradeSuggestion(
                suggestion_id="s1",
                legs=[OrderLeg(symbol="MES", action=OrderAction.SELL, quantity=1, strike=5800.0, option_right=None, expiration=date(2026, 3, 20))],
                projected_delta_change=-5.0,
                projected_theta_cost=-30.0,
                rationale="Trim delta with MES",
            )
        ]
        mock_engine.chain_snapshot = MagicMock(return_value=[
            SimpleNamespace(underlying="ES", expiry="20260320", strike=5800.0, right="C", bid=12.0, ask=12.5, delta=0.16)
        ])

        tools_context, tool_log_lines = tab._build_tools_context()

        assert "tool:get_portfolio_state" in tools_context
        assert tools_context["tool:get_trades_view_state"]["copilot_profile"] == "work"
        assert tools_context["tool:get_portfolio_state"]["largest_spx_delta_positions"][0]["symbol"] == "ES"
        assert tools_context["tool:get_portfolio_state"]["active_chain"]["underlying"] == "ES"
        assert tools_context["tool:get_portfolio_state"]["current_ai_suggestions"][0]["rationale"] == "Trim delta with MES"
        assert any("tool:get_portfolio_state" in line for line in tool_log_lines)

    def test_build_chat_request_prioritizes_portfolio_state(self, qtbot, mock_engine):
        tab = AIRiskTab(mock_engine)
        qtbot.addWidget(tab)
        tab._context = {
            "summary": {"total_spx_delta": 10.0},
            "positions": [],
            "open_orders": [],
            "recent_fills": [],
            "order_log": [],
            "last_prices": {},
            "prices": {},
        }

        system, prompt, tools_context, _tool_log_lines = tab._build_chat_request("What should I trade?")

        assert "Prioritize the structured portfolio state summary" in system
        assert "Portfolio state (prioritize this summary first)" in prompt
        assert "What should I trade?" in prompt
        assert "tool:get_portfolio_state" in tools_context

    @pytest.mark.asyncio
    async def test_wait_for_session_response_resets_inactivity_timeout(self, qtbot, mock_engine):
        tab = AIRiskTab(mock_engine)
        qtbot.addWidget(tab)

        class SlowSession:
            async def send_and_wait(self, payload):
                await asyncio.sleep(0.16)
                return {"ok": True, "payload": payload}

        activity_event = asyncio.Event()

        async def pulse_activity():
            await asyncio.sleep(0.05)
            activity_event.set()
            await asyncio.sleep(0.05)
            activity_event.set()

        asyncio.create_task(pulse_activity())
        result = await tab._wait_for_session_response(
            SlowSession(),
            {"prompt": "hello"},
            inactivity_timeout=0.08,
            activity_event=activity_event,
        )

        assert result["ok"] is True
        assert result["payload"]["prompt"] == "hello"

    @pytest.mark.asyncio
    async def test_wait_for_session_response_times_out_without_activity(self, qtbot, mock_engine):
        tab = AIRiskTab(mock_engine)
        qtbot.addWidget(tab)

        class VerySlowSession:
            async def send_and_wait(self, payload):
                await asyncio.sleep(0.2)
                return payload

        with pytest.raises(asyncio.TimeoutError):
            await tab._wait_for_session_response(
                VerySlowSession(),
                {"prompt": "hello"},
                inactivity_timeout=0.05,
                activity_event=asyncio.Event(),
            )


class TestGetCopilotAccount:
    """Unit tests for _get_copilot_account() account-detection function."""

    def _mock_run(self, stdout: str, returncode: int = 0):
        result = MagicMock()
        result.returncode = returncode
        result.stdout = stdout
        result.stderr = ""
        return result

    def test_returns_login_line_when_present(self):
        """Picks first line that contains 'Logged in to'."""
        stdout = (
            "github.com\n"
            "  ✓ Logged in to github.com as jsanchezbcn (oauth_token)\n"
            "  ✓ Git operations: https\n"
        )
        with patch("subprocess.run", return_value=self._mock_run(stdout, 0)):
            result = _get_copilot_account()
        assert "jsanchezbcn" in result

    def test_returns_account_line_when_no_login_line(self):
        """Falls back to any line containing 'Account'."""
        stdout = (
            "github.com\n"
            "  Account: jsanchezbcn\n"
        )
        with patch("subprocess.run", return_value=self._mock_run(stdout, 0)):
            result = _get_copilot_account()
        assert "jsanchezbcn" in result

    def test_falls_back_to_as_split_when_no_keyword_line(self):
        """Falls back to splitting on 'as' when none of the keyword lines matched."""
        stdout = "Connected as jsanchezbcn (token)\n"
        with patch("subprocess.run", return_value=self._mock_run(stdout, 0)):
            result = _get_copilot_account()
        assert "jsanchezbcn" in result

    def test_returns_unknown_when_gh_fails(self):
        """Returns unknown string when gh exits non-zero and git also fails."""
        fail = MagicMock()
        fail.returncode = 1
        fail.stdout = ""
        fail.stderr = ""
        with patch("subprocess.run", return_value=fail):
            result = _get_copilot_account()
        assert "unknown" in result.lower() or "GitHub" in result

    def test_falls_back_to_git_on_file_not_found(self):
        """When gh is not installed, falls back to git config user.name."""
        git_result = MagicMock()
        git_result.returncode = 0
        git_result.stdout = "jsanchezbcn\n"

        def side_effect(cmd, **kwargs):
            if "gh" in cmd:
                raise FileNotFoundError
            return git_result

        with patch("subprocess.run", side_effect=side_effect):
            result = _get_copilot_account()
        assert "jsanchezbcn" in result

    def test_returns_detection_failed_on_timeout(self):
        """Returns detection-failed string on TimeoutExpired."""
        import subprocess

        def side_effect(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 5)

        with patch("subprocess.run", side_effect=side_effect):
            result = _get_copilot_account()
        assert "GitHub Copilot" in result
