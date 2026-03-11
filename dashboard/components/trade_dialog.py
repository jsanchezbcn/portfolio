"""dashboard/components/trade_dialog.py — Inline Trade Ticket Dialog.

Replaces the "draft ready" popup with a fully interactive trade ticket modal
that contains bid/ask, qty editing, WhatIf simulation, and live order submission
— all without leaving the dialog or scrolling to a separate Order Builder.

Flow
----
1. "Create Trade" is clicked anywhere → session state sets td_active=True +
   td_legs / td_source / td_rationale
2. On every render, render_trade_dialog() checks td_active and shows the modal
3. Inside the modal:
   a. Leg cards with bid/ask fetch per leg
   b. Qty inputs              (editable)
   c. Rationale field         (editable)
   d. 🔍 Simulate Trade       → calls execution_engine.simulate() inline
   e. Margin + Greeks display
   f. ☐ Authorization checkbox
   g. 🚨 CONFIRM & SUBMIT     → calls execution_engine.submit()
4. Success or rejection is shown inline; dialog closes on submission
"""
from __future__ import annotations

import logging
from datetime import date as _date
from typing import Optional

import streamlit as st

from models.order import (
    Order,
    OrderAction,
    OrderLeg,
    OrderStatus,
    OrderType,
    OptionRight,
    SimulationResult,
)

logger = logging.getLogger(__name__)

# ── Session-state keys (prefix td_ to avoid clashing with main Order Builder) ─
_TD_ACTIVE          = "td_active"          # bool  — dialog is open
_TD_LEGS            = "td_legs"            # list[dict] — staged legs
_TD_SOURCE          = "td_source"          # str
_TD_RATIONALE       = "td_rationale"       # str
_TD_SIM_RESULT      = "td_sim_result"      # SimulationResult | None
_TD_SIM_RUNNING     = "td_sim_running"     # bool
_TD_APPROVED        = "td_approved"        # bool
_TD_SUBMITTING      = "td_submitting"      # bool
_TD_SUB_RESULT      = "td_sub_result"      # Order | None
_TD_ORDER           = "td_order"           # Order — last built order


def open_trade_dialog(
    legs: list[dict],
    source: str = "",
    rationale: str = "",
) -> None:
    """Stage a trade and open the dialog on next render.

    Call this instead of the old _prefill_order_builder_from_legs flow when
    you want the inline trade ticket to appear.
    """
    st.session_state[_TD_LEGS]      = legs
    st.session_state[_TD_SOURCE]    = source
    st.session_state[_TD_RATIONALE] = rationale
    st.session_state[_TD_ACTIVE]    = True
    st.session_state[_TD_SIM_RESULT]   = None
    st.session_state[_TD_SIM_RUNNING]  = False
    st.session_state[_TD_APPROVED]     = False
    st.session_state[_TD_SUBMITTING]   = False
    st.session_state[_TD_SUB_RESULT]   = None
    st.session_state[_TD_ORDER]        = None
    # Reset per-leg qty overrides, quotes, and combo price
    for i in range(len(legs)):
        st.session_state.pop(f"td_qty_{i}", None)
        st.session_state.pop(f"td_quote_{i}", None)
    st.session_state.pop("td_combo_price", None)


def render_trade_dialog(
    execution_engine=None,
    account_id: str = "",
    current_portfolio_greeks=None,
    regime: str = "neutral_volatility",
    market_data_service=None,
) -> None:
    """Call this once per Streamlit run. Shows the trade ticket if active."""
    if not st.session_state.get(_TD_ACTIVE):
        return

    @st.dialog("🛡️ Trade Ticket", width="large")
    def _show() -> None:
        _render_trade_ticket(
            execution_engine=execution_engine,
            account_id=account_id,
            current_portfolio_greeks=current_portfolio_greeks,
            regime=regime,
            market_data_service=market_data_service,
        )

    _show()


# ---------------------------------------------------------------------------
# Internal render
# ---------------------------------------------------------------------------

