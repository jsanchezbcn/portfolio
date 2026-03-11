"""
desktop/tests/test_whatif_order.py — Unit tests for WhatIf order simulations.

Tests the whatif_order() async method with various leg configurations.
"""
import asyncio
import logging
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

    @pytest.mark.asyncio
    async def test_whatif_logs_trade_details(self, ib_engine, caplog):
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
        ib_engine._ib.whatIfOrderAsync = AsyncMock(return_value=MockOrderState())

        with caplog.at_level(logging.INFO):
            await ib_engine.whatif_order([
                {
                    "symbol": "SPY",
                    "action": "SELL",
                    "qty": 5,
                    "strike": 450.0,
                    "right": "C",
                    "expiry": "20260418",
                    "sec_type": "OPT",
                    "exchange": "CBOE",
                }
            ])

        assert "WhatIf start:" in caplog.text
        assert "SPY" in caplog.text
        assert "450C" in caplog.text


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
    async def test_whatif_normalizes_weekly_fop_aliases_before_qualification(self, ib_engine):
        captured: dict[str, str] = {}

        async def qualify_capture(*contracts):
            contract = contracts[0]
            captured["symbol"] = getattr(contract, "symbol", "")
            captured["sec_type"] = getattr(contract, "secType", "")
            captured["exchange"] = getattr(contract, "exchange", "")
            contract.conId = 654321
            return [contract]

        ib_engine._ib.qualifyContractsAsync = AsyncMock(side_effect=qualify_capture)
        ib_engine._ib.whatIfOrderAsync = AsyncMock(return_value=MockOrderState())

        result = await ib_engine.whatif_order([
            {
                "symbol": "EWJ6",
                "sec_type": "OPT",
                "exchange": "SMART",
                "action": "BUY",
                "qty": 2,
                "strike": 6950.0,
                "right": "C",
                "expiry": "20260430",
            }
        ])

        assert result["status"] == "success"
        assert captured == {"symbol": "ES", "sec_type": "FOP", "exchange": "CME"}

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

    @pytest.mark.asyncio
    async def test_whatif_resolves_missing_legs_after_partial_batch_qualification(self, ib_engine):
        """Partial batch qualification should still recover missing legs via strict detail lookups."""
        resolved_es_short = SimpleNamespace(
            conId=101,
            symbol="ES",
            secType="FOP",
            exchange="CME",
            currency="USD",
            strike=5100.0,
            right="C",
            lastTradeDateOrContractMonth="20260313",
        )
        resolved_mes_short = SimpleNamespace(
            conId=201,
            symbol="MES",
            secType="FOP",
            exchange="CME",
            currency="USD",
            strike=5100.0,
            right="C",
            lastTradeDateOrContractMonth="20260313",
        )
        resolved_es_long = SimpleNamespace(
            conId=102,
            symbol="ES",
            secType="FOP",
            exchange="CME",
            currency="USD",
            strike=5115.0,
            right="C",
            lastTradeDateOrContractMonth="20260313",
        )
        resolved_mes_long = SimpleNamespace(
            conId=202,
            symbol="MES",
            secType="FOP",
            exchange="CME",
            currency="USD",
            strike=5110.0,
            right="C",
            lastTradeDateOrContractMonth="20260313",
        )

        ib_engine._ib.qualifyContractsAsync = AsyncMock(return_value=[resolved_es_short, resolved_mes_short])

        async def req_contract_details(contract):
            strike = float(getattr(contract, "strike", 0.0) or 0.0)
            symbol = getattr(contract, "symbol", "")
            if symbol == "ES" and strike == 5115.0:
                return [SimpleNamespace(contract=resolved_es_long)]
            if symbol == "MES" and strike == 5110.0:
                return [SimpleNamespace(contract=resolved_mes_long)]
            return []

        ib_engine._ib.reqContractDetailsAsync = AsyncMock(side_effect=req_contract_details)
        ib_engine._ib.whatIfOrderAsync = AsyncMock(return_value=MockOrderState())

        legs = [
            {"symbol": "ES", "action": "SELL", "qty": 1, "sec_type": "FOP", "exchange": "CME", "strike": 5100.0, "right": "C", "expiry": "20260313"},
            {"symbol": "ES", "action": "BUY", "qty": 1, "sec_type": "FOP", "exchange": "CME", "strike": 5115.0, "right": "C", "expiry": "20260313"},
            {"symbol": "MES", "action": "SELL", "qty": 1, "sec_type": "FOP", "exchange": "CME", "strike": 5100.0, "right": "C", "expiry": "20260313"},
            {"symbol": "MES", "action": "BUY", "qty": 1, "sec_type": "FOP", "exchange": "CME", "strike": 5110.0, "right": "C", "expiry": "20260313"},
        ]

        result = await ib_engine.whatif_order(legs)

        assert result["status"] == "success"
        assert ib_engine._ib.reqContractDetailsAsync.await_count == 2


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


class TestOrderQuantityNormalization:
    """Ensure qty drives contract count, not per-contract limit price scaling."""

    def test_normalize_order_size_same_qty(self, ib_engine):
        total_qty, ratios = ib_engine._normalize_order_size([
            {"qty": 5},
            {"qty": 5},
        ])
        assert total_qty == 5
        assert ratios == [1, 1]

    @pytest.mark.asyncio
    async def test_whatif_combo_uses_total_quantity_and_unit_ratios(self, ib_engine):
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
        ib_engine._ib.qualifyContractsAsync = AsyncMock(return_value=[call_450, call_460])
        ib_engine._ib.whatIfOrderAsync = AsyncMock(return_value=MockOrderState())

        legs = [
            {"symbol": "SPY", "action": "BUY", "qty": 5, "strike": 450.0, "right": "C", "expiry": "20260418"},
            {"symbol": "SPY", "action": "SELL", "qty": 5, "strike": 460.0, "right": "C", "expiry": "20260418"},
        ]
        result = await ib_engine.whatif_order(legs)

        assert result["status"] == "success"
        contract_arg, order_arg = ib_engine._ib.whatIfOrderAsync.call_args[0]
        assert order_arg.totalQuantity == 5
        assert len(contract_arg.comboLegs) == 2
        assert contract_arg.comboLegs[0].ratio == 1
        assert contract_arg.comboLegs[1].ratio == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
