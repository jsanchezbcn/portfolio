"""dashboard/pages/journal.py — Trade Journal page.

Wraps the existing trade_journal_view component.
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

st.title("📓 Trade Journal")

try:
    from dashboard.components.trade_journal_view import render_trade_journal
    from database.business_store import PostgresBusinessStore

    store = PostgresBusinessStore()
    render_trade_journal(store)
except Exception as exc:
    st.error(f"Trade journal unavailable: {exc}")
