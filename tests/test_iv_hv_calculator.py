from __future__ import annotations

from datetime import date
from unittest.mock import Mock, patch

import pandas as pd

from agent_tools.market_data_tools import MarketDataTools
from agent_tools.portfolio_tools import PortfolioTools
from models.unified_position import InstrumentType, UnifiedPosition


def _history(close_values: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"Close": close_values})


@patch("agent_tools.market_data_tools.yf.Ticker")
def test_get_historical_volatility_calculates_30d(mock_ticker: Mock) -> None:
    closes = [100 + (idx * 0.6) for idx in range(45)]
    mock_obj = Mock()
    mock_obj.history.return_value = _history(closes)
    mock_ticker.return_value = mock_obj

    tools = MarketDataTools()
    hv = tools.get_historical_volatility(["AAPL"], lookback_days=30)

    assert "AAPL" in hv
    assert hv["AAPL"] >= 0.0


def test_get_iv_analysis_classifies_thresholds_and_skips_missing_iv() -> None:
    tools = PortfolioTools()
    positions = [
        UnifiedPosition(
            symbol="AAPL240621C200",
            instrument_type=InstrumentType.OPTION,
            broker="ibkr",
            quantity=1,
            avg_price=1,
            market_value=1,
            unrealized_pnl=0,
            underlying="AAPL",
            strike=200,
            expiration=date(2026, 6, 21),
            option_type="call",
            iv=0.40,
        ),
        UnifiedPosition(
            symbol="MSFT240621P400",
            instrument_type=InstrumentType.OPTION,
            broker="ibkr",
            quantity=1,
            avg_price=1,
            market_value=1,
            unrealized_pnl=0,
            underlying="MSFT",
            strike=400,
            expiration=date(2026, 6, 21),
            option_type="put",
            iv=0.27,
        ),
        UnifiedPosition(
            symbol="NVDA240621C900",
            instrument_type=InstrumentType.OPTION,
            broker="ibkr",
            quantity=1,
            avg_price=1,
            market_value=1,
            unrealized_pnl=0,
            underlying="NVDA",
            strike=900,
            expiration=date(2026, 6, 21),
            option_type="call",
            iv=0.18,
        ),
        UnifiedPosition(
            symbol="AMD240621C180",
            instrument_type=InstrumentType.OPTION,
            broker="ibkr",
            quantity=1,
            avg_price=1,
            market_value=1,
            unrealized_pnl=0,
            underlying="AMD",
            strike=180,
            expiration=date(2026, 6, 21),
            option_type="call",
            iv=None,
        ),
    ]

    hv = {
        "AAPL": 0.22,
        "MSFT": 0.16,
        "NVDA": 0.24,
        "AMD": 0.21,
    }

    analysis = tools.get_iv_analysis(positions, hv)
    by_symbol = {row["underlying"]: row for row in analysis}

    assert len(analysis) == 3
    assert by_symbol["AAPL"]["signal"] == "strong_sell_edge"
    assert by_symbol["MSFT"]["signal"] == "moderate_sell_edge"
    assert by_symbol["NVDA"]["signal"] == "buy_edge"