def _render_trade_ticket(
    execution_engine,
    account_id: str,
    current_portfolio_greeks,
    regime: str,
    market_data_service,
) -> None:
    legs: list[dict]  = st.session_state.get(_TD_LEGS, [])
    source: str       = st.session_state.get(_TD_SOURCE, "")
    rationale_init: str = st.session_state.get(_TD_RATIONALE, "")

    if not legs:
        st.warning("No legs staged.")
        if st.button("Close", key="td_close_empty"):
            _close_dialog()
        return

    # ── Header ────────────────────────────────────────────────────────────
    if source:
        st.caption(f"Source: **{source}**")

    broker_ok = execution_engine is not None
    if not broker_ok:
        st.error("⚠ Broker unavailable — simulation disabled.")

    sim_result: Optional[SimulationResult] = st.session_state.get(_TD_SIM_RESULT)
    sub_result: Optional[Order]           = st.session_state.get(_TD_SUB_RESULT)

    # ── Submission outcome (final state) ─────────────────────────────────
    if sub_result is not None:
        _render_submission_result(sub_result)
        if st.button("✖ Close", key="td_close_after_submit", use_container_width=True):
            _close_dialog()
        return

    # ── Leg cards ─────────────────────────────────────────────────────────
    st.markdown("#### Legs")
    for i, leg in enumerate(legs):
        _render_leg_card(i, leg, market_data_service)

    # ── Combo Net Quote (for multi-leg) ───────────────────────────────────
    _render_combo_pricing(legs)

    st.markdown("---")

    # ── Rationale ─────────────────────────────────────────────────────────
    rationale = st.text_area(
        "Rationale",
        value=rationale_init,
        placeholder="Why this trade…",
        key="td_rationale_input",
        height=60,
    )

    # ── Simulate ──────────────────────────────────────────────────────────
    sim_running = st.session_state.get(_TD_SIM_RUNNING, False)

    btn_col, cap_col = st.columns([2, 5])
    with btn_col:
        sim_clicked = st.button(
            "⏳ Simulating…" if sim_running else "🔍 Simulate Trade",
            disabled=(sim_running or not broker_ok),
            key="td_simulate_btn",
            type="primary",
            use_container_width=True,
        )
    with cap_col:
        st.caption("Calls IBKR WhatIf — no orders transmitted")

    if sim_clicked and not sim_running and broker_ok:
        order = _build_order_from_dialog(legs, "LIMIT", rationale)
        if order is not None:
            st.session_state[_TD_ORDER]      = order
            st.session_state[_TD_SIM_RUNNING] = True
            st.session_state[_TD_SIM_RESULT]  = None
            st.session_state[_TD_APPROVED]    = False
            with st.spinner("Sending WhatIf request to IBKR…"):
                try:
                    result = execution_engine.simulate(
                        account_id=account_id,
                        order=order,
                        current_portfolio_greeks=current_portfolio_greeks,
                        regime=regime,
                    )
                except Exception as exc:
                    logger.exception("Trade dialog simulate error")
                    result = SimulationResult(error=f"Unexpected error: {exc}")
            st.session_state[_TD_SIM_RESULT]  = result
            st.session_state[_TD_SIM_RUNNING] = False
            st.rerun()

    # ── Simulation results ────────────────────────────────────────────────
    if sim_result is not None:
        _render_sim_results(sim_result)

        if not sim_result.error:
            _render_approval(
                execution_engine=execution_engine,
                account_id=account_id,
                current_portfolio_greeks=current_portfolio_greeks,
                regime=regime,
            )

    # ── Cancel button (bottom) ────────────────────────────────────────────
    st.markdown("---")
    if st.button("✖ Cancel — close without submitting", key="td_cancel", use_container_width=False):
        _close_dialog()


# ---------------------------------------------------------------------------
# Leg card with qty + bid/ask
# ---------------------------------------------------------------------------

