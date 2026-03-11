"""dashboard/pages/trade.py — Trading terminal page.

Layout (inspired by Tastytrade / IBKR TWS):

┌──────────────────────────────────────────────────────────────────────┐
│  🔍 [Symbol search ____________] [Analyze]                           │
│  AAPL  $185.20 ▲ +1.32 (+0.72%)  │  IV Rank: 45  │  HV30: 22%      │
├────────────────────────────────┬────────────────────────────────────-┤
│  📈 Price Chart                │  🎯 Order Builder                   │
│  (Plotly OHLC / line chart)    │  (pre-filled from chain selection)  │
├────────────────────────────────┴─────────────────────────────────────┤
│  📊 Options Chain (Tastytrade-style dual-wing)                       │
│  Expiration pills  [Jan 17] [Feb 21] [Mar 21] [Apr 18] …            │
│  PUTS     |  STRIKE  |  CALLS                                        │
│  …        |  550.00  |  …                                           │
└──────────────────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ── Path bootstrap ────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.chdir(_ROOT)

# ── Internal imports ──────────────────────────────────────────────────────────
from dashboard.app import get_services  # noqa: E402  — reuse cached services
from dashboard.components.options_chain_viewer import render_options_chain_viewer  # noqa: E402
from dashboard.components.order_builder import render_order_builder  # noqa: E402

LOGGER = logging.getLogger(__name__)

# ── Aesthetic helpers ─────────────────────────────────────────────────────────
_CSS = """
<style>
/* Quote bar */
.quote-bar   { display:flex; gap:2rem; align-items:baseline; flex-wrap:wrap; margin-bottom:.5rem; }
.quote-price { font-size:2rem; font-weight:700; }
.quote-up    { color:#22c55e; font-size:1.2rem; }
.quote-down  { color:#ef4444; font-size:1.2rem; }
.quote-neutral{ color:#94a3b8; font-size:1.2rem; }
.quote-badge { background:#1e293b; padding:.15rem .6rem; border-radius:.4rem;
               font-size:.8rem; color:#cbd5e1; }
/* Section headings */
.section-head{ font-size:1rem; font-weight:600; color:#94a3b8;
               text-transform:uppercase; letter-spacing:.05em; margin:.5rem 0 .25rem; }
</style>
"""


# ─────────────────────────────────────────────────────────────────────────────
# Page
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)

    # ── Services (shared cache with Portfolio page) ───────────────────────
    adapter, portfolio_tools, market_tools, regime_detector = get_services()
    market_data_svc = getattr(adapter, "_market_data_service", None) or getattr(adapter, "market_data_service", None)
    # Also try to get it from the market_tools
    if market_data_svc is None:
        try:
            from core.market_data import MarketDataService
            market_data_svc = MarketDataService()
        except Exception:
            pass

    # ── Account id (needed for order builder) ────────────────────────────
    try:
        accounts = adapter.get_accounts() or []
    except Exception:
        accounts = []
    account_options = [a for a in (os.getenv("IB_ACCOUNTS", "").split(",")) if a.strip()]
    if not account_options and accounts:
        account_options = accounts
    account_id: str = (
        st.sidebar.selectbox("IBKR Account", options=account_options, index=0)
        if account_options else ""
    )

    # ── 1. Symbol Search ─────────────────────────────────────────────────
    st.markdown("<div class='section-head'>Symbol Lookup</div>", unsafe_allow_html=True)
    search_col, btn_col = st.columns([5, 1])
    with search_col:
        raw_symbol = st.text_input(
            "Symbol",
            value=st.session_state.get("trade_symbol", "SPY"),
            placeholder="e.g. AAPL, SPY, ES, /MES…",
            label_visibility="collapsed",
            key="trade_symbol_input",
        )
    with btn_col:
        analyze = st.button("🔍 Analyze", use_container_width=True, type="primary")

    if analyze or st.session_state.get("trade_symbol") != raw_symbol.strip().upper():
        st.session_state["trade_symbol"] = raw_symbol.strip().upper()
        # Clear cached chain data when symbol changes
        for key in list(st.session_state.keys()):
            if key.startswith("ocv_"):
                del st.session_state[key]

    symbol: str = st.session_state.get("trade_symbol", "SPY")
    if not symbol:
        st.info("Enter a symbol above and click **Analyze** to begin.")
        return

    # ── 2. Quote header ──────────────────────────────────────────────────
    _render_quote_header(symbol=symbol, market_tools=market_tools)

    st.markdown("---")

    # ── 3. Chart | Order Builder columns ─────────────────────────────────
    chart_col, order_col = st.columns([3, 2], gap="medium")

    with chart_col:
        _render_price_chart(symbol=symbol, market_tools=market_tools)

    with order_col:
        st.markdown("<div class='section-head'>🎯 Order Builder</div>", unsafe_allow_html=True)

        # Build execution engine if available
        _exec_engine = _get_execution_engine(adapter=adapter, account_id=account_id)

        # Fetch portfolio greeks for impact analysis (non-blocking)
        try:
            from models.order import PortfolioGreeks
            pg = _get_portfolio_greeks(portfolio_tools=portfolio_tools, account_id=account_id)
        except Exception:
            pg = None

        render_order_builder(
            execution_engine=_exec_engine,
            account_id=account_id,
            current_portfolio_greeks=pg,
            market_data_service=market_data_svc,
        )

    st.markdown("---")

    # ── 4. Options Chain ─────────────────────────────────────────────────
    st.markdown(f"<div class='section-head'>📊 Options Chain — {symbol}</div>", unsafe_allow_html=True)

    selected_option = render_options_chain_viewer(
        symbol=symbol,
        market_data_service=market_data_svc,
        adapter=adapter,
        session_key_prefix="ocv",
    )

    # ── 5. Wire chain selection → Order Builder pre-fill ─────────────────
    if selected_option:
        _stage_order_from_chain(selected_option)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

@st.fragment(run_every="30s")
def _render_quote_header(symbol: str, market_tools: Any) -> None:
    """Auto-refreshing quote bar (updates every 30 s without full page reload)."""
    quote: dict = {}
    try:
        raw = market_tools.get_market_data(symbol) if market_tools else {}
        if isinstance(raw, dict):
            quote = raw
    except Exception as exc:
        LOGGER.debug("Quote fetch failed for %s: %s", symbol, exc)

    last   = _safe_float(quote.get("last") or quote.get("price") or quote.get("close"))
    change = _safe_float(quote.get("change") or quote.get("net_change"))
    pct    = _safe_float(quote.get("change_pct") or quote.get("pct_change"))
    iv_rank= _safe_float(quote.get("iv_rank") or quote.get("ivr"))
    hv30   = _safe_float(quote.get("historical_volatility") or quote.get("hv30"))

    # Format price & change
    price_str  = f"${last:.2f}" if last is not None else "—"
    if change is not None:
        direction = "up" if change >= 0 else "down"
        sign = "▲" if change >= 0 else "▼"
        change_str = f"{sign} {abs(change):.2f}"
        if pct is not None:
            change_str += f" ({abs(pct):.2f}%)"
    else:
        direction  = "neutral"
        change_str = ""

    iv_badge  = f"<span class='quote-badge'>IV Rank: {iv_rank:.0f}</span>" if iv_rank is not None else ""
    hv_badge  = f"<span class='quote-badge'>HV30: {hv30*100:.1f}%</span>" if hv30 is not None else ""

    st.markdown(
        f"<div class='quote-bar'>"
        f"  <span style='font-size:1.4rem;font-weight:700'>{symbol}</span>"
        f"  <span class='quote-price'>{price_str}</span>"
        f"  <span class='quote-{direction}'>{change_str}</span>"
        f"  {iv_badge}{hv_badge}"
        f"</div>",
        unsafe_allow_html=True,
    )


@st.fragment
def _render_price_chart(symbol: str, market_tools: Any) -> None:
    """Plotly OHLC / line chart for the symbol."""
    st.markdown("<div class='section-head'>📈 Price Chart</div>", unsafe_allow_html=True)

    chart_range = st.pills(
        "Range",
        ["1D", "5D", "1M", "3M", "6M", "1Y", "2Y"],
        default="3M",
        key="trade_chart_range",
        label_visibility="collapsed",
    )

    hist: Optional[pd.DataFrame] = None
    try:
        fn = getattr(market_tools, "get_historical_prices", None) or getattr(market_tools, "get_price_history", None)
        if callable(fn):
            days_map = {"1D": 1, "5D": 5, "1M": 30, "3M": 90, "6M": 180, "1Y": 365, "2Y": 730}
            days = days_map.get(chart_range or "3M", 90)
            hist = fn(symbol, days=days)
    except Exception as exc:
        LOGGER.debug("Price history fetch: %s", exc)

    if hist is not None and not hist.empty:
        # Detect OHLC vs close-only
        has_ohlc = all(c in hist.columns for c in ("open", "high", "low", "close"))
        fig = go.Figure()
        if has_ohlc:
            fig.add_trace(go.Candlestick(
                x=hist.index if hist.index.dtype != object else hist.get("date") or hist.index,
                open=hist["open"], high=hist["high"],
                low=hist["low"],  close=hist["close"],
                name=symbol,
                increasing_line_color="#22c55e",
                decreasing_line_color="#ef4444",
            ))
        else:
            close_col = "close" if "close" in hist.columns else hist.columns[0]
            fig.add_trace(go.Scatter(
                x=hist.index, y=hist[close_col],
                mode="lines",
                line=dict(color="#6366f1", width=2),
                name=symbol,
            ))
        fig.update_layout(
            template="plotly_dark",
            height=320,
            margin=dict(l=0, r=0, t=24, b=0),
            xaxis_rangeslider_visible=False,
            showlegend=False,
            xaxis=dict(title=""),
            yaxis=dict(title="Price", tickprefix="$"),
        )
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    else:
        st.info(
            f"No price history available for **{symbol}**. "
            "Connect a market data provider (Polygon, Alpaca, Tastytrade, etc.) "
            "and implement `MarketDataTools.get_historical_prices()`."
        )


def _stage_order_from_chain(opt: dict) -> None:
    """Pre-fill the Order Builder session state from a chain selection."""
    from datetime import date as _d
    symbol  = opt.get("symbol", "")
    expiry  = opt.get("expiry", "")
    strike  = opt.get("strike")
    right   = opt.get("right", "CALL")
    bid     = opt.get("bid")
    ask     = opt.get("ask")
    mid     = opt.get("mid")

    # Derive a reasonable limit price (mid, or best of bid/ask)
    if mid is not None:
        limit_price = round(mid, 2)
    elif bid is not None and ask is not None:
        limit_price = round((bid + ask) / 2, 2)
    else:
        limit_price = None

    expiry_date = None
    try:
        expiry_date = _d.fromisoformat(expiry)
    except Exception:
        pass

    # Use the same session-state keys as the order builder (ob_ prefix)
    st.session_state["ob_leg_count"] = 1
    st.session_state["ob_instr_0"]   = "Option"
    st.session_state["ob_symbol_0"]  = symbol
    st.session_state["ob_action_0"]  = "BUY"
    st.session_state["ob_qty_0"]     = 1
    st.session_state["ob_right_0"]   = right
    if expiry_date:
        st.session_state["ob_expiry_0"] = expiry_date
    if strike is not None:
        st.session_state["ob_strike_0"] = float(strike)
    if limit_price is not None:
        st.session_state["ob_price_0"] = limit_price

    st.success(
        f"✅ Pre-filled Order Builder: **{right} {symbol} {strike} exp {expiry}** "
        f"@ ~${limit_price} mid — scroll up to review and submit."
    )


@st.cache_data(ttl=30, show_spinner=False)
def _get_cached_greeks(account_id: str) -> Optional[dict]:
    """Cache-wrapped placeholder for portfolio greeks."""
    return None


def _get_portfolio_greeks(portfolio_tools: Any, account_id: str):
    """Non-blocking best-effort portfolio Greeks for impact display."""
    try:
        from models.order import PortfolioGreeks
        fn = getattr(portfolio_tools, "get_portfolio_greeks", None)
        if callable(fn):
            return fn(account_id)
    except Exception:
        pass
    return None


def _get_execution_engine(adapter: Any, account_id: str) -> Optional[Any]:
    """Instantiate ExecutionEngine if the broker is connected."""
    try:
        from core.execution import ExecutionEngine
        return ExecutionEngine(
            client=adapter.client,
            account_id=account_id,
        )
    except Exception as exc:
        LOGGER.debug("ExecutionEngine unavailable: %s", exc)
        return None


def _safe_float(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return None if str(f) in ("nan", "inf", "-inf") else f
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
main()
