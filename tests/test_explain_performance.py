"""
Tests for skills/explain_performance.py — User Story 4: Natural-language trade explanation.

TDD: written BEFORE implementation (T037–T040).

Test IDs:
- T037: happy path — thesis + Greeks + sentiment → string mentions thesis + Greek name
- T038: Vega-drag path — entry Vega=-50, current Vega=-150 → "vega" in output
- T039: unknown trade_id → returns message containing "not found"
- T040: missing market_intel → skill returns explanation without crashing
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skills.explain_performance import ExplainPerformanceSkill


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def journal_entry(
    trade_id: str = "uuid-trade-001",
    symbol: str = "AAPL",
    entry_greeks: dict[str, Any] | None = None,
    thesis: str = "Long vol play ahead of earnings",
) -> dict[str, Any]:
    return {
        "trade_id": trade_id,
        "symbol": symbol,
        "entry_greeks_json": entry_greeks
        or {"delta": 0.3, "gamma": 0.02, "theta": -15.0, "vega": -50.0, "iv": 0.28},
        "thesis": thesis,
    }


def current_greeks(
    delta: float = 0.25,
    gamma: float = 0.018,
    theta: float = -12.0,
    vega: float = -45.0,
    iv: float = 0.25,
) -> dict[str, Any]:
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "iv": iv}


def market_intel_rows(symbol: str = "AAPL", score: float = 0.5) -> list[dict[str, Any]]:
    return [
        {
            "id": "uuid-intel-001",
            "symbol": symbol,
            "source": "alpaca",
            "content": "Positive earnings beat expectations",
            "sentiment_score": score,
        }
    ]


@pytest.fixture
def mock_db() -> MagicMock:
    db = MagicMock()
    db.get_trade_journal_entry = AsyncMock(return_value=journal_entry())
    db.get_market_intel_for_trade = AsyncMock(
        return_value=market_intel_rows()
    )
    return db


@pytest.fixture
def mock_options_cache() -> MagicMock:
    cache = MagicMock()
    cache.fetch_and_cache_options_for_underlying = AsyncMock(return_value=[])
    return cache


@pytest.fixture
def skill(mock_db: MagicMock, mock_options_cache: MagicMock) -> ExplainPerformanceSkill:
    return ExplainPerformanceSkill(
        db=mock_db,
        options_cache=mock_options_cache,
        llm_model="gpt-4o-mini",
    )


# ---------------------------------------------------------------------------
# T037 — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_happy_path(skill: ExplainPerformanceSkill, mock_db: MagicMock) -> None:
    """T037: Returned string mentions original thesis and at least one Greek by name."""
    thesis_text = "Long vol play ahead of earnings"
    mock_db.get_trade_journal_entry = AsyncMock(
        return_value=journal_entry(thesis=thesis_text)
    )

    current = current_greeks(delta=0.25, vega=-45.0)

    with patch.object(skill, "_fetch_current_greeks", AsyncMock(return_value=current)):
        with patch.object(
            skill, "_call_llm", AsyncMock(return_value="The trade thesis was: Long vol play ahead of earnings. Delta has decreased from 0.30 to 0.25.")
        ):
            result = await skill.explain("uuid-trade-001")

    assert isinstance(result, str)
    assert len(result) > 0
    # Must mention the thesis
    assert "Long vol" in result or "earnings" in result
    # Must mention at least one Greek
    greek_names = ["delta", "gamma", "theta", "vega", "iv"]
    assert any(g in result.lower() for g in greek_names)


# ---------------------------------------------------------------------------
# T038 — Vega-drag path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_vega_drag(skill: ExplainPerformanceSkill, mock_db: MagicMock) -> None:
    """T038: entry Vega=-50, current Vega=-150 → 'vega' appears in output."""
    entry = journal_entry(
        entry_greeks={"delta": 0.3, "gamma": 0.02, "theta": -15.0, "vega": -50.0, "iv": 0.28}
    )
    mock_db.get_trade_journal_entry = AsyncMock(return_value=entry)

    current = current_greeks(vega=-150.0)

    with patch.object(skill, "_fetch_current_greeks", AsyncMock(return_value=current)):
        with patch.object(
            skill,
            "_call_llm",
            AsyncMock(return_value="Vega drag is significant: position went from -50 to -150 Vega."),
        ):
            result = await skill.explain("uuid-trade-001")

    assert "vega" in result.lower()


# ---------------------------------------------------------------------------
# T039 — unknown trade_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_unknown_trade_id(skill: ExplainPerformanceSkill, mock_db: MagicMock) -> None:
    """T039: Unknown trade_id → returns user-facing message containing 'not found'."""
    mock_db.get_trade_journal_entry = AsyncMock(return_value=None)

    result = await skill.explain("nonexistent-trade-id")

    assert isinstance(result, str)
    assert "not found" in result.lower()


# ---------------------------------------------------------------------------
# T040 — missing market_intel rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_missing_market_intel(
    skill: ExplainPerformanceSkill, mock_db: MagicMock
) -> None:
    """T040: trade_journal row exists but no market_intel rows → no crash."""
    mock_db.get_trade_journal_entry = AsyncMock(return_value=journal_entry())
    mock_db.get_market_intel_for_trade = AsyncMock(return_value=[])

    current = current_greeks()

    with patch.object(skill, "_fetch_current_greeks", AsyncMock(return_value=current)):
        with patch.object(
            skill,
            "_call_llm",
            AsyncMock(return_value="No sentiment data available. Trade is performing steadily."),
        ):
            result = await skill.explain("uuid-trade-001")

    assert isinstance(result, str)
    assert len(result) > 0
