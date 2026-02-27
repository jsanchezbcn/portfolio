"""dashboard/components/positions_view.py — Split positions into Futures/Stocks vs Options.

Shows:
  - Futures/Stocks table: symbol, qty, delta, SPX delta
  - Options table: symbol, strike, expiry, qty, delta, theta, vega, gamma,
    greeks_source, last_refreshed, staleness highlighting (yellow >5 min, red >30 min),
    Buy/Sell/Roll action buttons
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

import pandas as pd
import streamlit as st

LOGGER = logging.getLogger(__name__)

# ── Commission constants ──────────────────────────────────────────────────────
COMMISSIONS = {
    "ES": 1.40,
    "MES": 0.47,
    "NQ": 1.40,
    "MNQ": 0.47,
    "DEFAULT_OPTION": 0.65,
    "DEFAULT_STOCK": 0.005,  # per share
}


def _greeks_age_minutes(position: Any) -> Optional[float]:
    """Minutes since position's Greek timestamp was recorded."""
    ts = getattr(position, "timestamp", None)
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 60.0


def _staleness_css(age_min: Optional[float]) -> str:
    """Return CSS background colour class for staleness."""
    if age_min is None:
        return ""
    if age_min > 30:
        return "background-color: rgba(220, 38, 38, 0.25)"   # red tint
    if age_min > 5:
        return "background-color: rgba(234, 179, 8, 0.25)"   # yellow tint
    return ""


