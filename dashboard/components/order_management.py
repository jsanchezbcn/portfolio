"""dashboard/components/order_management.py — Open Orders tab.

Displays all open/pending IBKR orders for the selected account with
inline Cancel and Modify actions.

SAFETY CONTRACT
===============
- Cancel requires an explicit confirmation checkbox before the DELETE is issued.
- Modify requires the user to enter a new price/qty and confirm before POST.
- No action is ever auto-triggered; every mutation is user-initiated.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import streamlit as st

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# IBKR order status groups
# ---------------------------------------------------------------------------
_ACTIVE_STATUSES = {
    "PreSubmitted",
    "Submitted",
    "PendingSubmit",
    "PendingCancel",
    "Held",
    "Inactive",
}

_STATUS_COLOR = {
    "Filled": "green",
    "Cancelled": "red",
    "Rejected": "red",
    "PreSubmitted": "blue",
    "Submitted": "blue",
    "PendingSubmit": "orange",
    "PendingCancel": "orange",
    "Held": "orange",
    "Inactive": "grey",
}


def _status_badge(status: str) -> str:
    color = _STATUS_COLOR.get(status, "grey")
    return f":{color}[{status}]"


# ---------------------------------------------------------------------------
# IBKR API helpers
# ---------------------------------------------------------------------------


def _fetch_open_orders(client: Any, account_id: str) -> list[dict]:
    """Call GET /v1/api/iserver/account/orders and return order list."""
    try:
        url = f"{client.base_url}/v1/api/iserver/account/orders"
        resp = client.session.get(
            url,
            params={"accountId": account_id, "filters": "active"},
            verify=False,
            timeout=10,
        )
        if resp.status_code == 200:
            body = resp.json()
            orders = body.get("orders", []) if isinstance(body, dict) else body
            return [o for o in (orders if isinstance(orders, list) else [])]
        logger.warning(
            "fetch_open_orders HTTP %d: %s", resp.status_code, resp.text[:200]
        )
        return []
    except Exception as exc:
        logger.warning("fetch_open_orders failed: %s", exc)
        return []


def _cancel_order(client: Any, account_id: str, order_id: str) -> tuple[bool, str]:
    """Issue DELETE /v1/api/iserver/account/{acctId}/order/{orderId}.

    Returns (success, message).
    """
    try:
        url = f"{client.base_url}/v1/api/iserver/account/{account_id}/order/{order_id}"
        resp = client.session.delete(url, verify=False, timeout=10)
        if resp.status_code in (200, 202):
            body = resp.json() if resp.text else {}
            msg = body.get("msg") or body.get("message") or "Cancellation submitted."
            return True, str(msg)
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return False, f"Request failed: {exc}"


def _modify_order(
    client: Any,
    account_id: str,
    order_id: str,
    *,
    new_price: Optional[float],
    new_quantity: Optional[float],
) -> tuple[bool, str]:
    """Issue POST /v1/api/iserver/account/{acctId}/order/{orderId} with updated fields.

    Returns (success, message).
    """
    payload: dict[str, Any] = {}
    if new_price is not None:
        payload["price"] = new_price
    if new_quantity is not None:
        payload["quantity"] = new_quantity

    if not payload:
        return False, "No changes to submit."

    try:
        url = f"{client.base_url}/v1/api/iserver/account/{account_id}/order/{order_id}"
        resp = client.session.post(url, json=payload, verify=False, timeout=10)
        if resp.status_code in (200, 202):
            body = resp.json() if resp.text else {}
            # IBKR may return a list of confirmation questions
            if isinstance(body, list):
                # Auto-confirm if it's a soft warning (same pattern as submit)
                order_replies = body
                all_ok = True
                for reply_obj in order_replies:
                    reply_id = reply_obj.get("id") or reply_obj.get("order_id", "")
                    confirm_url = (
                        f"{client.base_url}/v1/api/iserver/reply/{reply_id}"
                    )
                    confirm_resp = client.session.post(
                        confirm_url,
                        json={"confirmed": True},
                        verify=False,
                        timeout=10,
                    )
                    if confirm_resp.status_code not in (200, 202):
                        all_ok = False
                if all_ok:
                    return True, "Modification submitted and confirmed."
                return False, "Modification submitted but confirmation failed."
            msg = body.get("msg") or body.get("message") or "Modification submitted."
            return True, str(msg)
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return False, f"Request failed: {exc}"


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


def render_order_management(
    ibkr_gateway_client: Any,
    account_id: str,
) -> None:
    """Render the Open Orders management panel.

    Parameters
    ----------
    ibkr_gateway_client:
        IBKRClient instance (has ``.base_url`` and ``.session``).
    account_id:
        IBKR account ID to scope the order list.
    """
    st.subheader("📋 Open Orders")

    if ibkr_gateway_client is None:
        st.warning("IBKR gateway client unavailable — cannot fetch orders.")
        return

    # ── Refresh controls ─────────────────────────────────────────────
    col_refresh, col_account, _ = st.columns([1, 2, 3])
    with col_refresh:
        if st.button("🔄 Refresh Orders", key="om_refresh"):
            st.session_state.pop("om_orders_cache", None)
            st.session_state.pop("om_cache_account", None)
            st.rerun()

    with col_account:
        st.caption(f"Account: **{account_id}**")

    # ── Fetch (session-state cache) ───────────────────────────────────
    cached_account = st.session_state.get("om_cache_account")
    if (
        "om_orders_cache" not in st.session_state
        or cached_account != account_id
    ):
        with st.spinner("Fetching open orders…"):
            orders = _fetch_open_orders(ibkr_gateway_client, account_id)
        st.session_state["om_orders_cache"] = orders
        st.session_state["om_cache_account"] = account_id
    else:
        orders = st.session_state["om_orders_cache"]

    if not orders:
        st.info("No open orders for this account.")
        return

    st.caption(f"Showing **{len(orders)}** order(s)")

    # ── Per-order rows ────────────────────────────────────────────────
    for idx, order in enumerate(orders):
        order_id = str(order.get("orderId") or order.get("order_id") or "")
        symbol = order.get("ticker") or order.get("symbol") or order.get("conidex") or "—"
        side = str(order.get("side") or order.get("sideDescription") or "").upper()
        qty = order.get("totalSize") or order.get("remainingQuantity") or order.get("quantity") or 0
        filled = order.get("filledQuantity") or order.get("filled") or 0
        order_type = str(order.get("orderType") or order.get("order_type") or "MKT")
        price = order.get("price") or order.get("lmtPrice") or None
        status = str(order.get("status") or order.get("order_status") or "Unknown")
        description = order.get("description1") or order.get("description") or ""

        # Skip already-terminal orders from the "active" filter response
        if status in {"Filled", "Cancelled", "Rejected"}:
            continue

        with st.container(border=True):
            # ── Header row ──────────────────────────────────────────
            h_col1, h_col2, h_col3, h_col4 = st.columns([3, 2, 2, 2])
            with h_col1:
                side_color = "green" if side in ("BUY", "B") else "red"
                st.markdown(
                    f"**{symbol}** &nbsp; :{side_color}[{side}] &nbsp; qty **{qty}**"
                    + (f" (filled {filled})" if filled else ""),
                    unsafe_allow_html=True,
                )
                if description:
                    st.caption(description)
            with h_col2:
                price_str = f"@ {price:.4f}" if price is not None else "@ MKT"
                st.markdown(f"`{order_type}` {price_str}")
            with h_col3:
                st.markdown(_status_badge(status))
                if order_id:
                    st.caption(f"Order ID: {order_id}")
            with h_col4:
                # Placeholder — action buttons in expanders below
                pass

            # ── Action expanders ────────────────────────────────────
            action_col1, action_col2 = st.columns(2)

            # ── Cancel ─────────────────────────────────────────────
            with action_col1:
                with st.expander("🗑️ Cancel Order"):
                    st.warning(
                        f"Cancel **{side} {qty}x {symbol}** (Order {order_id})?"
                    )
                    confirmed = st.checkbox(
                        "I confirm I want to cancel this order",
                        key=f"om_cancel_confirm_{idx}_{order_id}",
                    )
                    if st.button(
                        "Submit Cancellation",
                        key=f"om_cancel_btn_{idx}_{order_id}",
                        disabled=not confirmed,
                        type="primary",
                    ):
                        ok, msg = _cancel_order(
                            ibkr_gateway_client, account_id, order_id
                        )
                        if ok:
                            st.success(f"✅ {msg}")
                            # Bust cache so next render re-fetches
                            st.session_state.pop("om_orders_cache", None)
                        else:
                            st.error(f"❌ {msg}")

            # ── Modify ─────────────────────────────────────────────
            with action_col2:
                with st.expander("✏️ Modify Order"):
                    st.caption("Leave a field at 0 to leave it unchanged.")
                    new_price_input = st.number_input(
                        "New Limit Price",
                        min_value=0.0,
                        value=float(price) if price is not None else 0.0,
                        step=0.01,
                        format="%.4f",
                        key=f"om_mod_price_{idx}_{order_id}",
                    )
                    new_qty_input = st.number_input(
                        "New Quantity",
                        min_value=0.0,
                        value=float(qty) if qty else 0.0,
                        step=1.0,
                        format="%.0f",
                        key=f"om_mod_qty_{idx}_{order_id}",
                    )
                    mod_confirmed = st.checkbox(
                        "I confirm the new parameters",
                        key=f"om_mod_confirm_{idx}_{order_id}",
                    )
                    if st.button(
                        "Submit Modification",
                        key=f"om_mod_btn_{idx}_{order_id}",
                        disabled=not mod_confirmed,
                    ):
                        _new_price = new_price_input if new_price_input > 0 else None
                        _new_qty = new_qty_input if new_qty_input > 0 else None
                        ok, msg = _modify_order(
                            ibkr_gateway_client,
                            account_id,
                            order_id,
                            new_price=_new_price,
                            new_quantity=_new_qty,
                        )
                        if ok:
                            st.success(f"✅ {msg}")
                            st.session_state.pop("om_orders_cache", None)
                        else:
                            st.error(f"❌ {msg}")

    # ── Summary strip ─────────────────────────────────────────────────
    st.divider()
    active_count = sum(
        1
        for o in orders
        if str(o.get("status") or o.get("order_status") or "") not in {"Filled", "Cancelled", "Rejected"}
    )
    st.caption(
        f"📊 {active_count} active order(s) shown. "
        "Use **🔄 Refresh Orders** to sync with IBKR after any action."
    )
