"""
desktop/tests/test_whatif_order.py — Unit tests for WhatIf order simulations.

Tests the whatif_order() async method with various leg configurations.
"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock
from types import SimpleNamespace
from datetime import datetime, timedelta

from desktop.engine.ib_engine import IBEngine


@pytest.fixture
def ib_engine():
    """Create an IBEngine instance with mocked IB connection."""
    engine = IBEngine()
    engine._ib = MagicMock()
    engine._db = MagicMock()
    engine._db_ok = False
    engine._account_id = "U123"
    engine._chain_cache = {}
    engine._positions_snapshot = []
    engine._greeks_cache = {}
    engine._symbol_betas = {}
    engine._beta_default = 1.0
    return engine


class MockOrderState:
    """Mock IB OrderState object with margin attributes."""
    def __init__(
        self,
        init_margin_change=1000.0,
        maint_margin_change=500.0,
        equity_with_loan_change=-1000.0,
    ):
        self.initMarginChange = init_margin_change
        self.maintMarginChange = maint_margin_change
        self.equityWithLoanChange = equity_with_loan_change
        self.status = "Submitted"


class TestWhatIfSingleLeg:
    """Test WhatIf with single stock order."""

    @pytest.mark.asyncio
    async def test_whatif_single_stock_buy(self, ib_engine):
        """Test single-leg stock BUY order."""
        # Mock contract qualification
        stock_contract = SimpleNamespace(
            conId=756646,  # SPY conId
            symbol="SPY",
            secType="STK",
            exchange="SMART",
            currency="USD",
            strike=0.0,
            right="",
            lastTradeDateOrContractMonth="",
        )
        ib_engine._ib.qualifyContractsAsync = AsyncMock(return_value=[stock_contract])

        # Mock WhatIf response
        order_state = MockOrderState(
            init_margin_change=5000.0,
            maint_margin_change=3000.0,
            equity_with_loan_change=-5000.0,
        )
        ib_engine._ib.whatIfOrderAsync = AsyncMock(return_value=order_state)

        legs = [{"symbol": "SPY", "action": "BUY", "qty": 10}]
        result = await ib_engine.whatif_order(legs)

        # Verify result structure
        assert isinstance(result, dict)
        assert result["status"] == "success"
        assert result["init_margin_change"] == 5000.0
        assert result["maint_margin_change"] == 3000.0
        assert result["equity_with_loan_change"] == -5000.0
        assert ib_engine._ib.whatIfOrderAsync.called

    @pytest.mark.asyncio
    async def test_whatif_single_option_call(self, ib_engine):
        """Test single-leg option SELL call."""
        # Mock option contract
        option_contract = SimpleNamespace(
            conId=123456789,
            symbol="SPY",
            secType="OPT",
            exchange="CBOE",
            currency="USD",
            strike=450.0,
            right="C",
            lastTradeDateOrContractMonth="20260418",
        )
        ib_engine._ib.qualifyContractsAsync = AsyncMock(return_value=[option_contract])

        # Mock WhatIf response (selling call generates credit)
        order_state = MockOrderState(
            init_margin_change=-2000.0,  # Reduces margin requirement
            maint_margin_change=-1200.0,
            equity_with_loan_change=3500.0,  # Credit received
        )
        ib_engine._ib.whatIfOrderAsync = AsyncMock(return_value=order_state)

        legs = [
            {
                "symbol": "SPY",
                "action": "SELL",
                "qty": 5,
                "strike": 450.0,
                "right": "C",
                "expiry": "20260418",
            }
        ]
        result = await ib_engine.whatif_order(legs)

        assert result["status"] == "success"
        assert result["init_margin_change"] == -2000.0  # Negative = margin freed
        assert result["maint_margin_change"] == -1200.0
        assert result["equity_with_loan_change"] == 3500.0
        assert ib_engine._ib.whatIfOrderAsync.called


class TestWhatIfMultiLeg:
    """Test WhatIf with multi-leg orders (spreads)."""

    @pytest.mark.asyncio
    async def test_whatif_call_spread_buy_low_sell_high(self, ib_engine):
        """Test call spread: BUY 450C, SELL 460C."""
        call_450 = SimpleNamespace(
            conId=111111111,
            symbol="SPY",
            secType="OPT",
            exchange="CBOE",
            currency="USD",
            strike=450.0,
            right="C",
            lastTradeDateOrContractMonth="20260418",
        )
        call_460 = SimpleNamespace(
            conId=222222222,
            symbol="SPY",
            secType="OPT",
            exchange="CBOE",
            currency="USD",
            strike=460.0,
            right="C",
            lastTradeDateOrContractMonth="20260418",
        )
        ib_engine._ib.qualifyContractsAsync = AsyncMock(
            return_value=[call_450, call_460]
        )

        # Call spread requires less margin than single long call
        order_state = MockOrderState(
            init_margin_change=2000.0,  # Max loss × multiplier
            maint_margin_change=1200.0,
            equity_with_loan_change=-2000.0,
        )
        ib_engine._ib.whatIfOrderAsync = AsyncMock(return_value=order_state)

        legs = [
            {
                "symbol": "SPY",
                "action": "BUY",
                "qty": 5,
                "strike": 450.0,
                "right": "C",
                "expiry": "20260418",
            },
            {
                "symbol": "SPY",
                "action": "SELL",
                "qty": 5,
                "strike": 460.0,
                "right": "C",
                "expiry": "20260418",
            },
        ]
        result = await ib_engine.whatif_order(legs)

        assert result["status"] == "success"
        assert result["init_margin_change"] == 2000.0
        # Verify BAG contract was used
        assert ib_engine._ib.whatIfOrderAsync.called


class TestWhatIfErrors:
    """Test WhatIf error handling."""

    @pytest.mark.asyncio
    async def test_whatif_timeout(self, ib_engine):
        """Test WhatIf timeout handling."""
        stock_contract = SimpleNamespace(
            conId=756646,
            symbol="SPY",
            secType="STK",
            exchange="SMART",
            currency="USD",
            strike=0.0,
            right="",
            lastTradeDateOrContractMonth="",
        )
        ib_engine._ib.qualifyContractsAsync = AsyncMock(return_value=[stock_contract])

        # Simulate timeout
        async def timeout_whatif(*args, **kwargs):
            raise asyncio.TimeoutError()

        ib_engine._ib.whatIfOrderAsync = AsyncMock(side_effect=timeout_whatif)

        legs = [{"symbol": "SPY", "action": "BUY", "qty": 10}]
        result = await ib_engine.whatif_order(legs)

        assert result["status"] == "timeout"
        assert "timed out" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_whatif_fop_infers_missing_expiry(self, ib_engine):
        """FOP legs missing expiry should infer nearest available expiry before qualification."""
        ib_engine.get_position_expiries = MagicMock(return_value=[])
        ib_engine.get_available_expiries = AsyncMock(return_value=["20260430"])

        captured: dict[str, str] = {}

        async def qualify_capture(*contracts):
            captured["expiry"] = str(getattr(contracts[0], "lastTradeDateOrContractMonth", ""))
            contract = contracts[0]
            contract.conId = 999999
            return [contract]

        ib_engine._ib.qualifyContractsAsync = AsyncMock(side_effect=qualify_capture)
        ib_engine._ib.whatIfOrderAsync = AsyncMock(return_value=MockOrderState())

        legs = [
            {
                "symbol": "MES",
                "sec_type": "FOP",
                "action": "BUY",
                "qty": 1,
                "strike": 5850.0,
                "right": "C",
                "expiry": "",
            }
        ]

        result = await ib_engine.whatif_order(legs)

        assert result["status"] == "success"
        assert captured["expiry"] == "20260430"

    @pytest.mark.asyncio
    async def test_whatif_no_contracts_qualified(self, ib_engine):
        """Test WhatIf when no contracts can be qualified."""
        # Empty return from qualification
        ib_engine._ib.qualifyContractsAsync = AsyncMock(return_value=[])

        legs = [{"symbol": "INVALID_SYMBOL", "action": "BUY", "qty": 10}]
        result = await ib_engine.whatif_order(legs)

        assert result["status"] == "error" or "error" in result
        assert "No contracts qualified" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_whatif_partial_qualification(self, ib_engine):
        """Test WhatIf fails when only some legs qualify."""
        call_contract = SimpleNamespace(
            conId=111111111,
            symbol="SPY",
            secType="OPT",
            exchange="CBOE",
            currency="USD",
            strike=450.0,
            right="C",
            lastTradeDateOrContractMonth="20260418",
        )
        # Only one of two legs qualifies
        ib_engine._ib.qualifyContractsAsync = AsyncMock(return_value=[call_contract])

        legs = [
            {
                "symbol": "SPY",
                "action": "BUY",
                "qty": 5,
                "strike": 450.0,
                "right": "C",
                "expiry": "20260418",
            },
            {
                "symbol": "SPY",
                "action": "SELL",
                "qty": 5,
                "strike": 460.0,
                "right": "C",
                "expiry": "20260418",
            },
        ]
        result = await ib_engine.whatif_order(legs)

        assert result["status"] == "error"
        assert "Only 1/2" in result.get("error", "")


class TestWhatIfWithRealValues:
    """Test WhatIf with realistic margin values."""

    @pytest.mark.asyncio
    async def test_whatif_iron_condor_margin(self, ib_engine):
        """Test iron condor margin requirements."""
        # Mock 4 contracts: BUY P, SELL P, SELL C, BUY C
        put_long = SimpleNamespace(
            conId=111, symbol="ES", secType="FOP", strike=6750.0, right="P"
        )
        put_short = SimpleNamespace(
            conId=222, symbol="ES", secType="FOP", strike=6775.0, right="P"
        )
        call_short = SimpleNamespace(
            conId=333, symbol="ES", secType="FOP", strike=6825.0, right="C"
        )
        call_long = SimpleNamespace(
            conId=444, symbol="ES", secType="FOP", strike=6850.0, right="C"
        )

        ib_engine._ib.qualifyContractsAsync = AsyncMock(
            return_value=[put_long, put_short, call_short, call_long]
        )

        # Iron condor margin = max width × multiplier
        # Width = 25 points × 50 (ES multiplier) = 1250
        order_state = MockOrderState(
            init_margin_change=1250.0,
            maint_margin_change=937.5,
            equity_with_loan_change=-1250.0,
        )
        ib_engine._ib.whatIfOrderAsync = AsyncMock(return_value=order_state)

        legs = [
            {
                "symbol": "ES",
                "action": "BUY",
                "qty": 1,
                "strike": 6750.0,
                "right": "P",
                "expiry": "20260430",
            },
            {
                "symbol": "ES",
                "action": "SELL",
                "qty": 1,
                "strike": 6775.0,
                "right": "P",
                "expiry": "20260430",
            },
            {
                "symbol": "ES",
                "action": "SELL",
                "qty": 1,
                "strike": 6825.0,
                "right": "C",
                "expiry": "20260430",
            },
            {
                "symbol": "ES",
                "action": "BUY",
                "qty": 1,
                "strike": 6850.0,
                "right": "C",
                "expiry": "20260430",
            },
        ]
        result = await ib_engine.whatif_order(legs)

        assert result["status"] == "success"
        assert result["init_margin_change"] == 1250.0
        assert result["maint_margin_change"] == 937.5


class TestWhatIfNullHandling:
    """Test WhatIf null value handling."""

    @pytest.mark.asyncio
    async def test_whatif_null_margin_values(self, ib_engine):
        """Test handling of null margin values from IB."""
        stock_contract = SimpleNamespace(
            conId=756646,
            symbol="SPY",
            secType="STK",
            exchange="SMART",
            currency="USD",
            strike=0.0,
            right="",
            lastTradeDateOrContractMonth="",
        )
        ib_engine._ib.qualifyContractsAsync = AsyncMock(return_value=[stock_contract])

        # Return OrderState with None values
        bad_order_state = MockOrderState()
        bad_order_state.initMarginChange = None
        bad_order_state.maintMarginChange = None
        bad_order_state.equityWithLoanChange = None

        ib_engine._ib.whatIfOrderAsync = AsyncMock(return_value=bad_order_state)

        legs = [{"symbol": "SPY", "action": "BUY", "qty": 10}]
        result = await ib_engine.whatif_order(legs)

        # Should handle None values gracefully (convert to 0.0)
        assert result["status"] == "success"
        assert result["init_margin_change"] == 0.0
        assert result["maint_margin_change"] == 0.0
        assert result["equity_with_loan_change"] == 0.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