def render_positions_split(
    positions: list,
    ibkr_option_scaling: bool = False,
    adapter: Any = None,
    account_id: str = "",
    exec_engine: Any = None,
    prefill_order_fn: Optional[Callable[..., bool]] = None,
) -> None:
    """Render positions in two tables: Futures/Stocks and Options."""
    if not positions:
        st.info("No positions to display.")
        return

    futures_stocks = [p for p in positions if p.instrument_type.name != "OPTION"]
    options = [p for p in positions if p.instrument_type.name == "OPTION"]

    # ── Futures / Stocks ──────────────────────────────────────────────────
    st.subheader(f"📊 Futures & Stocks ({len(futures_stocks)})")
    if futures_stocks:
        fs_rows = []
        for p in futures_stocks:
            fs_rows.append({
                "Symbol": p.symbol,
                "Type": p.instrument_type.name,
                "Underlying": p.underlying or p.symbol,
                "Qty": float(p.quantity),
                "Delta": float(p.delta),
                "SPX Δ": float(p.spx_delta),
                "Mkt Value": float(p.market_value),
                "P&L": float(p.unrealized_pnl),
            })
        fs_df = pd.DataFrame(fs_rows)
        st.dataframe(
            fs_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Qty": st.column_config.NumberColumn(format="%.0f"),
                "Delta": st.column_config.NumberColumn(format="%.2f"),
                "SPX Δ": st.column_config.NumberColumn(format="%.2f"),
                "Mkt Value": st.column_config.NumberColumn(format="$%.2f"),
                "P&L": st.column_config.NumberColumn(format="$%.2f"),
            },
        )
    else:
        st.caption("No futures or stock positions.")

    # ── Options ───────────────────────────────────────────────────────────
    st.subheader(f"📋 Options ({len(options)})")
    if not options:
        st.caption("No option positions.")
        return

    # Build rows with staleness info
    opt_rows = []
    for p in options:
        scale = 100.0 if ibkr_option_scaling else 1.0
        age = _greeks_age_minutes(p)
        age_str = f"{age:.0f}m" if age is not None else "N/A"
        staleness = "🟢 Fresh"
        if age is not None:
            if age > 30:
                staleness = "🔴 Stale (>30m)"
            elif age > 5:
                staleness = "🟡 Aging (>5m)"

        opt_rows.append({
            "_pos": p,  # hidden reference for buttons
            "Symbol": p.symbol,
            "Underlying": p.underlying or "",
            "Type": p.option_type or "",
            "Strike": float(p.strike) if p.strike else 0,
            "Expiry": p.expiration.isoformat() if p.expiration else "",
            "DTE": p.days_to_expiration if p.days_to_expiration is not None else "",
            "Qty": float(p.quantity),
            "Δ (Delta)": float(p.delta) * scale,
            "Θ (Theta)": float(p.theta) * scale,
            "V (Vega)": float(p.vega) * scale,
            "Γ (Gamma)": float(p.gamma) * scale,
            "SPX Δ": float(p.spx_delta),
            "IV": f"{float(p.iv)*100:.1f}%" if p.iv else "—",
            "Source": getattr(p, "greeks_source", "none"),
            "Age": age_str,
            "Status": staleness,
        })

    # Display table (without _pos column)
    display_cols = [c for c in opt_rows[0].keys() if c != "_pos"]
    opt_df = pd.DataFrame([{k: v for k, v in r.items() if k != "_pos"} for r in opt_rows])
    sel = st.dataframe(
        opt_df,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="positions_options_table",
        column_config={
            "Strike": st.column_config.NumberColumn(format="%.2f"),
            "Qty": st.column_config.NumberColumn(format="%.0f"),
            "Δ (Delta)": st.column_config.NumberColumn(format="%.3f"),
            "Θ (Theta)": st.column_config.NumberColumn(format="%.2f"),
            "V (Vega)": st.column_config.NumberColumn(format="%.2f"),
            "Γ (Gamma)": st.column_config.NumberColumn(format="%.4f"),
            "SPX Δ": st.column_config.NumberColumn(format="%.2f"),
        },
    )

    _sel_data = sel if isinstance(sel, dict) else (sel.to_dict() if hasattr(sel, "to_dict") else {})
    _sel_rows = (_sel_data.get("selection") or {}).get("rows") or []
    if not _sel_rows:
        st.caption("Select an option row to see Buy / Sell / Roll actions.")
        _render_roll_dialog(adapter=adapter, prefill_order_fn=prefill_order_fn)
        return

    _row_idx = int(_sel_rows[0])
    if _row_idx < 0 or _row_idx >= len(opt_rows):
        return

    p = opt_rows[_row_idx]["_pos"]
    underlying = p.underlying or p.symbol
    strike = p.strike
    right = (p.option_type or "").upper()
    expiry = p.expiration
    qty = max(1, abs(int(float(p.quantity or 1))))
    is_short = float(p.quantity or 0.0) < 0

    st.markdown(
        f"**Selected:** {underlying} {right} {strike} exp {expiry} "
        f"({'Short' if is_short else 'Long'} {qty}x)"
    )
    cols = st.columns(4)
    with cols[0]:
        if st.button("📈 Buy", key=f"pos_buy_sel_{_row_idx}", use_container_width=True):
            _stage_position_order(p, "BUY", qty, prefill_order_fn=prefill_order_fn)
            st.rerun()
    with cols[1]:
        if st.button("📉 Sell", key=f"pos_sell_sel_{_row_idx}", use_container_width=True):
            _stage_position_order(p, "SELL", qty, prefill_order_fn=prefill_order_fn)
            st.rerun()
    with cols[2]:
        if st.button("🔄 Roll +7d", key=f"pos_roll_sel_{_row_idx}", use_container_width=True):
            st.session_state["roll_picker_position"] = {
                "underlying": str(getattr(p, "underlying", "") or getattr(p, "symbol", "")),
                "symbol": str(getattr(p, "symbol", "") or ""),
                "expiration": getattr(p, "expiration", None),
                "strike": float(getattr(p, "strike", 0.0) or 0.0),
                "option_type": str(getattr(p, "option_type", "") or "").upper(),
                "quantity": float(getattr(p, "quantity", 0.0) or 0.0),
                "broker_id": getattr(p, "broker_id", None),
            }
            st.session_state["roll_picker_default_days"] = 7
            st.session_state["roll_picker_open"] = True
            st.rerun()
    with cols[3]:
        if st.button("🔄 Roll +30d", key=f"pos_roll30_sel_{_row_idx}", use_container_width=True):
            st.session_state["roll_picker_position"] = {
                "underlying": str(getattr(p, "underlying", "") or getattr(p, "symbol", "")),
                "symbol": str(getattr(p, "symbol", "") or ""),
                "expiration": getattr(p, "expiration", None),
                "strike": float(getattr(p, "strike", 0.0) or 0.0),
                "option_type": str(getattr(p, "option_type", "") or "").upper(),
                "quantity": float(getattr(p, "quantity", 0.0) or 0.0),
                "broker_id": getattr(p, "broker_id", None),
            }
            st.session_state["roll_picker_default_days"] = 30
            st.session_state["roll_picker_open"] = True
            st.rerun()

    _render_roll_dialog(adapter=adapter, prefill_order_fn=prefill_order_fn)


def _run_async(coro):
    import asyncio
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result(timeout=40)