def _render_leg_card(leg_index: int, leg: dict, market_data_service) -> None:
    """Render one leg as a compact card with qty input and per-leg bid/ask (read-only)."""
    action   = str(leg.get("action", "BUY")).upper()
    symbol   = str(leg.get("symbol", "?")).upper()
    itype    = str(leg.get("instrument_type", "Option"))
    strike   = leg.get("strike")
    right    = str(leg.get("right") or "").upper()
    expiry   = leg.get("expiry")
    conid    = leg.get("conid") or leg.get("conId")

    # Format expiry
    expiry_str = ""
    if expiry:
        if isinstance(expiry, str):
            expiry_str = expiry[:10]
        elif hasattr(expiry, "strftime"):
            expiry_str = expiry.strftime("%Y-%m-%d")

    # Leg label
    if itype == "Option" and strike:
        leg_label = f"{right} {strike:,.0f}  exp {expiry_str}"
    elif itype == "Future":
        leg_label = f"Future{(' exp ' + expiry_str) if expiry_str else ''}"
    else:
        leg_label = itype

    action_color = "#e74c3c" if action == "SELL" else "#27ae60"

    with st.container(border=True):
        # Header row: action badge + symbol + description + qty input
        h1, h2 = st.columns([6, 4])
        with h1:
            st.markdown(
                f"<span style='background:{action_color};color:white;"
                f"padding:3px 10px;border-radius:4px;font-weight:700;"
                f"font-size:0.9em'>{action}</span> "
                f"&nbsp;<b style='font-size:1.1em'>{symbol}</b>&nbsp;"
                f"<span style='color:#888;font-size:0.9em'>{leg_label}</span>",
                unsafe_allow_html=True,
            )

        with h2:
            default_qty = int(leg.get("qty", leg.get("quantity", 1)) or 1)
            qty_key = f"td_qty_{leg_index}"
            if qty_key not in st.session_state:
                st.session_state[qty_key] = default_qty

            qty_val = st.number_input(
                "Contracts",
                min_value=1,
                max_value=999,
                step=1,
                key=qty_key,
                label_visibility="collapsed",
                help="Number of contracts",
            )
            st.caption("contracts")

        # Bid/Ask row — per-leg (informational, not editable price)
        quote_key = f"td_quote_{leg_index}"
        cached_quote = st.session_state.get(quote_key)

        # Inline bid/ask from leg data (proposer provides bid/ask/mid)
        leg_bid = leg.get("bid")
        leg_ask = leg.get("ask")
        leg_mid = leg.get("mid")

        qa_col, btn_col = st.columns([6, 2])
        with qa_col:
            if cached_quote:
                bid  = f"${cached_quote.bid:.2f}"  if cached_quote.bid  is not None else "—"
                ask  = f"${cached_quote.ask:.2f}"  if cached_quote.ask  is not None else "—"
                last = f"${cached_quote.last:.2f}" if cached_quote.last is not None else "—"
                st.markdown(
                    f"<span style='font-size:0.85em;color:#aaa'>Bid&nbsp;</span>"
                    f"<b style='color:#e74c3c'>{bid}</b>"
                    f"<span style='font-size:0.85em;color:#aaa'>&nbsp;/&nbsp;Ask&nbsp;</span>"
                    f"<b style='color:#27ae60'>{ask}</b>"
                    f"<span style='font-size:0.85em;color:#aaa'>&nbsp;Last&nbsp;</span>"
                    f"<b>{last}</b>",
                    unsafe_allow_html=True,
                )
            elif leg_bid is not None and leg_ask is not None:
                # Use proposer-provided prices as fallback
                st.markdown(
                    f"<span style='font-size:0.85em;color:#aaa'>Bid&nbsp;</span>"
                    f"<b style='color:#e74c3c'>${float(leg_bid):.2f}</b>"
                    f"<span style='font-size:0.85em;color:#aaa'>&nbsp;/&nbsp;Ask&nbsp;</span>"
                    f"<b style='color:#27ae60'>${float(leg_ask):.2f}</b>"
                    + (f"<span style='font-size:0.85em;color:#aaa'>&nbsp;Mid&nbsp;</span>"
                       f"<b>${float(leg_mid):.2f}</b>" if leg_mid is not None else ""),
                    unsafe_allow_html=True,
                )
            else:
                st.caption("Bid / Ask — click 💲 to fetch")

        with btn_col:
            fetch_disabled = market_data_service is None
            if st.button(
                "💲",
                key=f"td_fetch_price_{leg_index}",
                disabled=fetch_disabled,
                help="Fetch live bid/ask/last" if not fetch_disabled else "Market data unavailable",
                use_container_width=True,
            ):
                with st.spinner(""):
                    try:
                        _conid_int = int(conid) if conid else 0
                        if _conid_int > 0 and market_data_service is not None:
                            quote = market_data_service.get_quote_by_conid(_conid_int, symbol=symbol)
                        elif itype == "Future":
                            quote = market_data_service.get_futures_quote(symbol)
                        else:
                            quote = market_data_service.get_quote(symbol)
                        st.session_state[quote_key] = quote
                        if quote is None or not quote.is_valid():
                            st.warning(f"No live price available for {symbol}")
                    except Exception as exc:
                        st.warning(f"Price fetch failed: {exc}")
                st.rerun()

        # Store qty for _build_order_from_dialog
        leg["_dialog_qty"] = qty_val


