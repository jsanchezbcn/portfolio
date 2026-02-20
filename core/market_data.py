"""core/market_data.py — Real-time market data service for the order builder.

Provides live bid/ask/last quotes for stocks, futures, and options chains
via IBKR Client Portal API and Tastytrade.

Data sources (by instrument type):
  Stocks / ETFs  — IBKR /iserver/marketdata/snapshot (fields 84/86/31)
  Futures        — IBKR secdef/search + secdef/info → front-month conid → snapshot
  Options chain  — Tastytrade SDK (primary); IBKR snapshot fallback

SAFETY NOTE: This module is READ-ONLY.  No orders are placed.
"""

from __future__ import annotations

import asyncio
import calendar as _cal
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

logger = logging.getLogger(__name__)

# IBKR snapshot field codes
_FIELD_LAST = "31"
_FIELD_BID  = "84"
_FIELD_ASK  = "86"
_PRICE_FIELDS = f"{_FIELD_LAST},{_FIELD_BID},{_FIELD_ASK}"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Quote:
    """Real-time bid/ask/last quote for a single instrument."""

    symbol: str
    conid: Optional[int] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def mid(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2.0
        return self.last

    @property
    def spread(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None:
            return self.ask - self.bid
        return None

    def is_valid(self) -> bool:
        return self.bid is not None or self.ask is not None or self.last is not None


@dataclass
class OptionQuote:
    """One row in an options chain — strike × expiry × call/put with live quotes."""

    symbol: str                          # Tastytrade/DXFeed symbol
    underlying: str
    expiry: str                          # YYYY-MM-DD
    strike: float
    option_type: str                     # "call" or "put"
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0
    delta: float = 0.0
    iv: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    conid: Optional[str] = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0 if self.ask > 0 else self.last

    @property
    def spread(self) -> float:
        return self.ask - self.bid


# ---------------------------------------------------------------------------
# MarketDataService
# ---------------------------------------------------------------------------


class MarketDataService:
    """Unified real-time quote service for stocks, futures, and options.

    Parameters
    ----------
    ibkr_client:
        An ``IBKRClient`` instance (from ``ibkr_portfolio_client.py``).
    tastytrade_fetcher:
        An optional ``TastytradeOptionsFetcher`` instance for options chain data.
        When ``None``, options chain falls back to IBKR only.
    """

    def __init__(self, ibkr_client, tastytrade_fetcher=None) -> None:
        self._ibkr = ibkr_client
        self._tt = tastytrade_fetcher

    # ------------------------------------------------------------------
    # Symbol → conid resolution
    # ------------------------------------------------------------------

    def resolve_conid(
        self,
        symbol: str,
        sec_type: str = "STK",
        exchange: str = "SMART",
    ) -> Optional[int]:
        """Resolve a symbol to an IBKR contract ID via ``/iserver/secdef/search``.

        Parameters
        ----------
        symbol:
            Ticker symbol, e.g. ``"AAPL"`` or ``"ES"``.
        sec_type:
            IBKR security type: ``"STK"`` (stock/ETF), ``"FUT"`` (futures),
            ``"OPT"`` (option), ``"CASH"`` (FX).
        exchange:
            Exchange filter.  ``"SMART"`` lets IBKR route automatically.

        Returns
        -------
        int | None
            The conid, or ``None`` on lookup failure.
        """
        try:
            resp = self._ibkr.session.get(
                f"{self._ibkr.base_url}/v1/api/iserver/secdef/search",
                params={"symbol": symbol, "secType": sec_type},
                verify=False,
                timeout=5,
            )
            if resp.status_code != 200:
                return None
            items = resp.json() if isinstance(resp.json(), list) else []
            for item in items:
                # For stocks: item has conid directly
                # For futures: conid is the parent; need secdef/info for actual contract
                sections = item.get("sections") or []
                has_type = any(
                    s.get("secType") == sec_type for s in sections
                ) if sections else True
                if has_type and item.get("conid"):
                    return int(item["conid"])
        except Exception as exc:
            logger.debug("resolve_conid(%s, %s) failed: %s", symbol, sec_type, exc)
        return None

    # ------------------------------------------------------------------
    # Stock / ETF quotes
    # ------------------------------------------------------------------

    def get_quote(self, symbol: str, sec_type: str = "STK") -> Optional[Quote]:
        """Fetch a real-time bid/ask/last quote for a stock or ETF.

        Uses IBKR ``/iserver/marketdata/snapshot`` with fields 84 (bid),
        86 (ask), 31 (last).

        Returns ``None`` on any error (never raises).
        """
        try:
            conid = self.resolve_conid(symbol, sec_type=sec_type)
            if conid is None:
                logger.debug("get_quote: could not resolve conid for %s", symbol)
                return None

            snapshot = self._ibkr.get_market_snapshot(
                [conid], fields=_PRICE_FIELDS, subscribe_sleep=1.0
            )
            if conid not in snapshot:
                return None

            item = snapshot[conid]
            return Quote(
                symbol=symbol,
                conid=conid,
                bid=_parse_price(item.get(_FIELD_BID)),
                ask=_parse_price(item.get(_FIELD_ASK)),
                last=_parse_price(item.get(_FIELD_LAST)),
            )
        except Exception as exc:
            logger.debug("get_quote(%s) failed: %s", symbol, exc)
            return None

    # ------------------------------------------------------------------
    # Futures quotes
    # ------------------------------------------------------------------

    def get_futures_quote(self, root_symbol: str) -> Optional[Quote]:
        """Fetch a real-time quote for the front-month futures contract.

        Two-step process:
          1. ``/iserver/secdef/search?symbol={root}&secType=FUT`` → parent conid
          2. ``/iserver/secdef/info`` for the front-month contract conid
          3. Snapshot for bid/ask/last

        Supported: ES, MES, NQ, MNQ, RTY, M2K, GC, MGC, SI, CL, NG, ZB, ZN.
        Falls back to ``get_quote(root, sec_type="FUT")`` for unlisted symbols.

        Returns ``None`` on any error (never raises).
        """
        try:
            # Step 1 — parent conid
            r1 = self._ibkr.session.get(
                f"{self._ibkr.base_url}/v1/api/iserver/secdef/search",
                params={"symbol": root_symbol.upper(), "secType": "FUT"},
                verify=False,
                timeout=5,
            )
            if r1.status_code != 200:
                return None

            items = r1.json() if isinstance(r1.json(), list) else []
            parent_conid: Optional[int] = None
            exchange: str = "CME"
            for item in items:
                sections = item.get("sections") or []
                if any(s.get("secType") == "FUT" for s in sections):
                    parent_conid = item.get("conid")
                    # Try to get the exchange from sections
                    for s in sections:
                        if s.get("secType") == "FUT" and s.get("exchange"):
                            exchange = s["exchange"]
                            break
                    break

            if not parent_conid:
                return None

            # Step 2 — front-month contract conid
            month_code = _front_month_code(root_symbol.upper())
            r2 = self._ibkr.session.get(
                f"{self._ibkr.base_url}/v1/api/iserver/secdef/info",
                params={
                    "conid":   parent_conid,
                    "sectype": "FUT",
                    "month":   month_code,
                    "exchange": exchange,
                },
                verify=False,
                timeout=5,
            )
            conid: Optional[int] = None
            if r2.status_code == 200:
                contracts = r2.json() if isinstance(r2.json(), list) else []
                if contracts:
                    conid = contracts[0].get("conid")

            if not conid:
                # Fallback: use parent conid directly
                conid = parent_conid

            # Step 3 — snapshot
            snapshot = self._ibkr.get_market_snapshot(
                [int(conid)], fields=_PRICE_FIELDS, subscribe_sleep=1.5
            )
            item = snapshot.get(int(conid))
            if item is None:
                return None

            return Quote(
                symbol=f"{root_symbol.upper()}{month_code}",
                conid=int(conid),
                bid=_parse_price(item.get(_FIELD_BID)),
                ask=_parse_price(item.get(_FIELD_ASK)),
                last=_parse_price(item.get(_FIELD_LAST)),
            )
        except Exception as exc:
            logger.debug("get_futures_quote(%s) failed: %s", root_symbol, exc)
            return None

    # ------------------------------------------------------------------
    # Options chain
    # ------------------------------------------------------------------

    def get_options_chain(
        self,
        underlying: str,
        expiry: Optional[str] = None,
    ) -> List[OptionQuote]:
        """Fetch a full options chain with live bid/ask/Greeks.

        Primary source: Tastytrade ``fetch_and_cache_options_for_underlying``
        (uses real streaming WebSocket data).
        Fallback: empty list with a warning logged.

        Parameters
        ----------
        underlying:
            Root symbol, e.g. ``"SPY"``, ``"SPX"``, ``"AAPL"``, ``"/ES"``.
        expiry:
            Optional ``YYYY-MM-DD`` string to filter to a single expiry.
            When ``None``, all expirations are returned (may be large).

        Returns
        -------
        list[OptionQuote]
            Sorted by expiry → strike → call before put.
            Empty list on error (never raises).
        """
        tt_chain = self._tt_chain(underlying, expiry)
        if tt_chain:
            return tt_chain

        logger.debug(
            "get_options_chain(%s): Tastytrade unavailable, returning empty chain",
            underlying,
        )
        return []

    def _tt_chain(
        self,
        underlying: str,
        expiry: Optional[str] = None,
    ) -> List[OptionQuote]:
        """Fetch options chain from Tastytrade, converting OptionData → OptionQuote."""
        if self._tt is None:
            return []
        try:
            # Use the synchronous simulate_prefetch path which uses the cache
            cached: dict = self._tt.simulate_prefetch(underlying)  # {key: OptionData}
            if not cached:
                # Try the async fetch path via run_in_executor
                cached = _run_sync(
                    self._tt.fetch_and_cache_options_for_underlying(underlying)
                )

            result: List[OptionQuote] = []
            for key, od in cached.items():
                if expiry and od.expiration and od.expiration != expiry:
                    continue
                result.append(
                    OptionQuote(
                        symbol=od.symbol,
                        underlying=od.underlying,
                        expiry=od.expiration or "",
                        strike=od.strike,
                        option_type=od.option_type,
                        bid=od.bid,
                        ask=od.ask,
                        last=od.mid,
                        delta=od.delta,
                        iv=od.iv,
                        gamma=od.gamma,
                        theta=od.theta,
                        vega=od.vega,
                    )
                )
            result.sort(key=lambda q: (q.expiry, q.strike, q.option_type))
            return result
        except Exception as exc:
            logger.debug("_tt_chain(%s) failed: %s", underlying, exc)
            return []


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_price(raw) -> Optional[float]:
    """Convert IBKR snapshot field value to float, or None if unavailable."""
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _front_month_code(root: str) -> str:
    """Return the IBKR front-month expiry code (e.g. ``"MAR26"``) for a futures root.

    Uses quarterly cycle for financial futures (ES, MES, NQ, MNQ, RTY, M2K).
    Uses monthly cycle for commodities (GC, SI, CL, NG, ZB, ZN, ZC, ZS, ZW).
    """
    from datetime import datetime

    now = datetime.now(timezone.utc)
    # Quarterly (March/June/Sep/Dec expiry)
    quarterly_roots = {
        "ES", "MES", "NQ", "MNQ", "RTY", "M2K", "YM", "MYM",
        "GE", "SR3", "ZQ",
    }
    if root in quarterly_roots:
        cycle = [3, 6, 9, 12]
    else:
        # Monthly (all months)
        cycle = list(range(1, 13))

    year = now.year
    for candidate_month in cycle:
        candidate_year = year if candidate_month >= now.month else year + 1
        _, days_in_month = _cal.monthrange(candidate_year, candidate_month)
        # Futures typically expire on third Friday
        fridays = [
            d for d in range(1, days_in_month + 1)
            if _cal.weekday(candidate_year, candidate_month, d) == 4
        ]
        if len(fridays) >= 3:
            third_friday = datetime(
                candidate_year, candidate_month, fridays[2], tzinfo=timezone.utc
            )
            if third_friday > now:
                abbr = _cal.month_abbr[candidate_month].upper()
                return f"{abbr}{str(candidate_year)[2:]}"

    # Fallback
    abbr = _cal.month_abbr[cycle[0]].upper()
    return f"{abbr}{str(now.year + 1)[2:]}"


def _run_sync(coro):
    """Run an async coroutine synchronously (for use in Streamlit's sync context)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # In Streamlit, the loop may already be running; use a fresh thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result(timeout=10)
        else:
            return loop.run_until_complete(coro)
    except Exception as exc:
        logger.debug("_run_sync failed: %s", exc)
        return {}
