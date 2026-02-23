"""tests/test_ai_risk_auditor.py — Unit tests for LLMRiskAuditor.suggest_trades().

T047 [US5]: TDD tests that must FAIL before T048-T050 are implemented.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from models.order import (
    AITradeSuggestion,
    OrderAction,
    OrderLeg,
    PortfolioGreeks,
    RiskBreach,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_breach(**kwargs) -> RiskBreach:
    defaults = dict(
        breach_type="delta_cap",
        threshold_value=30.0,
        actual_value=45.2,
        regime="medium_vol",
        vix=22.5,
    )
    defaults.update(kwargs)
    return RiskBreach(**defaults)


def _make_greeks(**kwargs) -> PortfolioGreeks:
    defaults = dict(spx_delta=45.2, gamma=-0.002, theta=380.0, vega=-7500.0)
    defaults.update(kwargs)
    return PortfolioGreeks(**defaults)


def _valid_llm_json() -> str:
    """Return a JSON array of exactly 3 suggestions the LLM might return."""
    legs_template = [{"symbol": "SPX", "action": "SELL", "quantity": 1}]
    suggestions = [
        {
            "legs": legs_template,
            "projected_delta_change": -15.0,
            "projected_theta_cost": -120.0,
            "rationale": "Sell 1 SPX put spread to reduce delta exposure.",
        },
        {
            "legs": legs_template,
            "projected_delta_change": -10.0,
            "projected_theta_cost": -90.0,
            "rationale": "Add short delta via ES futures micro.",
        },
        {
            "legs": legs_template,
            "projected_delta_change": -5.0,
            "projected_theta_cost": -60.0,
            "rationale": "Trim existing long call position.",
        },
    ]
    return json.dumps(suggestions)


# ---------------------------------------------------------------------------
# T047: suggest_trades() tests
# ---------------------------------------------------------------------------

class TestSuggestTrades:
    """Tests for LLMRiskAuditor.suggest_trades()."""

    def _make_auditor(self):
        from agents.llm_risk_auditor import LLMRiskAuditor
        from database.local_store import LocalStore
        store = LocalStore(db_path=":memory:")
        return LLMRiskAuditor(db=store)

    def test_returns_three_suggestions_on_valid_llm_response(self):
        """When LLM returns 3 valid JSON suggestions, suggest_trades() returns 3 AITradeSuggestion."""
        auditor = self._make_auditor()
        greeks = _make_greeks()
        breach = _make_breach()

        with patch("agents.llm_risk_auditor.async_llm_chat", new=AsyncMock(return_value=_valid_llm_json())):
            result = _run(auditor.suggest_trades(
                portfolio_greeks=greeks,
                vix=22.5,
                regime="medium_vol",
                breach=breach,
                theta_budget=400.0,
            ))

        assert isinstance(result, list)
        assert len(result) == 3
        assert all(isinstance(s, AITradeSuggestion) for s in result)

    def test_suggestion_fields_populated(self):
        """Each suggestion has legs, projected_delta_change, projected_theta_cost, rationale."""
        auditor = self._make_auditor()

        with patch("agents.llm_risk_auditor.async_llm_chat", new=AsyncMock(return_value=_valid_llm_json())):
            result = _run(auditor.suggest_trades(
                portfolio_greeks=_make_greeks(),
                vix=22.5,
                regime="medium_vol",
                breach=_make_breach(),
                theta_budget=400.0,
            ))

        s = result[0]
        assert isinstance(s.legs, list)
        assert len(s.legs) >= 1
        assert isinstance(s.legs[0], OrderLeg)
        assert s.projected_delta_change == pytest.approx(-15.0)
        assert s.projected_theta_cost == pytest.approx(-120.0)
        assert "delta" in s.rationale.lower()

    def test_llm_timeout_returns_empty_list_no_exception(self):
        """When async_llm_chat raises asyncio.TimeoutError, suggest_trades() returns [] silently."""
        auditor = self._make_auditor()

        with patch("agents.llm_risk_auditor.async_llm_chat", new=AsyncMock(side_effect=asyncio.TimeoutError)):
            result = _run(auditor.suggest_trades(
                portfolio_greeks=_make_greeks(),
                vix=22.5,
                regime="medium_vol",
                breach=_make_breach(),
                theta_budget=400.0,
            ))

        assert result == []

    def test_llm_invalid_json_returns_empty_list_no_exception(self):
        """When LLM returns non-JSON text, suggest_trades() returns []."""
        auditor = self._make_auditor()

        with patch("agents.llm_risk_auditor.async_llm_chat", new=AsyncMock(return_value="I cannot assist with that.")):
            result = _run(auditor.suggest_trades(
                portfolio_greeks=_make_greeks(),
                vix=22.5,
                regime="medium_vol",
                breach=_make_breach(),
                theta_budget=400.0,
            ))

        assert result == []

    def test_llm_partial_json_returns_empty_list(self):
        """When LLM returns valid JSON but wrong schema, suggest_trades() returns []."""
        auditor = self._make_auditor()
        bad_json = json.dumps({"answer": "not a list"})

        with patch("agents.llm_risk_auditor.async_llm_chat", new=AsyncMock(return_value=bad_json)):
            result = _run(auditor.suggest_trades(
                portfolio_greeks=_make_greeks(),
                vix=22.5,
                regime="medium_vol",
                breach=_make_breach(),
                theta_budget=400.0,
            ))

        assert result == []

    def test_llm_exception_returns_empty_list(self):
        """Any unexpected exception from LLM returns [] without propagating."""
        auditor = self._make_auditor()

        with patch("agents.llm_risk_auditor.async_llm_chat", new=AsyncMock(side_effect=RuntimeError("network error"))):
            result = _run(auditor.suggest_trades(
                portfolio_greeks=_make_greeks(),
                vix=22.5,
                regime="medium_vol",
                breach=_make_breach(),
                theta_budget=400.0,
            ))

        assert result == []

    def test_suggestion_id_is_unique(self):
        """Each suggestion has a unique suggestion_id."""
        auditor = self._make_auditor()

        with patch("agents.llm_risk_auditor.async_llm_chat", new=AsyncMock(return_value=_valid_llm_json())):
            result = _run(auditor.suggest_trades(
                portfolio_greeks=_make_greeks(),
                vix=22.5,
                regime="medium_vol",
                breach=_make_breach(),
                theta_budget=400.0,
            ))

        ids = [s.suggestion_id for s in result]
        assert len(set(ids)) == 3, "All suggestion IDs must be unique"

    def test_rationale_stored_in_journal_when_acted_upon(self):
        """AI rationale is available on suggestion and can be stored in a journal entry."""
        auditor = self._make_auditor()

        with patch("agents.llm_risk_auditor.async_llm_chat", new=AsyncMock(return_value=_valid_llm_json())):
            suggestions = _run(auditor.suggest_trades(
                portfolio_greeks=_make_greeks(),
                vix=22.5,
                regime="medium_vol",
                breach=_make_breach(),
                theta_budget=400.0,
            ))

        from models.order import TradeJournalEntry
        s = suggestions[0]
        entry = TradeJournalEntry(
            underlying="SPX",
            ai_suggestion_id=s.suggestion_id,
            ai_rationale=s.rationale,
        )
        assert entry.ai_suggestion_id == s.suggestion_id
        assert entry.ai_rationale == s.rationale

    def test_prompt_includes_breach_context(self):
        """The prompt sent to the LLM includes breach type, threshold, actual value."""
        auditor = self._make_auditor()
        captured_prompt = {}

        async def capture_llm(prompt, **kwargs):
            captured_prompt["value"] = prompt
            return _valid_llm_json()

        breach = _make_breach(breach_type="delta_cap", threshold_value=30.0, actual_value=45.2)

        with patch("agents.llm_risk_auditor.async_llm_chat", new=AsyncMock(side_effect=capture_llm)):
            _run(auditor.suggest_trades(
                portfolio_greeks=_make_greeks(),
                vix=22.5,
                regime="medium_vol",
                breach=breach,
                theta_budget=400.0,
            ))

        prompt = captured_prompt["value"]
        assert "delta_cap" in prompt
        assert "30.0" in prompt or "30" in prompt
        assert "45.2" in prompt or "45" in prompt

    def test_empty_breach_none_returns_suggestions(self):
        """suggest_trades() still works when breach is None (general audit mode)."""
        auditor = self._make_auditor()

        with patch("agents.llm_risk_auditor.async_llm_chat", new=AsyncMock(return_value=_valid_llm_json())):
            result = _run(auditor.suggest_trades(
                portfolio_greeks=_make_greeks(),
                vix=22.5,
                regime="medium_vol",
                breach=None,
                theta_budget=400.0,
            ))

        assert isinstance(result, list)
        assert len(result) == 3