# ---------------------------------------------------------------------------
# Combo net pricing (multi-leg) + limit price input
# ---------------------------------------------------------------------------

def _render_combo_pricing(legs: list[dict]) -> None:
    """Show combo net bid/ask/mid for multi-leg + a single limit price input.

    For single-leg trades, shows limit price directly from that leg's quote.
    For multi-leg (spreads), calculates the net debit/credit from all legs.
    """
    n_legs = len(legs)
    has_quotes = False

    # Gather per-leg quote data (from live fetch or proposer data)
    leg_quotes: list[dict] = []
    for i, leg in enumerate(legs):
        cached = st.session_state.get(f"td_quote_{i}")
        action = str(leg.get("action", "BUY")).upper()
        qty = int(st.session_state.get(f"td_qty_{i}", leg.get("qty", leg.get("quantity", 1))) or 1)

        bid = ask = mid = None
        if cached:
            bid = cached.bid
            ask = cached.ask
            mid = (bid + ask) / 2.0 if (bid is not None and ask is not None) else None
            has_quotes = True
        elif leg.get("bid") is not None and leg.get("ask") is not None:
            bid = float(leg["bid"])
            ask = float(leg["ask"])
            mid = float(leg["mid"]) if leg.get("mid") is not None else (bid + ask) / 2.0
            has_quotes = True

        leg_quotes.append({"action": action, "qty": qty, "bid": bid, "ask": ask, "mid": mid})

    # ── Combo net calculation ─────────────────────────────────────────────
    combo_bid = combo_ask = combo_mid = None
    if has_quotes:
        _cb = _ca = _cm = 0.0
        all_quoted = True
        for lq in leg_quotes:
            if lq["bid"] is None or lq["ask"] is None:
                all_quoted = False
                break
            if lq["action"] == "SELL":
                _cb += lq["qty"] * lq["bid"]
                _ca += lq["qty"] * lq["ask"]
                _cm += lq["qty"] * lq["mid"]
            else:  # BUY
                _cb -= lq["qty"] * lq["ask"]
                _ca -= lq["qty"] * lq["bid"]
                _cm -= lq["qty"] * lq["mid"]
        if all_quoted:
            combo_bid = _cb
            combo_ask = _ca
            combo_mid = _cm

    # ── Display ───────────────────────────────────────────────────────────
    st.markdown("---")
    if n_legs > 1:
        st.markdown("#### Combo Net Price")
    else:
        st.markdown("#### Order Price")

    if combo_bid is not None:
        _sign = "Credit" if combo_mid > 0 else "Debit"
        q1, q2, q3, q4 = st.columns(4)
        with q1:
            st.metric("Net Bid", f"${combo_bid:+.2f}")
        with q2:
            st.metric("Net Ask", f"${combo_ask:+.2f}")
        with q3:
            st.metric("Net Mid", f"${combo_mid:+.2f}")
        with q4:
            spread = abs(combo_ask - combo_bid)
            st.metric("Spread", f"${spread:.2f}")
        st.caption(f"{'💰 Net ' + _sign if combo_mid != 0 else 'Even'} "
                   f"{'— positive = you receive premium' if combo_mid > 0 else '— negative = you pay premium' if combo_mid < 0 else ''}")
    elif not has_quotes:
        st.caption("Fetch quotes on each leg above to see combo pricing")
    else:
        st.warning("Not all legs have quotes — combo price incomplete")

    # ── Limit price input with auto-fill from combo ───────────────────────
    price_key = "td_combo_price"
    if price_key not in st.session_state:
        st.session_state[price_key] = 0.0

    p_col, mid_btn, nat_btn = st.columns([4, 2, 2])
    with p_col:
        st.number_input(
            "Limit Price (net)",
            step=0.05,
            format="%.2f",
            key=price_key,
            help="Net debit/credit for the combo. Negative = debit (you pay). Positive = credit (you receive). 0 = market order.",
        )
    with mid_btn:
        if combo_mid is not None:
            if st.button(f"📥 Mid {combo_mid:+.2f}", key="td_use_mid", use_container_width=True):
                st.session_state[price_key] = round(combo_mid, 2)
                st.rerun()
        else:
            st.button("📥 Mid —", key="td_use_mid", disabled=True, use_container_width=True)
    with nat_btn:
        if combo_bid is not None:
            # "Natural" = best available (bid for sells, worst for buyers)
            nat = combo_bid if combo_mid >= 0 else combo_ask
            if st.button(f"📥 Nat {nat:+.2f}", key="td_use_nat", use_container_width=True):
                st.session_state[price_key] = round(nat, 2)
                st.rerun()
        else:
            st.button("📥 Nat —", key="td_use_nat", disabled=True, use_container_width=True)