def _render_roll_dialog(adapter: Any = None, prefill_order_fn: Optional[Callable[..., bool]] = None) -> None:
    if not st.session_state.get("roll_picker_open"):
        return

    position = st.session_state.get("roll_picker_position")
    if position is None:
        st.session_state["roll_picker_open"] = False
        return

    if isinstance(position, dict):
        underlying = str(position.get("underlying") or position.get("symbol") or "").upper().lstrip("/")
        current_exp = position.get("expiration")
        current_strike = float(position.get("strike", 0.0) or 0.0)
        current_right = str(position.get("option_type", "") or "").upper()
        qty = max(1, abs(int(float(position.get("quantity", 1) or 1))))
        is_short = float(position.get("quantity", 0.0) or 0.0) < 0
        position_broker_id = position.get("broker_id")
    else:
        underlying = str(getattr(position, "underlying", "") or getattr(position, "symbol", "")).upper().lstrip("/")
        current_exp = getattr(position, "expiration", None)
        current_strike = float(getattr(position, "strike", 0.0) or 0.0)
        current_right = str(getattr(position, "option_type", "") or "").upper()
        qty = max(1, abs(int(float(getattr(position, "quantity", 1) or 1))))
        is_short = float(getattr(position, "quantity", 0.0) or 0.0) < 0
        position_broker_id = getattr(position, "broker_id", None)

    default_days = int(st.session_state.get("roll_picker_default_days", 7) or 7)

    @st.dialog("🔄 Roll Option", width="large")
    def _roll_dialog() -> None:
        st.caption(f"Current: {underlying} {current_right} {current_strike} exp {current_exp} qty {qty}")
        if adapter is None:
            st.warning("Adapter unavailable — cannot load expirations for roll.")
            if st.button("Close", key="roll_picker_close_no_adapter"):
                st.session_state["roll_picker_open"] = False
                st.rerun()
            return

        dte_min = max(0, default_days - 5)
        dte_max = min(365, default_days + 90)
        exp_key = f"roll_picker_exp_{underlying}_{dte_min}_{dte_max}"
        chain_key = f"roll_picker_chain_{underlying}"

        if st.button("Load expirations", key="roll_picker_load_exp"):
            try:
                exp_rows = _run_async(
                    adapter.fetch_option_expirations_tws(
                        underlying=underlying,
                        dte_min=int(dte_min),
                        dte_max=int(dte_max),
                    )
                )
                st.session_state[exp_key] = exp_rows or []
            except Exception as exc:
                st.error(f"Could not load expirations: {exc}")

        exp_rows = st.session_state.get(exp_key, [])
        if not exp_rows:
            st.info("Load expirations to pick a roll target.")
            if st.button("Close", key="roll_picker_close_empty"):
                st.session_state["roll_picker_open"] = False
                st.rerun()
            return

        labels = [f"{r.get('expiry')} ({r.get('dte')}D)" for r in exp_rows]
        sel_label = st.selectbox("Target expiry", labels, key="roll_picker_expiry_select")
        sel_exp = exp_rows[labels.index(sel_label)].get("expiry")

        if st.button("Load strikes", key="roll_picker_load_chain"):
            try:
                rows = _run_async(
                    adapter.fetch_option_chain_matrix_tws(
                        underlying=underlying,
                        expiry=str(sel_exp),
                        atm_price=float(current_strike if current_strike > 0 else 0.0),
                        strikes_each_side=15,
                    )
                )
                st.session_state[chain_key] = rows or []
            except Exception as exc:
                st.error(f"Could not load strikes: {exc}")

        chain_rows = st.session_state.get(chain_key, [])
        strikes = sorted({float(r.get("strike")) for r in chain_rows if r.get("strike") is not None})
        if not strikes:
            st.info("Load strikes to choose target strike/right.")
            if st.button("Close", key="roll_picker_close_no_strikes"):
                st.session_state["roll_picker_open"] = False
                st.rerun()
            return

        c1, c2 = st.columns(2)
        with c1:
            target_right = st.selectbox("Target right", ["CALL", "PUT"], index=0 if current_right == "CALL" else 1)
        with c2:
            default_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - current_strike)) if current_strike else 0
            target_strike = st.selectbox("Target strike", strikes, index=default_idx)

        close_action = "BUY" if is_short else "SELL"
        open_action = "SELL" if is_short else "BUY"
        open_conid = None
        for _r in chain_rows:
            try:
                _stk = float(_r.get("strike"))
                _right = str(_r.get("right") or "").upper()
            except Exception:
                continue
            if abs(_stk - float(target_strike)) < 1e-9 and _right.startswith(target_right[:1]):
                open_conid = _r.get("conid") or _r.get("conId") or _r.get("contract_id")
                break

        if st.button("Create Roll Draft", type="primary", key="roll_picker_create"):
            legs = [
                {
                    "action": close_action,
                    "symbol": underlying,
                    "qty": qty,
                    "instrument_type": "Option",
                    "strike": current_strike,
                    "right": current_right if current_right in ("CALL", "PUT") else target_right,
                    "expiry": current_exp,
                    "conid": position_broker_id,
                },
                {
                    "action": open_action,
                    "symbol": underlying,
                    "qty": qty,
                    "instrument_type": "Option",
                    "strike": float(target_strike),
                    "right": target_right,
                    "expiry": str(sel_exp),
                    "conid": open_conid,
                },
            ]
            rationale = (
                f"Roll {underlying}: {close_action} {qty}x {current_right}{current_strike} {current_exp} → "
                f"{open_action} {qty}x {target_right}{target_strike} {sel_exp}"
            )
            if callable(prefill_order_fn):
                prefill_order_fn(legs=legs, source_label=f"roll {underlying}", rationale=rationale)
            else:
                st.session_state["ob_prefill_data"] = {
                    "leg_count": 2,
                    "legs": legs,
                    "rationale": rationale,
                    "reset_approved": True,
                }
            st.session_state["roll_picker_open"] = False
            st.rerun()

        if st.button("Cancel", key="roll_picker_cancel"):
            st.session_state["roll_picker_open"] = False
            st.rerun()

    _roll_dialog()


