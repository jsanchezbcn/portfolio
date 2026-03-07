"""desktop/engine/ib_engine.py — Central IBKR engine (ib_async + asyncpg).

Inspired by vnpy's MainEngine pattern: a single long-lived event loop owns the
IBKR socket connection and PostgreSQL pool.  PySide6 widgets connect to Qt
signals emitted here; no ib_async objects leak into the GUI thread.

Public API (all async, called via qasync from UI):
    connect()            — connect to IB Gateway + PostgreSQL
    disconnect()         — tear down gracefully
    refresh_positions()  — pull positions + PnL + Greeks, emit signal
    refresh_account()    — pull account summary, emit signal
    get_chain()          — fetch option chain for a symbol/expiry
    place_order()        — build + transmit a live order, log fill to DB
    whatif_order()       — simulate without transmitting
    get_market_snapshot()— fetch a real-time quote for a symbol
    get_open_orders()    — list open/working orders
    cancel_order()       — cancel a working order
    cancel_all_orders()  — cancel all open orders
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ib_async import IB, Contract, FuturesOption, Option, Stock, Future, MarketOrder, LimitOrder, Trade

import time as _time_mod  # for cache TTL checks
from PySide6.QtCore import QObject, Signal

from desktop.db.database import Database
from desktop.engine.greeks_engine import GreeksEngine

logger = logging.getLogger(__name__)

# Silence ib_async loggers — they log all IB errors at ERROR level
# but we handle them ourselves in _on_ib_error with proper filtering.
logging.getLogger("ib_async.wrapper").setLevel(logging.CRITICAL)
logging.getLogger("ib_async.ib").setLevel(logging.CRITICAL)

# ── Futures SPX-equivalent contract multipliers ───────────────────────────
# These map the root symbol of an equity-index futures contract to the number
# of SPX-equivalent units per 1 contract (i.e. notional / index_price).
# ES=50 means 1 /ES contract moves $50 per SPX point; MES=5 is the micro.
_FUT_SPX_MULTIPLIERS: dict[str, float] = {
    "ES":  50.0,   # E-mini S&P 500
    "MES":  5.0,   # Micro E-mini S&P 500
    "NQ":  20.0,   # E-mini NASDAQ-100
    "MNQ":  2.0,   # Micro E-mini NASDAQ-100
    "RTY": 50.0,   # E-mini Russell 2000
    "M2K":  5.0,   # Micro E-mini Russell 2000
    "YM":   5.0,   # E-mini Dow Jones
    "MYM":  0.5,   # Micro E-mini Dow Jones
    "SP":  250.0,  # Full-size S&P 500 (legacy)
}  # Add more as needed; unknown symbols default to 0 (not SPX-correlated)


# ── Data containers emitted via signals ───────────────────────────────────

@dataclass
class PositionRow:
    """Flattened position for the UI table."""
    conid: int
    symbol: str
    sec_type: str
    underlying: str
    strike: float | None
    right: str | None
    expiry: str | None
    quantity: float
    avg_cost: float
    market_price: float
    market_value: float
    unrealized_pnl: float
    realized_pnl: float
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    iv: float | None
    spx_delta: float | None
    greeks_source: str | None = None
    underlying_price: float | None = None


@dataclass
class AccountSummary:
    """Key account metrics."""
    account_id: str
    net_liquidation: float
    total_cash: float
    buying_power: float
    init_margin: float
    maint_margin: float
    unrealized_pnl: float
    realized_pnl: float


@dataclass
class ChainRow:
    """Single strike in an option chain."""
    underlying: str
    expiry: str
    strike: float
    right: str  # 'C' or 'P'
    conid: int
    bid: float | None
    ask: float | None
    last: float | None
    volume: int
    open_interest: int
    iv: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None


@dataclass
class MarketSnapshot:
    """Real-time quote snapshot for a single instrument."""
    symbol: str
    last: float | None
    bid: float | None
    ask: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int
    timestamp: str  # ISO-8601 UTC


@dataclass
class PortfolioRiskSummary:
    """Aggregated risk metrics across all positions."""
    total_positions: int
    total_value: float
    total_spx_delta: float
    total_delta: float
    total_gamma: float
    total_theta: float
    total_vega: float
    theta_vega_ratio: float
    gross_exposure: float
    net_exposure: float
    options_count: int
    stocks_count: int


@dataclass
class OpenOrder:
    """In-flight order from IB."""
    order_id: int
    perm_id: int
    symbol: str
    action: str
    quantity: float
    order_type: str
    limit_price: float | None
    status: str
    filled: float
    remaining: float
    avg_fill_price: float


# ── Engine ────────────────────────────────────────────────────────────────

class IBEngine(QObject):
    """Central engine connecting ib_async ↔ PostgreSQL ↔ PySide6 signals.

    All methods are async; call them from the qasync event loop.
    Signals are thread-safe Qt signals that the UI can connect to.
    """

    # ── Qt Signals (emitted on the event-loop thread) ─────────────────────
    connected         = Signal()                        # IB + DB ready
    disconnected      = Signal()                        # IB disconnected
    positions_updated = Signal(list)                    # list[PositionRow]
    account_updated   = Signal(object)                  # AccountSummary
    risk_updated      = Signal(object)                  # PortfolioRiskSummary
    chain_ready       = Signal(list)                    # list[ChainRow]
    order_filled      = Signal(dict)                    # fill details
    order_status      = Signal(dict)                    # status update
    orders_updated    = Signal(list)                    # list[OpenOrder]
    market_snapshot   = Signal(object)                  # MarketSnapshot
    error_occurred    = Signal(str)                     # error message
    connection_state  = Signal(str, str)                # state, detail

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4001,
        client_id: int = 30,
        db_dsn: str = "postgresql://portfoliouser:portfoliopass@localhost:5432/portfoliodb",
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._host = host
        self._port = port
        self._client_id = client_id
        self._ib = IB()
        self._db = Database(db_dsn)
        self._db_ok = False
        self._account_id: str = ""
        self._manual_disconnect_requested = False
        self._reconnect_task: asyncio.Task | None = None
        self._reconnect_lock = asyncio.Lock()
        self._watchdog_max_attempts = max(1, int(os.getenv("IB_RECONNECT_MAX_ATTEMPTS", "5")))
        self._watchdog_backoff_cap = max(5, int(os.getenv("IB_RECONNECT_BACKOFF_CAP_SECONDS", "120")))
        self._resolve_warning_ttl = max(5.0, float(os.getenv("IB_RESOLVE_WARNING_TTL_SECONDS", "60")))
        self._resolve_warning_cache: dict[str, float] = {}
        self._active_chain_request: dict[str, Any] | None = None
        self._beta_default = 1.0
        self._symbol_betas: dict[str, float] = {}
        self._greeks_engine = GreeksEngine(risk_free_rate=float(os.getenv("IB_LOCAL_GREEKS_RISK_FREE_RATE", "0.01")))
        self._load_beta_config()
        # ── Option chain caches (keyed by "underlying|expiry|sec_type|exchange") ──
        # TTL: 86400s (1 day).  Cleared on reconnect.
        self._chain_cache:  dict[str, tuple[float, list]] = {}   # key → (ts, ChainRow list)
        self._expiry_cache: dict[str, tuple[float, list]] = {}   # key → (ts, expiry strings)
        # ── Streaming subscriptions for live chain prices ──
        # conid → ib_async Ticker; cancelled when chain tab hidden / symbol changes
        self._chain_tickers: dict[int, Any] = {}
        # ── Positions snapshot (refreshed on every portfolio update) ──
        # Used to supplement chain expiry picker with expiries from live positions
        self._positions_snapshot: list = []
        # ── Latest account summary + market snapshots for agent workers ──
        self._last_account_summary = None
        self._market_snapshots: dict[str, dict] = {}
        # ── Last-seen price cache: symbol.upper() → float
        # Populated from every price source so WhatIf/submit never see 0.0 ──
        self._last_price_cache: dict[str, float] = {}
        # ── Persistent option greeks cache: conId → greek dict
        # Keeps the last known non-empty greeks so positions that temporarily
        # fail to get live data (e.g. stock options, pre/post market) still
        # show sensible values rather than going blank.
        self._greeks_cache: dict[int, dict] = {}

        # Wire ib_async event callbacks
        self._ib.connectedEvent += self._on_ib_connected
        self._ib.disconnectedEvent += self._on_ib_disconnected
        self._ib.errorEvent += self._on_ib_error
        self._ib.orderStatusEvent += self._on_order_status

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to IB Gateway and (optionally) PostgreSQL."""
        self._manual_disconnect_requested = False
        self.connection_state.emit("connecting", f"Connecting to {self._host}:{self._port}")
        # Database is optional — don't block IB connection if DB is down
        try:
            await self._db.connect()
            self._db_ok = True
            logger.info("Database connected")
            # Load last known Greeks from database for after-hours display
            try:
                cached_greeks = await self._db.get_cached_greeks(self._account_id or "U2052408")
                self._greeks_cache.update(cached_greeks)
                if cached_greeks:
                    logger.info("Loaded %d cached Greeks from database", len(cached_greeks))
            except Exception as exc:
                logger.debug("Failed to load cached Greeks: %s", exc)
        except Exception as exc:
            self._db_ok = False
            logger.warning("Database unavailable (continuing without): %s", exc)

        logger.info("Connecting to IB at %s:%d clientId=%d …", self._host, self._port, self._client_id)
        await self._ib.connectAsync(self._host, self._port, clientId=self._client_id, timeout=30)
        accounts = self._ib.managedAccounts()
        self._account_id = accounts[0] if accounts else ""
        
        # Reload Greeks cache with the correct account_id
        if self._db_ok and self._account_id:
            try:
                cached_greeks = await self._db.get_cached_greeks(self._account_id)
                self._greeks_cache.clear()
                self._greeks_cache.update(cached_greeks)
                if cached_greeks:
                    logger.info("Loaded %d cached Greeks from database for account %s", len(cached_greeks), self._account_id)
            except Exception as exc:
                logger.debug("Failed to reload cached Greeks after account discovery: %s", exc)
        
        logger.info("IB connected — account %s", self._account_id)
        self.connection_state.emit("connected", f"Connected to {self._account_id}")
        self.connected.emit()

    async def disconnect(self) -> None:
        """Gracefully disconnect from IB and cancel all open subscriptions."""
        self._manual_disconnect_requested = True
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            self._reconnect_task = None
        # ── Cancel all live chain market-data subscriptions ──────────────
        for _conid, (c, _t) in list(getattr(self, "_chain_tickers", {}).items()):
            try:
                self._ib.cancelMktData(c)
            except Exception:
                pass
        if hasattr(self, "_chain_tickers"):
            self._chain_tickers.clear()
        # ── Cancel any pending reqSecDefOptParams / reqContractDetails ───
        # ib_async tracks these internally; calling cancelSecDefOptParams is
        # not needed — just let the IB object handle cleanup on disconnect.
        if self._ib.isConnected():
            try:
                self._ib.disconnect()
            except Exception as exc:
                logger.debug("IB disconnect error (ignored): %s", exc)
        # ── Clear caches so reconnect starts clean ────────────────────────
        if hasattr(self, "_chain_cache"):
            self._chain_cache.clear()
        if hasattr(self, "_expiry_cache"):
            self._expiry_cache.clear()
        if self._db_ok:
            try:
                await self._db.close()
            except Exception:
                pass
            self._db_ok = False
        if not self._ib.isConnected():
            self.connection_state.emit("disconnected", "Disconnected from IBKR")
            self.disconnected.emit()

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def is_connected(self) -> bool:
        return self._ib.isConnected()

    def positions_snapshot(self) -> list:
        """Return the latest cached positions list (may be empty before first refresh)."""
        return list(self._positions_snapshot)

    def account_snapshot(self) -> "AccountSummary | None":
        """Return the last fetched AccountSummary, or None if not yet available."""
        return getattr(self, "_last_account_summary", None)

    def chain_snapshot(self) -> list:
        """Return the most recently fetched chain rows from cache (any underlier)."""
        if not self._chain_cache:
            return []
        # Return the freshest cached chain
        best = max(self._chain_cache.items(), key=lambda kv: kv[1][0])
        return list(best[1][1])

    def last_market_snapshot(self, symbol: str) -> dict | None:
        """Return the cached market data dict for *symbol* if available."""
        return (getattr(self, "_market_snapshots", {}) or {}).get(symbol.upper())

    def last_price(self, symbol: str) -> float | None:
        """Return the best-known last price for *symbol* from any data source.

        Sources polled in order: last_price_cache → market_snapshots.
        Returns None if no price has ever been seen.
        """
        sym = symbol.upper()
        cached = (getattr(self, "_last_price_cache", {}) or {}).get(sym)
        if cached and cached > 0:
            return cached
        snap = (getattr(self, "_market_snapshots", {}) or {}).get(sym)
        if snap:
            p = snap.get("last") or snap.get("bid") or snap.get("close")
            if p and float(p) > 0:
                return float(p)
        return None

    # ── helpers ───────────────────────────────────────────────────────────

    async def _qualify_underlying(self, symbol: str, sec_type: str, exchange: str) -> Contract:
        """Resolve an underlying contract, handling ambiguous FUT via reqContractDetails."""
        if sec_type in ("FOP", "FUT"):
            und = Future(symbol=symbol, exchange=exchange, currency="USD")
            details = await self._ib.reqContractDetailsAsync(und)
            if not details:
                raise ValueError(f"No contract details for {symbol} FUT {exchange}")
            details.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
            return details[0].contract
        else:
            if symbol.upper() in ("SPX", "RUT", "VIX", "NDX"):
                from ib_async import Index
                und = Index(symbol=symbol.upper(), exchange="CBOE" if symbol.upper() in ("SPX", "VIX") else "SMART", currency="USD")
            else:
                und = Stock(symbol=symbol, exchange=exchange or "SMART", currency="USD")
            qualified = await self._ib.qualifyContractsAsync(und)
            if not qualified or not qualified[0].conId:
                raise ValueError(f"Cannot qualify contract: {symbol} {sec_type} {exchange}")
            return qualified[0]

    # ── positions ─────────────────────────────────────────────────────────

    async def refresh_positions(self) -> list[PositionRow]:
        """Fetch live positions + PnL + Greeks from IB, persist to DB, emit signal.

        Uses reqPnLSingle for per-position P&L and reqMktData snapshots for Greeks.
        """
        ib_positions = self._ib.positions()
        rows: list[dict[str, Any]] = []
        result: list[PositionRow] = []
        spx_proxy_price = await self._spx_proxy_price_async()

        # Gather dynamic betas
        unique_stocks = {p.contract.symbol for p in ib_positions if p.contract.secType in ("STK", "OPT", "FOP", "FUT")}
        await asyncio.gather(*[self._fetch_dynamic_beta(s) for s in unique_stocks])

        # ── Step 1: Request portfolio PnL to get unrealized/realized PnL per contract
        portfolio_items = self._ib.portfolio(self._account_id) if self._account_id else []
        pnl_by_conid: dict[int, dict] = {}
        for item in portfolio_items:
            pnl_by_conid[item.contract.conId] = {
                "market_price": float(item.marketPrice),
                "market_value": float(item.marketValue),
                "unrealized_pnl": float(item.unrealizedPNL),
                "realized_pnl": float(item.realizedPNL),
                "avg_cost": float(item.averageCost),
            }

        # ── Step 1b: Fix missing exchange on ALL positions
        for pos in ib_positions:
            c = pos.contract
            if not c.exchange:
                c.exchange = self._infer_exchange(c)

        # ── Step 2: Request Greeks for option positions via batched streaming
        option_positions = [
            p for p in ib_positions
            if p.contract.secType in ("OPT", "FOP")
        ]
        option_greeks_by_conid: dict[int, dict[str, float | None]] = {}
        greeks_generic_ticks = os.getenv("IB_GREEKS_GENERIC_TICKS", "100,101,104,106")
        batch_size = max(5, int(os.getenv("IB_GREEKS_BATCH_SIZE", "40")))
        batch_wait_s = max(0.5, float(os.getenv("IB_GREEKS_BATCH_WAIT_SECONDS", "1.8")))
        retry_batch_size = max(5, int(os.getenv("IB_GREEKS_RETRY_BATCH_SIZE", "20")))
        retry_wait_s = max(0.5, float(os.getenv("IB_GREEKS_RETRY_WAIT_SECONDS", "1.2")))
        retry_max_contracts = max(0, int(os.getenv("IB_GREEKS_RETRY_MAX_CONTRACTS", "120")))

        async def collect_batch(batch_positions: list[Any], wait_s: float) -> dict[int, dict[str, float | None]]:
            batch_greeks: dict[int, dict[str, float | None]] = {}
            active_tickers: list[tuple[Any, Any]] = []
            for batch_pos in batch_positions:
                contract = batch_pos.contract
                try:
                    ticker = self._ib.reqMktData(
                        contract,
                        genericTickList=greeks_generic_ticks,
                        snapshot=False,
                        regulatorySnapshot=False,
                    )
                    active_tickers.append((contract, ticker))
                except Exception as exc:
                    logger.debug("Greeks request failed for %s: %s", contract.localSymbol, exc)

            if active_tickers:
                await asyncio.sleep(wait_s)

            for contract, ticker in active_tickers:
                g = self._extract_option_greeks_from_ticker(ticker)
                batch_greeks[contract.conId] = g
                # Persist non-empty Greeks to the long-lived cache
                if any(v is not None for v in (g.get("delta"), g.get("gamma"), g.get("theta"), g.get("vega"))):
                    self._greeks_cache[contract.conId] = g
                try:
                    self._ib.cancelMktData(contract)
                except Exception:
                    pass

            return batch_greeks

        for start in range(0, len(option_positions), batch_size):
            batch = option_positions[start:start + batch_size]
            option_greeks_by_conid.update(await collect_batch(batch, batch_wait_s))

        # Retry only options with no populated greek fields (common when data arrives late).
        missing_positions = [
            p for p in option_positions
            if not any((option_greeks_by_conid.get(p.contract.conId, {}) or {}).get(k) is not None for k in ("delta", "gamma", "theta", "vega", "iv"))
        ]
        if missing_positions:
            missing_positions.sort(key=lambda p: abs(float(getattr(p, "position", 0.0))), reverse=True)
            if retry_max_contracts > 0:
                missing_positions = missing_positions[:retry_max_contracts]
            logger.info("Retrying Greeks for %d/%d option positions", len(missing_positions), len(option_positions))
            for start in range(0, len(missing_positions), retry_batch_size):
                retry_batch = missing_positions[start:start + retry_batch_size]
                option_greeks_by_conid.update(await collect_batch(retry_batch, retry_wait_s))

        # ── Step 3: Build PositionRow with PnL + Greeks
        for pos in ib_positions:
            c = pos.contract
            pnl = pnl_by_conid.get(c.conId, {})
            # Prefer live greeks; fall back to last known non-empty greeks when live is empty
            live_g = option_greeks_by_conid.get(c.conId, {})
            greeks_source: str | None = None
            if not any(live_g.get(k) is not None for k in ("delta", "gamma", "theta", "vega")) and c.conId in self._greeks_cache:
                ticker_greeks = {**self._greeks_cache[c.conId], **{k: v for k, v in live_g.items() if v is not None}}
                greeks_source = "cached"
            else:
                ticker_greeks = live_g
                if any(live_g.get(k) is not None for k in ("delta", "gamma", "theta", "vega")):
                    greeks_source = "live"

            if c.secType in ("OPT", "FOP"):
                estimated = self._estimate_option_greeks(contract=c, ticker_greeks=ticker_greeks)
                if estimated and not any(ticker_greeks.get(k) is not None for k in ("delta", "gamma", "theta", "vega")):
                    ticker_greeks = {**ticker_greeks, **estimated}
                    greeks_source = str(estimated.get("source") or "estimated_bsm")

            mkt_price = pnl.get("market_price", 0.0)
            mkt_value = pnl.get("market_value", 0.0)
            upnl = pnl.get("unrealized_pnl", 0.0)
            rpnl = pnl.get("realized_pnl", 0.0)

            delta = ticker_greeks.get("delta")
            gamma = ticker_greeks.get("gamma")
            theta = ticker_greeks.get("theta")
            vega = ticker_greeks.get("vega")
            iv = ticker_greeks.get("iv")

            if c.secType in ("OPT", "FOP"):
                mult = float(c.multiplier or 1)
                qty = float(pos.position)
                if delta is not None:
                    delta = delta * qty * mult
                if gamma is not None:
                    gamma = gamma * qty * mult
                if theta is not None:
                    theta = theta * qty * mult
                if vega is not None:
                    vega = vega * qty * mult

            if c.secType == "STK":
                ref_price = (
                    float(mkt_price or 0.0)
                    or float(pos.avgCost or 0.0)
                    or float(getattr(c, "lastTradePrice", 0.0) or 0.0)
                )
                spx_delta = self._compute_spx_weighted_delta(
                    symbol=c.symbol,
                    quantity=float(pos.position),
                    price=ref_price,
                    underlying_delta=1.0,
                    multiplier=1.0,
                    spx_proxy_price=spx_proxy_price,
                ) if ref_price > 0 else None
                # For stocks the meaningful delta IS the SPX-weighted beta delta
                # (not the raw share count which is useless for cross-asset comparison)
                delta = spx_delta
            elif c.secType == "FUT":
                # Futures have delta=1 per contract; SPX delta = qty × SPX-multiplier.
                # Use lookup table for index futures; unknown symbols get 0 (non-SPX).
                qty = float(pos.position)
                raw_mult = float(c.multiplier or 1)
                spx_mult = _FUT_SPX_MULTIPLIERS.get(c.symbol, 0.0)
                delta = qty * raw_mult   # dollar-delta: moves $raw_mult per index point
                spx_delta = qty * spx_mult if spx_mult else None
            elif c.secType in ("OPT", "FOP"):
                und_price = ticker_greeks.get("undPrice")
                raw_delta = ticker_greeks.get("delta")
                qty = float(pos.position)
                raw_mult = float(c.multiplier or 100)
                # For index-correlated underlyings (ES, MES, SPX, SPXW …) the
                # delta is already in SPX-equivalent units: Δ × qty × multiplier.
                # DO NOT apply the beta×(price/spx_proxy) normalisation used for
                # individual stocks — that would double-scale the index exposure.
                _INDEX_UNDERLYINGS = {
                    "ES", "MES", "NQ", "MNQ", "RTY", "M2K", "YM", "MYM",
                    "SP", "SPX", "SPXW", "XSP", "NDX", "RUT",
                }
                if c.symbol in _INDEX_UNDERLYINGS:
                    spx_delta = delta  # delta already scaled by qty×mult above
                elif und_price and raw_delta is not None:
                    spx_delta = self._compute_spx_weighted_delta(
                        symbol=c.symbol,
                        quantity=qty,
                        price=und_price,
                        underlying_delta=raw_delta,
                        multiplier=raw_mult,
                        spx_proxy_price=spx_proxy_price,
                    )
                else:
                    spx_delta = delta
            else:
                spx_delta = delta

            # Capture underlying price for display
            if c.secType in ("OPT", "FOP"):
                und_price = ticker_greeks.get("undPrice")
            elif c.secType == "STK":
                und_price = mkt_price  # For stocks, underlying price = market price
            elif c.secType == "FUT":
                und_price = mkt_price  # For futures, show the contract price
            else:
                und_price = None

            row = {
                "conid": c.conId,
                "symbol": c.localSymbol or c.symbol,
                "sec_type": c.secType,
                "exchange": c.exchange,
                "currency": c.currency,
                "underlying": c.symbol if c.secType in ("OPT", "FOP") else None,
                "strike": c.strike if c.secType in ("OPT", "FOP") else None,
                "option_right": c.right if c.secType in ("OPT", "FOP") else None,
                "expiry": self._parse_expiry(c.lastTradeDateOrContractMonth) if c.secType in ("OPT", "FOP", "FUT") else None,
                "multiplier": float(c.multiplier or 1),
                "quantity": float(pos.position),
                "avg_cost": pnl.get("avg_cost", float(pos.avgCost)),
                "market_price": mkt_price,
                "market_value": mkt_value,
                "unrealized_pnl": upnl,
                "realized_pnl": rpnl,
            }
            rows.append(row)

            result.append(PositionRow(
                conid=c.conId,
                symbol=row["symbol"],
                sec_type=row["sec_type"],
                underlying=row["underlying"] or "",
                strike=row["strike"],
                right=row["option_right"],
                expiry=row["expiry"],
                quantity=row["quantity"],
                avg_cost=row["avg_cost"],
                market_price=mkt_price,
                market_value=mkt_value,
                unrealized_pnl=upnl,
                realized_pnl=rpnl,
                underlying_price=und_price,
                delta=delta,
                gamma=gamma,
                theta=theta,
                vega=vega,
                iv=iv,
                spx_delta=spx_delta,
                greeks_source=greeks_source,
            ))

            # ── Populate last-price cache from position data ──────────────
            if mkt_price and float(mkt_price) > 0:
                cache_sym = (c.localSymbol or c.symbol).upper()
                self._last_price_cache[cache_sym] = float(mkt_price)
                # Also cache the bare underlying symbol (e.g. "ES") for order entry
                if c.secType in ("OPT", "FOP") and c.symbol:
                    und_sym = c.symbol.upper()
                    und_p = ticker_greeks.get("undPrice")
                    if und_p and float(und_p) > 0:
                        self._last_price_cache[und_sym] = float(und_p)

        # Note: snapshot=True subscriptions auto-terminate after delivery.
        # No need to call cancelMktData — doing so causes Error 300 "Can't find EId".

        # ── Step 4: Compute aggregate risk summary
        total_delta = sum(r.delta or 0 for r in result)
        total_gamma = sum(r.gamma or 0 for r in result)
        total_theta = sum(r.theta or 0 for r in result)
        total_vega = sum(r.vega or 0 for r in result)
        total_spx_delta = sum(r.spx_delta or 0 for r in result)
        gross_exposure = sum(abs(r.market_value or 0) for r in result)
        net_exposure = sum(r.market_value or 0 for r in result)
        opts = sum(1 for r in result if r.sec_type in ("OPT", "FOP"))
        stks = sum(1 for r in result if r.sec_type == "STK")

        risk = PortfolioRiskSummary(
            total_positions=len(result),
            total_value=sum(r.market_value or 0 for r in result),
            total_spx_delta=total_spx_delta,
            total_delta=total_delta,
            total_gamma=total_gamma,
            total_theta=total_theta,
            total_vega=total_vega,
            theta_vega_ratio=total_theta / total_vega if total_vega != 0 else 0.0,
            gross_exposure=gross_exposure,
            net_exposure=net_exposure,
            options_count=opts,
            stocks_count=stks,
        )
        self.risk_updated.emit(risk)

        # Persist
        if self._db_ok:
            try:
                await self._db.upsert_positions(self._account_id, rows)
            except Exception as exc:
                logger.warning("DB upsert_positions failed: %s", exc)
        self._positions_snapshot = result
        self.positions_updated.emit(result)
        return result

    def _extract_option_greeks_from_ticker(self, ticker: Any) -> dict[str, float | None]:
        """Extract option Greeks from model/bid/ask/last greeks in priority order.
        
        Also captures undPrice (underlying price) required for SPX-weighted delta.
        """
        greek_sources = [
            getattr(ticker, "modelGreeks", None),
            getattr(ticker, "bidGreeks", None),
            getattr(ticker, "askGreeks", None),
            getattr(ticker, "lastGreeks", None),
        ]
        # Always collect undPrice from most reliable source first
        und_price: float | None = None
        for src in greek_sources:
            if not src:
                continue
            up = self._finite_or_none(getattr(src, "undPrice", None))
            if up and up > 0:
                und_price = up
                break
        # Also try the ticker-level lastPrice as fallback for undPrice
        if und_price is None or und_price <= 0:
            for attr in ("last", "close", "marketPrice"):
                v = self._finite_or_none(getattr(ticker, attr, None))
                if v and v > 0:
                    # This is the option price not underlying — skip, rely on undPrice only
                    break

        for src in greek_sources:
            if not src:
                continue
            delta = getattr(src, "delta", None)
            gamma = getattr(src, "gamma", None)
            theta = getattr(src, "theta", None)
            vega = getattr(src, "vega", None)
            iv = getattr(src, "impliedVol", None)
            if iv is None:
                iv = getattr(src, "impliedVolatility", None)
            if any(v is not None for v in (delta, gamma, theta, vega, iv)):
                return {
                    "delta": self._finite_or_none(delta),
                    "gamma": self._finite_or_none(gamma),
                    "theta": self._finite_or_none(theta),
                    "vega": self._finite_or_none(vega),
                    "iv": self._finite_or_none(iv),
                    "undPrice": und_price,
                }
        return {"delta": None, "gamma": None, "theta": None, "vega": None, "iv": None, "undPrice": und_price}

    def _estimate_option_greeks(self, *, contract: Any, ticker_greeks: dict[str, Any]) -> dict[str, float | str] | None:
        """Estimate missing option Greeks from last known underlying price and IV."""
        iv = self._finite_or_none(ticker_greeks.get("iv"))
        if iv is None and getattr(contract, "conId", 0) in self._greeks_cache:
            iv = self._finite_or_none(self._greeks_cache[getattr(contract, "conId", 0)].get("iv"))
        if iv is None or iv <= 0:
            return None

        und_price = self._finite_or_none(ticker_greeks.get("undPrice"))
        if und_price is None or und_price <= 0:
            und_price = self.last_price(getattr(contract, "symbol", ""))
        if und_price is None or und_price <= 0:
            return None

        expiry = self._parse_expiry(getattr(contract, "lastTradeDateOrContractMonth", None))
        estimate = self._greeks_engine.estimate(
            underlying_price=und_price,
            strike=float(getattr(contract, "strike", 0.0) or 0.0),
            expiry=expiry,
            right=str(getattr(contract, "right", "C") or "C"),
            iv=iv,
        )
        if estimate is None:
            return None
        return {
            "delta": estimate.delta,
            "gamma": estimate.gamma,
            "theta": estimate.theta,
            "vega": estimate.vega,
            "iv": estimate.iv,
            "undPrice": und_price,
            "source": estimate.source,
        }

    @staticmethod
    def _finite_or_none(value: Any) -> float | None:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        if f != f:
            return None
        return f

    def _load_beta_config(self) -> None:
        """Load symbol beta coefficients from beta_config.json (optional)."""
        try:
            path = Path(__file__).resolve().parents[2] / "beta_config.json"
            if not path.exists():
                return
            payload = json.loads(path.read_text(encoding="utf-8"))
            self._beta_default = float(payload.get("default_beta", 1.0) or 1.0)
            betas_raw = payload.get("betas", {}) or {}
            self._symbol_betas = {
                str(sym).upper(): float(beta)
                for sym, beta in betas_raw.items()
                if beta is not None
            }
        except Exception as exc:
            logger.debug("Failed loading beta_config.json: %s", exc)

    async def _fetch_dynamic_beta(self, symbol: str) -> None:
        if not symbol or symbol.upper() in self._symbol_betas:
            return
        self._symbol_betas[symbol.upper()] = self._beta_default
        try:
            from bs4 import BeautifulSoup
            from ib_async import Stock
            stock = Stock(symbol, "SMART", "USD")
            xml_data = await asyncio.wait_for(
                self._ib.reqFundamentalDataAsync(stock, "ReportSnapshot"),
                timeout=5
            )
            if xml_data:
                soup = BeautifulSoup(xml_data, "xml")
                beta_tag = soup.find("Ratio", {"FieldName": "BETA"})
                if beta_tag and beta_tag.text:
                    self._symbol_betas[symbol.upper()] = float(beta_tag.text)
        except Exception as exc:
            logger.debug(f"Failed to fetch dynamic beta for {symbol}: {exc}")

    def _symbol_beta(self, symbol: str) -> float:
        return float(self._symbol_betas.get(str(symbol or "").upper(), self._beta_default))

    def _compute_spx_weighted_delta(
        self,
        *,
        symbol: str,
        quantity: float,
        price: float,
        underlying_delta: float,
        multiplier: float,
        spx_proxy_price: float,
    ) -> float:
        """Calculate SPX-equivalent delta using beta and SPX proxy price."""
        spx_proxy = float(spx_proxy_price or 0.0)
        if spx_proxy <= 0:
            return float(quantity) * float(underlying_delta) * float(multiplier)
        beta = self._symbol_beta(symbol)
        return float(underlying_delta) * float(quantity) * beta * (float(price) / spx_proxy) * float(multiplier)

    async def _spx_proxy_price_async(self) -> float:
        """Best-effort SPX proxy from live SPY quote (×10), fallback to 6000."""
        try:
            spy = Stock(symbol="SPY", exchange="SMART", currency="USD")
            ticker = self._ib.reqMktData(spy, genericTickList="", snapshot=True, regulatorySnapshot=False)
            await asyncio.sleep(1)
            price = float(getattr(ticker, "last", 0.0) or getattr(ticker, "close", 0.0) or 0.0)
            if price > 0:
                return price * 10.0
        except Exception:
            pass
        return 6000.0

    # ── account summary ───────────────────────────────────────────────────

    async def refresh_account(self) -> AccountSummary | None:
        """Fetch account summary, save snapshot, emit signal."""
        tags = await self._ib.accountSummaryAsync()
        if not tags:
            return None

        vals: dict[str, float] = {}
        for tag in tags:
            if tag.account == self._account_id and tag.currency == "USD":
                try:
                    vals[tag.tag] = float(tag.value)
                except (ValueError, TypeError):
                    pass

        summary = AccountSummary(
            account_id=self._account_id,
            net_liquidation=vals.get("NetLiquidation", 0.0),
            total_cash=vals.get("TotalCashValue", 0.0),
            buying_power=vals.get("BuyingPower", 0.0),
            init_margin=vals.get("InitMarginReq", 0.0),
            maint_margin=vals.get("MaintMarginReq", 0.0),
            unrealized_pnl=vals.get("UnrealizedPnL", 0.0),
            realized_pnl=vals.get("RealizedPnL", 0.0),
        )

        # Persist snapshot
        if self._db_ok:
            try:
                await self._db.insert_account_snapshot({
                    "account_id": self._account_id,
                    "net_liquidation": summary.net_liquidation,
                    "total_cash": summary.total_cash,
                    "buying_power": summary.buying_power,
                    "init_margin": summary.init_margin,
                    "maint_margin": summary.maint_margin,
                    "unrealized_pnl": summary.unrealized_pnl,
                    "realized_pnl": summary.realized_pnl,
                })
            except Exception as exc:
                logger.warning("DB insert_account_snapshot failed: %s", exc)

        self._last_account_summary = summary
        self.account_updated.emit(summary)
        return summary

    # ── option chain ──────────────────────────────────────────────────────

    async def get_available_expiries(
        self,
        underlying: str,
        sec_type: str = "FOP",
        exchange: str = "CME",
    ) -> list[str]:
        """Return sorted list of available expiry strings (YYYYMMDD) for a symbol."""
        cache_key = f"expiries|{underlying}|{sec_type}|{exchange}"
        now = _time_mod.time()
        if cache_key in self._expiry_cache:
            ts, cached = self._expiry_cache[cache_key]
            if now - ts < 86400:  # 1-day TTL
                logger.debug("expiry cache hit for %s", underlying)
                return cached
        und_contract = await self._qualify_underlying(underlying, sec_type, exchange)

        # For FOP, futFopExchange must be set (e.g. "CME"); for stock options leave empty
        fut_fop_exchange = exchange if sec_type == "FOP" else ""

        chains: list[Any] = []
        try:
            chains = await asyncio.wait_for(
                self._ib.reqSecDefOptParamsAsync(
                    und_contract.symbol, fut_fop_exchange, und_contract.secType, und_contract.conId or 0,
                ),
                timeout=15,
            )
        except asyncio.TimeoutError:
            logger.warning("get_available_expiries timed out for %s (secDef)", underlying)

        if not chains and sec_type == "FOP":
            fallback = await self._fallback_fop_expiries_from_contract_details(underlying, exchange)
            if fallback:
                return fallback

        if not chains:
            return []

        # Merge expirations from ALL returned chain defs (IB may return multiple
        # entries per trading-class, e.g. quarterly ES, weekly EW1/EW2/EW3/EW4/E1D).
        today_str = date.today().strftime("%Y%m%d")
        all_expirations: set[str] = set()
        for cd in chains:
            for e in (cd.expirations or []):
                if e >= today_str:
                    all_expirations.add(e)
        result = sorted(all_expirations)
        self._expiry_cache[cache_key] = (_time_mod.time(), result)
        return result

    def get_position_expiries(self, underlying: str) -> list[str]:
        """Return sorted unique expiry strings from cached positions for the given underlying.

        Supplements the standard chain expiries with expiries from the user's
        existing FOP/OPT positions (e.g. weekly ES option series like EW1, E1D).
        The underlying match is prefix-based so 'ES' also matches 'EW1', 'E1D', etc.
        """
        today_str = date.today().strftime("%Y%m%d")
        expiries: set[str] = set()
        for p in self._positions_snapshot:
            sec_type = getattr(p, "sec_type", "")
            if sec_type not in ("FOP", "OPT"):
                continue
            pos_sym = (getattr(p, "symbol", "") or "").upper()
            exp = getattr(p, "expiry", None)
            if not exp:
                continue
            # Prefix match: 'ES' → matches ES, EW1, E1D, EXH etc.
            # 'MES' → matches only MES* symbols
            if not (pos_sym == underlying.upper() or pos_sym.startswith(underlying.upper())):
                continue
            if hasattr(exp, "strftime"):
                exp_str = exp.strftime("%Y%m%d")
            else:
                exp_str = str(exp).replace("-", "")[:8]
            if len(exp_str) == 8 and exp_str >= today_str:
                expiries.add(exp_str)
        return sorted(expiries)

    async def get_chain(
        self,
        underlying: str,
        expiry: date | None = None,
        sec_type: str = "FOP",
        exchange: str = "CME",
        max_strikes: int = 40,
        *,
        force_refresh: bool = False,
    ) -> list[ChainRow]:
        """Fetch the option chain for a symbol and expiry.

        For futures options (ES, MES) use sec_type='FOP', exchange='CME'.
        For equity options (SPY, QQQ) use sec_type='OPT', exchange='SMART'.
        max_strikes limits strikes to the N nearest around ATM (default 40).
        force_refresh=True bypasses the 1-day cache (used for streaming tick updates).
        """
        expiry_key = expiry.strftime("%Y%m%d") if expiry else "nearest"
        self._active_chain_request = {
            "underlying": underlying,
            "expiry": expiry,
            "sec_type": sec_type,
            "exchange": exchange,
            "max_strikes": max_strikes,
        }
        cache_key  = f"{underlying}|{expiry_key}|{sec_type}|{exchange}"
        now = _time_mod.time()
        if not force_refresh and cache_key in self._chain_cache:
            ts, cached_rows = self._chain_cache[cache_key]
            if now - ts < 86400:  # 1-day TTL
                logger.debug("chain cache hit for %s %s", underlying, expiry_key)
                self.chain_ready.emit(cached_rows)
                return cached_rows
        # Cancel any stale live-streaming subscriptions before fetching a new chain
        self.cancel_chain_streaming()
        # Resolve the underlying contract for ATM price lookup
        und_contract = await self._qualify_underlying(underlying, sec_type, exchange)

        # For FOP, futFopExchange must be set (e.g. "CME"); for stock options leave empty
        fut_fop_exchange = exchange if sec_type == "FOP" else ""

        try:
            chains = await asyncio.wait_for(
                self._ib.reqSecDefOptParamsAsync(
                    und_contract.symbol, fut_fop_exchange, und_contract.secType, und_contract.conId or 0,
                ),
                timeout=15,
            )
        except asyncio.TimeoutError:
            logger.warning("get_chain reqSecDefOptParams timed out for %s (market may be closed)", underlying)
            return []

        if not chains:
            return []

        # Pick the chain matching exchange and closest expiry
        chain_def = chains[0]
        for cd in chains:
            if cd.exchange == exchange:
                chain_def = cd
                break

        expiry_str = ""
        if expiry:
            expiry_str = expiry.strftime("%Y%m%d")
        elif chain_def.expirations:
            # Pick nearest expiry in the future
            today_str = date.today().strftime("%Y%m%d")
            future_exps = [e for e in sorted(chain_def.expirations) if e >= today_str]
            expiry_str = future_exps[0] if future_exps else chain_def.expirations[-1]

        if not expiry_str:
            logger.warning("get_chain: no expiry resolved for %s", underlying)
            return []

        # Build contracts for strikes near ATM (limit to max_strikes)
        all_strikes = sorted(chain_def.strikes)
        if max_strikes and len(all_strikes) > max_strikes:
            # Use the qualified underlying's last price to center around ATM
            atm_price = None
            if und_contract.conId:
                try:
                    ticker = self._ib.reqMktData(und_contract, genericTickList="", snapshot=True, regulatorySnapshot=False)
                    await asyncio.sleep(1)
                    if ticker.last and ticker.last > 0:
                        atm_price = ticker.last
                    elif ticker.close and ticker.close > 0:
                        atm_price = ticker.close
                except Exception:
                    pass

            if atm_price:
                # Find the strike closest to ATM
                mid_idx = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i] - atm_price))
            else:
                mid_idx = len(all_strikes) // 2

            half = max_strikes // 2
            start = max(0, mid_idx - half)
            end = min(len(all_strikes), start + max_strikes)
            strikes = all_strikes[start:end]
            logger.info("Chain: using %d/%d strikes (ATM=%.1f, range=%.0f-%.0f)",
                        len(strikes), len(all_strikes), atm_price or 0,
                        strikes[0], strikes[-1])
        else:
            strikes = all_strikes
        contracts: list[Contract] = []
        for strike in strikes:
            for right in ("C", "P"):
                if sec_type == "FOP":
                    c = FuturesOption(
                        symbol=underlying,
                        lastTradeDateOrContractMonth=expiry_str,
                        strike=strike,
                        right=right,
                        exchange=exchange,
                        currency="USD",
                    )
                else:
                    c = Option(
                        symbol=underlying,
                        lastTradeDateOrContractMonth=expiry_str,
                        strike=strike,
                        right=right,
                        exchange=exchange or "SMART",
                        currency="USD",
                    )
                contracts.append(c)

        # Qualify contracts in batches (with timeout to prevent hangs)
        qualified: list[Contract] = []
        batch_size = 50
        for batch_start in range(0, len(contracts), batch_size):
            batch = contracts[batch_start:batch_start + batch_size]
            try:
                q = await asyncio.wait_for(
                    self._ib.qualifyContractsAsync(*batch),
                    timeout=15,
                )
                qualified.extend([c for c in q if c is not None and c.conId > 0])
            except asyncio.TimeoutError:
                logger.warning("Chain qualify batch timed out (batch %d)", batch_start)
            except Exception as exc:
                logger.warning("Chain qualify batch failed: %s", exc)

        if not qualified and sec_type == "FOP":
            qualified = await self._fallback_fop_contracts_for_expiry(
                underlying=underlying,
                exchange=exchange,
                expiry_str=expiry_str,
                max_strikes=max_strikes,
                und_contract=und_contract,
            )

        # Request streaming market data for all qualified contracts
        chain_generic_ticks = os.getenv("IB_CHAIN_GENERIC_TICKS", "100,101,104,106")
        tickers = []
        for c in qualified:
            t = self._ib.reqMktData(
                c,
                genericTickList=chain_generic_ticks,
                snapshot=False,
                regulatorySnapshot=False,
            )
            tickers.append((c, t))

        # Let streams populate for initial render
        await asyncio.sleep(2)

        result: list[ChainRow] = []
        for c, t in tickers:
            raw_volume = getattr(t, "volume", 0)
            if isinstance(raw_volume, (int, float)) and raw_volume == raw_volume and raw_volume >= 0:
                volume = int(raw_volume)
            else:
                volume = 0

            raw_open_interest = getattr(t, "openInterest", 0)
            if isinstance(raw_open_interest, (int, float)) and raw_open_interest == raw_open_interest and raw_open_interest >= 0:
                open_interest = int(raw_open_interest)
            else:
                open_interest = 0

            greeks = self._extract_option_greeks_from_ticker(t)
            result.append(ChainRow(
                underlying=underlying,
                expiry=c.lastTradeDateOrContractMonth,
                strike=c.strike,
                right=c.right,
                conid=c.conId,
                bid=t.bid if t.bid > 0 else None,
                ask=t.ask if t.ask > 0 else None,
                last=t.last if t.last > 0 else None,
                volume=volume,
                open_interest=open_interest,
                iv=greeks["iv"],
                delta=greeks["delta"],
                gamma=greeks["gamma"],
                theta=greeks["theta"],
                vega=greeks["vega"],
            ))

        # Store live tickers — do NOT cancel; kept alive for streaming price updates.
        # Call cancel_chain_streaming() to clean up when the chain tab is hidden.
        for c, t in tickers:
            if c.conId:
                self._chain_tickers[c.conId] = (c, t)

        # Write to 1-day cache before emitting
        self._chain_cache[cache_key] = (_time_mod.time(), result)
        self.chain_ready.emit(result)
        return result

    def read_chain_from_live_tickers(self, rows: list) -> list:
        """Return updated ChainRows with the latest bid/ask/greeks from live IB tickers.

        Only overwrites a field when the live ticker has valid (non-zero/non-NaN) data;
        otherwise the cached row value is preserved.  Fast: no IB requests.
        """
        updated = []
        for row in rows:
            t_info = self._chain_tickers.get(row.conid)
            if not t_info:
                updated.append(row)
                continue
            _, ticker = t_info

            def _pos(val, fallback):
                """Return val if it is a positive finite number, else fallback."""
                if val is not None and isinstance(val, (int, float)) and val == val and val > 0:
                    return val
                return fallback

            def _nonneg_int(val, fallback):
                if isinstance(val, (int, float)) and val == val and val >= 0:
                    return int(val)
                return fallback

            greeks = self._extract_option_greeks_from_ticker(ticker)
            updated.append(ChainRow(
                underlying=row.underlying,
                expiry=row.expiry,
                strike=row.strike,
                right=row.right,
                conid=row.conid,
                bid=_pos(ticker.bid, row.bid),
                ask=_pos(ticker.ask, row.ask),
                last=_pos(ticker.last, row.last),
                volume=_nonneg_int(getattr(ticker, "volume", None), row.volume),
                open_interest=_nonneg_int(getattr(ticker, "openInterest", None), row.open_interest),
                iv=greeks["iv"] or row.iv,
                delta=greeks["delta"] if greeks["delta"] is not None else row.delta,
                gamma=greeks["gamma"] if greeks["gamma"] is not None else row.gamma,
                theta=greeks["theta"] if greeks["theta"] is not None else row.theta,
                vega=greeks["vega"] if greeks["vega"] is not None else row.vega,
            ))
        return updated

    def cancel_chain_streaming(self) -> None:
        """Cancel all live chain market data subscriptions and clear the ticker store.

        Call this when the chain tab is hidden or the underlying / expiry changes.
        """
        for _conid, (c, _) in list(self._chain_tickers.items()):
            try:
                self._ib.cancelMktData(c)
            except Exception:
                pass
        self._chain_tickers.clear()

    async def _fallback_fop_contracts_for_expiry(
        self,
        underlying: str,
        exchange: str,
        expiry_str: str,
        max_strikes: int,
        und_contract: Contract,
    ) -> list[Contract]:
        """Fallback path: derive FOP contracts directly from reqContractDetails."""
        try:
            details = await asyncio.wait_for(
                self._ib.reqContractDetailsAsync(
                    FuturesOption(
                        symbol=underlying,
                        lastTradeDateOrContractMonth=expiry_str,
                        exchange=exchange,
                        currency="USD",
                    )
                ),
                timeout=25,
            )
        except asyncio.TimeoutError:
            logger.warning("FOP contract-details fallback timed out for %s %s", underlying, expiry_str)
            return []
        except Exception as exc:
            logger.warning("FOP contract-details fallback failed for %s %s: %s", underlying, expiry_str, exc)
            return []

        fallback_contracts = [
            d.contract for d in details
            if getattr(d, "contract", None)
            and getattr(d.contract, "conId", 0)
            and getattr(d.contract, "right", "") in ("C", "P")
            and getattr(d.contract, "strike", 0) > 0
        ]
        if not fallback_contracts:
            return []

        if max_strikes and max_strikes > 0:
            by_strike: dict[float, list[Contract]] = {}
            for contract in fallback_contracts:
                by_strike.setdefault(float(contract.strike), []).append(contract)
            all_strikes = sorted(by_strike.keys())
            if len(all_strikes) > max_strikes:
                atm_price = None
                if und_contract.conId:
                    try:
                        ticker = self._ib.reqMktData(und_contract, genericTickList="", snapshot=True, regulatorySnapshot=False)
                        await asyncio.sleep(1)
                        if ticker.last and ticker.last > 0:
                            atm_price = ticker.last
                        elif ticker.close and ticker.close > 0:
                            atm_price = ticker.close
                    except Exception:
                        pass
                if atm_price:
                    mid_idx = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i] - atm_price))
                else:
                    mid_idx = len(all_strikes) // 2
                half = max_strikes // 2
                start = max(0, mid_idx - half)
                end = min(len(all_strikes), start + max_strikes)
                keep_strikes = set(all_strikes[start:end])
                fallback_contracts = [
                    contract for contract in fallback_contracts if float(contract.strike) in keep_strikes
                ]

        dedup: dict[tuple[int, str], Contract] = {}
        for contract in fallback_contracts:
            dedup[(int(contract.conId), str(contract.right))] = contract

        result = list(dedup.values())
        logger.info("FOP fallback provided %d contracts for %s %s", len(result), underlying, expiry_str)
        return result

    async def _fallback_fop_expiries_from_contract_details(self, underlying: str, exchange: str) -> list[str]:
        """Fallback path for FOP expiries when reqSecDefOptParams is unavailable."""
        try:
            details = await asyncio.wait_for(
                self._ib.reqContractDetailsAsync(
                    FuturesOption(symbol=underlying, exchange=exchange, currency="USD")
                ),
                timeout=20,
            )
        except asyncio.TimeoutError:
            logger.warning("FOP fallback contract-details timed out for %s", underlying)
            return []
        except Exception as exc:
            logger.warning("FOP fallback contract-details failed for %s: %s", underlying, exc)
            return []

        expiries: set[str] = set()
        for d in details:
            contract = getattr(d, "contract", None)
            if not contract:
                continue
            expiry_text = str(getattr(contract, "lastTradeDateOrContractMonth", ""))
            expiry_yyyymmdd = expiry_text[:8]
            if len(expiry_yyyymmdd) == 8 and expiry_yyyymmdd.isdigit():
                expiries.add(expiry_yyyymmdd)
        if not expiries:
            return []

        today_str = date.today().strftime("%Y%m%d")
        result = sorted(e for e in expiries if e >= today_str)
        logger.info("FOP fallback provided %d expiries for %s", len(result), underlying)
        return result

    def _lookup_conid_from_positions(self, symbol: str, expiry: str, strike: float, right: str) -> int:
        """Search live positions snapshot for a matching contract's conId."""
        sym_up = symbol.upper()
        right_up = right.upper() if right else ""
        # Normalise target expiry to YYYYMMDD string
        target_exp = expiry.replace("-", "")[:8] if expiry else ""
        for pos in (self._positions_snapshot or []):
            c = getattr(pos, "contract", None)
            if not c:
                continue
            # Normalise position contract expiry
            pos_exp = (c.lastTradeDateOrContractMonth or "").replace("-", "")[:8]
            if (
                c.symbol.upper() == sym_up
                and (c.right or "").upper() == right_up
                and abs(float(c.strike or 0) - strike) < 0.01
                and pos_exp == target_exp
                and getattr(c, "conId", 0) > 0
            ):
                return c.conId
        return 0

    def _lookup_conid_from_chain(self, symbol: str, expiry: str, strike: float, right: str) -> int:
        """Search all cached chain data for a matching contract's conId."""
        sym_up = symbol.upper()
        right_up = right.upper() if right else ""
        for _cache_key, (_ts, rows) in self._chain_cache.items():
            for row in rows:
                if (
                    row.underlying.upper() == sym_up
                    and row.expiry == expiry
                    and row.right.upper() == right_up
                    and abs(row.strike - strike) < 0.01
                    and row.conid > 0
                ):
                    return row.conid
        return 0

    async def _prepare_leg_for_resolution(self, leg: dict[str, Any]) -> dict[str, Any]:
        """Normalize a leg before contract resolution, inferring missing expiry when possible."""
        prepared = dict(leg)
        expiry_raw = str(prepared.get("expiry") or "").replace("-", "").strip()
        if expiry_raw:
            prepared["expiry"] = expiry_raw
            return prepared

        sec_type = str(prepared.get("sec_type") or "FOP").upper()
        if sec_type not in ("FOP", "OPT"):
            return prepared

        symbol = str(prepared.get("symbol") or "").upper().strip()
        if not symbol:
            return prepared

        exchange = str(prepared.get("exchange") or ("CME" if sec_type == "FOP" else "SMART"))
        expiries = self.get_position_expiries(symbol)
        if not expiries:
            try:
                expiries = await self.get_available_expiries(symbol, sec_type=sec_type, exchange=exchange)
            except Exception as exc:
                logger.debug("_prepare_leg_for_resolution: expiry lookup failed for %s: %s", symbol, exc)

        if expiries:
            inferred = expiries[0]
            prepared["expiry"] = inferred
            logger.info("Inferred missing expiry for %s %s %.2f %s -> %s",
                        symbol,
                        sec_type,
                        float(prepared.get("strike") or 0),
                        str(prepared.get("right") or ""),
                        inferred)

        return prepared

    async def _resolve_contracts(self, legs: list[dict]) -> list:
        """Build contracts for legs, using chain cache and qualifyContractsAsync as fallback.

        1. Pre-fill conIds from the chain cache for any leg missing one.
        2. If all conIds are found skip qualifyContractsAsync entirely.
        3. Otherwise qualify only the contracts still missing conIds.
        """
        prepared_legs = [await self._prepare_leg_for_resolution(lg) for lg in legs]
        contracts = [self._leg_to_contract(lg) for lg in prepared_legs]
        # -- Stage 1: fill conIds from leg dict itself (chain click-to-trade)
        # -- Stage 2: chain cache lookup
        # -- Stage 3: positions snapshot lookup
        original_contracts = list(contracts)  # keep a copy for fallback
        for i, (c, lg) in enumerate(zip(contracts, prepared_legs)):
            if getattr(c, "conId", 0) > 0:
                continue
            sym  = lg.get("symbol", "")
            exp  = str(lg.get("expiry", ""))
            strk = float(lg.get("strike") or 0)
            rght = str(lg.get("right", ""))
            # chain cache first
            cid = self._lookup_conid_from_chain(sym, exp, strk, rght)
            # positions snapshot as second fallback
            if not cid:
                cid = self._lookup_conid_from_positions(sym, exp, strk, rght)
            if cid > 0:
                c.conId = cid
                contracts[i] = c

        all_have_conid = all(getattr(c, "conId", 0) > 0 for c in contracts)
        if not all_have_conid:
            try:
                qualified = await self._ib.qualifyContractsAsync(*contracts)
                # Merge back: keep qualified where conId came back; keep original elsewhere
                resolved = []
                for orig, qual in zip(contracts, qualified if qualified else []):
                    if qual and getattr(qual, "conId", 0) > 0:
                        resolved.append(qual)
                    elif getattr(orig, "conId", 0) > 0:
                        resolved.append(orig)  # chain/position lookup already gave us conId
                    # else: truly unresolvable — omit
                contracts = resolved
            except Exception as exc:
                logger.warning("qualifyContractsAsync failed: %s", exc)
                contracts = [c for c in contracts if c is not None]
        else:
            contracts = [c for c in contracts if c is not None]

        # Last-resort: if we ended up with fewer contracts than legs, use original
        # unqualified contracts so IB can attempt validation on its end
        if len(contracts) < len(prepared_legs):
            resolved_count = len(contracts)
            missing_legs = prepared_legs[resolved_count:]
            missing_symbols = [f"{lg.get('symbol')} {lg.get('expiry')} {lg.get('strike')} {lg.get('right')}" for lg in missing_legs]
            logger.warning(
                "_resolve_contracts: only %d/%d legs resolved — "
                "unresolved: %s — using unqualified contracts as fallback",
                resolved_count, len(prepared_legs), ", ".join(missing_symbols),
            )
            contracts += [self._leg_to_contract(lg) for lg in missing_legs]

        return contracts

    # ── order placement ───────────────────────────────────────────────────

    async def place_order(
        self,
        legs: list[dict[str, Any]],
        order_type: str = "LIMIT",
        limit_price: float | None = None,
        source: str = "manual",
        rationale: str = "",
    ) -> dict[str, Any]:
        """Build, transmit, and log a live order.

        Each leg dict: { symbol, action, qty, conid, strike, right, expiry, sec_type, exchange }
        """
        # Persist order as DRAFT (if DB available)
        order_record = None
        if self._db_ok:
            try:
                order_record = await self._db.insert_order({
                    "account_id": self._account_id,
                    "status": "DRAFT",
                    "order_type": order_type,
                    "side": self._infer_side(legs),
                    "limit_price": limit_price,
                    "legs": legs,
                    "source": source,
                    "rationale": rationale,
                })
            except Exception as exc:
                logger.warning("DB insert_order failed: %s", exc)

        try:
            contracts = await self._resolve_contracts(legs)

            if len(contracts) != len(legs):
                raise ValueError(
                    f"Only {len(contracts)}/{len(legs)} legs qualified — "
                    "check symbols, strikes, and expiry dates."
                )

            # Build IB order
            if order_type == "LIMIT" and limit_price is not None:
                ib_order = LimitOrder(
                    action=legs[0]["action"],
                    totalQuantity=int(legs[0].get("qty", 1)),
                    lmtPrice=limit_price,
                )
            else:
                ib_order = MarketOrder(
                    action=legs[0]["action"],
                    totalQuantity=int(legs[0].get("qty", 1)),
                )

            # For combo orders, add combo legs
            if len(contracts) > 1:
                from ib_async import ComboLeg, Contract as IBC
                bag = IBC()
                bag.symbol = contracts[0].symbol
                bag.secType = "BAG"
                bag.exchange = contracts[0].exchange or "SMART"
                bag.currency = "USD"
                bag.comboLegs = []
                for i, (c, lg) in enumerate(zip(contracts, legs)):
                    cl = ComboLeg()
                    cl.conId = c.conId
                    cl.ratio = int(lg.get("qty", 1))
                    cl.action = lg["action"]
                    cl.exchange = c.exchange or "SMART"
                    bag.comboLegs.append(cl)
                trade = self._ib.placeOrder(bag, ib_order)
            else:
                trade = self._ib.placeOrder(contracts[0], ib_order)

            # Update status to PENDING
            if self._db_ok and order_record:
                await self._db.update_order_status(
                    order_record, "PENDING",
                    broker_order_id=str(trade.order.orderId),
                )

            # Wait for fill (up to 30s)
            fill_info = await self._wait_for_fill(trade, timeout=30.0)

            if fill_info.get("status") == "Filled":
                if self._db_ok and order_record:
                    await self._db.update_order_status(
                        order_record, "FILLED",
                        filled_price=fill_info.get("avg_price"),
                    )
                    # Log fills
                    for f in trade.fills:
                        await self._db.insert_fill({
                            "order_id": order_record,
                            "account_id": self._account_id,
                            "conid": f.contract.conId,
                            "symbol": f.contract.localSymbol or f.contract.symbol,
                            "action": f.execution.side,
                            "quantity": f.execution.shares,
                            "fill_price": f.execution.price,
                            "commission": f.commissionReport.commission if f.commissionReport else 0,
                            "realized_pnl": f.commissionReport.realizedPNL if f.commissionReport else None,
                            "execution_id": f.execution.execId,
                        })
                self.order_filled.emit(fill_info)
            else:
                if self._db_ok and order_record:
                    await self._db.update_order_status(order_record, fill_info.get("status", "PENDING"))

            return {"order_id": str(order_record) if order_record else "N/A", **fill_info}

        except Exception as exc:
            logger.exception("Order placement failed")
            if self._db_ok and order_record:
                await self._db.update_order_status(order_record, "REJECTED")
            self.error_occurred.emit(str(exc))
            return {"order_id": str(order_record) if order_record else "N/A", "status": "REJECTED", "error": str(exc)}

    async def whatif_order(
        self,
        legs: list[dict[str, Any]],
        order_type: str = "LIMIT",
        limit_price: float | None = None,
    ) -> dict[str, Any]:
        """Run a WhatIf simulation without transmitting.
        
        Uses IB's whatIfOrderAsync() method to properly simulate margin impact.
        Returns margin deltas (init_margin_change, maint_margin_change).
        """
        contracts = await self._resolve_contracts(legs)
        qualified_count = sum(1 for c in contracts if getattr(c, "conId", 0) > 0)

        if qualified_count == 0:
            return {"error": "No contracts qualified — check symbol, expiry, and strike", "status": "error"}
        if qualified_count != len(legs):
            return {"error": f"Only {qualified_count}/{len(legs)} leg(s) qualified", "status": "error"}

        if order_type == "LIMIT" and limit_price is not None:
            ib_order = LimitOrder(
                action=legs[0]["action"],
                totalQuantity=int(legs[0].get("qty", 1)),
                lmtPrice=limit_price,
            )
        else:
            ib_order = MarketOrder(
                action=legs[0]["action"],
                totalQuantity=int(legs[0].get("qty", 1)),
            )

        if len(contracts) > 1:
            from ib_async import ComboLeg, Contract as IBC
            bag = IBC()
            bag.symbol = contracts[0].symbol
            bag.secType = "BAG"
            bag.exchange = getattr(contracts[0], "exchange", None) or "SMART"
            bag.currency = "USD"
            bag.comboLegs = []
            for c, lg in zip(contracts, legs):
                cl = ComboLeg()
                cl.conId = c.conId
                cl.ratio = int(lg.get("qty", 1))
                cl.action = lg["action"]
                cl.exchange = getattr(c, "exchange", None) or "SMART"
                bag.comboLegs.append(cl)
            contract = bag
        else:
            contract = contracts[0]

        # Use whatIfOrderAsync() which properly waits for IB's WhatIf response
        # Returns OrderState object with margin change attributes
        # IB margin calculations can take 5-30s depending on portfolio complexity
        whatif_timeout = 90.0  # Increased from 15s for complex portfolios
        try:
            order_state = await asyncio.wait_for(
                self._ib.whatIfOrderAsync(contract, ib_order),
                timeout=whatif_timeout,
            )
            
            init_change = _safe_float(getattr(order_state, "initMarginChange", None)) or 0.0
            maint_change = _safe_float(getattr(order_state, "maintMarginChange", None)) or 0.0
            equity_change = _safe_float(getattr(order_state, "equityWithLoanChange", None)) or 0.0
            
            logger.info(
                "WhatIf margin impact: init_change=%s, maint_change=%s, equity_change=%s",
                init_change, maint_change, equity_change,
            )
            
            return {
                "init_margin_change": init_change,
                "maint_margin_change": maint_change,
                "equity_with_loan_change": equity_change,
                "status": "success",
            }
        except asyncio.TimeoutError:
            logger.warning(
                "WhatIf simulation timed out after %.0f seconds — IB Gateway may be slow or unavailable",
                whatif_timeout,
            )
            return {
                "error": "WhatIf simulation timed out — IB Gateway may be slow or unavailable",
                "status": "timeout",
            }
        except Exception as exc:
            logger.error("WhatIf simulation failed: %s: %s", type(exc).__name__, exc)
            return {
                "error": f"WhatIf simulation failed: {exc}",
                "status": "error",
            }

    # ── market data ─────────────────────────────────────────────────────

    async def get_market_snapshot(self, symbol: str, sec_type: str = "STK", exchange: str = "SMART") -> MarketSnapshot:
        """Fetch a real-time quote snapshot for a single instrument."""
        contract = await self._qualify_underlying(symbol, sec_type, exchange)

        ticker = self._ib.reqMktData(contract, genericTickList="", snapshot=True, regulatorySnapshot=False)
        await asyncio.sleep(2)

        snap = MarketSnapshot(
            symbol=symbol,
            last=ticker.last if ticker.last and ticker.last > 0 else None,
            bid=ticker.bid if ticker.bid and ticker.bid > 0 else None,
            ask=ticker.ask if ticker.ask and ticker.ask > 0 else None,
            high=ticker.high if ticker.high and ticker.high > 0 else None,
            low=ticker.low if ticker.low and ticker.low > 0 else None,
            close=ticker.close if ticker.close and ticker.close > 0 else None,
            volume=int(ticker.volume) if ticker.volume and ticker.volume >= 0 else 0,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        # snapshot=True auto-terminates — no cancelMktData needed
        self._market_snapshots[symbol.upper()] = {
            "last": snap.last, "bid": snap.bid, "ask": snap.ask,
            "close": snap.close, "volume": snap.volume,
        }
        # ── update last-price cache ──────────────────────────────────────
        best_price = snap.last or snap.bid or snap.ask or snap.close
        if best_price and best_price > 0:
            self._last_price_cache[symbol.upper()] = float(best_price)
        self.market_snapshot.emit(snap)
        return snap

    def _build_simple_contract(self, symbol: str, sec_type: str, exchange: str) -> Contract:
        """Build a simple contract for market data requests."""
        if sec_type == "FUT":
            return Future(symbol=symbol, exchange=exchange or "CME", currency="USD")
        elif sec_type in ("STK", "STOCK", "ETF"):
            return Stock(symbol=symbol, exchange=exchange or "SMART", currency="USD")
        else:
            return Stock(symbol=symbol, exchange="SMART", currency="USD")

    # ── bid/ask for order entry ───────────────────────────────────────────

    async def get_bid_ask_for_legs(self, legs: list[dict]) -> list[dict]:
        """Fetch live bid/ask for each leg.

        Tries in order:
        1. Live chain ticker cache (no IB request needed)
        2. reqMktData snapshot for resolved contract

        Returns list of dicts: {"bid": float|None, "ask": float|None, "mid": float|None}
        """
        results: list[dict] = []
        for leg in legs:
            conid = int(leg.get("conid") or 0)
            bid: float | None = None
            ask: float | None = None

            # 1. Chain ticker cache (fast path)
            if conid and conid in self._chain_tickers:
                _, t = self._chain_tickers[conid]
                b = t.bid if (t.bid and t.bid > 0) else None
                a = t.ask if (t.ask and t.ask > 0) else None
                if b or a:
                    bid, ask = b, a

            # 2. reqMktData snapshot
            if bid is None and ask is None:
                try:
                    contracts = await self._resolve_contracts([leg])
                    if contracts:
                        c = contracts[0]
                        t = self._ib.reqMktData(
                            c, genericTickList="", snapshot=True, regulatorySnapshot=False
                        )
                        await asyncio.sleep(1.5)
                        bid = t.bid if (t.bid and t.bid > 0) else None
                        ask = t.ask if (t.ask and t.ask > 0) else None
                except Exception as exc:
                    logger.debug("get_bid_ask_for_legs: snapshot failed for leg %s: %s", leg, exc)

            mid = round((bid + ask) / 2.0, 2) if (bid and ask) else (bid or ask)
            results.append({"bid": bid, "ask": ask, "mid": mid})

        return results

    # ── open orders management ────────────────────────────────────────────

    async def get_open_orders(self) -> list[OpenOrder]:
        """Fetch all open/working orders from IB."""
        try:
            if hasattr(self._ib, "reqAllOpenOrdersAsync"):
                await asyncio.wait_for(self._ib.reqAllOpenOrdersAsync(), timeout=8)
            elif hasattr(self._ib, "reqAllOpenOrders"):
                self._ib.reqAllOpenOrders()
                await asyncio.wait_for(asyncio.sleep(0.4), timeout=8)
            elif hasattr(self._ib, "reqOpenOrdersAsync"):
                await asyncio.wait_for(self._ib.reqOpenOrdersAsync(), timeout=8)
            elif hasattr(self._ib, "reqOpenOrders"):
                self._ib.reqOpenOrders()
                await asyncio.sleep(0.4)
        except Exception as exc:
            logger.debug("reqAllOpenOrders refresh failed: %s", exc)

        trades = self._ib.openTrades()
        result: list[OpenOrder] = []
        for trade in trades:
            o = trade.order
            os_ = trade.orderStatus
            c = trade.contract
            result.append(OpenOrder(
                order_id=o.orderId,
                perm_id=o.permId,
                symbol=c.localSymbol or c.symbol,
                action=o.action,
                quantity=float(o.totalQuantity),
                order_type=o.orderType,
                limit_price=o.lmtPrice if o.orderType == "LMT" else None,
                status=os_.status,
                filled=float(os_.filled),
                remaining=float(os_.remaining),
                avg_fill_price=float(os_.avgFillPrice),
            ))

        self.orders_updated.emit(result)
        return result

    async def cancel_order(self, order_id: int) -> None:
        """Cancel a specific order by orderId."""
        trades = self._ib.openTrades()
        for trade in trades:
            if trade.order.orderId == order_id:
                self._ib.cancelOrder(trade.order)
                logger.info("Cancelled order %d", order_id)
                return
        raise ValueError(f"Order {order_id} not found in open trades")

    async def cancel_all_orders(self) -> int:
        """Cancel all open orders. Returns count cancelled."""
        trades = self._ib.openTrades()
        for trade in trades:
            self._ib.cancelOrder(trade.order)
        logger.info("Cancelled %d open orders", len(trades))
        return len(trades)

    async def modify_order_price(self, order_id: int, new_price: float) -> None:
        """Modify the limit price of an open order (transmits immediately)."""
        trades = self._ib.openTrades()
        for trade in trades:
            if trade.order.orderId == order_id:
                order = trade.order
                if order.orderType not in ("LMT", "LIMIT"):
                    raise ValueError(f"Order {order_id} is {order.orderType} — only LIMIT orders can be repriced")
                order.lmtPrice = round(float(new_price), 2)
                self._ib.placeOrder(trade.contract, order)
                logger.info("Modified order %d → new price %.2f", order_id, new_price)
                return
        raise ValueError(f"Order {order_id} not found in open trades")

    # ── helpers ───────────────────────────────────────────────────────────

    def _leg_to_contract(self, leg: dict[str, Any]) -> Contract:
        """Convert a leg dict to an ib_async Contract."""
        sec_type = leg.get("sec_type", "FOP")
        symbol = leg.get("symbol", "")
        exchange = leg.get("exchange", "")

        # ------------------------------------------------------------------
        # If the caller already resolved a conId we can skip all field
        # lookups — IB will recognise the contract by conId alone.
        # ------------------------------------------------------------------
        conid_val = leg.get("conid")
        try:
            conid_int = int(conid_val) if conid_val is not None else 0
        except (TypeError, ValueError):
            conid_int = 0

        # _FOP_MULTIPLIERS helps IB's qualifyContractsAsync find the contract.
        _FOP_MULT_HINTS: dict[str, str] = {
            "ES": "50", "MES": "5", "NQ": "20", "MNQ": "2",
            "RTY": "50", "M2K": "5", "YM": "5", "MYM": "0.5",
        }
        if sec_type == "FOP":
            c = FuturesOption(
                symbol=symbol,
                lastTradeDateOrContractMonth=str(leg.get("expiry", "")),
                strike=float(leg.get("strike") or 0),
                right=leg.get("right", "C"),
                exchange=exchange or "CME",
                currency="USD",
                multiplier=leg.get("multiplier") or _FOP_MULT_HINTS.get(symbol.upper(), ""),
                tradingClass=symbol.upper(),  # needed to disambiguate ES vs MES on CME
            )
        elif sec_type == "OPT":
            c = Option(
                symbol=symbol,
                lastTradeDateOrContractMonth=str(leg.get("expiry", "")),
                strike=float(leg.get("strike") or 0),
                right=leg.get("right", "C"),
                exchange=exchange or "SMART",
                currency="USD",
            )
        elif sec_type == "FUT":
            c = Future(
                symbol=symbol,
                lastTradeDateOrContractMonth=str(leg.get("expiry", "")),
                exchange=exchange or "CME",
                currency="USD",
            )
        else:
            c = Stock(symbol=symbol, exchange=exchange or "SMART", currency="USD")

        if conid_int > 0:
            c.conId = conid_int
        return c

    @staticmethod
    def _infer_exchange(contract) -> str:
        """Infer the correct exchange for a contract lacking one."""
        sec = getattr(contract, "secType", "")
        sym = getattr(contract, "symbol", "")
        if sec == "FOP":
            return "CME"   # ES, MES, NQ, MNQ, etc.
        if sec == "OPT":
            return "SMART"
        if sec == "FUT":
            return "CME"
        if sec == "STK":
            return "SMART"
        return "SMART"

    @staticmethod
    def _parse_expiry(val) -> date | None:
        """Convert IBKR expiry string (YYYYMMDD) or date to datetime.date for DB."""
        if val is None:
            return None
        if isinstance(val, date):
            return val
        if not isinstance(val, str):
            val = str(val)
        try:
            return date(int(val[:4]), int(val[4:6]), int(val[6:8]))
        except (ValueError, IndexError, TypeError):
            return None

    @staticmethod
    def _infer_side(legs: list[dict]) -> str:
        actions = {lg.get("action", "").upper() for lg in legs}
        if actions == {"BUY"}:
            return "BUY"
        elif actions == {"SELL"}:
            return "SELL"
        return "COMBO"

    async def _wait_for_fill(self, trade: Trade, timeout: float = 30.0) -> dict[str, Any]:
        """Poll trade status until filled or timeout."""
        elapsed = 0.0
        interval = 0.5
        while elapsed < timeout:
            await asyncio.sleep(interval)
            elapsed += interval

            status = trade.orderStatus.status
            if status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                break

        fills = trade.fills
        avg_price = 0.0
        if fills:
            total_qty = sum(f.execution.shares for f in fills)
            avg_price = sum(f.execution.price * f.execution.shares for f in fills) / total_qty if total_qty > 0 else 0.0

        return {
            "status": trade.orderStatus.status,
            "avg_price": avg_price,
            "filled_qty": sum(f.execution.shares for f in fills) if fills else 0,
            "remaining": trade.orderStatus.remaining,
            "broker_order_id": str(trade.order.orderId),
        }

    async def _attempt_reconnect(self) -> None:
        async with self._reconnect_lock:
            for attempt in range(1, self._watchdog_max_attempts + 1):
                if self._manual_disconnect_requested:
                    return

                delay = min(2 ** (attempt - 1), self._watchdog_backoff_cap)
                self.connection_state.emit(
                    "reconnecting",
                    f"Reconnecting to IBKR (attempt {attempt}/{self._watchdog_max_attempts}) in {delay}s",
                )
                await asyncio.sleep(delay)

                try:
                    await self._ib.connectAsync(self._host, self._port, clientId=self._client_id, timeout=30)
                    accounts = self._ib.managedAccounts()
                    self._account_id = accounts[0] if accounts else self._account_id
                    self.connection_state.emit("reconnected", f"Reconnected to {self._account_id or 'IBKR'}")
                    self.connected.emit()
                    await self._restore_streams_after_reconnect()
                    self._reconnect_task = None
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Reconnect attempt %d/%d failed: %s", attempt, self._watchdog_max_attempts, exc)

            message = f"Unable to reconnect after {self._watchdog_max_attempts} attempts"
            logger.error(message)
            self.connection_state.emit("failed", message)
            self.error_occurred.emit(message)
            self._reconnect_task = None

    async def _restore_streams_after_reconnect(self) -> None:
        if not self._active_chain_request:
            return
        try:
            await self.get_chain(force_refresh=True, **self._active_chain_request)
        except Exception as exc:
            logger.debug("Failed to restore chain subscriptions after reconnect: %s", exc)

    def _log_resolve_warning(
        self,
        signature: str,
        resolved_count: int,
        total_legs: int,
        missing_symbols: list[str],
    ) -> None:
        now = _time_mod.time()
        last_ts = self._resolve_warning_cache.get(signature, 0.0)
        if now - last_ts >= self._resolve_warning_ttl:
            logger.warning(
                "_resolve_contracts: only %d/%d legs resolved after all lookups — falling back to unqualified contracts for remaining legs",
                resolved_count,
                total_legs,
            )
            self._resolve_warning_cache[signature] = now
            return
        logger.debug(
            "Suppressed duplicate resolve warning for %s (unresolved: %s)",
            signature,
            ", ".join(missing_symbols),
        )

    # ── ib_async event handlers ───────────────────────────────────────────

    def _on_ib_connected(self) -> None:
        logger.info("IB connected event")

    def _on_ib_disconnected(self) -> None:
        logger.warning("IB disconnected event")
        self.connection_state.emit("disconnected", "IB Gateway connection lost")
        self.disconnected.emit()
        if self._manual_disconnect_requested:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = loop.create_task(self._attempt_reconnect())

    def _on_ib_error(self, reqId: int, errorCode: int, errorString: str, contract: Any) -> None:
        # Filter out benign warnings (farm connections, market data, snapshot cancels)
        if errorCode in (2104, 2105, 2106, 2107, 2108, 2158, 2119):
            return
        # Error 200 = no security definition found (expected for invalid strikes)
        # Error 300 = "Can't find EId" — benign snapshot cancel race
        # Error 321 = validation (missing exchange)
        # Error 322 = no derivatives returned for chain query
        # Error 10089 = market data subscription required (not subscribed via API)
        # Error 10090 = Part of requested market data is not subscribed
        # Error 10358 = Fundamentals data is not allowed for this account type
        # Error 2103 = Market data farm connection broken (market closed/reconnecting)
        if errorCode in (200, 300, 321, 322, 430, 10089, 10090, 10358, 2103):
            logger.debug("IB Error %d (reqId %d): %s", errorCode, reqId, errorString)
            return
        msg = f"IB Error {errorCode}: {errorString}"
        logger.error(msg)
        self.error_occurred.emit(msg)

    def _on_order_status(self, trade: Trade) -> None:
        info = {
            "order_id": trade.order.orderId,
            "status": trade.orderStatus.status,
            "filled": trade.orderStatus.filled,
            "remaining": trade.orderStatus.remaining,
            "avg_fill_price": trade.orderStatus.avgFillPrice,
        }
        self.order_status.emit(info)


def _safe_float(val: Any) -> float | None:
    """Convert IB's string margin values to float, handling empty/None."""
    if val is None:
        return None
    try:
        f = float(val)
        return f if f < 1e15 else None  # IB returns 1.7976931348623157E308 for "not applicable"
    except (ValueError, TypeError):
        return None



