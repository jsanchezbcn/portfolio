from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

import pandas as pd
import streamlit as st

LOGGER = logging.getLogger(__name__)


def _run_async(coro):
    """Run an async coroutine safely from any thread (Streamlit-safe).

    Always delegates to a fresh thread so ``asyncio.run()`` never hits an
    already-running loop (Tornado/Streamlit keep a loop in the main thread).
    """
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result(timeout=50)


def _safe_float(v: Any) -> float | None:
    try:
        if v in (None, "", "N/A"):
            return None
        return float(v)
    except Exception:
        return None


def _extract_side_df(rows: list[dict[str, Any]], right: str) -> pd.DataFrame:
    side = [r for r in rows if str(r.get("right") or "").upper().startswith(right)]
    data = []
    for r in side:
        data.append(
            {
                "Strike": _safe_float(r.get("strike")),
                "Bid": _safe_float(r.get("bid")),
                "Ask": _safe_float(r.get("ask")),
                "Mid": _safe_float(r.get("mid")),
                "Δ": _safe_float(r.get("delta")),
                "Θ": _safe_float(r.get("theta")),
                "IV": _safe_float(r.get("iv")),
                "_conid": r.get("conid") or r.get("conId") or r.get("contract_id"),
            }
        )
    df = pd.DataFrame(data)
    if df.empty:
        return df
    return df.sort_values("Strike", ascending=False).reset_index(drop=True)


