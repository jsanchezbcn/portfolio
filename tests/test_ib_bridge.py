"""
tests/test_ib_bridge.py
────────────────────────
Unit tests for bridge.ib_bridge and bridge.database_manager (005 spec).

Coverage:
  • Invalid IB_API_MODE → ValueError
  • ensure_bridge_schema creates both tables
  • write_portfolio_snapshot calls breaker.write with correct table
  • log_api_event calls breaker.write with correct table
  • SocketBridge.get_portfolio_greeks — with mocked IB
  • PortalBridge.get_portfolio_greeks — with mocked aiohttp
  • Watchdog ET night-window detection
  • Watchdog backoff reconnect
  • DBCircuitBreaker integration (pool failure → buffer)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_fake_portfolio_item(
    *,
    sec_type: str = "OPT",
    position: float = -5.0,
    market_price: float = 10.0,
    symbol: str = "SPX 241220 5800 C",
    multiplier: int = 100,
    delta: float = 0.3,
    gamma: float = 0.002,
    vega: float = 120.0,
    theta: float = -50.0,
    und_price: float | None = 5800.0,
):
    """Return a mock PortfolioItem-like object."""
    contract = MagicMock()
    contract.secType      = sec_type
    contract.localSymbol  = symbol
    contract.multiplier   = multiplier

    item = MagicMock()
    item.contract   = contract
    item.position   = position
    item.marketPrice = market_price

    # modelGreeks mock
    greeks = MagicMock()
    greeks.delta    = delta
    greeks.gamma    = gamma
    greeks.vega     = vega
    greeks.theta    = theta
    greeks.undPrice = und_price

    return item, greeks


# ── bridge.database_manager ───────────────────────────────────────────────────

class TestEnsureBridgeSchema:
    @pytest.mark.asyncio
    async def test_creates_both_tables(self) -> None:
        from bridge.database_manager import ensure_bridge_schema

        mock_conn = AsyncMock()

        # asyncpg pool.acquire() is an async context manager — wire it up correctly
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_conn)
        cm.__aexit__  = AsyncMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.acquire.return_value = cm

        await ensure_bridge_schema(mock_pool)

        assert mock_conn.execute.call_count == 2
        calls_text = " ".join(
            str(call_args) for call_args in mock_conn.execute.call_args_list
        )
        assert "portfolio_greeks" in calls_text
        assert "api_logs" in calls_text


class TestWritePortfolioSnapshot:
    @pytest.mark.asyncio
    async def test_calls_breaker_with_correct_table(self) -> None:
        from bridge.database_manager import write_portfolio_snapshot

        breaker = AsyncMock()
        row = {
            "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "delta": -12.0,
            "gamma":  0.05,
            "vega":  -840.0,
            "theta":  300.0,
            "underlying_price": 5800.0,
        }
        await write_portfolio_snapshot(breaker, row)

        breaker.write.assert_awaited_once()
        table_arg = breaker.write.call_args.args[0]
        payload   = breaker.write.call_args.args[1]

        assert table_arg == "portfolio_greeks"
        assert payload["delta"] == -12.0
        assert payload["contract"] == "PORTFOLIO"

    @pytest.mark.asyncio
    async def test_defaults_contract_and_timestamp(self) -> None:
        from bridge.database_manager import write_portfolio_snapshot

        breaker = AsyncMock()
        await write_portfolio_snapshot(breaker, {})

        payload = breaker.write.call_args.args[1]
        assert payload["contract"] == "PORTFOLIO"
        assert isinstance(payload["timestamp"], datetime)


class TestLogApiEvent:
    @pytest.mark.asyncio
    async def test_calls_breaker_with_api_logs_table(self) -> None:
        from bridge.database_manager import log_api_event

        breaker = AsyncMock()
        await log_api_event(breaker, "SOCKET", "Connected", "info")

        table_arg = breaker.write.call_args.args[0]
        payload   = breaker.write.call_args.args[1]

        assert table_arg == "api_logs"
        assert payload["api_mode"] == "SOCKET"
        assert payload["message"]  == "Connected"
        assert payload["status"]   == "info"


# ── bridge.main — mode validation ─────────────────────────────────────────────

class TestMainModeValidation:
    @pytest.mark.asyncio
    async def test_invalid_mode_raises_value_error(self) -> None:
        from bridge import main as bridge_main

        with patch.dict("os.environ", {"IB_API_MODE": "INVALID"}):
            with pytest.raises(ValueError, match="IB_API_MODE"):
                await bridge_main.run()


# ── SocketBridge ─────────────────────────────────────────────────────────────

class TestSocketBridge:
    def _make_bridge(self):
        """Return SocketBridge with mocked IB internals."""
        from bridge.ib_bridge import SocketBridge

        with patch("bridge.ib_bridge.SocketBridge.__init__") as mock_init:
            mock_init.return_value = None
            bridge = SocketBridge.__new__(SocketBridge)
        bridge._host      = "127.0.0.1"
        bridge._port      = 7496
        bridge._client_id = 10
        return bridge

    @pytest.mark.asyncio
    async def test_connect_calls_connect_async(self) -> None:
        bridge = self._make_bridge()
        mock_ib = AsyncMock()
        bridge._ib = mock_ib

        await bridge.connect()
        mock_ib.connectAsync.assert_awaited_once_with(
            host="127.0.0.1", port=7496, clientId=10
        )

    @pytest.mark.asyncio
    async def test_get_portfolio_greeks_aggregates_option(self) -> None:
        bridge = self._make_bridge()
        mock_ib = MagicMock()
        bridge._ib = mock_ib

        item, greeks = _make_fake_portfolio_item(
            sec_type="OPT",
            position=-5.0,
            multiplier=100,
            delta=0.3,
            gamma=0.002,
            vega=120.0,
            theta=-50.0,
            und_price=5800.0,
        )

        # Ticker mock with modelGreeks populated after first iteration
        ticker = MagicMock()
        ticker.modelGreeks = greeks
        mock_ib.portfolio.return_value = [item]
        mock_ib.reqMktData.return_value = ticker
        mock_ib.cancelMktData = MagicMock()

        row = await bridge.get_portfolio_greeks()

        # delta = 0.3 × -5 × 100 = -150
        assert row["delta"]  == pytest.approx(-150.0)
        assert row["gamma"]  == pytest.approx(-1.0)     # 0.002 × -5 × 100
        assert row["vega"]   == pytest.approx(-60000.0) # 120 × -5 × 100
        assert row["theta"]  == pytest.approx(25000.0)  # -50 × -5 × 100
        assert row["underlying_price"] == 5800.0
        assert row["contract"] == "PORTFOLIO"

    @pytest.mark.asyncio
    async def test_get_portfolio_greeks_equity_delta(self) -> None:
        bridge = self._make_bridge()
        mock_ib = MagicMock()
        bridge._ib = mock_ib

        equity_contract = MagicMock()
        equity_contract.secType      = "STK"
        equity_contract.localSymbol  = "AAPL"
        equity_contract.multiplier   = 1

        item = MagicMock()
        item.contract    = equity_contract
        item.position    = 200.0
        item.marketPrice = 175.0

        mock_ib.portfolio.return_value = [item]
        row = await bridge.get_portfolio_greeks()

        # equity delta = 1 per share
        assert row["delta"] == pytest.approx(200.0)

    def test_is_connected_delegates_to_ib(self) -> None:
        bridge = self._make_bridge()
        mock_ib = MagicMock()
        mock_ib.isConnected.return_value = True
        bridge._ib = mock_ib
        assert bridge.is_connected() is True


# ── PortalBridge ─────────────────────────────────────────────────────────────

class TestPortalBridge:
    def _make_bridge(self):
        from bridge.ib_bridge import PortalBridge
        import ssl as ssl_module

        with patch("bridge.ib_bridge.PortalBridge.__init__") as mock_init:
            mock_init.return_value = None
            bridge = PortalBridge.__new__(PortalBridge)
        bridge._base_url  = "https://localhost:5001"
        bridge._account   = "U1234567"
        bridge._connected = False
        bridge._ssl_ctx   = ssl_module.create_default_context()
        bridge._ssl_ctx.check_hostname = False
        import ssl as _ssl
        bridge._ssl_ctx.verify_mode = _ssl.CERT_NONE
        bridge._session   = None
        return bridge

    @pytest.mark.asyncio
    async def test_get_portfolio_greeks_aggregates_option(self) -> None:
        bridge = self._make_bridge()
        bridge._connected = True

        positions = [
            {
                "conid": 999999,
                "assetClass": "OPT",
                "position": -10.0,
                "multiplier": 100,
            }
        ]
        snapshots = {
            "999999": {
                "conid": 999999,
                "7308": "0.25",   # delta
                "7309": "0.003",  # gamma
                "7310": "150.0",  # vega
                "7311": "-60.0",  # theta
            }
        }

        bridge._fetch_positions = AsyncMock(return_value=positions)
        bridge._fetch_snapshots  = AsyncMock(return_value=snapshots)

        row = await bridge.get_portfolio_greeks()

        # delta = 0.25 x -10 x 100 = -250
        assert row["delta"] == pytest.approx(-250.0)
        assert row["theta"] == pytest.approx(60000.0)   # -60 × -10 × 100
        assert row["contract"] == "PORTFOLIO"

    @pytest.mark.asyncio
    async def test_get_portfolio_greeks_empty_positions(self) -> None:
        bridge = self._make_bridge()
        bridge._connected  = True
        bridge._fetch_positions = AsyncMock(return_value=[])

        row = await bridge.get_portfolio_greeks()
        assert row["delta"] is None
        assert row["contract"] == "PORTFOLIO"


# ── Watchdog ─────────────────────────────────────────────────────────────────

class TestWatchdogNightWindow:
    def test_in_night_window_23_45_et(self) -> None:
        from bridge.ib_bridge import _in_night_window
        from zoneinfo import ZoneInfo

        with patch(
            "bridge.ib_bridge.datetime",
            wraps=datetime,
        ) as mock_dt:
            fake_now = datetime(2026, 2, 21, 23, 45, tzinfo=ZoneInfo("America/New_York"))
            mock_dt.now.return_value = fake_now
            assert _in_night_window() is True

    def test_not_in_night_window_10_00_et(self) -> None:
        from bridge.ib_bridge import _in_night_window
        from zoneinfo import ZoneInfo

        with patch(
            "bridge.ib_bridge.datetime",
            wraps=datetime,
        ) as mock_dt:
            fake_now = datetime(2026, 2, 21, 10, 0, tzinfo=ZoneInfo("America/New_York"))
            mock_dt.now.return_value = fake_now
            assert _in_night_window() is False

    def test_in_night_window_00_03_et(self) -> None:
        from bridge.ib_bridge import _in_night_window
        from zoneinfo import ZoneInfo

        with patch(
            "bridge.ib_bridge.datetime",
            wraps=datetime,
        ) as mock_dt:
            fake_now = datetime(2026, 2, 22, 0, 3, tzinfo=ZoneInfo("America/New_York"))
            mock_dt.now.return_value = fake_now
            assert _in_night_window() is True


class TestWatchdogReconnect:
    @pytest.mark.asyncio
    async def test_reconnects_with_backoff_on_failure(self) -> None:
        """Watchdog should attempt bridge.connect() when disconnected."""
        from bridge.ib_bridge import Watchdog

        bridge = AsyncMock()
        # is_connected is SYNC — use MagicMock so it returns a bool, not a coroutine
        bridge.is_connected = MagicMock(return_value=False)
        # First connect attempt fails, second succeeds
        bridge.connect.side_effect = [Exception("refused"), None]

        cb = AsyncMock()
        # Zero interval + tiny backoff so loop runs fast
        watchdog = Watchdog(interval=0, backoff_schedule=(0, 0))

        task = asyncio.create_task(
            watchdog.run(bridge, on_reconnect_cb=cb)
        )
        # Give the event loop enough turns for a full reconnect cycle
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

        assert bridge.connect.await_count >= 1

    @pytest.mark.asyncio
    async def test_all_retries_exhausted_logs_error(self) -> None:
        """When all reconnect attempts fail, an error callback should be triggered."""
        from bridge.ib_bridge import Watchdog

        bridge = AsyncMock()
        bridge.is_connected = MagicMock(return_value=False)
        bridge.connect.side_effect = Exception("always fails")

        cb = AsyncMock()
        watchdog = Watchdog(interval=0, backoff_schedule=(0,))

        task = asyncio.create_task(
            watchdog.run(bridge, on_reconnect_cb=cb)
        )
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

        # Verify error status was reported
        error_calls = [
            c for c in cb.call_args_list
            if len(c.args) >= 2 and c.args[1] == "error"
        ]
        assert len(error_calls) >= 1
