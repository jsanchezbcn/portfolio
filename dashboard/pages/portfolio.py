"""dashboard/pages/portfolio.py — Portfolio & Risk Dashboard page.

Thin wrapper around the existing dashboard/app.py content renderer.
Called by ``dashboard/main.py`` via ``st.navigation``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# Ensure project root is on path (st.Page runs files in isolation)
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dashboard.app import render_portfolio_content  # noqa: E402

render_portfolio_content()