def render_options_book(
    *,
    adapter: Any,
    summary: dict[str, Any],
    prefill_order_fn: Callable[..., bool],
    symbols: Optional[list[str]] = None,
) -> None:
    st.subheader("📚 Options Book")

    book_symbols = symbols or ["/ES", "ES", "MES", "SPY", "QQQ"]
    selected_symbol = st.selectbox("Underlying", options=book_symbols, key="opt_book_symbol")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        min_dte = st.number_input("Min DTE", min_value=0, max_value=365, value=0, key="opt_book_min_dte")
    with c2:
        max_dte = st.number_input("Max DTE", min_value=1, max_value=730, value=120, key="opt_book_max_dte")
    with c3:
        strikes_each_side = st.slider("Strikes each side", 2, 20, 8, key="opt_book_strikes")
    with c4:
        if st.button("🔄 Load Expirations", key="opt_book_load_exps", use_container_width=True):
            try:
                base = str(selected_symbol).upper().lstrip("/")
                rows = _run_async(
                    adapter.fetch_option_expirations_tws(
                        underlying=base,
                        dte_min=int(min_dte),
                        dte_max=int(max_dte),
                    )
                )
                st.session_state[f"opt_book_exps_{selected_symbol}"] = rows or []
            except Exception as exc:
                LOGGER.warning("Options-book expirations failed for %s: %s", selected_symbol, exc)
                st.warning(f"Could not load expirations: {exc}")

    exp_rows = st.session_state.get(f"opt_book_exps_{selected_symbol}", [])
    if not exp_rows:
        st.caption("Load expirations to open the options book tabs.")
        return

    exp_rows = [r for r in exp_rows if r.get("expiry")]
    exp_rows = sorted(exp_rows, key=lambda r: str(r.get("expiry") or ""))
    if not exp_rows:
        st.caption("No expirations returned.")
        return

    labels = [f"{r.get('expiry')} ({r.get('dte')}D)" for r in exp_rows]
    tabs = st.tabs(labels)

    for tab_i, tab in enumerate(tabs):
        row = exp_rows[tab_i]
        expiry = str(row.get("expiry"))
        chain_key = f"opt_book_chain_{selected_symbol}_{expiry}_{int(strikes_each_side)}"
        with tab:
            if st.button(f"📊 Load Chain {expiry}", key=f"opt_book_load_chain_{selected_symbol}_{expiry}"):
                try:
                    base = str(selected_symbol).upper().lstrip("/")
                    atm_guess = float(summary.get("spx_price", 0.0) or 0.0)
                    matrix_rows = _run_async(
                        adapter.fetch_option_chain_matrix_tws(
                            underlying=base,
                            expiry=expiry,
                            atm_price=atm_guess,
                            strikes_each_side=int(strikes_each_side),
                        )
                    )
                    st.session_state[chain_key] = matrix_rows or []
                except Exception as exc:
                    LOGGER.warning("Options-book chain load failed for %s %s: %s", selected_symbol, expiry, exc)
                    st.warning(f"Could not load chain: {exc}")

            chain_rows = st.session_state.get(chain_key, [])
            if not chain_rows:
                st.caption("Load chain for this expiration tab.")
                continue

            call_df = _extract_side_df(chain_rows, "C")
            put_df = _extract_side_df(chain_rows, "P")
            left, right = st.columns(2)

            with left:
                st.markdown("**Calls**")
                if call_df.empty:
                    st.caption("No call rows.")
                else:
                    _call_conids: dict[float, Any] = {}
                    for _, _row in call_df.iterrows():
                        _stk = _safe_float(_row.get("Strike"))
                        if _stk is not None:
                            _call_conids[float(_stk)] = _row.get("_conid")
                    sel_c = st.dataframe(
                        call_df.drop(columns=["_conid"], errors="ignore"),
                        use_container_width=True,
                        hide_index=True,
                        on_select="rerun",
                        selection_mode="single-row",
                        key=f"opt_book_calls_{selected_symbol}_{expiry}",
                    )
                    sel_rows = ((sel_c if isinstance(sel_c, dict) else {}).get("selection") or {}).get("rows") or []
                    if sel_rows:
                        strike = float(call_df.iloc[int(sel_rows[0])]["Strike"])
                        conid = _call_conids.get(float(strike))
                        a, b = st.columns(2)
                        with a:
                            if st.button("Buy Call", key=f"opt_book_buy_call_{selected_symbol}_{expiry}_{strike}"):
                                prefill_order_fn(
                                    legs=[{
                                        "action": "BUY",
                                        "symbol": str(selected_symbol).upper().lstrip("/"),
                                        "qty": 1,
                                        "instrument_type": "Option",
                                        "strike": strike,
                                        "right": "CALL",
                                        "expiry": expiry,
                                        "conid": conid,
                                    }],
                                    source_label=f"options book {selected_symbol} C{strike} {expiry}",
                                    rationale="Selected from options book",
                                )
                                st.rerun()
                        with b:
                            if st.button("Sell Call", key=f"opt_book_sell_call_{selected_symbol}_{expiry}_{strike}"):
                                prefill_order_fn(
                                    legs=[{
                                        "action": "SELL",
                                        "symbol": str(selected_symbol).upper().lstrip("/"),
                                        "qty": 1,
                                        "instrument_type": "Option",
                                        "strike": strike,
                                        "right": "CALL",
                                        "expiry": expiry,
                                        "conid": conid,
                                    }],
                                    source_label=f"options book {selected_symbol} C{strike} {expiry}",
                                    rationale="Selected from options book",
                                )
                                st.rerun()

            with right:
                st.markdown("**Puts**")
                if put_df.empty:
                    st.caption("No put rows.")
                else:
                    _put_conids: dict[float, Any] = {}
                    for _, _row in put_df.iterrows():
                        _stk = _safe_float(_row.get("Strike"))
                        if _stk is not None:
                            _put_conids[float(_stk)] = _row.get("_conid")
                    sel_p = st.dataframe(
                        put_df.drop(columns=["_conid"], errors="ignore"),
                        use_container_width=True,
                        hide_index=True,
                        on_select="rerun",
                        selection_mode="single-row",
                        key=f"opt_book_puts_{selected_symbol}_{expiry}",
                    )
                    sel_rows = ((sel_p if isinstance(sel_p, dict) else {}).get("selection") or {}).get("rows") or []
                    if sel_rows:
                        strike = float(put_df.iloc[int(sel_rows[0])]["Strike"])
                        conid = _put_conids.get(float(strike))
                        a, b = st.columns(2)
                        with a:
                            if st.button("Buy Put", key=f"opt_book_buy_put_{selected_symbol}_{expiry}_{strike}"):
                                prefill_order_fn(
                                    legs=[{
                                        "action": "BUY",
                                        "symbol": str(selected_symbol).upper().lstrip("/"),
                                        "qty": 1,
                                        "instrument_type": "Option",
                                        "strike": strike,
                                        "right": "PUT",
                                        "expiry": expiry,
                                        "conid": conid,
                                    }],
                                    source_label=f"options book {selected_symbol} P{strike} {expiry}",
                                    rationale="Selected from options book",
                                )
                                st.rerun()
                        with b:
                            if st.button("Sell Put", key=f"opt_book_sell_put_{selected_symbol}_{expiry}_{strike}"):
                                prefill_order_fn(
                                    legs=[{
                                        "action": "SELL",
                                        "symbol": str(selected_symbol).upper().lstrip("/"),
                                        "qty": 1,
                                        "instrument_type": "Option",
                                        "strike": strike,
                                        "right": "PUT",
                                        "expiry": expiry,
                                        "conid": conid,
                                    }],
                                    source_label=f"options book {selected_symbol} P{strike} {expiry}",
                                    rationale="Selected from options book",
                                )
                                st.rerun()