def _stage_position_order(
    position: Any,
    action: str,
    qty: int,
    prefill_order_fn: Optional[Callable[..., bool]] = None,
) -> None:
    """Pre-fill OrderBuilder session state for a simple Buy/Sell of an existing position."""
    right = (position.option_type or "").upper()
    expiry_date = position.expiration

    legs = [{
        "action": action,
        "symbol": position.underlying or position.symbol,
        "qty": qty,
        "instrument_type": "Option",
        "strike": float(position.strike) if position.strike else None,
        "right": right if right in ("CALL", "PUT") else None,
        "expiry": expiry_date,
        "conid": getattr(position, "broker_id", None),
    }]
    rationale = f"{action} {qty}x {position.symbol} @ {position.strike} {right} exp {expiry_date}"
    if callable(prefill_order_fn):
        prefill_order_fn(legs=legs, source_label=f"position {position.symbol}", rationale=rationale)
        return

    st.session_state["ob_prefill_data"] = {
        "leg_count": 1,
        "legs": legs,
        "rationale": rationale,
        "reset_approved": True,
    }
    st.session_state["ob_force_expand"] = True
    st.toast(f"📋 {action} order pre-filled for {position.symbol}", icon="✅")


def _stage_roll_order(
    position: Any,
    days_forward: int = 7,
    prefill_order_fn: Optional[Callable[..., bool]] = None,
) -> None:
    """Pre-fill OrderBuilder with a roll: close current + open at later expiry.

    Roll = opposite action to close current + same direction at new expiry.
    Default: +7 days from current expiration.
    """
    right = (position.option_type or "").upper()
    qty = abs(int(position.quantity))
    is_short = float(position.quantity) < 0
    expiry_date = position.expiration
    underlying = position.underlying or position.symbol

    # Close leg: opposite of current position
    close_action = "BUY" if is_short else "SELL"
    # Open leg: same direction as current position at new expiry
    open_action = "SELL" if is_short else "BUY"

    new_expiry = None
    if expiry_date:
        new_expiry = expiry_date + timedelta(days=days_forward)

    legs = [
        {
            "action": close_action,
            "symbol": underlying,
            "qty": qty,
            "instrument_type": "Option",
            "strike": float(position.strike) if position.strike else None,
            "right": right if right in ("CALL", "PUT") else None,
            "expiry": expiry_date,
        },
        {
            "action": open_action,
            "symbol": underlying,
            "qty": qty,
            "instrument_type": "Option",
            "strike": float(position.strike) if position.strike else None,
            "right": right if right in ("CALL", "PUT") else None,
            "expiry": new_expiry,
        },
    ]
    rationale = (
        f"Roll {underlying} {right} {position.strike}: "
        f"{close_action} exp {expiry_date} → {open_action} exp {new_expiry} (+{days_forward}d)"
    )
    if callable(prefill_order_fn):
        prefill_order_fn(legs=legs, source_label=f"roll {underlying}", rationale=rationale)
        return

    st.session_state["ob_prefill_data"] = {
        "leg_count": 2,
        "legs": legs,
        "rationale": rationale,
        "reset_approved": True,
    }
    st.session_state["ob_force_expand"] = True
    st.toast(f"🔄 Roll order pre-filled: {underlying} {right} {position.strike} +{days_forward}d", icon="✅")
