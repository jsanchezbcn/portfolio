"""dashboard/components/order_builder.py — Pre-trade simulation panel.

Provides a collapsible Streamlit component for building and simulating multi-leg
orders against the IBKR WhatIf API before any live submission.

SAFETY CONTRACT
===============
 - "Simulate Trade" calls ExecutionEngine.simulate() only (READ-ONLY WhatIf).
 - "Submit Order" is DISABLED until T030 is implemented.
 - No order is EVER transmitted from this component without an explicit
   user-initiated, 2-step confirmation (future T031).
 - MOC order type is blocked when option legs are present.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

import streamlit as st

from models.order import (
    Order,
    OrderAction,
    OrderLeg,
    OrderStatus,
    OrderType,
    OptionRight,
    PortfolioGreeks,
    SimulationResult,
)

logger = logging.getLogger(__name__)

# Session-state keys used by this component
_SS_ORDER = "ob_order"
_SS_RESULT = "ob_sim_result"
_SS_SIMULATING = "ob_simulating"
_SS_LEG_COUNT = "ob_leg_count"

# Maximum legs per order
_MAX_LEGS = 4


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------


def render_order_builder(
    execution_engine,           # ExecutionEngine | None
    account_id: str,
    current_portfolio_greeks: Optional[PortfolioGreeks] = None,
    regime: str = "neutral_volatility",
) -> None:
    """Render the full order-builder panel inside a Streamlit expander.

    Parameters
    ----------
    execution_engine:
        A live ``ExecutionEngine`` instance.  If ``None``, the panel renders in
        "broker unavailable" mode — Simulate button is disabled.
    account_id:
        IBKR account ID for WhatIf submission (e.g. ``"U12345"``).
    current_portfolio_greeks:
        Live portfolio Greeks used to estimate post-trade state.
        Defaults to zero when not supplied.
    regime:
        Risk regime key used for the delta-breach check.
    """
    with st.expander("📋 Order Builder — Pre-Trade Simulation", expanded=False):
        _render_inner(execution_engine, account_id, current_portfolio_greeks or PortfolioGreeks(), regime)


# ---------------------------------------------------------------------------
# Inner render
# ---------------------------------------------------------------------------


def _render_inner(
    execution_engine,
    account_id: str,
    current_portfolio_greeks: PortfolioGreeks,
    regime: str,
) -> None:
    """Render leg builder, simulate button, and results panel."""

    # --- broker-unavailable banner ----------------------------------------
    if execution_engine is None:
        st.error(
            "⚠ Broker unavailable — simulation disabled. "
            "Check IBKR Client Portal connection."
        )
        st.stop()

    # --- leg count picker ----------------------------------------------------
    if _SS_LEG_COUNT not in st.session_state:
        st.session_state[_SS_LEG_COUNT] = 1

    col_legs, col_type = st.columns([1, 2])
    with col_legs:
        n_legs = st.number_input(
            "Number of legs",
            min_value=1,
            max_value=_MAX_LEGS,
            value=st.session_state[_SS_LEG_COUNT],
            step=1,
            key="ob_n_legs_input",
        )
        st.session_state[_SS_LEG_COUNT] = n_legs

    with col_type:
        order_type_choice = st.selectbox(
            "Order type",
            options=["LIMIT", "MARKET", "MOC"],
            index=0,
            key="ob_order_type",
        )

    # --- leg builder ---------------------------------------------------------
    st.markdown("#### Leg Details")
    legs_config: list[dict] = []
    has_option_legs = False

    for i in range(n_legs):
        st.markdown(f"**Leg {i + 1}**")
        c1, c2, c3, c4, c5, c6, c7 = st.columns([2, 2, 1, 2, 2, 2, 2])

        with c1:
            action_str = st.selectbox(
                "Action",
                ["SELL", "BUY"],
                key=f"ob_action_{i}",
                label_visibility="collapsed",
            )
        with c2:
            symbol = st.text_input("Symbol", value="SPX", key=f"ob_symbol_{i}", label_visibility="collapsed")
        with c3:
            qty = st.number_input("Qty", min_value=1, value=1, step=1, key=f"ob_qty_{i}", label_visibility="collapsed")
        with c4:
            instrument_type = st.selectbox(
                "Type",
                ["Option", "Stock/ETF", "Future"],
                key=f"ob_itype_{i}",
                label_visibility="collapsed",
            )
        with c5:
            strike = None
            right_val = None
            if instrument_type == "Option":
                strike = st.number_input("Strike", value=5000.0, step=50.0, key=f"ob_strike_{i}", label_visibility="collapsed")
                has_option_legs = True
            else:
                st.text("")  # spacer
        with c6:
            if instrument_type == "Option":
                right_val = st.selectbox("Call/Put", ["CALL", "PUT"], key=f"ob_right_{i}", label_visibility="collapsed")
            else:
                st.text("")
        with c7:
            expiry: Optional[date] = None
            if instrument_type in ("Option", "Future"):
                expiry = st.date_input(
                    "Expiry",
                    value=date.today(),
                    key=f"ob_expiry_{i}",
                    label_visibility="collapsed",
                )
            else:
                st.text("")

        legs_config.append(
            {
                "action": action_str,
                "symbol": symbol,
                "qty": int(qty),
                "instrument_type": instrument_type,
                "strike": strike,
                "right": right_val,
                "expiry": expiry,
            }
        )

    # --- MOC constraint warning (T025) ----------------------------------------
    moc_selected = order_type_choice == "MOC"
    moc_blocked = moc_selected and has_option_legs
    if moc_blocked:
        st.warning("MOC not supported for options — switch to Limit or Market.")

    # --- Rationale textarea ---------------------------------------------------
    user_rationale = st.text_area(
        "Trade rationale (optional)",
        placeholder="e.g. Rolling short put up to collect more credit ...",
        key="ob_rationale",
        height=70,
    )

    st.divider()

    # --- Buttons row ---------------------------------------------------------
    simulating_in_flight = st.session_state.get(_SS_SIMULATING, False)
    sim_disabled = simulating_in_flight or moc_blocked
    prior_result: Optional[SimulationResult] = st.session_state.get(_SS_RESULT)
    # Submit disabled until T030 is implemented; also disabled if no successful simulation
    submit_disabled = True  # T030: will enable when submit() is implemented

    b_col1, b_col2, b_col3 = st.columns([2, 2, 4])
    with b_col1:
        simulate_clicked = st.button(
            "🔍 Simulate Trade" if not simulating_in_flight else "⏳ Simulating…",
            disabled=sim_disabled,
            key="ob_simulate_btn",
            use_container_width=True,
        )
    with b_col2:
        st.button(
            "🚫 Submit Order (T030)",
            disabled=submit_disabled,
            key="ob_submit_btn",
            use_container_width=True,
            help=(
                "Live order submission is not yet enabled.  "
                "Will be available in a future update (T030).  "
                "You will be asked to confirm before any order is transmitted."
            ),
        )
    with b_col3:
        st.caption(
            "⚠ Submit is disabled — live execution requires explicit 2-step confirmation (coming soon)"
        )

    # --- Run simulation -------------------------------------------------------
    if simulate_clicked and not sim_disabled:
        st.session_state[_SS_SIMULATING] = True
        st.session_state[_SS_RESULT] = None

        # Build Order object from UI state
        order = _build_order(legs_config, order_type_choice, user_rationale)

        if order is None:
            st.session_state[_SS_SIMULATING] = False
            st.rerun()
        else:
            with st.spinner("Sending WhatIf request to IBKR…"):
                try:
                    result = execution_engine.simulate(
                        account_id=account_id,
                        order=order,
                        current_portfolio_greeks=current_portfolio_greeks,
                        regime=regime,
                    )
                except Exception as exc:
                    logger.exception("Unexpected error during simulate()")
                    result = SimulationResult(error=f"Unexpected error: {exc}")

            st.session_state[_SS_RESULT] = result
            st.session_state[_SS_SIMULATING] = False
            st.rerun()

    # --- Results panel (T026) -------------------------------------------------
    prior_result = st.session_state.get(_SS_RESULT)
    if prior_result is not None:
        _render_results(prior_result)


# ---------------------------------------------------------------------------
# Results panel
# ---------------------------------------------------------------------------


def _render_results(result: SimulationResult) -> None:
    """Render the SimulationResult panel (T026)."""
    if result.error:
        st.error(f"❌ Simulation failed: {result.error}")
        st.caption("Fix the error above before submitting an order.")
        return

    st.success("✅ Simulation complete")

    # Margin summary
    m_col1, m_col2, m_col3 = st.columns(3)
    with m_col1:
        st.metric("Initial Margin Required", f"${result.margin_requirement:,.0f}")
    with m_col2:
        st.metric("Equity Before", f"${result.equity_before:,.0f}")
    with m_col3:
        delta = (result.equity_after or 0.0) - (result.equity_before or 0.0)
        st.metric(
            "Equity After",
            f"${result.equity_after:,.0f}",
            delta=f"${delta:+,.0f}",
            delta_color="normal",
        )

    # Post-trade Greeks table
    if result.post_trade_greeks:
        g = result.post_trade_greeks
        st.markdown("#### Post-Trade Portfolio Greeks")

        delta_label = "SPX Δ (beta-weighted)"
        delta_value = f"{g.spx_delta:+.2f}"

        if result.delta_breach:
            st.error(
                f"🚨 **Delta Breach** — Post-trade |Δ| `{abs(g.spx_delta):.1f}` "
                f"exceeds the regime limit.  Reduce position size or adjust legs."
            )

        cols = st.columns(4)
        with cols[0]:
            # Highlight delta in red when breach
            if result.delta_breach:
                st.markdown(
                    f"<div style='background:#ff4444;color:white;padding:8px;border-radius:4px;"
                    f"text-align:center'><b>{delta_label}</b><br/>"
                    f"<span style='font-size:1.4em'>{delta_value}</span></div>",
                    unsafe_allow_html=True,
                )
            else:
                st.metric(delta_label, delta_value)
        with cols[1]:
            st.metric("Theta (Θ)", f"{g.theta:+.2f}")
        with cols[2]:
            st.metric("Vega (V)", f"{g.vega:+.2f}")
        with cols[3]:
            st.metric("Gamma (Γ)", f"{g.gamma:+.4f}")


# ---------------------------------------------------------------------------
# Order construction helper
# ---------------------------------------------------------------------------


def _build_order(
    legs_config: list[dict],
    order_type_str: str,
    user_rationale: str,
) -> Optional[Order]:
    """Convert UI leg configuration to an ``Order`` dataclass.

    Returns ``None`` and shows a validation error if construction fails.
    """
    legs: list[OrderLeg] = []

    for i, cfg in enumerate(legs_config):
        symbol = (cfg.get("symbol") or "").strip().upper()
        if not symbol:
            st.error(f"Leg {i + 1}: symbol is required.")
            return None

        try:
            action = OrderAction[cfg["action"]]
        except KeyError:
            st.error(f"Leg {i + 1}: invalid action '{cfg['action']}'.")
            return None

        right: Optional[OptionRight] = None
        if cfg.get("instrument_type") == "Option":
            try:
                right = OptionRight[cfg["right"]]
            except (KeyError, TypeError):
                st.error(f"Leg {i + 1}: call/put selection is required for options.")
                return None

        legs.append(
            OrderLeg(
                symbol=symbol,
                action=action,
                quantity=int(cfg["qty"]),
                option_right=right,
                strike=cfg.get("strike"),
                expiration=cfg.get("expiry"),
            )
        )

    try:
        order_type = OrderType[order_type_str]
    except KeyError:
        st.error(f"Unknown order type: {order_type_str}")
        return None

    try:
        return Order(legs=legs, order_type=order_type, user_rationale=user_rationale)
    except ValueError as exc:
        st.error(f"Order validation error: {exc}")
        return None
