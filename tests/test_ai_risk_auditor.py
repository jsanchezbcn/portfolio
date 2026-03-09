"""tests/test_ai_risk_auditor.py — Unit tests for LLMRiskAuditor.suggest_trades().

T047 [US5]: TDD tests that must FAIL before T048-T050 are implemented.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from models.order import (
    AITradeSuggestion,
    OrderAction,
    OrderLeg,
    PortfolioGreeks,
    RiskBreach,
)


class _FakeAuditDB:
    def __init__(self) -> None:
        self.upsert_market_intel = AsyncMock(return_value="intel-1")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_breach(**kwargs) -> RiskBreach:
    defaults: dict[str, object] = dict(
        breach_type="delta_cap",
        threshold_value=30.0,
        actual_value=45.2,
        regime="medium_vol",
        vix=22.5,
    )
    defaults.update(kwargs)
    return RiskBreach(
        breach_type=str(defaults["breach_type"]),
        threshold_value=float(cast(float, defaults["threshold_value"])),
        actual_value=float(cast(float, defaults["actual_value"])),
        regime=str(defaults["regime"]),
        vix=float(cast(float, defaults["vix"])),
    )


def _make_greeks(**kwargs) -> PortfolioGreeks:
    defaults: dict[str, float] = dict(spx_delta=45.2, gamma=-0.002, theta=380.0, vega=-7500.0)
    defaults.update(kwargs)
    return PortfolioGreeks(
        spx_delta=float(defaults["spx_delta"]),
        gamma=float(defaults["gamma"]),
        theta=float(defaults["theta"]),
        vega=float(defaults["vega"]),
    )


def _valid_llm_json() -> str:
    """Return a JSON array of exactly 3 MES FOP suggestions the LLM might return."""
    legs_template = [
        {
            "symbol": "MES", "sec_type": "FOP", "exchange": "CME",
            "action": "SELL", "quantity": 1,
            "strike": 5900.0, "right": "C", "expiry": "20260321",
        }
    ]
    suggestions = [
        {
            "legs": legs_template,
            "projected_delta_change": -15.0,
            "projected_theta_cost": -120.0,
            "rationale": "Sell 1 MES call to reduce delta exposure.",
        },
        {
            "legs": legs_template,
            "projected_delta_change": -10.0,
            "projected_theta_cost": -90.0,
            "rationale": "Add short delta via MES call spread.",
        },
        {
            "legs": legs_template,
            "projected_delta_change": -5.0,
            "projected_theta_cost": -60.0,
            "rationale": "Trim existing long MES call position.",
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
        return LLMRiskAuditor(db=_FakeAuditDB())

    def test_prefers_fast_model_default(self, monkeypatch):
        from agents.llm_risk_auditor import LLMRiskAuditor

        monkeypatch.setenv("LLM_FAST_MODEL", "gpt-5-mini")
        monkeypatch.setenv("LLM_MODEL", "gpt-5")

        auditor = LLMRiskAuditor(db=_FakeAuditDB())

        assert auditor._model == "gpt-5-mini"

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

        s = next(s for s in result if s.projected_delta_change == pytest.approx(-15.0))
        assert isinstance(s.legs, list)
        assert len(s.legs) >= 1
        assert isinstance(s.legs[0], OrderLeg)
        assert s.projected_delta_change == pytest.approx(-15.0)
        assert s.projected_theta_cost == pytest.approx(-120.0)
        assert "delta" in s.rationale.lower()

    def test_llm_timeout_falls_back_to_suggestions(self):
        """When async_llm_chat raises asyncio.TimeoutError, suggest_trades() returns fallback suggestions."""
        auditor = self._make_auditor()

        with patch("agents.llm_risk_auditor.async_llm_chat", new=AsyncMock(side_effect=asyncio.TimeoutError)):
            result = _run(auditor.suggest_trades(
                portfolio_greeks=_make_greeks(),
                vix=22.5,
                regime="medium_vol",
                breach=_make_breach(),
                theta_budget=400.0,
            ))

        assert isinstance(result, list)
        assert len(result) >= 1
        assert all(isinstance(s, AITradeSuggestion) for s in result)

    def test_llm_invalid_json_falls_back_to_suggestions(self):
        """When LLM returns non-JSON text, suggest_trades() returns fallback MES suggestions."""
        auditor = self._make_auditor()

        with patch("agents.llm_risk_auditor.async_llm_chat", new=AsyncMock(return_value="I cannot assist with that.")):
            result = _run(auditor.suggest_trades(
                portfolio_greeks=_make_greeks(),
                vix=22.5,
                regime="medium_vol",
                breach=_make_breach(),
                theta_budget=400.0,
            ))

        assert isinstance(result, list)
        assert len(result) >= 1
        assert all(isinstance(s, AITradeSuggestion) for s in result)

    def test_llm_partial_json_falls_back_to_suggestions(self):
        """When LLM returns valid JSON but wrong schema, suggest_trades() returns fallback suggestions."""
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

        assert isinstance(result, list)
        assert len(result) >= 1
        assert all(isinstance(s, AITradeSuggestion) for s in result)

    def test_llm_exception_falls_back_to_suggestions(self):
        """Any unexpected exception from LLM returns fallback suggestions without propagating."""
        auditor = self._make_auditor()

        with patch("agents.llm_risk_auditor.async_llm_chat", new=AsyncMock(side_effect=RuntimeError("network error"))):
            result = _run(auditor.suggest_trades(
                portfolio_greeks=_make_greeks(),
                vix=22.5,
                regime="medium_vol",
                breach=_make_breach(),
                theta_budget=400.0,
            ))

        assert isinstance(result, list)
        assert len(result) >= 1
        assert all(isinstance(s, AITradeSuggestion) for s in result)

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

    def test_parsed_legs_include_option_fields(self):
        """_parse_suggestions populates strike, option_right, and expiration on each leg."""
        from datetime import date
        from models.order import OptionRight
        from agents.llm_risk_auditor import LLMRiskAuditor
        raw = _valid_llm_json()
        result = LLMRiskAuditor._parse_suggestions(raw)
        assert len(result) == 3
        leg = result[0].legs[0]
        assert leg.symbol == "MES"
        assert leg.strike == 5900.0
        assert leg.option_right == OptionRight.CALL
        assert leg.expiration == date(2026, 3, 21)

    def test_fallback_uses_mes_with_option_fields(self):
        """_fallback_suggestions uses MES symbol with strike/right/expiration populated."""
        from models.order import OptionRight
        from agents.llm_risk_auditor import LLMRiskAuditor
        result = LLMRiskAuditor._fallback_suggestions(
            portfolio_greeks=_make_greeks(spx_delta=120.0),  # delta breach
            breach=None,
            theta_budget=100.0,
            active_expiry="20260321",
            underlying="MES",
        )
        assert len(result) >= 1
        leg = result[0].legs[0]
        assert leg.symbol == "MES"
        assert leg.strike is not None
        assert leg.option_right in (OptionRight.CALL, OptionRight.PUT)
        assert leg.expiration is not None

    def test_active_expiry_appears_in_prompt(self):
        """_build_suggest_prompt includes active_expiry when provided."""
        from agents.llm_risk_auditor import LLMRiskAuditor
        prompt = LLMRiskAuditor._build_suggest_prompt(
            portfolio_greeks=_make_greeks(),
            vix=20.0,
            regime="neutral",
            breach=None,
            theta_budget=200.0,
            active_expiry="20260321",
            underlying="MES",
        )
        assert "20260321" in prompt
        assert "MES" in prompt

    def test_prompt_includes_execution_heuristics(self):
        """Suggestion prompt includes explicit product and direction heuristics."""
        from agents.llm_risk_auditor import LLMRiskAuditor

        prompt = LLMRiskAuditor._build_suggest_prompt(
            portfolio_greeks=_make_greeks(spx_delta=120.0, theta=-50.0),
            vix=28.0,
            regime="high_volatility",
            breach=_make_breach(breach_type="delta_cap", threshold_value=75.0, actual_value=120.0),
            theta_budget=100.0,
            active_expiry="20260321",
            underlying="MES",
        )

        assert "Execution Heuristics" in prompt
        assert "negative projected_delta_change" in prompt
        assert "theta spend" in prompt.lower()

    def test_rank_and_filter_suggestions_prefers_directionally_correct_trade(self):
        """Ranking keeps the suggestion that actually improves the breached delta direction."""
        from agents.llm_risk_auditor import LLMRiskAuditor

        wrong = AITradeSuggestion(
            legs=[OrderLeg(symbol="MES", action=OrderAction.BUY, quantity=1)],
            projected_delta_change=12.0,
            projected_theta_cost=20.0,
            rationale="Wrong-way hedge",
        )
        right = AITradeSuggestion(
            legs=[OrderLeg(symbol="MES", action=OrderAction.SELL, quantity=1)],
            projected_delta_change=-15.0,
            projected_theta_cost=5.0,
            rationale="Better execution and spread quality",
        )

        ranked = LLMRiskAuditor._rank_and_filter_suggestions(
            [wrong, right],
            breach=_make_breach(breach_type="delta_cap", threshold_value=30.0, actual_value=45.0),
            theta_budget=50.0,
            active_expiry="",
            underlying="MES",
        )

        assert ranked[0].projected_delta_change == pytest.approx(-15.0)

    def test_rank_and_filter_suggestions_drops_wrong_symbol_and_overspend(self):
        """Ranking drops non-ES/MES suggestions and trades that blow through theta budget."""
        from agents.llm_risk_auditor import LLMRiskAuditor
        kept = AITradeSuggestion(
            legs=[OrderLeg(symbol="MES", action=OrderAction.SELL, quantity=1)],
            projected_delta_change=-10.0,
            projected_theta_cost=10.0,
            rationale="Keep me",
        )
        bad_symbol = AITradeSuggestion(
            legs=[OrderLeg(symbol="SPY", action=OrderAction.SELL, quantity=1)],
            projected_delta_change=-10.0,
            projected_theta_cost=10.0,
            rationale="Wrong symbol",
        )
        overspend = AITradeSuggestion(
            legs=[OrderLeg(symbol="MES", action=OrderAction.SELL, quantity=1)],
            projected_delta_change=-10.0,
            projected_theta_cost=200.0,
            rationale="Too expensive",
        )

        ranked = LLMRiskAuditor._rank_and_filter_suggestions(
            [kept, bad_symbol, overspend],
            breach=_make_breach(),
            theta_budget=50.0,
            active_expiry="",
            underlying="MES",
        )

        assert ranked == [kept]

    def test_fetch_market_data_context_formats_chain_rows(self):
        """Live market-data context handles engine dataclasses instead of dict-only payloads."""
        from agents.llm_risk_auditor import LLMRiskAuditor
        from desktop.engine.ib_engine import MarketSnapshot, ChainRow

        auditor = self._make_auditor()
        engine = AsyncMock()
        engine.get_market_snapshot = AsyncMock(return_value=MarketSnapshot(
            symbol="MES",
            last=5862.0,
            bid=5861.5,
            ask=5862.5,
            high=5870.0,
            low=5850.0,
            close=5855.0,
            volume=10,
            timestamp="2026-03-06T10:30:00Z",
        ))
        engine.get_chain = AsyncMock(return_value=[
            ChainRow("MES", "20260321", 5850.0, "C", 1, 15.0, 15.5, 15.25, 0, 0, 0.2, 0.4, 0.01, -2.0, 5.0),
            ChainRow("MES", "20260321", 5850.0, "P", 2, 13.0, 13.5, 13.25, 0, 0, 0.2, -0.4, 0.01, -2.0, 5.0),
        ])

        result = _run(auditor._fetch_market_data_context(
            ib_engine=engine,
            underlying="MES",
            active_expiry="20260321",
        ))

        assert "Live Market Data" in result
        assert "5850" in result
        assert "15.00" in result
