"""dashboard/components/options_chain_viewer.py — Tastytrade-style options chain.

Layout (mirrors Tastytrade / ThinkorSwim chain format):
─────────────────────────────────────────────────────────────────────────────
 Expiration pills  ←  Jan 17  Feb 21  Mar 21  Apr 18  Jun 20  …
─────────────────────────────────────────────────────────────────────────────
 PUTS                                      STRIKE    CALLS
 OI   Vol  IV%   Θ    Δ   Ask  Bid  │  ×× PRICE  ××  │  Bid  Ask   Δ   Θ  IV%  Vol  OI
─────────────────────────────────────────────────────────────────────────────
Clicking a row fires a callback that pre-fills the order builder (or updates
a caller-owned session-state key).

Usage
-----
    from dashboard.components.options_chain_viewer import render_options_chain_viewer

    selected = render_options_chain_viewer(
        symbol="SPY",
        market_data_service=mds,
        adapter=ibkr_adapter,
        session_key_prefix="trade",   # session-state namespace
    )
    if selected:
        # selected = {"symbol": "SPY", "expiry": "2026-03-21",
        #              "strike": 550.0, "right": "CALL", "bid": 1.10, "ask": 1.15}
        stage_order_builder(selected)
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

import pandas as pd
import streamlit as st

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ATM_BG = "#1a3a1a"        # subtle green tint for ATM rows
_ITM_CALL_BG = "#0a1a2e"   # dark blue — in-the-money call side
_ITM_PUT_BG = "#2e1a0a"    # dark amber — in-the-money put side


def _dte(expiry_str: str) -> int:
    """Days to expiration from today."""
    try:
        exp = date.fromisoformat(expiry_str)
        return (exp - date.today()).days
    except Exception:
        return 0


def _fmt(val: Any, decimals: int = 2, prefix: str = "", suffix: str = "") -> str:
    try:
        return f"{prefix}{float(val):.{decimals}f}{suffix}"
    except (TypeError, ValueError):
        return "—"


def _sign_delta(val: Any, side: str) -> str:
    """Format delta with correct sign convention displayed to user."""
    try:
        v = float(val)
        return f"{v:+.3f}"
    except (TypeError, ValueError):
        return "—"


# ---------------------------------------------------------------------------
# Main renderer
# ---------------------------------------------------------------------------


def render_options_chain_viewer(
    symbol: str,
    market_data_service: Any,          # core.market_data.MarketDataService
    adapter: Any,                       # adapters.ibkr_adapter.IBKRAdapter
    session_key_prefix: str = "ocv",
    strikes_each_side: int = 8,
) -> Optional[dict]:
    """Render a full Tastytrade-style options chain for *symbol*.

    Returns
    -------
    dict | None
        ``None`` as long as no strike is selected.  When the user clicks a
        strike, returns::

            {
                "symbol": str,
                "expiry": str,          # ISO date, e.g. "2026-03-21"
                "strike": float,
                "right": str,           # "CALL" | "PUT"
                "bid": float | None,
                "ask": float | None,
                "mid": float | None,
                "delta": float | None,
                "iv": float | None,
                "theta": float | None,
            }
    """
    sk = session_key_prefix  # short alias for session-state keys

    # ── 1. Load expirations ───────────────────────────────────────────────
    exp_cache_key  = f"{sk}_expirations_{symbol}"
    chain_cache_key = f"{sk}_chain_{symbol}"
    sel_exp_key    = f"{sk}_selected_expiry"
    dte_min_key    = f"{sk}_dte_min"
    dte_max_key    = f"{sk}_dte_max"
    strikes_key    = f"{sk}_strikes_each_side"
    sel_result_key = f"{sk}_selected_strike"

    # ── DTE filter controls ───────────────────────────────────────────────
    fc1, fc2, fc3, fc4 = st.columns([2, 2, 2, 2])
    with fc1:
        dte_min = st.number_input(
            "Min DTE", min_value=0, max_value=365,
            value=st.session_state.get(dte_min_key, 0),
            key=f"{sk}_dte_min_input",
        )
        st.session_state[dte_min_key] = dte_min
    with fc2:
        dte_max = st.number_input(
            "Max DTE", min_value=1, max_value=730,
            value=st.session_state.get(dte_max_key, 120),
            key=f"{sk}_dte_max_input",
        )
        st.session_state[dte_max_key] = dte_max
    with fc3:
        strikes_each_side = st.slider(
            "Strikes each side", min_value=3, max_value=20,
            value=st.session_state.get(strikes_key, strikes_each_side),
            key=f"{sk}_strikes_slider",
        )
        st.session_state[strikes_key] = strikes_each_side
    with fc4:
        st.write("")  # spacer
        load_exp = st.button(
            "🔄 Load Expirations",
            key=f"{sk}_load_exp_btn",
            use_container_width=True,
        )

    if load_exp:
        if adapter is None:
            st.warning("No broker connection. Connect to IBKR first.")
        else:
            with st.spinner(f"Fetching {symbol} expirations…"):
                try:
                    import asyncio
                    loop = asyncio.new_event_loop()
                    rows = loop.run_until_complete(
                        adapter.fetch_option_expirations_tws(
                            underlying=symbol,
                            dte_min=int(dte_min),
                            dte_max=int(dte_max),
                        )
                    )
                    loop.close()
                    st.session_state[exp_cache_key] = rows or []
                    # Reset any previously selected expiry
                    st.session_state.pop(sel_exp_key, None)
                    st.session_state.pop(chain_cache_key, None)
                except Exception as exc:
                    logger.warning("Failed to fetch expirations for %s: %s", symbol, exc)
                    st.error(f"Could not fetch expirations: {exc}")

    exp_rows: list[dict] = st.session_state.get(exp_cache_key, [])

    if not exp_rows:
        st.info(
            f"Click **Load Expirations** to fetch available option expiration dates for **{symbol}**."
        )
        return None

    # ── 2. Expiration pills (Tastytrade style) ────────────────────────────
    exp_labels: list[str] = []
    exp_map: dict[str, str] = {}   # label → ISO date
    for row in sorted(exp_rows, key=lambda r: r.get("expiration", "")):
        iso = row.get("expiration", "")
        if not iso:
            continue
        dte_val = _dte(iso)
        try:
            label = f"{date.fromisoformat(iso).strftime('%b %d')} ({dte_val}d)"
        except Exception:
            label = iso
        exp_labels.append(label)
        exp_map[label] = iso

    if not exp_labels:
        st.warning("No expiration dates found in the loaded data.")
        return None

    prev_exp_label = st.session_state.get(sel_exp_key, exp_labels[0])
    if prev_exp_label not in exp_labels:
        prev_exp_label = exp_labels[0]

    selected_label = st.pills(
        "Expiration",
        exp_labels,
        default=prev_exp_label,
        key=f"{sk}_exp_pills",
        label_visibility="collapsed",
    )
    if selected_label is None:
        selected_label = exp_labels[0]
    st.session_state[sel_exp_key] = selected_label
    selected_expiry: str = exp_map[selected_label]

    # ── 3. Load chain for selected expiry ─────────────────────────────────
    chain_exp_key = f"{chain_cache_key}_{selected_expiry}"
    load_chain_btn = st.button(
        f"📊 Load Chain — {selected_label}",
        key=f"{sk}_load_chain_{selected_expiry}",
    )

    if load_chain_btn:
        _load_chain(
            symbol=symbol,
            expiry=selected_expiry,
            adapter=adapter,
            market_data_service=market_data_service,
            cache_key=chain_exp_key,
        )

    chain_data: list[dict] = st.session_state.get(chain_exp_key, [])

    if not chain_data:
        st.caption("Click **Load Chain** to fetch option data for this expiration.")
        return None

    # ── 4. Build butterfly-wing dataframe ─────────────────────────────────
    df, spot = _build_chain_df(chain_data, symbol, strikes_each_side)
    if df is None or df.empty:
        st.info("No chain data available for this expiration.")
        return None

    st.caption(
        f"**{symbol}** · Expiry: **{selected_label}** · "
        f"Underlying: **{_fmt(spot, 2, prefix='$')}** · "
        f"{len(df)} strikes shown"
    )

    # Style ATM row (closest strike to spot)
    _render_chain_table(df=df, spot=spot, sk=sk, selected_expiry=selected_expiry)

    # ── 5. Return selected strike if user clicked ─────────────────────────
    sel = st.session_state.get(sel_result_key)
    if sel and sel.get("_expiry") == selected_expiry:
        return {
            "symbol": symbol,
            "expiry": selected_expiry,
            "strike": sel.get("strike"),
            "right": sel.get("right"),
            "bid": sel.get("bid"),
            "ask": sel.get("ask"),
            "mid": sel.get("mid"),
            "delta": sel.get("delta"),
            "iv": sel.get("iv"),
            "theta": sel.get("theta"),
        }
    return None


# ---------------------------------------------------------------------------
# Chain loader
# ---------------------------------------------------------------------------

def _load_chain(
    symbol: str,
    expiry: str,
    adapter: Any,
    market_data_service: Any,
    cache_key: str,
) -> None:
    """Fetch chain data and cache in session state."""
    rows: list[dict] = []
    with st.spinner(f"Loading {symbol} {expiry} option chain…"):
        # ─── Try IBKR TWS adapter first ───────────────────────────────────
        fn_tws = getattr(adapter, "fetch_option_chain_tws", None)
        if callable(fn_tws):
            try:
                import asyncio
                loop = asyncio.new_event_loop()
                rows = loop.run_until_complete(fn_tws(symbol, expiry)) or []
                loop.close()
            except Exception as exc:
                logger.warning("IBKR TWS chain fetch failed: %s", exc)

        # ─── Fallback: MarketDataService (Tastytrade / etc.) ──────────────
        if not rows and market_data_service is not None:
            fn_mds = getattr(market_data_service, "get_options_chain", None)
            if callable(fn_mds):
                try:
                    raw = fn_mds(symbol)
                    # Filter to requested expiry
                    rows = [
                        r for r in (raw or [])
                        if str(getattr(r, "expiration", "") or "").startswith(expiry[:10])
                        or str(r.get("expiration", "") if isinstance(r, dict) else "").startswith(expiry[:10])
                    ]
                    # Normalise to dicts
                    rows = [
                        r if isinstance(r, dict) else r.__dict__
                        for r in rows
                    ]
                except Exception as exc:
                    logger.warning("MarketDataService chain fetch failed: %s", exc)

    if rows:
        st.session_state[cache_key] = rows
        st.success(f"Loaded {len(rows)} option quotes for {expiry}.")
    else:
        st.warning("No chain data returned. Check broker connection.")


# ---------------------------------------------------------------------------
# DataFrame builder
# ---------------------------------------------------------------------------

def _build_chain_df(
    chain_data: list[dict],
    symbol: str,
    strikes_each_side: int,
) -> tuple[Optional[pd.DataFrame], Optional[float]]:
    """Return (wing_df, spot_price) with puts-left, strike-centre, calls-right."""
    puts: dict[float, dict]  = {}
    calls: dict[float, dict] = {}
    spot: Optional[float]    = None

    for row in chain_data:
        # Normalise row — various sources use different field names
        if not isinstance(row, dict):
            try:
                row = row.__dict__
            except Exception:
                continue

        right  = str(row.get("option_type") or row.get("right") or "").upper()
        strike = row.get("strike") or row.get("strike_price")
        try:
            strike = float(strike)
        except (TypeError, ValueError):
            continue

        entry = {
            "bid":   _safe_float(row.get("bid")),
            "ask":   _safe_float(row.get("ask")),
            "mid":   _safe_float(row.get("mid")),
            "delta": _safe_float(row.get("delta")),
            "iv":    _safe_float(row.get("iv") or row.get("implied_volatility")),
            "theta": _safe_float(row.get("theta")),
            "oi":    _safe_int(row.get("open_interest") or row.get("oi")),
            "vol":   _safe_int(row.get("volume") or row.get("vol")),
        }
        if spot is None:
            _ul = row.get("underlying_price") or row.get("spot")
            if _ul:
                try:
                    spot = float(_ul)
                except (TypeError, ValueError):
                    pass

        if "CALL" in right or right == "C":
            calls[strike] = entry
        elif "PUT" in right or right == "P":
            puts[strike] = entry

    if not (puts or calls):
        return None, spot

    all_strikes = sorted(set(puts) | set(calls))
    if not all_strikes:
        return None, spot

    # Centre on ATM
    target = spot or (all_strikes[len(all_strikes) // 2])
    atm_strike = min(all_strikes, key=lambda s: abs(s - target))
    atm_idx = all_strikes.index(atm_strike)
    lo = max(0, atm_idx - strikes_each_side)
    hi = min(len(all_strikes), atm_idx + strikes_each_side + 1)
    visible_strikes = all_strikes[lo:hi]

    rows = []
    empty = {"bid": None, "ask": None, "mid": None, "delta": None,
             "iv": None, "theta": None, "oi": None, "vol": None}
    for s in visible_strikes:
        p = puts.get(s, empty)
        c = calls.get(s, empty)
        rows.append({
            # PUT side (left)
            "put_oi":   p["oi"],
            "put_vol":  p["vol"],
            "put_iv":   _pct(p["iv"]),
            "put_th":   _fmt(p["theta"]),
            "put_delta": _fmt(p["delta"]),
            "put_ask":  _fmt(p["ask"]),
            "put_bid":  _fmt(p["bid"]),
            # Centre
            "strike": s,
            # CALL side (right)
            "call_bid":   _fmt(c["bid"]),
            "call_ask":   _fmt(c["ask"]),
            "call_delta": _fmt(c["delta"]),
            "call_th":    _fmt(c["theta"]),
            "call_iv":    _pct(c["iv"]),
            "call_vol":   c["vol"],
            "call_oi":    c["oi"],
        })

    return pd.DataFrame(rows), spot


def _render_chain_table(df: pd.DataFrame, spot: Optional[float], sk: str, selected_expiry: str) -> None:
    """Render the dual-wing chain table with ATM highlighting and click-to-select."""

    # Rename columns to display labels
    col_rename = {
        "put_oi":    "OI",
        "put_vol":   "Vol",
        "put_iv":    "IV%",
        "put_th":    "Θ",
        "put_delta": "Δ",
        "put_ask":   "Ask",
        "put_bid":   "Bid",
        "strike":    "STRIKE",
        "call_bid":  "Bid",
        "call_ask":  "Ask",
        "call_delta":"Δ ",
        "call_th":   "Θ ",
        "call_iv":   "IV% ",
        "call_vol":  "Vol ",
        "call_oi":   "OI ",
    }
    display_df = df.rename(columns=col_rename)

    # Column config for rich display
    col_cfg = {
        "STRIKE": st.column_config.NumberColumn("STRIKE", format="%.2f"),
        "IV%":    st.column_config.TextColumn("IV%"),
        "IV% ":   st.column_config.TextColumn("IV%"),
        "Δ":      st.column_config.TextColumn("Δ (Put)"),
        "Δ ":     st.column_config.TextColumn("Δ (Call)"),
        "OI":     st.column_config.NumberColumn("OI", format="%d"),
        "OI ":    st.column_config.NumberColumn("OI", format="%d"),
        "Vol":    st.column_config.NumberColumn("Vol", format="%d"),
        "Vol ":   st.column_config.NumberColumn("Vol", format="%d"),
    }

    event = st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config=col_cfg,
        key=f"{sk}_chain_df_{selected_expiry}",
    )

    # Handle row selection → store selected strike info in session state
    sel_result_key = f"{sk}_selected_strike"
    selected_rows = (event.selection or {}).get("rows", [])
    if selected_rows:
        row_idx = selected_rows[0]
        row = df.iloc[row_idx]
        strike_val = float(row["strike"])

        # Determine which side was selected (prompt user if ambiguous)
        st.markdown("**Select side:**")
        side_col1, side_col2, _ = st.columns([2, 2, 6])
        with side_col1:
            if st.button(
                f"📥 PUT @ {strike_val:.2f}",
                key=f"{sk}_sel_put_{row_idx}_{selected_expiry}",
                type="secondary",
            ):
                st.session_state[sel_result_key] = {
                    "_expiry": selected_expiry,
                    "strike":  strike_val,
                    "right":   "PUT",
                    "bid":     _safe_float(row["put_bid"]),
                    "ask":     _safe_float(row["put_ask"]),
                    "mid":     _safe_float(row.get("put_mid")),
                    "delta":   _safe_float(row["put_delta"]),
                    "iv":      _safe_float(row["put_iv"]),
                    "theta":   _safe_float(row["put_th"]),
                }
                st.rerun()
        with side_col2:
            if st.button(
                f"📥 CALL @ {strike_val:.2f}",
                key=f"{sk}_sel_call_{row_idx}_{selected_expiry}",
                type="secondary",
            ):
                st.session_state[sel_result_key] = {
                    "_expiry": selected_expiry,
                    "strike":  strike_val,
                    "right":   "CALL",
                    "bid":     _safe_float(row["call_bid"]),
                    "ask":     _safe_float(row["call_ask"]),
                    "mid":     _safe_float(row.get("call_mid")),
                    "delta":   _safe_float(row["call_delta"]),
                    "iv":      _safe_float(row["call_iv"]),
                    "theta":   _safe_float(row["call_th"]),
                }
                st.rerun()

    # Legend
    st.caption(
        "🟩 ATM row centered on current price · "
        "Click a row then **PUT / CALL** to pre-fill the Order Builder"
    )


# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------

def _safe_float(v: Any) -> Optional[float]:
    try:
        f = float(str(v).replace(",", ""))
        return None if str(f) in ("nan", "inf", "-inf") else f
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> Optional[int]:
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _pct(v: Any) -> str:
    f = _safe_float(v)
    if f is None:
        return "—"
    return f"{f * 100:.1f}%"
