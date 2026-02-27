"""
bridge/ib_bridge.py
────────────────────
IBKR bridge implementations: SOCKET (ib_async) and PORTAL (REST / Client Portal).

Usage
  bridge = SocketBridge()        # IB_API_MODE=SOCKET (default)
  bridge = PortalBridge()        # IB_API_MODE=PORTAL

  await bridge.connect()
  row = await bridge.get_portfolio_greeks()   # → dict ready for DB insert
  await bridge.disconnect()

Watchdog
  watchdog = Watchdog()
  asyncio.create_task(watchdog.run(bridge, on_reconnect_cb=log_api_event_partial))
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

import aiohttp

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

API_MODE_SOCKET = "SOCKET"
API_MODE_PORTAL = "PORTAL"
VALID_API_MODES = frozenset({API_MODE_SOCKET, API_MODE_PORTAL})


def normalize_api_mode(value: str | None) -> str:
    """Normalize and validate IB bridge API mode.

    Accepts values with optional inline comments (e.g. "SOCKET # note").
    Raises ValueError for unsupported modes.
    """
    raw_mode = (value or API_MODE_SOCKET).split("#", 1)[0].strip().upper()
    if raw_mode not in VALID_API_MODES:
        raise ValueError(f"IB_API_MODE must be 'SOCKET' or 'PORTAL', got {raw_mode!r}")
    return raw_mode

# ── Watchdog tuning constants ─────────────────────────────────────────────────
_WATCHDOG_INTERVAL   = 30        # seconds between health checks
_BACKOFF_SCHEDULE    = (5, 10, 20)   # seconds between reconnect retries
_NIGHT_WINDOW_START  = (23, 40)  # (hour, minute) ET — IBKR gateway restarts
_NIGHT_WINDOW_END    = (0,  5)   # (hour, minute) ET — safe to reconnect after
_NIGHT_END_HOUR      = 0
_NIGHT_END_MINUTE    = 5


def _seconds_until_et(hour: int, minute: int) -> float:
    """Return seconds from now until *hour*:*minute* ET (always positive, wraps midnight)."""
    now_et = datetime.now(_ET)
    target = now_et.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_et:
        # already past — target is tomorrow
        from datetime import timedelta
        target += timedelta(days=1)
    return (target - now_et).total_seconds()


def _in_night_window() -> bool:
    """Return True if current ET time is inside the nightly gateway-restart window."""
    now_et = datetime.now(_ET)
    h, m = now_et.hour, now_et.minute
    start_h, start_m = _NIGHT_WINDOW_START
    # window: 23:40 → next day 00:05
    if h == 23 and m >= start_m:
        return True
    if h == _NIGHT_END_HOUR and m < _NIGHT_END_MINUTE:
        return True
    return False


# ── Abstract base ─────────────────────────────────────────────────────────────

class IBridgeBase(ABC):
    """Protocol that both SocketBridge and PortalBridge must satisfy."""

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to IBKR."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Cleanly tear down the connection."""

    @abstractmethod
    async def get_portfolio_greeks(self) -> dict:
        """Return aggregated portfolio Greeks dict.

        Keys:
          contract         – str  ('PORTFOLIO')
          delta            – float | None
          gamma            – float | None
          vega             – float | None
          theta            – float | None
          underlying_price – float | None
          timestamp        – datetime (UTC)
        """

    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if a live session exists."""

    @abstractmethod
    async def get_recent_executions(self, since: datetime | None = None) -> list[dict]:
        """Return recent execution rows normalized for persistence."""


# ── SOCKET implementation ─────────────────────────────────────────────────────

class SocketBridge(IBridgeBase):
    """Connect to IB Gateway / TWS via the ib_async (IBC) SOCKET API.

    Environment variables:
      IB_SOCKET_HOST   – default: 127.0.0.1
      IB_SOCKET_PORT   – default: 7496
      IB_CLIENT_ID     – default: 10  (must differ from TWS/iPad client; those use 0)
    """

    def __init__(self) -> None:
        from ib_async import IB
        self._ib = IB()
        self._host    = os.getenv("IB_SOCKET_HOST", "127.0.0.1")
        self._port    = int(os.getenv("IB_SOCKET_PORT", "7496"))
        self._client_id = int(os.getenv("IB_CLIENT_ID", "10"))

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def connect(self) -> None:
        await self._ib.connectAsync(
            host=self._host,
            port=self._port,
            clientId=self._client_id,
        )
        logger.info("SocketBridge connected to %s:%d (clientId=%d)",
                    self._host, self._port, self._client_id)

    async def disconnect(self) -> None:
        self._ib.disconnect()
        logger.info("SocketBridge disconnected")

    def is_connected(self) -> bool:
        return self._ib.isConnected()

    async def get_recent_executions(self, since: datetime | None = None) -> list[dict]:
        """Return recent executions from TWS/IB Gateway via ib_async."""
        try:
            fills = self._ib.executions()
        except Exception as exc:
            logger.warning("Could not read socket executions: %s", exc)
            return []

        rows: list[dict] = []
        for fill in fills or []:
            execution = getattr(fill, "execution", fill)
            exec_time = getattr(execution, "time", None)
            if isinstance(exec_time, datetime) and exec_time.tzinfo is None:
                exec_time = exec_time.replace(tzinfo=timezone.utc)
            if since is not None and isinstance(exec_time, datetime) and exec_time < since:
                continue
            rows.append(
                {
                    "broker": "IBKR",
                    "account_id": getattr(execution, "acctNumber", None),
                    "broker_execution_id": getattr(execution, "execId", None),
                    "symbol": getattr(getattr(fill, "contract", None), "localSymbol", None)
                    or getattr(getattr(fill, "contract", None), "symbol", None),
                    "side": getattr(execution, "side", None),
                    "quantity": getattr(execution, "shares", None),
                    "price": getattr(execution, "price", None),
                    "commission": getattr(getattr(fill, "commissionReport", None), "commission", None),
                    "execution_time": exec_time,
                }
            )
        return rows

    # ── Greeks ─────────────────────────────────────────────────────────────

    async def get_portfolio_greeks(self) -> dict:
        """Aggregate portfolio-level Greeks from all open option positions.

        For option positions: use modelGreeks from reqMktData.
        For equity positions: delta = 1.0 per share, gamma/vega/theta = 0.
        """
        items = self._ib.portfolio()

        agg = dict(delta=0.0, gamma=0.0, vega=0.0, theta=0.0, underlying_price=None)

        for item in items:
            contract = item.contract
            qty = item.position          # signed position size (can be negative)
            if qty == 0:
                continue

            sec_type = getattr(contract, "secType", "")

            if sec_type == "OPT":
                try:
                    ticker = await asyncio.wait_for(
                        self._request_greeks(contract),
                        timeout=5.0,
                    )
                    greeks = ticker.modelGreeks if ticker else None
                    if greeks:
                        multiplier = float(getattr(contract, "multiplier", 100) or 100)
                        if greeks.delta is not None:
                            agg["delta"] += greeks.delta * qty * multiplier
                        if greeks.gamma is not None:
                            agg["gamma"] += greeks.gamma * qty * multiplier
                        if greeks.vega is not None:
                            agg["vega"]  += greeks.vega  * qty * multiplier
                        if greeks.theta is not None:
                            agg["theta"] += greeks.theta * qty * multiplier
                        if greeks.undPrice is not None and agg["underlying_price"] is None:
                            agg["underlying_price"] = greeks.undPrice
                except asyncio.TimeoutError:
                    logger.warning("Greek request timed out for %s", contract.localSymbol)
                except Exception as exc:
                    logger.warning("Greek request failed for %s: %s", contract.localSymbol, exc)

            elif sec_type in ("STK", "ETF"):
                # Equity: delta = 1 per share; gamma/vega/theta = 0
                agg["delta"] += float(qty)
                mkt_price = item.marketPrice
                if mkt_price and agg["underlying_price"] is None:
                    agg["underlying_price"] = float(mkt_price)

        agg["contract"]  = "PORTFOLIO"
        agg["timestamp"] = datetime.now(timezone.utc)
        return agg

    async def _request_greeks(self, contract):
        """Subscribe to market data and wait for modelGreeks to populate."""
        from ib_async import util as ib_util
        ticker = self._ib.reqMktData(contract, "", False, False)
        # wait until modelGreeks is available (poll up to 3 s)
        for _ in range(30):
            if ticker.modelGreeks is not None:
                break
            await asyncio.sleep(0.1)
        self._ib.cancelMktData(contract)
        return ticker


# ── PORTAL implementation ─────────────────────────────────────────────────────

class PortalBridge(IBridgeBase):
    """Connect to IB Client Portal REST API (PORTAL / Web API Gateway).

    Environment variables:
      IBKR_GATEWAY_URL  – default: https://localhost:5001
      IBKR_ACCOUNT_ID   – required for positions endpoint
    """

    # Fields for Greeks: 7308=delta, 7309=gamma, 7310=vega, 7311=theta
    _GREEK_FIELDS = "7308,7309,7310,7311"

    def __init__(self) -> None:
        self._base_url  = os.getenv("IBKR_GATEWAY_URL", "https://localhost:5001").rstrip("/")
        self._account   = os.getenv("IBKR_ACCOUNT_ID", "")
        self._ssl_ctx   = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode    = ssl.CERT_NONE
        self._session: Optional[aiohttp.ClientSession] = None
        self._connected = False

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def connect(self) -> None:
        connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
        self._session   = aiohttp.ClientSession(connector=connector)
        # Tickle the auth-check endpoint to confirm session is alive
        await self._tickle()
        self._connected = True
        logger.info("PortalBridge connected to %s", self._base_url)

    async def disconnect(self) -> None:
        self._connected = False
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("PortalBridge disconnected")

    def is_connected(self) -> bool:
        return self._connected and self._session is not None and not self._session.closed

    async def get_recent_executions(self, since: datetime | None = None) -> list[dict]:
        """Best-effort: Client Portal execution endpoint varies by gateway build.

        Returns an empty list when unsupported to keep polling resilient.
        """
        if not self._session:
            return []

        url = f"{self._base_url}/v1/api/iserver/account/trades"
        try:
            async with self._session.get(url) as resp:
                if resp.status >= 400:
                    return []
                data = await resp.json()
        except Exception:
            return []

        rows: list[dict] = []
        for item in data if isinstance(data, list) else []:
            trade_time = item.get("tradeTime") or item.get("trade_time")
            parsed_time: datetime | None = None
            if isinstance(trade_time, str):
                try:
                    parsed_time = datetime.fromisoformat(trade_time.replace("Z", "+00:00"))
                except ValueError:
                    parsed_time = None
            if since is not None and parsed_time is not None and parsed_time < since:
                continue
            rows.append(
                {
                    "broker": "IBKR",
                    "account_id": item.get("acctId") or item.get("account"),
                    "broker_execution_id": item.get("execution_id") or item.get("executionID") or item.get("execId"),
                    "symbol": item.get("symbol"),
                    "side": item.get("side"),
                    "quantity": item.get("size") or item.get("quantity"),
                    "price": item.get("price"),
                    "commission": item.get("commission"),
                    "execution_time": parsed_time,
                }
            )
        return rows

    # ── Greeks ─────────────────────────────────────────────────────────────

    async def get_portfolio_greeks(self) -> dict:
        positions = await self._fetch_positions()
        if not positions:
            return {
                "contract": "PORTFOLIO",
                "delta": None, "gamma": None, "vega": None, "theta": None,
                "underlying_price": None,
                "timestamp": datetime.now(timezone.utc),
            }

        # Collect conids of option positions
        opt_conids = [
            str(p["conid"])
            for p in positions
            if p.get("assetClass", "").upper() in ("OPT", "FOP")
        ]

        snapshots: dict[str, dict] = {}
        if opt_conids:
            snapshots = await self._fetch_snapshots(opt_conids)

        agg = dict(delta=0.0, gamma=0.0, vega=0.0, theta=0.0, underlying_price=None)

        for pos in positions:
            qty       = float(pos.get("position", 0))
            asset_cls = pos.get("assetClass", "").upper()
            conid_str = str(pos.get("conid", ""))
            multiplier = float(pos.get("multiplier", 1) or 1)

            if qty == 0:
                continue

            if asset_cls in ("OPT", "FOP"):
                snap = snapshots.get(conid_str, {})
                try:
                    delta = float(snap.get("7308", 0) or 0)
                    gamma = float(snap.get("7309", 0) or 0)
                    vega  = float(snap.get("7310", 0) or 0)
                    theta = float(snap.get("7311", 0) or 0)
                    agg["delta"] += delta * qty * multiplier
                    agg["gamma"] += gamma * qty * multiplier
                    agg["vega"]  += vega  * qty * multiplier
                    agg["theta"] += theta * qty * multiplier
                except (TypeError, ValueError) as exc:
                    logger.warning("Bad Greek value for conid %s: %s", conid_str, exc)

            elif asset_cls in ("STK", "ETF"):
                agg["delta"] += qty

        agg["contract"]  = "PORTFOLIO"
        agg["timestamp"] = datetime.now(timezone.utc)
        return agg

    # ── HTTP helpers ───────────────────────────────────────────────────────

    async def _tickle(self) -> None:
        url = f"{self._base_url}/v1/api/tickle"
        async with self._session.post(url) as resp:
            resp.raise_for_status()

    async def _fetch_positions(self) -> list[dict]:
        if not self._account:
            raise ValueError("IBKR_ACCOUNT_ID env var not set")
        url = f"{self._base_url}/v1/api/portfolio/{self._account}/positions/0"
        async with self._session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _fetch_snapshots(self, conids: list[str]) -> dict[str, dict]:
        """Fetch market data snapshots for a list of conids.

        Returns {conid_str: {field_code: value, ...}}.
        """
        conids_param = ",".join(conids)
        url = (
            f"{self._base_url}/v1/api/iserver/marketdata/snapshot"
            f"?conids={conids_param}&fields={self._GREEK_FIELDS}"
        )
        async with self._session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return {str(item.get("conid", "")): item for item in data}


# ── Watchdog ──────────────────────────────────────────────────────────────────

class Watchdog:
    """Monitors bridge health and reconnects on failure.

    Handles the nightly IB Gateway restart window (23:40–00:05 ET) by sleeping
    until the gateway is back up rather than spamming reconnect attempts.

    Usage::
        watchdog = Watchdog()
        asyncio.create_task(
            watchdog.run(bridge, on_reconnect_cb=partial(log_api_event, breaker, mode))
        )
    """

    def __init__(
        self,
        interval: int = _WATCHDOG_INTERVAL,
        backoff_schedule: tuple[int, ...] = _BACKOFF_SCHEDULE,
    ) -> None:
        self._interval = interval
        self._backoff  = backoff_schedule

    async def run(
        self,
        bridge: IBridgeBase,
        *,
        on_reconnect_cb: Optional[Callable[[str, str], Awaitable[None]]] = None,
    ) -> None:
        """Continuously monitor *bridge* health; reconnect as needed.

        Args:
            bridge:           The active IBridgeBase instance.
            on_reconnect_cb:  Async callable(message: str, status: str) called
                              on connect / disconnect / error events.
        """
        async def _notify(msg: str, status: str = "info") -> None:
            logger.info("[watchdog] %s", msg) if status == "info" else logger.warning("[watchdog] %s", msg)
            if on_reconnect_cb:
                try:
                    await on_reconnect_cb(msg, status)
                except Exception as exc:
                    logger.debug("on_reconnect_cb failed: %s", exc)

        while True:
            await asyncio.sleep(self._interval)

            if bridge.is_connected():
                continue  # all good

            # ── bridge is disconnected ──────────────────────────────────────
            await _notify("Bridge disconnected — attempting reconnect", "warning")

            # Check night window first
            if _in_night_window():
                secs = _seconds_until_et(_NIGHT_END_HOUR, _NIGHT_END_MINUTE)
                await _notify(
                    f"In IB nightly restart window — sleeping {secs/60:.1f} min until 00:05 ET",
                    "warning",
                )
                await asyncio.sleep(secs + 10)   # +10 s safety margin

            # Backoff retry loop
            for attempt, delay in enumerate(self._backoff, start=1):
                try:
                    await bridge.connect()
                    await _notify(
                        f"Reconnected after {attempt} attempt(s)", "info"
                    )
                    break
                except Exception as exc:
                    await _notify(
                        f"Reconnect attempt {attempt} failed: {exc} — retrying in {delay}s",
                        "warning",
                    )
                    await asyncio.sleep(delay)
            else:
                await _notify(
                    "All reconnect attempts exhausted — watchdog will retry next cycle",
                    "error",
                )


def build_bridge_from_env() -> IBridgeBase:
    """Construct the concrete bridge implementation from `IB_API_MODE`."""
    mode = normalize_api_mode(os.getenv("IB_API_MODE", API_MODE_SOCKET))
    return SocketBridge() if mode == API_MODE_SOCKET else PortalBridge()