# ---------------------------------------------------------------------------
# Simulation results display
# ---------------------------------------------------------------------------

def _render_sim_results(result: SimulationResult) -> None:
    st.markdown("---")
    if result.error:
        st.error(f"❌ Simulation failed: {result.error}")
        st.caption("Fix the error above before submitting.")
        return

    st.success("✅ WhatIf simulation complete")

    m1, m2, m3 = st.columns(3)
    with m1:
        margin = result.margin_requirement or 0.0
        st.metric("Initial Margin Required", f"${margin:,.0f}")
    with m2:
        eq_before = result.equity_before or 0.0
        st.metric("Equity Before", f"${eq_before:,.0f}")
    with m3:
        eq_after  = result.equity_after  or 0.0
        delta_eq  = eq_after - eq_before
        st.metric("Equity After", f"${eq_after:,.0f}", delta=f"${delta_eq:+,.0f}")

    if result.post_trade_greeks:
        g = result.post_trade_greeks
        g1, g2, g3, g4 = st.columns(4)
        with g1:
            delta_val = f"{g.spx_delta:+.2f}"
            if result.delta_breach:
                st.markdown(
                    f"<div style='background:#e74c3c;color:white;padding:6px;"
                    f"border-radius:4px;text-align:center'>"
                    f"<small>SPX Δ</small><br/><b>{delta_val}</b> ⚠</div>",
                    unsafe_allow_html=True,
                )
            else:
                st.metric("SPX Δ", delta_val)
        with g2:
            st.metric("θ Theta",  f"{g.theta:+.2f}")
        with g3:
            st.metric("V Vega",   f"{g.vega:+.2f}")
        with g4:
            st.metric("Γ Gamma",  f"{g.gamma:+.4f}")


# ---------------------------------------------------------------------------
# Approval + submit
# ---------------------------------------------------------------------------

def _render_approval(execution_engine, account_id, current_portfolio_greeks, regime) -> None:
    st.markdown("---")
    st.markdown("#### 🔐 Authorization")
    st.warning(
        "**Once submitted this is a LIVE order.** "
        "Verify all leg details and the margin estimate above before proceeding."
    )

    approved = st.checkbox(
        "✅ I have reviewed this trade and authorize it as a **LIVE ORDER**",
        key=_TD_APPROVED,
        value=st.session_state.get(_TD_APPROVED, False),
    )

    submitting = st.session_state.get(_TD_SUBMITTING, False)

    if not approved:
        st.button(
            "🚫 CONFIRM & SUBMIT — check the box above first",
            disabled=True,
            key="td_submit_disabled",
            use_container_width=True,
        )
        return

    submit_clicked = st.button(
        "🚨 CONFIRM & SUBMIT — LIVE ORDER",
        type="primary",
        disabled=submitting,
        key="td_submit_live",
        use_container_width=True,
        help="Immediately transmits a live order to IBKR.",
    )

    if submit_clicked and not submitting:
        order: Optional[Order] = st.session_state.get(_TD_ORDER)
        if order is None:
            st.error("Order not found — please re-simulate.")
            return

        st.session_state[_TD_SUBMITTING] = True
        with st.spinner("⏳ Submitting live order to IBKR…"):
            try:
                submitted = execution_engine.submit(
                    account_id=account_id,
                    order=order,
                    pre_greeks=current_portfolio_greeks,
                    regime=regime,
                )
            except Exception as exc:
                logger.exception("Trade dialog submit error")
                order.rejection_reason = f"Unexpected submit error: {exc}"
                order.transition_to(OrderStatus.REJECTED)
                submitted = order

        st.session_state[_TD_SUB_RESULT]  = submitted
        st.session_state[_TD_SUBMITTING]  = False
        st.session_state[_TD_APPROVED]    = False
        # Close the dialog after submission
        st.session_state[_TD_ACTIVE] = False
        # Trigger position refresh
        st.session_state["positions"] = None
        st.rerun()


