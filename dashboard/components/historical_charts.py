"""dashboard/components/historical_charts.py — Portfolio time-series charts (T062-T065b).

Renders:
- Net Liquidation vs SPX Beta-Delta dual-axis chart (T062)
- Delta/Theta ratio efficiency line chart (T063)
- Sebastian |Theta|/|Vega| ratio gauge/line (T065b) [Constitution §III MANDATORY]
- Time-range filter: 1D, 1W, 1M, All (T064)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import streamlit as st

LOGGER = logging.getLogger(__name__)

# Color constants
_GREEN_BAND = (0.25, 0.40)     # Sebastian ratio target
_RED_BAND_LO = 0.20
_RED_BAND_HI = 0.50


# ---------------------------------------------------------------------------
# Public entry point (T065)
# ---------------------------------------------------------------------------

def render_historical_charts(local_store: Any) -> None:
    """Render all portfolio time-series charts in the dashboard."""
    st.subheader("📈 Historical Portfolio Charts")

    # ── Time-range filter (T064) ──────────────────────────────────────────
    col_range = st.columns([1, 3])
    with col_range[0]:
        time_range = st.selectbox(
            "Time Range",
            options=["1D", "1W", "1M", "All"],
            index=1,
            key="hist_time_range",
        )

    snapshots = _load_snapshots(local_store, time_range)

    if not snapshots:
        st.info("ℹ️ No snapshot data available yet. Snapshots are captured every 15 minutes while the dashboard is running.")
        return

    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    # ── NLV vs SPX Delta dual-axis chart (T062) ───────────────────────────
    st.markdown("#### Net Liquidation vs SPX Beta-Delta")
    _render_nlv_delta_chart(snapshots, go, make_subplots)

    # ── Theta/Delta efficiency ratio (T063) ───────────────────────────────
    st.markdown("#### Income-to-Risk Efficiency (Θ/Δ)")
    _render_delta_theta_chart(snapshots, go)

    # ── Sebastian |Theta|/|Vega| ratio [MANDATORY per Constitution §III] (T065b)
    st.markdown("#### Sebastian Ratio |Θ|/|V|")
    _render_sebastian_ratio_chart(snapshots, go)


# ---------------------------------------------------------------------------
# Chart renderers
# ---------------------------------------------------------------------------

def render_account_vs_delta_chart(snapshots: list[dict]) -> None:
    """T062 — Dual-axis chart: NLV (primary) vs SPX Beta-Delta (secondary)."""
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
        _render_nlv_delta_chart(snapshots, go, make_subplots)
    except Exception as exc:
        st.warning(f"NLV/Delta chart error: {exc}")


def render_delta_theta_ratio_chart(snapshots: list[dict]) -> None:
    """T063 — Theta/Delta ratio line chart."""
    try:
        import plotly.graph_objects as go
        _render_delta_theta_chart(snapshots, go)
    except Exception as exc:
        st.warning(f"Theta/Delta chart error: {exc}")


# ---------------------------------------------------------------------------
# Private chart helpers
# ---------------------------------------------------------------------------

def _render_nlv_delta_chart(snapshots, go, make_subplots) -> None:
    """Primary y: Net Liquidation Value; secondary y: SPX Equivalent Delta."""
    times = [s.get("captured_at", "") for s in snapshots]
    nlv_values = [s.get("net_liquidation") for s in snapshots]
    delta_values = [s.get("spx_delta") for s in snapshots]

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    # NLV line (primary)
    fig.add_trace(
        go.Scatter(
            x=times,
            y=nlv_values,
            name="Net Liquidation ($)",
            line=dict(color="#2ecc71", width=2),
            connectgaps=False,
        ),
        secondary_y=False,
    )

    # SPX Delta line (secondary)
    fig.add_trace(
        go.Scatter(
            x=times,
            y=delta_values,
            name="SPX Beta-Delta",
            line=dict(color="#e74c3c", width=2, dash="dot"),
            connectgaps=False,
        ),
        secondary_y=True,
    )

    # Axes labels
    fig.update_yaxes(title_text="Net Liquidation Value ($)", secondary_y=False)
    fig.update_yaxes(title_text="SPX Beta-Delta", secondary_y=True)
    fig.update_xaxes(title_text="Time")
    fig.update_layout(
        height=350,
        margin=dict(l=20, r=20, t=20, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        hovermode="x unified",
    )

    st.plotly_chart(fig, use_container_width=True)


def _render_delta_theta_chart(snapshots, go) -> None:
    """T063 — Y: Income-to-Risk Efficiency Ratio (Θ/Δ); gaps for None values."""
    times = [s.get("captured_at", "") for s in snapshots]
    ratios = [s.get("delta_theta_ratio") for s in snapshots]

    # Split into segments separated by None to create gaps
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=times,
            y=ratios,
            name="Θ/Δ Ratio",
            line=dict(color="#9b59b6", width=2),
            connectgaps=False,  # None values create gaps
            mode="lines",
        )
    )
    # Add zero reference line
    fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)

    fig.update_layout(
        yaxis_title="Income-to-Risk Efficiency Ratio (Θ/Δ)",
        xaxis_title="Time",
        height=300,
        margin=dict(l=20, r=20, t=20, b=20),
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)


def _render_sebastian_ratio_chart(snapshots, go) -> None:
    """T065b — |Theta|/|Vega| Sebastian ratio; green 0.25–0.40; red <0.20 or >0.50."""
    times = [s.get("captured_at", "") for s in snapshots]
    ratios: list[float | None] = []
    for s in snapshots:
        theta = s.get("theta")
        vega = s.get("vega")
        if theta is not None and vega is not None and vega != 0.0 and theta != 0.0:
            ratios.append(abs(theta) / abs(vega))
        else:
            ratios.append(None)

    # Color each point by zone
    colors = []
    for r in ratios:
        if r is None:
            colors.append("gray")
        elif r < _RED_BAND_LO or r > _RED_BAND_HI:
            colors.append("#e74c3c")
        elif _GREEN_BAND[0] <= r <= _GREEN_BAND[1]:
            colors.append("#2ecc71")
        else:
            colors.append("#f39c12")

    fig = go.Figure()

    # Shaded target band
    fig.add_hrect(
        y0=_GREEN_BAND[0], y1=_GREEN_BAND[1],
        fillcolor="rgba(46,204,113,0.1)",
        layer="below",
        line_width=0,
        annotation_text="Target (0.25–0.40)",
        annotation_position="top left",
    )
    # Red lower bound
    fig.add_hline(
        y=_RED_BAND_LO, line_dash="dot", line_color="#e74c3c", opacity=0.7,
        annotation_text="Floor 0.20", annotation_position="right",
    )
    # Red upper bound
    fig.add_hline(
        y=_RED_BAND_HI, line_dash="dot", line_color="#e74c3c", opacity=0.7,
        annotation_text="Cap 0.50", annotation_position="right",
    )

    fig.add_trace(
        go.Scatter(
            x=times,
            y=ratios,
            mode="lines+markers",
            name="Sebastian Ratio (|Θ|/|V|)",
            line=dict(color="#3498db", width=2),
            marker=dict(color=colors, size=6),
            connectgaps=False,
        )
    )

    fig.update_layout(
        yaxis_title="Sebastian Ratio (|Θ|/|V|)",
        xaxis_title="Time",
        height=300,
        margin=dict(l=20, r=20, t=20, b=20),
        hovermode="x unified",
        yaxis=dict(range=[0, max(0.75, max((r or 0) for r in ratios) * 1.2)]),
    )
    st.plotly_chart(fig, use_container_width=True)

    # Current value callout
    last_ratio = next((r for r in reversed(ratios) if r is not None), None)
    if last_ratio is not None:
        zone = (
            "🟢 Target"
            if _GREEN_BAND[0] <= last_ratio <= _GREEN_BAND[1]
            else ("🔴 Outside bounds" if last_ratio < _RED_BAND_LO or last_ratio > _RED_BAND_HI
                  else "🟡 Sub-optimal")
        )
        st.caption(f"Current Sebastian Ratio: **{last_ratio:.3f}** — {zone}")


# ---------------------------------------------------------------------------
# Data loader
# ---------------------------------------------------------------------------

def _load_snapshots(local_store: Any, time_range: str) -> list[dict]:
    """Fetch snapshots from LocalStore filtered by time range."""
    import asyncio

    try:
        now = datetime.now(timezone.utc)
        delta_map = {"1D": timedelta(days=1), "1W": timedelta(weeks=1), "1M": timedelta(days=30)}
        start_dt = None
        if time_range in delta_map:
            start_dt = (now - delta_map[time_range]).isoformat()

        loop = asyncio.new_event_loop()
        try:
            rows = loop.run_until_complete(
                local_store.query_snapshots(start_dt=start_dt)
            )
        finally:
            loop.close()
        return rows
    except Exception as exc:
        LOGGER.warning("_load_snapshots error: %s", exc)
        return []
