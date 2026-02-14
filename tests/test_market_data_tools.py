from unittest.mock import Mock, patch
from collections.abc import Sequence

import pandas as pd

from agent_tools.market_data_tools import MarketDataTools


def _history(close_values: Sequence[float]) -> pd.DataFrame:
    return pd.DataFrame({"Close": close_values})


@patch("agent_tools.market_data_tools.yf.Ticker")
def test_get_vix_data(mock_ticker: Mock) -> None:
    mock_vix = Mock()
    mock_vix.history.return_value = _history([18.0, 19.0])

    mock_vix3m = Mock()
    mock_vix3m.history.return_value = _history([20.0, 21.0])

    mock_ticker.side_effect = [mock_vix, mock_vix3m]

    tools = MarketDataTools()
    data = tools.get_vix_data()

    assert data["vix"] == 19.0
    assert data["vix3m"] == 21.0
    assert round(data["term_structure"], 3) == round(21.0 / 19.0, 3)


@patch("agent_tools.market_data_tools.yf.Ticker")
def test_get_spx_data(mock_ticker: Mock) -> None:
    close_prices = [5000 + i for i in range(40)]
    mock_spx = Mock()
    mock_spx.history.return_value = _history(close_prices)
    mock_ticker.return_value = mock_spx

    tools = MarketDataTools()
    data = tools.get_spx_data()

    assert data["spx"] == close_prices[-1]
    assert data["realized_vol_30d"] >= 0.0