# ---------------------------------------------------------------------------
# Submission result banner (shown in main page after close)
# ---------------------------------------------------------------------------

def render_submission_banner() -> None:
    """Call this in app.py once per run to show submission result banner."""
    sub = st.session_state.get(_TD_SUB_RESULT)
    if sub is None:
        return
    _render_submission_result(sub)
    if st.button("✖ Dismiss", key="td_dismiss_banner"):
        st.session_state.pop(_TD_SUB_RESULT, None)
        st.rerun()


def _render_submission_result(order: Order) -> None:
    if order.status == OrderStatus.FILLED:
        st.success(
            f"✅ **Order FILLED** — broker ID: `{order.broker_order_id}`"
            + (f"  at {order.filled_at.strftime('%H:%M:%S UTC')}" if order.filled_at else "")
        )
    elif order.status == OrderStatus.REJECTED:
        reason = order.rejection_reason or "No reason provided."
        st.error(f"❌ **Order REJECTED** — {reason}")
        st.caption("The order was NOT transmitted. Correct and re-simulate before retrying.")
    elif order.status == OrderStatus.CANCELLED:
        st.warning("🚫 **Order CANCELLED** by broker.")
    elif order.status == OrderStatus.PENDING:
        st.warning(
            f"⚠ **Order status UNKNOWN** — broker ID: `{order.broker_order_id}`  \n"
            "Polling timed out. **Verify status in IBKR before placing more trades.**"
        )
    else:
        st.info(f"ℹ Order status: `{order.status.value}`")


# ---------------------------------------------------------------------------
# Order construction
# ---------------------------------------------------------------------------

def _build_order_from_dialog(
    legs: list[dict],
    order_type_str: str,
    rationale: str,
) -> Optional[Order]:
    """Build an Order from the dialog's current leg + qty state."""
    order_legs: list[OrderLeg] = []

    for i, leg in enumerate(legs):
        symbol = str(leg.get("symbol") or "").strip().upper()
        if not symbol:
            st.error(f"Leg {i + 1}: symbol is required.")
            return None

        action_str = str(leg.get("action", "BUY")).upper()
        try:
            action = OrderAction[action_str]
        except KeyError:
            st.error(f"Leg {i + 1}: invalid action '{action_str}'.")
            return None

        # Qty — may have been overridden in the dialog
        qty = int(st.session_state.get(f"td_qty_{i}", leg.get("qty", 1)) or 1)
        qty = max(1, qty)

        itype  = str(leg.get("instrument_type", "Option"))
        right: Optional[OptionRight] = None
        if itype == "Option":
            right_str = str(leg.get("right") or "").upper()
            try:
                right = OptionRight[right_str]
            except KeyError:
                st.error(f"Leg {i + 1}: invalid option right '{right_str}'.")
                return None

        expiry_raw = leg.get("expiry")
        expiry_date: Optional[_date] = None
        if expiry_raw:
            if isinstance(expiry_raw, str):
                try:
                    expiry_date = _date.fromisoformat(expiry_raw[:10])
                except ValueError:
                    pass
            elif isinstance(expiry_raw, _date):
                expiry_date = expiry_raw

        order_legs.append(
            OrderLeg(
                symbol=symbol,
                action=action,
                quantity=qty,
                option_right=right,
                strike=float(leg["strike"]) if leg.get("strike") is not None else None,
                expiration=expiry_date,
                conid=str(leg["conid"]) if leg.get("conid") not in (None, "") else None,
            )
        )

    try:
        order_type = OrderType[order_type_str]
    except KeyError:
        st.error(f"Unknown order type: {order_type_str}")
        return None

    # Collect limit price from the combo net price input
    _combo_price = float(st.session_state.get("td_combo_price", 0.0) or 0.0)
    _net_price: Optional[float] = _combo_price if _combo_price != 0.0 else None

    try:
        return Order(
            legs=order_legs,
            order_type=order_type,
            user_rationale=rationale,
            limit_price=_net_price,
        )
    except ValueError as exc:
        st.error(f"Order validation error: {exc}")
        return None


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _close_dialog() -> None:
    st.session_state[_TD_ACTIVE] = False
    st.rerun()
