"""dashboard/main.py — Trading Platform navigation entry point.

Run with:
    streamlit run dashboard/main.py --server.port 8506

Architecture
------------
This is the ONLY file that calls ``st.set_page_config``.
It uses ``st.navigation`` (Streamlit ≥ 1.36) to define 5 pages:

  📊  Portfolio   — Risk dashboard, Greeks, position table (existing app.py)
  📈  Trade       — Symbol search, options chain, order builder        ← NEW
  📋  Orders      — Live orders with cancel / modify                   ← NEW
  📓  Journal     — Trade journal with CSV export
  🤖  AI / Risk   — AI assistant + risk audit + arbitrage signals

The pages run inside the same Streamlit process and share
``st.cache_resource`` objects (IBKRAdapter, MarketDataService, etc.).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st

# ── Path bootstrap ────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.chdir(_ROOT)

# ── Page config (called ONCE here, never inside page files) ──────────────────
st.set_page_config(
    page_title="Trading Platform",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom top-bar CSS ────────────────────────────────────────────────────────
st.markdown(
    """
    <style>
    /* Tighter sidebar nav items */
    [data-testid="stSidebarNav"] li { padding: .15rem 0; }
    /* Accent the active nav item */
    [data-testid="stSidebarNav"] li[aria-selected="true"] a {
        color: #6366f1 !important;
        font-weight: 700;
    }
    /* Page title area */
    .platform-header {
        display: flex;
        align-items: center;
        gap: .75rem;
        font-size: 1.1rem;
        font-weight: 600;
        color: #94a3b8;
        margin-bottom: .5rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Navigation ────────────────────────────────────────────────────────────────
_PAGES_DIR = Path(__file__).parent / "pages"

portfolio_page = st.Page(
    str(_PAGES_DIR / "portfolio.py"),
    title="Portfolio",
    icon="📊",
    default=True,
)
trade_page = st.Page(
    str(_PAGES_DIR / "trade.py"),
    title="Trade",
    icon="📈",
)
orders_page = st.Page(
    str(_PAGES_DIR / "orders.py"),
    title="Orders",
    icon="📋",
)
journal_page = st.Page(
    str(_PAGES_DIR / "journal.py"),
    title="Journal",
    icon="📓",
)

nav = st.navigation(
    {
        "Trading": [trade_page, orders_page],
        "Monitor":  [portfolio_page, journal_page],
    },
    expanded=True,
)

# ── Small sidebar footer: account NLV / BP summary ───────────────────────────
with st.sidebar:
    st.markdown("---")
    _show_acct = st.sidebar.checkbox("Show account summary", value=False)
    if _show_acct:
        try:
            from dashboard.app import get_services
            import os as _os
            _adapter, *_ = get_services()
            _accounts = [a for a in _os.getenv("IB_ACCOUNTS", "").split(",") if a.strip()]
            if _accounts:
                _summ = _adapter.get_account_summary(_accounts[0]) or {}
                _nlv  = _summ.get("netliquidation")
                _bp   = _summ.get("excessliquidity") or _summ.get("availablefunds")
                if _nlv:
                    st.metric("Net Liq", f"${float(_nlv):,.0f}")
                if _bp:
                    st.metric("Buying Power", f"${float(_bp):,.0f}")
        except Exception:
            st.caption("Account summary unavailable.")

# ── Run selected page ─────────────────────────────────────────────────────────
nav.run()
