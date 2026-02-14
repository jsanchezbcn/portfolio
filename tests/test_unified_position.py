from datetime import date

import pytest
from pydantic import ValidationError

from models.unified_position import InstrumentType, UnifiedPosition


def test_equity_position_defaults() -> None:
    position = UnifiedPosition(
        symbol="AAPL",
        instrument_type=InstrumentType.EQUITY,
        broker="ibkr",
        quantity=10,
        avg_price=180.0,
        market_value=1810.0,
        unrealized_pnl=10.0,
    )

    assert position.delta == 0.0
    assert position.gamma == 0.0
    assert position.theta == 0.0
    assert position.vega == 0.0
    assert position.dte_bucket == "N/A"


def test_option_requires_fields() -> None:
    with pytest.raises(ValidationError):
        UnifiedPosition(
            symbol="AAPL 20MAR26 180 C",
            instrument_type=InstrumentType.OPTION,
            broker="ibkr",
            quantity=1,
            avg_price=2.0,
            market_value=200.0,
            unrealized_pnl=10.0,
        )


def test_option_bucket_ranges() -> None:
    option = UnifiedPosition(
        symbol="AAPL 20MAR26 180 C",
        instrument_type=InstrumentType.OPTION,
        broker="ibkr",
        quantity=1,
        avg_price=2.0,
        market_value=200.0,
        unrealized_pnl=10.0,
        underlying="AAPL",
        strike=180.0,
        expiration=date(2026, 3, 20),
        option_type="call",
        days_to_expiration=31,
    )

    assert option.dte_bucket == "31-60"
