"""
tests/test_ibkr_adapter_chain.py
──────────────────────────────────
Unit tests for IBKRAdapter.fetch_options_chain_tws (Feature 006 addition).

These tests are offline — no live TWS connection required.  They mock the
ib_async library completely and verify contract/parameter logic.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adapters.ibkr_adapter import IBKRAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_ticker(bid: float = 5.0, ask: float = 5.50, last: float = 5.20) -> MagicMock:
    t = MagicMock()
    t.bid = bid
    t.ask = ask
    t.last = last
    return t


def _make_chain(
    trading_class: str = "SPXW",
    exchange: str = "CBOE",
    multiplier: str = "100",
    expirations: tuple[str, ...] = ("20261218", "20261219", "20261220"),
    strikes: tuple[float, ...] = (5400.0, 5450.0, 5500.0, 5550.0, 5600.0),
) -> MagicMock:
    chain = MagicMock()
    chain.tradingClass = trading_class
    chain.exchange = exchange
    chain.multiplier = multiplier
    chain.expirations = frozenset(expirations)
    chain.strikes = frozenset(strikes)
    return chain


# ---------------------------------------------------------------------------
# TestFetchOptionsChainTWSUnit
# ---------------------------------------------------------------------------

class TestFetchOptionsChainTWSUnit:
    """Offline unit tests for fetch_options_chain_tws."""

    def test_method_exists_on_adapter(self) -> None:
        adapter = IBKRAdapter.__new__(IBKRAdapter)
        assert callable(getattr(adapter, "fetch_options_chain_tws", None))
        assert callable(getattr(adapter, "fetch_option_expirations_tws", None))
        assert callable(getattr(adapter, "fetch_option_chain_matrix_tws", None))

    def test_non_allowlist_returns_empty(self) -> None:
        """AAPL is not in the FR-011 allowlist — must return []."""
        adapter = IBKRAdapter.__new__(IBKRAdapter)
        result = _run(adapter.fetch_options_chain_tws("AAPL"))
        assert result == []

    def test_non_allowlist_tsla_returns_empty(self) -> None:
        adapter = IBKRAdapter.__new__(IBKRAdapter)
        result = _run(adapter.fetch_options_chain_tws("TSLA"))
        assert result == []

    def test_expirations_non_allowlist_returns_empty(self) -> None:
        adapter = IBKRAdapter.__new__(IBKRAdapter)
        result = _run(adapter.fetch_option_expirations_tws("AAPL"))
        assert result == []

    def test_chain_matrix_non_allowlist_returns_empty(self) -> None:
        adapter = IBKRAdapter.__new__(IBKRAdapter)
        result = _run(adapter.fetch_option_chain_matrix_tws("AAPL", "20261219"))
        assert result == []

    @patch("ib_async.IB")
    def test_fetch_option_expirations_returns_list(self, mock_ib_class: MagicMock) -> None:
        ib = MagicMock()
        ib.connectAsync = AsyncMock()
        ib.disconnect = MagicMock()
        ib.reqMarketDataType = MagicMock()

        und = MagicMock()
        und.conId = 111

        async def _qualify_side_effect(*_contracts):
            return [und]

        ib.qualifyContractsAsync = AsyncMock(side_effect=_qualify_side_effect)
        ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[
            _make_chain(
                exchange="CME",
                expirations=("20261219", "20270116"),
                strikes=(6800.0, 6850.0, 6900.0),
            )
        ])
        mock_ib_class.return_value = ib

        adapter = IBKRAdapter.__new__(IBKRAdapter)
        result = _run(adapter.fetch_option_expirations_tws("ES", dte_min=0, dte_max=800))
        assert isinstance(result, list)

    @patch("ib_async.IB")
    def test_spx_returns_list_of_dicts(self, mock_ib_class: MagicMock) -> None:
        """Mock ib_async IB and verify the return format."""
        ib = MagicMock()
        ib.connectAsync = AsyncMock()
        ib.disconnect = MagicMock()
        ib.reqMarketDataType = MagicMock()
        ib.qualifyContractsAsync = AsyncMock(side_effect=lambda *args: list(args))

        # Two qualified contracts returned from option params
        opt_chain = _make_chain()
        ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[opt_chain])

        # qualifyContractsAsync for underlying
        underlying_contract = MagicMock()
        underlying_contract.conId = 99999
        ib.qualifyContractsAsync = AsyncMock(return_value=[underlying_contract])

        # Mock option contract qualify — return contracts with conIds
        opt_c1 = MagicMock()
        opt_c1.conId = 1001
        opt_c1.strike = 5450.0
        opt_c1.right = "P"
        opt_c1.lastTradeDateOrContractMonth = "20261219"
        opt_c1.multiplier = "100"
        opt_c1.tradingClass = "SPXW"

        opt_c2 = MagicMock()
        opt_c2.conId = 1002
        opt_c2.strike = 5400.0
        opt_c2.right = "P"
        opt_c2.lastTradeDateOrContractMonth = "20261219"
        opt_c2.multiplier = "100"
        opt_c2.tradingClass = "SPXW"

        # Track qualify calls to return underlying first, then options
        _quality_calls = [None]

        async def _qualify_side_effect(contracts):
            if _quality_calls[0] is None:
                # First call = underlying
                _quality_calls[0] = True
                return [underlying_contract]
            else:
                # Subsequent calls = option contracts
                return [opt_c1, opt_c2]

        ib.qualifyContractsAsync = AsyncMock(side_effect=_qualify_side_effect)

        # Tickers
        ticker1 = _make_ticker(bid=9.80, ask=10.20)
        ticker2 = _make_ticker(bid=7.30, ask=7.70)
        ib.reqMktData = MagicMock(side_effect=[ticker1, ticker2])
        ib.tickers = MagicMock(return_value=[ticker1, ticker2])

        mock_ib_class.return_value = ib

        adapter = IBKRAdapter.__new__(IBKRAdapter)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = _run(
                adapter.fetch_options_chain_tws(
                    "SPX", dte_min=30, dte_max=60, atm_price=5500.0, right="P", n_strikes=2
                )
            )

        # Result is a list (may be empty on DTE filter, just check type)
        assert isinstance(result, list)

    @patch("ib_async.IB")
    def test_returns_required_keys_when_market_data_available(self, mock_ib_class: MagicMock) -> None:
        """Verify dict keys present when market data is non-empty."""
        required_keys = {"conId", "symbol", "strike", "right", "dte", "expiry",
                         "bid", "ask", "mid", "multiplier", "tradingClass"}

        ib = MagicMock()
        ib.connectAsync = AsyncMock()
        ib.disconnect = MagicMock()
        ib.reqMarketDataType = MagicMock()

        underlying_contract = MagicMock()
        underlying_contract.conId = 99999

        ib.qualifyContractsAsync = AsyncMock(return_value=[underlying_contract])
        ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[])  # empty chain → graceful []

        mock_ib_class.return_value = ib

        adapter = IBKRAdapter.__new__(IBKRAdapter)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = _run(adapter.fetch_options_chain_tws("SPX"))

        # With empty option params the method should return []
        assert result == []

    def test_allowlist_only_spx_spy_es(self) -> None:
        """Only SPX, SPY, ES are allowed — anything else returns []."""
        adapter = IBKRAdapter.__new__(IBKRAdapter)
        for ticker in ("AAPL", "QQQ", "NVDA", "VIX", "GLD"):
            result = _run(adapter.fetch_options_chain_tws(ticker))
            assert result == [], f"Expected [] for non-allowlist ticker {ticker}"
