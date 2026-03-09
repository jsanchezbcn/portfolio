"""dashboard/components/trade_journal_view.py — Postgres-backed journal notes UI."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Async helper (Streamlit runs synchronously; store access is async)
# ---------------------------------------------------------------------------

def _run_async(coro):
    """Run an async coroutine safely from any thread (Streamlit-safe).

    Always delegates to a fresh thread so ``asyncio.run()`` never hits an
    already-running loop (Tornado/Streamlit keep a loop in the main thread).
    """
    import concurrent.futures
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=10)
    except Exception as exc:
        logger.warning("Async operation failed in trade_journal_view: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _filter_rows(rows: list[dict[str, Any]], *, start_dt: str | None, end_dt: str | None) -> list[dict[str, Any]]:
    filtered = rows
    if start_dt:
        filtered = [r for r in filtered if str(r.get("created_at") or "") >= start_dt]
    if end_dt:
        filtered = [r for r in filtered if str(r.get("created_at") or "") <= end_dt]
    return filtered


def _build_display_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "Created": str(r.get("created_at") or "")[:19].replace("T", " "),
            "Title": str(r.get("title") or ""),
            "Tags": ", ".join(str(tag) for tag in (r.get("tags") or [])),
            "Body": str(r.get("body") or "")[:160],
        }
        for r in rows
    ]

def render_trade_journal(store) -> None:
    """Render the shared Postgres-backed journal notes panel."""
    import pandas as pd
    import streamlit as st

    st.subheader("📓 Trade Journal")

    if store is None:
        st.warning("Trade journal unavailable — Postgres store not connected.")
        return

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
            selected_range = st.selectbox("Date range", list(date_options.keys()), index=1, key="tj_date_range")
            start_date, end_date = date_options[selected_range]

        with col2:
            search = st.text_input("Search", value="", placeholder="Title or body", key="tj_instrument")

        with col3:
            tag = st.text_input("Tag", value="", placeholder="hedge, review", key="tj_regime")

    start_dt = (
        datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc).isoformat()
        if start_date else None
    )
    end_dt = (
        datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=timezone.utc).isoformat()
        if end_date else None
    )

    with st.spinner("Loading journal…"):
        rows = _run_async(store.list_journal_notes(search=search.strip() or None, tag=tag.strip() or None, limit=500)) or []

    rows = _filter_rows(rows, start_dt=start_dt, end_dt=end_dt)
    if not rows:
        st.info("No journal notes match the current filters.")
        return

    tagged_count = sum(1 for r in rows if r.get("tags"))
    avg_body_len = sum(len(str(r.get("body") or "")) for r in rows) / max(len(rows), 1)
    m1, m2, m3 = st.columns(3)
    m1.metric("Total Notes", len(rows))
    m2.metric("Tagged Notes", tagged_count)
    m3.metric("Avg Note Length", f"{avg_body_len:.0f} chars")

    display_rows = _build_display_rows(rows)
    st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True)

    with st.expander("🔍 Row detail (select row index)", expanded=False):
        row_idx = st.number_input("Row #", min_value=0, max_value=max(len(rows) - 1, 0), value=0, step=1, key="tj_row_detail_idx")
        raw = rows[row_idx]
        st.markdown(f"**Title:** {raw.get('title') or '—'}")
        st.markdown(f"**Tags:** {', '.join(raw.get('tags') or []) or '—'}")
        st.markdown("**Body:**")
        st.write(raw.get("body") or "")

    csv_data = pd.DataFrame(display_rows).to_csv(index=False)
    st.download_button(
        label="⬇ Export CSV",
        data=csv_data,
        file_name=f"journal_notes_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
        help="Download all filtered journal notes as a CSV file",
    )
