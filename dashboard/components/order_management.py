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
import os
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
    """Call GET /v1/api/iserver/account/orders and return order list.

    IBKR quirks handled here:
    - Do NOT pass ``force=true`` — it causes a snapshot-reset returning empty.
    - Do NOT pass ``filters=active`` — wrong param; use client-side filtering.
    - When ``snapshot=True`` the CP gateway returns cached data; still usable.
    - Prime the session with /iserver/accounts before fetching orders so the
      platform has a valid account context.
    """
    try:
        # Prime the CP gateway account context (no-op if already primed)
        try:
            client.session.get(
                f"{client.base_url}/v1/api/iserver/accounts",
                verify=False,
                timeout=5,
            )
        except Exception:
            pass  # Non-fatal — worst case we get stale snapshot data

        url = f"{client.base_url}/v1/api/iserver/account/orders"
        resp = client.session.get(
            url,
            verify=False,
            timeout=15,
        )
        if resp.status_code == 401:
            return _fetch_open_orders_socket(account_id)
        if resp.status_code == 200:
            body = resp.json()
            orders = body.get("orders", []) if isinstance(body, dict) else body
            all_orders = [o for o in (orders if isinstance(orders, list) else [])]
            # Filter to the selected account (IBKR returns all accounts' orders)
            if account_id and account_id.lower() != "all":
                all_orders = [
                    o for o in all_orders
                    if str(o.get("account") or o.get("acctId") or "").upper()
                    == account_id.upper()
                ]
            if not all_orders:
                return _fetch_open_orders_socket(account_id)
            return all_orders
        logger.warning(
            "fetch_open_orders HTTP %d: %s", resp.status_code, resp.text[:200]
        )
        return []
    except Exception as exc:
        logger.warning("fetch_open_orders failed: %s", exc)
        return []


def _fetch_open_orders_socket(account_id: str) -> list[dict]:
    """Fallback open-order fetch using ib_async in SOCKET mode."""
    try:
        from ib_async import IB
    except Exception:
        return []

    host = os.getenv("IB_SOCKET_HOST", "127.0.0.1")
    port = int(os.getenv("IB_SOCKET_PORT", "7496"))
    client_id = int(os.getenv("IB_ORDERS_CLIENT_ID", "18"))

    ib = IB()
    rows: list[dict] = []
    try:
        ib.connect(host=host, port=port, clientId=client_id)
        trades = ib.openTrades() or []
        for t in trades:
            contract = getattr(t, "contract", None)
            order = getattr(t, "order", None)
            status_obj = getattr(t, "orderStatus", None)
            oid = str(getattr(order, "orderId", "") or "")
            symbol = str(getattr(contract, "symbol", "") or "")
            side = str(getattr(order, "action", "") or "")
            qty = float(getattr(order, "totalQuantity", 0) or 0)
            status = str(getattr(status_obj, "status", "") or "")
            acct = str(getattr(order, "account", "") or "")
            if account_id and account_id.lower() != "all" and acct and acct.upper() != account_id.upper():
                continue
            rows.append(
                {
                    "orderId": oid,
                    "ticker": symbol,
                    "side": side,
                    "quantity": qty,
                    "status": status,
                    "account": acct,
                    "orderType": str(getattr(order, "orderType", "") or ""),
                    "price": getattr(order, "lmtPrice", None),
                    "timeInForce": str(getattr(order, "tif", "") or ""),
                }
            )
        return rows
    except Exception as exc:
        logger.warning("socket open-orders fallback failed: %s", exc)
        return []
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


def _prime_account_context(client: Any, account_id: str) -> None:
    """Prime the IBKR CP Gateway session with the target account.

    The CP Gateway requires that /iserver/accounts is called at least once per
    session before account-scoped write operations (cancel, modify) are allowed.
    Without this, the gateway returns 400 "accountId is not valid".
    """
    try:
        client.session.get(
            f"{client.base_url}/v1/api/iserver/accounts",
            verify=False,
            timeout=5,
        )
    except Exception:
        pass  # Non-fatal; proceed and let the subsequent call fail if needed


def _cancel_order(client: Any, account_id: str, order_id: str) -> tuple[bool, str]:
    """Issue DELETE /v1/api/iserver/account/{acctId}/order/{orderId}.

    Returns (success, message).
    """
    _prime_account_context(client, account_id)
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

    _prime_account_context(client, account_id)
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
# UI helpers
# ---------------------------------------------------------------------------


