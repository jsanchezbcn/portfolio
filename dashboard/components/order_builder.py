"""dashboard/components/order_builder.py — Pre-trade simulation and order builder.

Provides a collapsible Streamlit component for:
  - Building multi-leg orders (up to 4 legs)
  - Fetching real-time bid/ask/last quotes per leg (T-RT4)
  - Browsing live options chain with strike picker (T-RT5)
  - Simulating against the IBKR WhatIf API (READ-ONLY)
  - Human-reviewed 2-step approval before live submission (T031)

SAFETY CONTRACT
===============
 - "Simulate Trade" → ExecutionEngine.simulate() ONLY (READ-ONLY WhatIf).
 - "Submit Order" requires explicit human approval:
     Step 1: Review order summary panel
     Step 2: Check "I authorize this order" checkbox
     Step 3: Click red "CONFIRM & SUBMIT — LIVE ORDER" button
 - MOC order type is blocked when option legs are present.
 - engine.submit() is NEVER called outside the approval flow.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

import pandas as pd
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

# ── Session-state keys ─────────────────────────────────────────────────────
_SS_ORDER       = "ob_order"
_SS_RESULT      = "ob_sim_result"
_SS_SIMULATING  = "ob_simulating"
_SS_LEG_COUNT   = "ob_leg_count"
_SS_APPROVED    = "ob_approved"       # T031: human checkbox
_SS_SUBMITTING  = "ob_submitting"     # T031: submit in flight
_SS_SUB_RESULT  = "ob_submit_result"  # T031: final Order after submit

# Maximum legs per order
_MAX_LEGS = 4


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------


def render_order_builder(
    execution_engine,                   # ExecutionEngine | None
    account_id: str,
    current_portfolio_greeks: Optional[PortfolioGreeks] = None,
    regime: str = "neutral_volatility",
    market_data_service=None,           # MarketDataService | None  (T-RT1)
) -> None:
    """Render the full order-builder panel inside a Streamlit expander.

    Parameters
    ----------
    execution_engine:
        A live ``ExecutionEngine`` instance.  If ``None``, the panel renders
        in "broker unavailable" mode — Simulate button is disabled.
    account_id:
        IBKR account ID for WhatIf and order submission (e.g. ``"U12345"``).
    current_portfolio_greeks:
        Live portfolio Greeks used to estimate post-trade state.
    regime:
        Risk regime key used for the delta-breach check.
    market_data_service:
        Optional ``MarketDataService`` for real-time prices and options chain.
        When ``None``, price-fetch buttons are disabled.
    """
    with st.expander("📋 Order Builder — Pre-Trade Simulation", expanded=False):
        if market_data_service is not None:
            st.caption("📡 Market data connected — use 💲 per-leg to fetch live bid/ask/last")
        else:
            st.caption("📡 Market data unavailable — live prices disabled (check IBKR/Tastytrade connection)")
        _render_inner(
            execution_engine,
            account_id,
            current_portfolio_greeks or PortfolioGreeks(),
            regime,
            market_data_service,
        )


# ---------------------------------------------------------------------------
# Inner render
# ---------------------------------------------------------------------------


def _render_inner(
    execution_engine,
    account_id: str,
    current_portfolio_greeks: PortfolioGreeks,
    regime: str,
    market_data_service,
) -> None:
    """Render leg builder, simulate button, results, and approval section."""

    # ── Broker-unavailable banner ──────────────────────────────────────────
    if execution_engine is None:
        st.error(
            "⚠ Broker unavailable — simulation disabled. "
            "Check IBKR Client Portal connection."
        )
        st.stop()

    # ── Leg count + order type ─────────────────────────────────────────────
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

    # ── Leg builder ───────────────────────────────────────────────────────
    st.markdown("#### Leg Details")
    legs_config: list[dict] = []
    has_option_legs = False

    for i in range(n_legs):
        st.markdown(f"**Leg {i + 1}**")

        # Row 1: main leg inputs
        c1, c2, c3, c4 = st.columns([1, 2, 1, 2])
        with c1:
            action_str = st.selectbox(
                "Action", ["SELL", "BUY"],
                key=f"ob_action_{i}", label_visibility="collapsed",
            )
        with c2:
            symbol = st.text_input(
                "Symbol", value="SPX",
                key=f"ob_symbol_{i}", label_visibility="collapsed",
            )
        with c3:
            qty = st.number_input(
                "Qty", min_value=1, value=1, step=1,
                key=f"ob_qty_{i}", label_visibility="collapsed",
            )
        with c4:
            instrument_type = st.selectbox(
                "Type", ["Option", "Stock/ETF", "Future"],
                key=f"ob_itype_{i}", label_visibility="collapsed",
            )

        # Row 2: option-specific fields + real-time price (T-RT4)
        c5, c6, c7, c8 = st.columns([2, 2, 2, 3])
        strike: Optional[float] = None
        right_val: Optional[str] = None
        expiry: Optional[date] = None

        with c5:
            if instrument_type == "Option":
                strike = st.number_input(
                    "Strike", value=5000.0, step=50.0,
                    key=f"ob_strike_{i}", label_visibility="collapsed",
                )
                has_option_legs = True
            else:
                st.empty()

        with c6:
            if instrument_type == "Option":
                right_val = st.selectbox(
                    "Call/Put", ["CALL", "PUT"],
                    key=f"ob_right_{i}", label_visibility="collapsed",
                )
            else:
                st.empty()

        with c7:
            if instrument_type in ("Option", "Future"):
                expiry = st.date_input(
                    "Expiry", value=date.today(),
                    key=f"ob_expiry_{i}", label_visibility="collapsed",
                )
            else:
                st.empty()

        with c8:
            # T-RT4: real-time price fetch per leg ─────────────────────────
            _render_leg_price(i, symbol.strip().upper(), instrument_type, market_data_service)

        # Row 3: options chain picker (T-RT5)
        if instrument_type == "Option":
            _render_options_chain(i, symbol.strip().upper(), market_data_service)

        legs_config.append({
            "action": action_str,
            "symbol": symbol,
            "qty": int(qty),
            "instrument_type": instrument_type,
            "strike": strike,
            "right": right_val,
            "expiry": expiry,
        })

    # ── MOC constraint warning ─────────────────────────────────────────────
    moc_selected = order_type_choice == "MOC"
    moc_blocked = moc_selected and has_option_legs
    if moc_blocked:
        st.warning("MOC not supported for options — switch to Limit or Market.")

    # ── Rationale textarea ─────────────────────────────────────────────────
    user_rationale = st.text_area(
        "Trade rationale (optional)",
        placeholder="e.g. Rolling short put up to collect more credit …",
        key="ob_rationale",
        height=70,
    )

    st.divider()

    # ── Simulate button ────────────────────────────────────────────────────
    simulating = st.session_state.get(_SS_SIMULATING, False)
    sim_disabled = simulating or moc_blocked
    prior_result: Optional[SimulationResult] = st.session_state.get(_SS_RESULT)

    bcol1, bcol2 = st.columns([2, 5])
    with bcol1:
        simulate_clicked = st.button(
            "🔍 Simulate Trade" if not simulating else "⏳ Simulating…",
            disabled=sim_disabled,
            key="ob_simulate_btn",
            use_container_width=True,
        )
    with bcol2:
        st.caption("Simulation calls IBKR WhatIf — no orders transmitted")

    # ── Run simulation ─────────────────────────────────────────────────────
    if simulate_clicked and not sim_disabled:
        st.session_state[_SS_SIMULATING] = True
        st.session_state[_SS_RESULT] = None
        st.session_state[_SS_APPROVED] = False
        st.session_state[_SS_SUB_RESULT] = None

        order = _build_order(legs_config, order_type_choice, user_rationale)
        if order is None:
            st.session_state[_SS_SIMULATING] = False
            st.rerun()
        else:
            st.session_state[_SS_ORDER] = order
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

    # ── Results panel ──────────────────────────────────────────────────────
    prior_result = st.session_state.get(_SS_RESULT)
    if prior_result is not None:
        _render_results(prior_result)

    # ── T031: Human-approval section (only after successful simulation) ─────
    if prior_result is not None and not prior_result.error:
        order: Optional[Order] = st.session_state.get(_SS_ORDER)
        if order is not None and order.status == OrderStatus.SIMULATED:
            _render_approval_section(
                execution_engine, account_id, order, prior_result,
                pre_greeks=current_portfolio_greeks,
                regime=regime,
            )

    # ── Submission result (post-submit) ────────────────────────────────────
    sub_result: Optional[Order] = st.session_state.get(_SS_SUB_RESULT)
    if sub_result is not None:
        _render_submission_result(sub_result)


# ---------------------------------------------------------------------------
# T-RT4: Real-time price badge per leg
# ---------------------------------------------------------------------------


def _render_leg_price(
    leg_index: int,
    symbol: str,
    instrument_type: str,
    market_data_service,
) -> None:
    """Fetch and display live bid/ask/last for a single leg."""
    price_key = f"ob_quote_{leg_index}"
    btn_key = f"ob_fetch_price_{leg_index}"

    if market_data_service is None:
        st.button(
            "💲 Price",
            key=btn_key,
            disabled=True,
            help="Market data unavailable — check IBKR/Tastytrade connection",
        )
        return

    # Show cached quote if any
    cached = st.session_state.get(price_key)
    if cached:
        bid = f"{cached.bid:.2f}" if cached.bid is not None else "—"
        ask = f"{cached.ask:.2f}" if cached.ask is not None else "—"
        last = f"{cached.last:.2f}" if cached.last is not None else "—"
        st.markdown(
            f"<small>B:<b>{bid}</b> A:<b>{ask}</b> L:<b>{last}</b></small>",
            unsafe_allow_html=True,
        )

    if st.button("💲 Price", key=btn_key, help="Fetch current bid/ask/last from broker"):
        with st.spinner(""):
            try:
                if instrument_type == "Future":
                    quote = market_data_service.get_futures_quote(symbol)
                else:
                    quote = market_data_service.get_quote(symbol)
                st.session_state[price_key] = quote
                if quote is None or not quote.is_valid():
                    st.warning(f"No price available for {symbol}")
            except Exception as exc:
                logger.debug("Price fetch failed for %s: %s", symbol, exc)
                st.warning(f"Price fetch failed: {exc}")
        st.rerun()


# ---------------------------------------------------------------------------
# T-RT5: Options chain picker
# ---------------------------------------------------------------------------


def _render_options_chain(
    leg_index: int,
    symbol: str,
    market_data_service,
) -> None:
    """Render an options chain table that populates strike/expiry fields on selection."""
    if market_data_service is None:
        return

    chain_key = f"ob_chain_{leg_index}"
    expiry_key = f"ob_chain_expiry_{leg_index}"

    with st.expander(f"📊 Live Options Chain — {symbol or '…'}", expanded=False):
        fetch_col, _ = st.columns([2, 4])
        with fetch_col:
            if st.button(f"🔄 Load Chain", key=f"ob_load_chain_{leg_index}"):
                with st.spinner(f"Fetching {symbol} options chain…"):
                    try:
                        chain = market_data_service.get_options_chain(symbol)
                        st.session_state[chain_key] = chain
                        if not chain:
                            st.warning(
                                f"No options chain available for {symbol}. "
                                "Ensure Tastytrade is connected."
                            )
                    except Exception as exc:
                        logger.debug("Chain fetch failed for %s: %s", symbol, exc)
                        st.warning(f"Chain fetch failed: {exc}")
                st.rerun()

        chain: list = st.session_state.get(chain_key, [])
        if not chain:
            st.caption("Click 'Load Chain' to fetch live options data.")
            return

        # Expiry filter
        expiries = sorted({q.expiry for q in chain if q.expiry})
        selected_expiry = st.selectbox(
            "Expiry",
            options=expiries,
            key=expiry_key,
        )

        filtered = [q for q in chain if q.expiry == selected_expiry]

        if not filtered:
            st.caption("No strikes for this expiry.")
            return

        # Build display DataFrame
        rows = []
        for q in filtered:
            rows.append({
                "Type": q.option_type.upper(),
                "Strike": q.strike,
                "Bid": f"{q.bid:.2f}" if q.bid else "—",
                "Ask": f"{q.ask:.2f}" if q.ask else "—",
                "Δ":  f"{q.delta:+.3f}" if q.delta else "—",
                "IV%": f"{q.iv * 100:.1f}" if q.iv else "—",
                "Θ": f"{q.theta:.2f}" if q.theta else "—",
            })

        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)

        # Strike selector that syncs to the leg's strike/right/expiry fields
        st.caption(
            "Select a strike below to auto-fill this leg's Strike, Call/Put, and Expiry fields:"
        )
        col_a, col_b, col_c = st.columns(3)
        with col_a:
            selected_type = st.selectbox(
                "Type", ["CALL", "PUT"],
                key=f"ob_chain_sel_type_{leg_index}",
            )
        with col_b:
            strike_options = sorted(
                {q.strike for q in filtered if q.option_type.upper() == selected_type}
            )
            if strike_options:
                selected_strike = st.selectbox(
                    "Strike", strike_options,
                    key=f"ob_chain_sel_strike_{leg_index}",
                )
            else:
                selected_strike = None
                st.caption("—")

        with col_c:
            if st.button("✔ Use This Strike", key=f"ob_chain_apply_{leg_index}"):
                if selected_strike is not None:
                    st.session_state[f"ob_strike_{leg_index}"] = float(selected_strike)
                    st.session_state[f"ob_right_{leg_index}"] = selected_type
                    st.session_state[f"ob_expiry_{leg_index}"] = (
                        date.fromisoformat(selected_expiry)
                        if selected_expiry
                        else date.today()
                    )
                    st.rerun()


# ---------------------------------------------------------------------------
# Simulation results panel
# ---------------------------------------------------------------------------


def _render_results(result: SimulationResult) -> None:
    """Render the SimulationResult panel."""
    if result.error:
        st.error(f"❌ Simulation failed: {result.error}")
        st.caption("Fix the error above before submitting an order.")
        return

    st.success("✅ Simulation complete")

    m_col1, m_col2, m_col3 = st.columns(3)
    with m_col1:
        st.metric("Initial Margin Required", f"${result.margin_requirement:,.0f}")
    with m_col2:
        st.metric("Equity Before", f"${result.equity_before:,.0f}")
    with m_col3:
        delta_eq = (result.equity_after or 0.0) - (result.equity_before or 0.0)
        st.metric(
            "Equity After",
            f"${result.equity_after:,.0f}",
            delta=f"${delta_eq:+,.0f}",
            delta_color="normal",
        )

    if result.post_trade_greeks:
        g = result.post_trade_greeks
        st.markdown("#### Post-Trade Portfolio Greeks")

        if result.delta_breach:
            st.error(
                f"🚨 **Delta Breach** — Post-trade |Δ| `{abs(g.spx_delta):.1f}` "
                f"exceeds the regime limit.  Reduce position size or adjust legs."
            )

        cols = st.columns(4)
        with cols[0]:
            delta_label = "SPX Δ (β-weighted)"
            delta_value = f"{g.spx_delta:+.2f}"
            if result.delta_breach:
                st.markdown(
                    f"<div style='background:#ff4444;color:white;padding:8px;"
                    f"border-radius:4px;text-align:center'>"
                    f"<b>{delta_label}</b><br/>"
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
# T031: Human-approval section
# ---------------------------------------------------------------------------


def _render_approval_section(
    execution_engine,
    account_id: str,
    order: Order,
    sim_result: SimulationResult,
    pre_greeks=None,      # PortfolioGreeks | None  (T042)
    regime: str = "neutral_volatility",  # T041
) -> None:
    """Render the mandatory 2-step human approval panel (T031).

    Step 1: Read-only order summary (user reviews)
    Step 2: Checkbox confirming authorization
    Step 3: Red submit button (only active after checkbox)

    SAFETY: engine.submit() is ONLY called from this function after both
    the checkbox and button have been explicitly activated by the user.
    """
    st.divider()
    st.markdown("### 🔐 Order Approval Required")
    st.warning(
        "⚠ Reviewing the simulation above is **mandatory** before submitting. "
        "Once submitted, this order is **live** and will be executed at market."
    )

    # ── Step 1: Order summary (read-only review) ──────────────────────────
    with st.expander("📋 Step 1 — Review Order Details", expanded=True):
        st.markdown(f"**Order Type:** `{order.order_type.value}`")
        st.markdown(f"**Legs:** {len(order.legs)}")
        for idx, leg in enumerate(order.legs, 1):
            right = f" {leg.option_right.name}" if leg.option_right else ""
            strike = f" @ {leg.strike}" if leg.strike else ""
            expiry = f" exp {leg.expiration}" if leg.expiration else ""
            st.markdown(
                f"  Leg {idx}: **{leg.action.value}** {leg.quantity}× "
                f"`{leg.symbol}`{right}{strike}{expiry}"
            )
        if sim_result.margin_requirement is not None:
            st.markdown(
                f"**Estimated Initial Margin:** `${sim_result.margin_requirement:,.0f}`"
            )
        if order.user_rationale:
            st.markdown(f"**Rationale:** {order.user_rationale}")

    # ── Step 2: Authorization checkbox ───────────────────────────────────
    approved = st.checkbox(
        "✅ I have reviewed the order above and authorize its submission as a **LIVE ORDER**",
        key=_SS_APPROVED,
        value=st.session_state.get(_SS_APPROVED, False),
    )

    # ── Step 3: Conditional submit button ────────────────────────────────
    submitting = st.session_state.get(_SS_SUBMITTING, False)

    if not approved:
        st.button(
            "🚫 Confirm & Submit — LIVE ORDER  (check the box above first)",
            disabled=True,
            key="ob_submit_disabled",
            use_container_width=True,
        )
        return

    submit_clicked = st.button(
        "🚨 CONFIRM & SUBMIT — LIVE ORDER",
        type="primary",
        disabled=submitting,
        key="ob_submit_live",
        use_container_width=True,
        help=(
            "This will immediately transmit a live order to IBKR. "
            "Ensure you have reviewed all details above."
        ),
    )

    if submit_clicked and not submitting:
        st.session_state[_SS_SUBMITTING] = True
        with st.spinner("⏳ Submitting live order to IBKR…"):
            try:
                submitted_order = execution_engine.submit(
                    account_id=account_id,
                    order=order,
                    pre_greeks=pre_greeks,
                    regime=regime,
                )
            except Exception as exc:
                logger.exception("Unexpected error during submit()")
                submitted_order = order
                submitted_order.rejection_reason = f"Unexpected submission error: {exc}"
                submitted_order.transition_to(OrderStatus.REJECTED)

        st.session_state[_SS_SUB_RESULT] = submitted_order
        st.session_state[_SS_SUBMITTING] = False
        # Reset approval state after submission
        st.session_state[_SS_APPROVED] = False
        st.rerun()


def _render_submission_result(order: Order) -> None:
    """Show the outcome of a live order submission."""
    import streamlit as _st
    st.divider()
    if order.status == OrderStatus.FILLED:
        st.success(
            f"✅ **Order FILLED** — Broker order ID: `{order.broker_order_id}`  "
            f"at {order.filled_at.strftime('%H:%M:%S UTC') if order.filled_at else '—'}"
        )
        # T035: Trigger position refresh so next page load re-fetches the portfolio
        _st.session_state["positions"] = None
    elif order.status == OrderStatus.REJECTED:
        # T033: Show broker rejection reason in red
        rejection_msg = order.rejection_reason or "No reason provided by broker."
        st.error(f"❌ **Order REJECTED** — {rejection_msg}")
        st.caption(
            "The order was NOT transmitted. Review the rejection reason above "
            "and correct your order before re-simulating."
        )
    elif order.status == OrderStatus.CANCELLED:
        st.warning("🚫 **Order CANCELLED** by broker.")
    elif order.status == OrderStatus.PENDING:
        # T034: Surface "status unknown" clearly
        st.warning(
            f"⚠ **Order status UNKNOWN** — Broker order ID: `{order.broker_order_id}`  \n"
            "Polling timed out before a fill or rejection was confirmed.  \n"
            "**Verify order status directly in your IBKR platform** before "
            "placing additional trades."
        )
    else:
        st.info(f"ℹ Order status: `{order.status.value}`")


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
