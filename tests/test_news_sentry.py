"""
Tests for agents/news_sentry.py — User Story 2: Automated sentiment score.

TDD: written BEFORE implementation (T018–T020).

Test IDs:
- T018: fetch_and_score() — 3 mock headlines → LLM called once, record written
- T019: no_news path — empty list → record with sentiment_score=None, content="no_news"
- T020: resilience — httpx.HTTPError caught, nothing written to DB
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch, call
from typing import Any

import httpx
import pytest

from agents.news_sentry import NewsSentry


# ---------------------------------------------------------------------------
# Helper fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db() -> MagicMock:
    db = MagicMock()
    db.insert_market_intel = AsyncMock(return_value="uuid-intel-001")
    return db


@pytest.fixture
def sentry(mock_db: MagicMock) -> NewsSentry:
    return NewsSentry(symbols=["AAPL"], db=mock_db, interval_seconds=900)


# ---------------------------------------------------------------------------
# T018 — happy path: 3 headlines → LLM called once → record written
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_and_score_happy_path(
    sentry: NewsSentry, mock_db: MagicMock
) -> None:
    """T018: 3 headlines → LLM called once, SentimentRecord written with score in [-1,+1]."""
    headlines = [
        "Apple reports record revenue",
        "iPhone sales beat estimates",
        "Apple expands into new markets",
    ]

    with patch.object(sentry, "_fetch_news", AsyncMock(return_value=headlines)):
        with patch.object(
            sentry,
            "_score_sentiment",
            AsyncMock(return_value=(0.7, "Positive earnings beat expectations")),
        ) as mock_score:
            await sentry.fetch_and_score("AAPL")

    # LLM called exactly once
    mock_score.assert_awaited_once_with(headlines, "AAPL")

    # DB write with correct score
    mock_db.insert_market_intel.assert_awaited_once()
    call_kwargs = mock_db.insert_market_intel.call_args.kwargs
    assert call_kwargs["symbol"] == "AAPL"
    assert call_kwargs["sentiment_score"] == 0.7
    assert -1.0 <= call_kwargs["sentiment_score"] <= 1.0
    # Summary length in words ≤ 50
    summary: str = call_kwargs["content"]
    assert len(summary.split()) <= 50


@pytest.mark.asyncio
async def test_score_clamped_to_bounds(
    sentry: NewsSentry, mock_db: MagicMock
) -> None:
    """T018: Scores outside [-1, +1] from LLM must be clamped."""
    headlines = ["Some headline"]

    with patch.object(sentry, "_fetch_news", AsyncMock(return_value=headlines)):
        with patch.object(
            sentry,
            "_score_sentiment",
            AsyncMock(return_value=(2.5, "Very bullish")),
        ):
            await sentry.fetch_and_score("AAPL")

    call_kwargs = mock_db.insert_market_intel.call_args.kwargs
    # Score must be clamped
    assert call_kwargs["sentiment_score"] <= 1.0


# ---------------------------------------------------------------------------
# T019 — no_news path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_and_score_no_news(
    sentry: NewsSentry, mock_db: MagicMock
) -> None:
    """T019: Empty headlines → record with sentiment_score=None, content='no_news'."""
    with patch.object(sentry, "_fetch_news", AsyncMock(return_value=[])):
        await sentry.fetch_and_score("AAPL")

    mock_db.insert_market_intel.assert_awaited_once()
    call_kwargs = mock_db.insert_market_intel.call_args.kwargs
    assert call_kwargs["sentiment_score"] is None
    assert call_kwargs["content"] == "no_news"


# ---------------------------------------------------------------------------
# T020 — resilience: httpx.HTTPError caught, nothing written
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_and_score_http_error_caught(
    sentry: NewsSentry, mock_db: MagicMock
) -> None:
    """T020: httpx.HTTPError from _fetch_news is caught; nothing written to DB."""

    async def _raise_http(*args: Any, **kwargs: Any) -> list[str]:
        raise httpx.HTTPError("Connection refused")

    with patch.object(sentry, "_fetch_news", _raise_http):
        # Should NOT propagate
        await sentry.fetch_and_score("AAPL")

    mock_db.insert_market_intel.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_and_score_generic_error_caught(
    sentry: NewsSentry, mock_db: MagicMock
) -> None:
    """T020 extension: Any unexpected error is caught; ticker loop continues."""

    async def _raise(symbol: str) -> list[str]:
        raise RuntimeError("Unexpected API error")

    with patch.object(sentry, "_fetch_news", _raise):
        await sentry.fetch_and_score("AAPL")  # must not raise

    mock_db.insert_market_intel.assert_not_awaited()