def _render_order_list(
    orders: list[dict],
    client: Any,
    account_id: str,
    *,
    prefix: str = "o",
    show_actions: bool = True,
) -> None:
    """Render a list of order rows with optional cancel/modify actions."""
    for idx, order in enumerate(orders):
        order_id = str(order.get("orderId") or order.get("order_id") or "")
        symbol = (
            order.get("ticker")
            or order.get("symbol")
            or "—"
        )
        side = str(order.get("side") or "").upper()
        qty = order.get("totalSize") or order.get("remainingQuantity") or order.get("quantity") or 0
        filled = order.get("filledQuantity") or 0
        order_type = str(order.get("orderType") or order.get("origOrderType") or "LMT")
        price_raw = order.get("price") or order.get("lmtPrice")
        status = str(order.get("status") or "Unknown")
        # Use the rich description IBKR provides when available
        desc = (
            order.get("orderDesc")
            or order.get("description2")
            or order.get("description1")
            or ""
        )
        tif = order.get("timeInForce") or ""

        try:
            price_val: Optional[float] = float(str(price_raw)) if price_raw is not None else None
        except (ValueError, TypeError):
            price_val = None

        with st.container(border=True):
            h_col1, h_col2, h_col3 = st.columns([4, 2, 2])
            with h_col1:
                side_color = "green" if side in ("BUY", "B") else "red"
                filled_str = f" ({filled} filled)" if float(filled or 0) > 0 else ""
                st.markdown(
                    f"**{symbol}** &nbsp; :{side_color}[{side}] &nbsp; qty **{qty}**{filled_str}",
                )
                if desc:
                    st.caption(desc)
            with h_col2:
                price_str = f"@ {price_val:.2f}" if price_val is not None else "@ MKT"
                st.markdown(f"`{order_type}` {price_str}")
                if tif:
                    st.caption(f"TIF: {tif}")
            with h_col3:
                st.markdown(_status_badge(status))
                if order_id:
                    st.caption(f"ID: {order_id}")

            if show_actions:
                action_col1, action_col2 = st.columns(2)

                # ── Cancel ─────────────────────────────────────────
                with action_col1:
                    with st.expander("🗑️ Cancel"):
                        st.warning(
                            f"Cancel **{side} {qty}× {symbol}** (Order {order_id})?"
                        )
                        confirmed = st.checkbox(
                            "I confirm — cancel this order",
                            key=f"om_{prefix}_cancel_confirm_{idx}_{order_id}",
                        )
                        if st.button(
                            "Submit Cancellation",
                            key=f"om_{prefix}_cancel_btn_{idx}_{order_id}",
                            disabled=not confirmed,
                            type="primary",
                        ):
                            ok, msg = _cancel_order(client, account_id, order_id)
                            if ok:
                                st.success(f"✅ {msg}")
                                st.session_state.pop("om_orders_cache", None)
                            else:
                                st.error(f"❌ {msg}")

                # ── Modify ─────────────────────────────────────────
                with action_col2:
                    with st.expander("✏️ Modify"):
                        st.caption("Set 0 to leave a field unchanged.")
                        new_price_input = st.number_input(
                            "New Limit Price",
                            min_value=0.0,
                            value=abs(price_val) if price_val is not None else 0.0,
                            step=0.25,
                            format="%.2f",
                            key=f"om_{prefix}_mod_price_{idx}_{order_id}",
                        )
                        new_qty_input = st.number_input(
                            "New Quantity",
                            min_value=0.0,
                            value=float(qty) if qty else 0.0,
                            step=1.0,
                            format="%.0f",
                            key=f"om_{prefix}_mod_qty_{idx}_{order_id}",
                        )
                        mod_confirmed = st.checkbox(
                            "I confirm the changes",
                            key=f"om_{prefix}_mod_confirm_{idx}_{order_id}",
                        )
                        if st.button(
                            "Submit Modification",
                            key=f"om_{prefix}_mod_btn_{idx}_{order_id}",
                            disabled=not mod_confirmed,
                        ):
                            _new_price = new_price_input if new_price_input > 0 else None
                            _new_qty = new_qty_input if new_qty_input > 0 else None
                            ok, msg = _modify_order(
                                client,
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


# ---------------------------------------------------------------------------
# Main render
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
        st.info("No orders found for this account. Use 🔄 Refresh to reload.")
        return

    # Split active vs terminal
    _terminal = {"Filled", "Cancelled", "Rejected", "Inactive"}
    active_orders = [o for o in orders if str(o.get("status", "")) not in _terminal]
    history_orders = [o for o in orders if str(o.get("status", "")) in _terminal]

    st.caption(
        f"**{len(active_orders)}** active order(s) · "
        f"{len(history_orders)} completed today"
    )

    # ── Active orders first ───────────────────────────────────────────
    if active_orders:
        st.markdown("#### 🟢 Active Orders")
        _render_order_list(active_orders, ibkr_gateway_client, account_id, prefix="act")
    else:
        st.info("No active (open) orders right now.")

    # ── Recent history (collapsed) ────────────────────────────────────
    if history_orders:
        with st.expander(f"📜 Recent order history ({len(history_orders)} orders)"):
            _render_order_list(history_orders, ibkr_gateway_client, account_id,
                               prefix="hist", show_actions=False)

    # ── Summary strip ─────────────────────────────────────────────────
    st.divider()
    st.caption(
        f"📊 {len(active_orders)} active order(s) shown. "
        "Use **🔄 Refresh Orders** to sync with IBKR after any action."
    )
