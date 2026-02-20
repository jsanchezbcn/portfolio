"""dashboard/components/flatten_risk.py — Flatten Risk panic button (T069-T073).

Renders:
- "Flatten Risk" prominent button accessible from main panel
- Confirmation dialog showing all buy-to-close orders + estimated margin release
- "Confirm Flatten" → submit all orders simultaneously via asyncio.gather equivalent
- "Cancel" → no orders sent
- After confirms, remaining unfilled orders stay in blotter (T073)
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any

import streamlit as st

LOGGER = logging.getLogger(__name__)

# Session state keys
_SS_PENDING_FLATTEN = "flatten_pending_orders"
_SS_FLATTEN_STATUS  = "flatten_status"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def render_flatten_risk(
    *,
    execution_engine: Any,
    account_id: str,
    positions: list[Any],
) -> None:
    """Render the Flatten Risk button and confirmation dialog.

    Args:
        execution_engine: ``ExecutionEngine`` instance (or None when broker unavailable).
        account_id:       Active IBKR account ID.
        positions:        Current ``list[UnifiedPosition]`` from adapter.
    """
    st.subheader("⚡ Flatten Risk")

    if execution_engine is None:
        st.warning("Flatten Risk unavailable — broker not connected.")
        return

    # ── Status banner ──────────────────────────────────────────────────────
    _render_status_banner()

    # ── Flatten button (T069) ──────────────────────────────────────────────
    pending: list[Any] | None = st.session_state.get(_SS_PENDING_FLATTEN)

    if pending is None:
        # Normal state: show the trigger button
        if st.button(
            "🚨 Flatten Risk — Buy to Close All Short Options",
            type="primary",
            key="flatten_risk_trigger_btn",
            help="Generates buy-to-close market orders for all short option positions. "
                 "You will be shown a confirmation screen before any orders are sent.",
        ):
            orders = execution_engine.flatten_risk(positions)
            if not orders:
                st.info("✅ No short option positions to close.")
            else:
                st.session_state[_SS_PENDING_FLATTEN] = orders
                st.session_state[_SS_FLATTEN_STATUS] = None
                st.rerun()
        return

    # ── Confirmation dialog (T070) ─────────────────────────────────────────
    _render_confirmation_dialog(
        pending_orders=pending,
        execution_engine=execution_engine,
        account_id=account_id,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _render_status_banner() -> None:
    """Show last flatten outcome if any."""
    status = st.session_state.get(_SS_FLATTEN_STATUS)
    if status == "success":
        st.success("✅ Flatten orders submitted. Check Open Orders panel for fill status.")
    elif status == "cancelled":
        st.info("ℹ️ Flatten cancelled — no orders were sent.")
    elif isinstance(status, str) and status.startswith("error:"):
        st.error(f"❌ Flatten error: {status[6:]}")


def _render_confirmation_dialog(
    *,
    pending_orders: list[Any],
    execution_engine: Any,
    account_id: str,
) -> None:
    """Show order table + Confirm / Cancel buttons (T070)."""
    st.warning(
        f"⚠️ You are about to submit **{len(pending_orders)} buy-to-close MARKET order(s)**. "
        "This cannot be undone once confirmed."
    )

    # Build summary table
    import pandas as pd
    rows = []
    for order in pending_orders:
        for leg in order.legs:
            rows.append({
                "Symbol": leg.symbol,
                "Action": leg.action.value if hasattr(leg.action, "value") else str(leg.action),
                "Qty": leg.quantity,
                "Type": order.order_type.value if hasattr(order.order_type, "value") else "MARKET",
                "Rationale": order.user_rationale or "Flatten Risk",
            })

    st.dataframe(pd.DataFrame(rows), use_container_width=True)

    # Estimated margin release (simplified: #orders × placeholder)
    est_margin_release = len(pending_orders) * 5_000  # rough heuristic
    st.caption(f"Estimated margin release: ~${est_margin_release:,.0f} (indicative only)")

    col_confirm, col_cancel = st.columns([1, 1])

    with col_confirm:
        if st.button(
            "✅ Confirm Flatten",
            type="primary",
            key="flatten_confirm_btn",
        ):
            _submit_flatten_batch(
                orders=pending_orders,
                execution_engine=execution_engine,
                account_id=account_id,
            )

    with col_cancel:
        if st.button("❌ Cancel", key="flatten_cancel_btn"):
            st.session_state.pop(_SS_PENDING_FLATTEN, None)
            st.session_state[_SS_FLATTEN_STATUS] = "cancelled"
            st.rerun()


def _submit_flatten_batch(
    *,
    orders: list[Any],
    execution_engine: Any,
    account_id: str,
) -> None:
    """Submit all flatten orders simultaneously using a thread pool (T071).

    Each fill is journaled with FLATTEN strategy tag and standard rationale (T072).
    Unfilled orders remain in blotter as PENDING (T073) — handled by
    render_order_management via execution_engine's in-memory order registry.
    """
    errors: list[str] = []

    def _submit_one(order: Any) -> None:
        try:
            execution_engine.submit(
                account_id=account_id,
                order=order,
                regime="FLATTEN",
            )
        except Exception as exc:
            errors.append(f"{getattr(order.legs[0], 'symbol', '?')}: {exc}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(orders)) as pool:
        futures = [pool.submit(_submit_one, o) for o in orders]
        for f in concurrent.futures.as_completed(futures, timeout=30):
            try:
                f.result()
            except Exception as exc:
                errors.append(str(exc))

    st.session_state.pop(_SS_PENDING_FLATTEN, None)

    if errors:
        st.session_state[_SS_FLATTEN_STATUS] = f"error:{'; '.join(errors[:3])}"
    else:
        st.session_state[_SS_FLATTEN_STATUS] = "success"

    st.rerun()
