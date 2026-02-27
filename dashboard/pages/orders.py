"""dashboard/pages/orders.py — Live Orders page.

Wraps the existing order_management component and adds order history
with auto-refresh via st.fragment.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.chdir(_ROOT)

from dashboard.app import get_services  # noqa: E402
from dashboard.components.order_management import render_order_management  # noqa: E402

# ── Services ──────────────────────────────────────────────────────────────────
adapter, _, _, _ = get_services()

# ── Account selector ──────────────────────────────────────────────────────────
account_options = [a for a in (os.getenv("IB_ACCOUNTS", "").split(",")) if a.strip()]
if not account_options:
    try:
        account_options = adapter.get_accounts() or []
    except Exception:
        account_options = []

account_id: str = (
    st.sidebar.selectbox("IBKR Account", options=account_options, index=0)
    if account_options else ""
)

st.title("📋 Orders")

auto_refresh = st.sidebar.checkbox("Auto-refresh (10 s)", value=True)

@st.fragment(run_every="10s" if auto_refresh else None)
def _orders_fragment() -> None:
    render_order_management(
        ibkr_gateway_client=adapter.client,
        account_id=account_id,
    )

_orders_fragment()
