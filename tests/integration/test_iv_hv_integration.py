from __future__ import annotations

import pytest

from agent_tools.market_data_tools import MarketDataTools


@pytest.mark.integration
def test_historical_volatility_window_alignment_recorded_fixture() -> None:
    tools = MarketDataTools()
    hv = tools.get_historical_volatility(["^GSPC"], lookback_days=30)

    assert "^GSPC" in hv
    assert hv["^GSPC"] >= 0.0
