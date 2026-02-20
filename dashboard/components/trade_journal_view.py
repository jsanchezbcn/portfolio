"""dashboard/components/trade_journal_view.py — T043-T045: Trade Journal UI.

Renders the trade journal tab with:
- Reverse-chronological fill table
- Date range / instrument / regime filters
- Export CSV download button
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Async helper (Streamlit runs synchronously; LocalStore is async)
# ---------------------------------------------------------------------------

def _run_async(coro):
    """Run an async coroutine from a synchronous Streamlit context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result(timeout=10)
        return loop.run_until_complete(coro)
    except Exception as exc:
        logger.warning("Async operation failed in trade_journal_view: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def render_trade_journal(local_store) -> None:
    """Render the Trade Journal panel.

    Parameters
    ----------
    local_store:
        A connected ``LocalStore`` instance.
    """
    import streamlit as st

    st.subheader("📓 Trade Journal")

    if local_store is None:
        st.warning("Trade journal unavailable — LocalStore not connected.")
        return

    # ── Filters ──────────────────────────────────────────────────────────────
    with st.expander("🔍 Filters", expanded=False):
        col1, col2, col3 = st.columns(3)

        with col1:
            today = date.today()
            date_options = {
                "Today": (today, today),
                "Last 7 days": (today - timedelta(days=7), today),
                "Last 30 days": (today - timedelta(days=30), today),
                "Last 90 days": (today - timedelta(days=90), today),
                "All time": (None, None),
            }
            selected_range = st.selectbox(
                "Date range",
                list(date_options.keys()),
                index=1,  # Default: Last 7 days
                key="tj_date_range",
            )
            start_date, end_date = date_options[selected_range]

        with col2:
            instrument_filter = st.text_input(
                "Symbol / Underlying",
                value="",
                placeholder="e.g. SPX, AAPL, /ES",
                key="tj_instrument",
            )

        with col3:
            regime_filter = st.selectbox(
                "Regime",
                ["All", "low_volatility", "neutral_volatility", "high_volatility", "crisis_mode"],
                index=0,
                key="tj_regime",
            )

    # ── Build query params ────────────────────────────────────────────────────
    start_dt = (
        datetime(start_date.year, start_date.month, start_date.day,
                 tzinfo=timezone.utc).isoformat()
        if start_date else None
    )
    end_dt = (
        datetime(end_date.year, end_date.month, end_date.day,
                 23, 59, 59, tzinfo=timezone.utc).isoformat()
        if end_date else None
    )
    instrument = instrument_filter.strip() or None
    regime = regime_filter if regime_filter != "All" else None

    # ── Fetch rows ────────────────────────────────────────────────────────────
    with st.spinner("Loading journal…"):
        rows = _run_async(
            local_store.query_journal(
                start_dt=start_dt,
                end_dt=end_dt,
                instrument=instrument,
                regime=regime,
                limit=500,
            )
        ) or []

    if not rows:
        st.info("No fills match the current filters.")
        return

    # ── Render summary metrics ─────────────────────────────────────────────────
    total_fills = len(rows)
    total_pnl = sum(
        r.get("net_debit_credit") or 0.0 for r in rows
        if r.get("net_debit_credit") is not None
    )
    m1, m2, m3 = st.columns(3)
    m1.metric("Total Fills", total_fills)
    m2.metric("Net Credit/Debit", f"${total_pnl:+,.2f}")
    m3.metric(
        "Avg VIX at Fill",
        f"{sum(r.get('vix_at_fill') or 0 for r in rows if r.get('vix_at_fill')) / max(sum(1 for r in rows if r.get('vix_at_fill')), 1):.1f}",
    )

    # ── Table columns (T043) ──────────────────────────────────────────────────
    import pandas as pd

    display_rows = []
    for r in rows:
        # Parse legs_json for a human-readable summary
        try:
            legs = json.loads(r.get("legs_json") or "[]")
            legs_summary = " / ".join(
                f"{lg.get('action','?')} {lg.get('quantity','?')}× {lg.get('symbol','?')}"
                for lg in (legs if isinstance(legs, list) else [])
            ) or "—"
        except Exception:
            legs_summary = "—"

        display_rows.append({
            "Timestamp": r.get("created_at", "")[:19].replace("T", " "),
            "Underlying": r.get("underlying", ""),
            "Strategy": r.get("strategy_tag") or "—",
            "Status": r.get("status", ""),
            "Net Cr/Dr": (
                f"${r['net_debit_credit']:+,.2f}"
                if r.get("net_debit_credit") is not None else "—"
            ),
            "VIX": (
                f"{r['vix_at_fill']:.1f}"
                if r.get("vix_at_fill") is not None else "—"
            ),
            "Regime": r.get("regime") or "—",
            "Rationale": (r.get("user_rationale") or "")[:60],
            "Legs": legs_summary,
        })

    df = pd.DataFrame(display_rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    # ── Row detail expander ───────────────────────────────────────────────────
    with st.expander("🔍 Row detail (select row index)", expanded=False):
        row_idx = st.number_input(
            "Row #", min_value=0, max_value=max(len(rows) - 1, 0), value=0, step=1,
            key="tj_row_detail_idx",
        )
        raw = rows[row_idx]
        col_l, col_r = st.columns(2)
        with col_l:
            st.markdown("**Pre-trade Greeks:**")
            try:
                st.json(json.loads(raw.get("pre_greeks_json") or "{}"))
            except Exception:
                st.text(raw.get("pre_greeks_json"))
        with col_r:
            st.markdown("**Post-trade Greeks:**")
            try:
                st.json(json.loads(raw.get("post_greeks_json") or "{}"))
            except Exception:
                st.text(raw.get("post_greeks_json"))
        st.markdown(f"**Broker Order ID:** `{raw.get('broker_order_id') or '—'}`")
        if raw.get("ai_suggestion_id"):
            st.markdown(f"**AI Suggestion ID:** `{raw['ai_suggestion_id']}`")
        if raw.get("ai_rationale"):
            st.markdown(f"**AI Rationale:** {raw['ai_rationale']}")

    # ── Export CSV (T045) ─────────────────────────────────────────────────────
    csv_data = local_store.export_csv(rows)
    if csv_data:
        st.download_button(
            label="⬇ Export CSV",
            data=csv_data,
            file_name=f"trade_journal_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            help="Download all filtered fills as a CSV file",
        )
