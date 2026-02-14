from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
import json
import logging

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.chdir(PROJECT_ROOT)

from adapters.ibkr_adapter import IBKRAdapter
from agent_config import AGENT_SYSTEM_PROMPT, TOOL_SCHEMAS
from agent_tools.market_data_tools import MarketDataTools
from agent_tools.portfolio_tools import PortfolioTools
from ibkr_portfolio_client import load_dotenv
from logging_config import setup_logging
from risk_engine.regime_detector import RegimeDetector


LOGGER = setup_logging("dashboard")


def positions_cache_path(account_id: str) -> Path:
    safe_account = str(account_id).replace("/", "_")
    return PROJECT_ROOT / f".positions_snapshot_{safe_account}.json"


def save_positions_snapshot(account_id: str, positions: list) -> None:
    path = positions_cache_path(account_id)
    payload = {
        "saved_at": datetime.utcnow().isoformat(),
        "positions": [position.model_dump(mode="json") for position in positions],
    }
    path.write_text(json.dumps(payload))


def load_positions_snapshot(account_id: str) -> tuple[list, str | None]:
    path = positions_cache_path(account_id)
    if not path.exists():
        return [], None
    try:
        payload = json.loads(path.read_text())
        saved_at = payload.get("saved_at")
        raw_positions = payload.get("positions", [])
        from models.unified_position import UnifiedPosition

        positions = [UnifiedPosition.model_validate(item) for item in raw_positions]
        return positions, saved_at
    except Exception:
        return [], None


@st.cache_resource
def get_services() -> tuple[IBKRAdapter, PortfolioTools, MarketDataTools, RegimeDetector]:
    load_dotenv(str(PROJECT_ROOT / ".env"))
    adapter = IBKRAdapter()
    portfolio_tools = PortfolioTools()
    market_tools = MarketDataTools()
    regime_detector = RegimeDetector(PROJECT_ROOT / "config/risk_matrix.yaml")
    return adapter, portfolio_tools, market_tools, regime_detector


@st.cache_data(ttl=120)
def get_cached_vix_data() -> dict:
    """Return cached VIX payload for dashboard reads."""
    return MarketDataTools().get_vix_data()


@st.cache_data(ttl=120)
def get_cached_macro_data() -> dict:
    """Return cached macro indicators payload for dashboard reads."""
    return asyncio.run(MarketDataTools().get_macro_indicators())


@st.cache_data(ttl=900)
def get_cached_historical_volatility(symbols: tuple[str, ...], lookback_days: int = 30) -> dict[str, float]:
    """Return cached historical volatility by symbol."""
    return MarketDataTools().get_historical_volatility(symbols, lookback_days=lookback_days)


def _safe_iso_now() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.utcnow().isoformat()


def _age_minutes_from_iso(timestamp: str | None) -> float | None:
    """Return age in minutes from an ISO timestamp."""
    if not timestamp:
        return None
    try:
        age_minutes = (datetime.utcnow() - datetime.fromisoformat(timestamp)).total_seconds() / 60.0
        return max(age_minutes, 0.0)
    except ValueError:
        return None


def render_regime_banner(regime_name: str, vix_data: dict, macro_data: dict | None = None) -> None:
    color_map = {
        "low_volatility": "#2ecc71",
        "neutral_volatility": "#4da3ff",
        "high_volatility": "#f39c12",
        "crisis_mode": "#e74c3c",
    }
    color = color_map.get(regime_name, "#4da3ff")
    recession_probability = None
    macro_source = "unavailable"
    macro_timestamp = None
    if isinstance(macro_data, dict):
        recession_probability = macro_data.get("recession_probability")
        macro_source = str(macro_data.get("source") or "unavailable")
        macro_timestamp = macro_data.get("timestamp")

    recession_label = "N/A"
    if recession_probability is not None:
        try:
            recession_label = f"{float(recession_probability) * 100:.1f}%"
        except (TypeError, ValueError):
            recession_label = "N/A"

    st.markdown(
        f"""
        <div style='padding: 0.75rem; border-radius: 8px; background-color: {color}; color: white;'>
          <b>Regime:</b> {regime_name.replace('_', ' ').title()} |
          <b>VIX:</b> {vix_data['vix']:.2f} |
          <b>Term Structure:</b> {vix_data['term_structure']:.3f} |
          <b>Recession Prob:</b> {recession_label} ({macro_source}) |
          <b>Macro Ts:</b> {macro_timestamp or 'N/A'}
        </div>
        """,
        unsafe_allow_html=True,
    )


def build_positions_dataframe(positions: list, ibkr_option_scaling: bool) -> pd.DataFrame:
    rows: list[dict] = []
    for position in positions:
        is_option = position.instrument_type.name == "OPTION"
        scale = 100.0 if (ibkr_option_scaling and is_option) else 1.0
        contract_multiplier = float(getattr(position, "contract_multiplier", 1.0) or 1.0)

        quantity = float(position.quantity)
        delta = float(position.delta) * scale
        theta = float(position.theta) * scale
        vega = float(position.vega) * scale
        gamma = float(position.gamma) * scale

        rows.append(
            {
                "Symbol": position.symbol,
                "Type": position.instrument_type.name,
                "Underlying": position.underlying or "",
                "Qty": quantity,
                "Multiplier": contract_multiplier,
                "Expiration": position.expiration.isoformat() if position.expiration else "",
                "Strike": float(position.strike) if position.strike is not None else None,
                "OptionType": position.option_type or "",
                "Delta": delta,
                "Theta": theta,
                "Vega": vega,
                "Gamma": gamma,
                "SPX Delta": float(position.spx_delta),
                "Greek Source": getattr(position, "greeks_source", "none"),
                "Delta/Unit": (delta / quantity) if quantity else 0.0,
                "Theta/Unit": (theta / quantity) if quantity else 0.0,
                "Vega/Unit": (vega / quantity) if quantity else 0.0,
            }
        )

    table = pd.DataFrame(rows)
    if not table.empty:
        table = table.sort_values(by=["Type", "Underlying", "Symbol"], ascending=[True, True, True]).reset_index(
            drop=True
        )
    return table


def main() -> None:
    LOGGER.info("Starting dashboard run")
    st.set_page_config(page_title="Portfolio Risk Manager", page_icon="📊", layout="wide")
    st.title("Portfolio Risk Manager")

    adapter, portfolio_tools, market_tools, regime_detector = get_services()

    st.sidebar.header("Inputs")
    reload_accounts = st.sidebar.button("Reload Accounts")
    if st.sidebar.button("Sign in to IBKR"):
        gateway_ok = adapter.client.check_gateway_status()
        if not gateway_ok:
            gateway_ok = adapter.client.start_gateway()
        if gateway_ok:
            adapter.client.initiate_sso_login()
            st.sidebar.success("IBKR sign-in initiated. Open gateway and complete login.")
            st.sidebar.markdown("[Open IBKR Gateway Login](https://localhost:5001)")
        else:
            st.sidebar.error("Unable to start/connect to IBKR Gateway. Keep current cached mode or start gateway manually.")

    if "ibkr_accounts" not in st.session_state or reload_accounts:
        try:
            live_accounts = adapter.client.get_accounts()
            st.session_state["ibkr_accounts"] = live_accounts
        except Exception:
            st.session_state["ibkr_accounts"] = []

    ibkr_accounts = st.session_state.get("ibkr_accounts", [])
    account_options = [
        account.get("accountId") or account.get("id")
        for account in ibkr_accounts
        if (account.get("accountId") or account.get("id"))
    ]

    if not account_options:
        st.error("No IBKR accounts available from gateway. Use 'Sign in to IBKR' in the sidebar, then click 'Reload Accounts'.")
        st.stop()

    account_id = st.sidebar.selectbox("IBKR Account", options=account_options, index=0)
    refresh = st.sidebar.button("Refresh")
    show_positions_table = st.sidebar.checkbox("Show per-position Greeks", value=True)
    ibkr_option_scaling = st.sidebar.checkbox("IBKR-style option scaling (x100)", value=False)
    use_cached_fallback = st.sidebar.checkbox("Use latest cached portfolio if IBKR unavailable", value=True)
    ibkr_only_mode = st.sidebar.checkbox("IBKR-only mode (no external Greeks)", value=False)

    st.sidebar.subheader("Greeks Diagnostics")
    disable_tasty_cache = st.sidebar.checkbox(
        "Disable Tasty cache (live fetch only)",
        value=bool(getattr(adapter, "disable_tasty_cache", False)),
    )
    force_refresh_on_miss = st.sidebar.checkbox(
        "Force live fetch on cache miss",
        value=bool(getattr(adapter, "force_refresh_on_miss", True)),
        disabled=disable_tasty_cache,
    )
    adapter.disable_tasty_cache = bool(disable_tasty_cache)
    adapter.force_refresh_on_miss = bool(force_refresh_on_miss)

    if refresh:
        get_cached_vix_data.clear()
        get_cached_macro_data.clear()
        get_cached_historical_volatility.clear()

    if account_id is None:
        st.error("Unable to resolve a valid IBKR account ID from gateway response.")
        st.stop()

    with st.spinner("Loading portfolio and market data..."):
        data_refresh = st.session_state.setdefault("data_refresh_timestamps", {})
        positions = st.session_state.get("positions")
        if not isinstance(positions, list):
            positions = []
        previous_positions_for_account = positions
        previous_account = st.session_state.get("selected_account")
        fallback_saved_at = None
        st.session_state["greeks_refresh_fallback"] = False
        st.session_state["greeks_refresh_fallback_reason"] = ""
        if refresh or positions is None or previous_account != account_id:
            fetched_positions = asyncio.run(adapter.fetch_positions(account_id))
            if fetched_positions:
                positions = fetched_positions
                save_positions_snapshot(account_id, positions)
                data_refresh["positions"] = _safe_iso_now()
            elif use_cached_fallback:
                cached_positions, fallback_saved_at = load_positions_snapshot(account_id)
                positions = cached_positions
                if fallback_saved_at:
                    data_refresh["positions"] = fallback_saved_at
            else:
                positions = []
                data_refresh["positions"] = _safe_iso_now()
            positions = st.session_state["positions"] = positions
            st.session_state["selected_account"] = account_id
            st.session_state["fallback_saved_at"] = fallback_saved_at
        else:
            fallback_saved_at = st.session_state.get("fallback_saved_at")

        if positions and not ibkr_only_mode:
            positions = asyncio.run(adapter.fetch_greeks(positions))
            data_refresh["greeks"] = _safe_iso_now()

            options_count = sum(1 for position in positions if position.instrument_type.name == "OPTION")
            greeks_status = getattr(adapter, "last_greeks_status", {})
            cache_miss_count = int(greeks_status.get("cache_miss_count", 0))
            miss_ratio = (cache_miss_count / options_count) if options_count else 0.0

            previous_positions_list = previous_positions_for_account if isinstance(previous_positions_for_account, list) else []
            can_reuse_previous = (
                refresh
                and previous_account == account_id
                and use_cached_fallback
                and bool(previous_positions_list)
            )

            if can_reuse_previous and options_count > 0 and miss_ratio >= 0.8:
                previous_option_positions = [
                    p for p in previous_positions_list if p.instrument_type.name == "OPTION"
                ]
                previous_has_nonzero_greeks = any(
                    abs(float(getattr(p, "theta", 0.0))) > 0.0
                    or abs(float(getattr(p, "vega", 0.0))) > 0.0
                    or abs(float(getattr(p, "gamma", 0.0))) > 0.0
                    for p in previous_option_positions
                )
                if previous_has_nonzero_greeks:
                    positions = previous_positions_list
                    st.session_state["greeks_refresh_fallback"] = True
                    st.session_state["greeks_refresh_fallback_reason"] = (
                        f"Latest refresh had {cache_miss_count}/{options_count} missing option Greeks; "
                        "reusing previous in-session snapshot."
                    )

        if positions:
            save_positions_snapshot(account_id, positions)
            st.session_state["positions"] = positions

        vix_data = get_cached_vix_data()
        data_refresh["vix"] = str(vix_data.get("timestamp") or _safe_iso_now())
        macro_data = get_cached_macro_data()
        data_refresh["macro"] = str(macro_data.get("timestamp") or _safe_iso_now())
        regime = regime_detector.detect_regime(
            vix=vix_data["vix"],
            term_structure=vix_data["term_structure"],
            recession_probability=macro_data.get("recession_probability") if isinstance(macro_data, dict) else None,
        )
        summary_getter = getattr(adapter.client, "get_account_summary", None)
        summary_payload = summary_getter(account_id) if callable(summary_getter) else {}
        ibkr_summary: dict[str, object] = summary_payload if isinstance(summary_payload, dict) else {}

    summary = portfolio_tools.get_portfolio_summary(positions)
    violations = portfolio_tools.check_risk_limits(summary, regime)

    previous_regime = st.session_state.get("last_regime_name")
    current_regime = str(regime.name)
    if previous_regime is not None and previous_regime != current_regime:
        st.error(
            "⚠️ Regime transition detected: "
            f"{previous_regime.replace('_', ' ').title()} → {current_regime.replace('_', ' ').title()}"
        )
    st.session_state["last_regime_name"] = current_regime
    st.markdown(
        f"""
        <script>
            document.title = "Portfolio Risk Manager - {current_regime.replace('_', ' ').title()}";
        </script>
        """,
        unsafe_allow_html=True,
    )

    render_regime_banner(regime.name, vix_data, macro_data)

    with st.sidebar.expander("Data Freshness", expanded=False):
        data_refresh = st.session_state.get("data_refresh_timestamps", {})
        for key, label in [
            ("positions", "Positions"),
            ("greeks", "Greeks"),
            ("vix", "VIX"),
            ("macro", "Macro"),
            ("iv_hv", "IV/HV"),
        ]:
            timestamp = data_refresh.get(key)
            age_minutes = _age_minutes_from_iso(timestamp)
            if timestamp and age_minutes is not None:
                st.caption(f"{label}: {timestamp} UTC ({age_minutes:.1f} min old)")
            else:
                st.caption(f"{label}: N/A")

    if ibkr_summary:
        st.subheader("IBKR Account Summary")

        def _to_float(value: object) -> float | None:
            try:
                if isinstance(value, dict):
                    amount = value.get("amount")
                    if amount in (None, "", "N/A"):
                        return None
                    return float(amount)
                if value in (None, "", "N/A"):
                    return None
                return float(str(value).replace(",", ""))
            except (TypeError, ValueError):
                return None

        net_liq = _to_float(ibkr_summary.get("netliquidation"))
        buying_power = _to_float(ibkr_summary.get("buyingpower"))
        maint_margin = _to_float(ibkr_summary.get("maintmarginreq"))
        excess_liq = _to_float(ibkr_summary.get("excessliquidity"))

        ibkr_cols = st.columns(4)
        ibkr_cols[0].metric("Net Liquidation", f"{net_liq:,.2f}" if net_liq is not None else "N/A")
        ibkr_cols[1].metric("Buying Power", f"{buying_power:,.2f}" if buying_power is not None else "N/A")
        ibkr_cols[2].metric("Maint Margin", f"{maint_margin:,.2f}" if maint_margin is not None else "N/A")
        ibkr_cols[3].metric("Excess Liquidity", f"{excess_liq:,.2f}" if excess_liq is not None else "N/A")

    if ibkr_only_mode:
        st.info(
            "IBKR-only mode is enabled. External options Greek enrichment is skipped. "
            "Use this mode to compare account/position structure directly with IBKR CPAPI data."
        )

    if not positions:
        st.warning(
            f"No positions returned for account {account_id}. Select a different account or confirm open positions exist in IBKR."
        )
    elif st.session_state.get("greeks_refresh_fallback"):
        st.warning(st.session_state.get("greeks_refresh_fallback_reason") or "Reusing previous snapshot due to degraded refresh.")
    elif st.session_state.get("fallback_saved_at"):
        st.warning(
            "Using latest cached portfolio snapshot because IBKR positions were unavailable. "
            f"Snapshot time: {st.session_state.get('fallback_saved_at')} UTC"
        )

    if positions:
        latest_timestamp = max(position.timestamp for position in positions if position.timestamp)
        age_minutes = (datetime.utcnow() - latest_timestamp).total_seconds() / 60
        st.caption(f"Greeks timestamp: {latest_timestamp.isoformat()} UTC ({age_minutes:.1f} minutes old)")
        if age_minutes > 10:
            st.warning("Greeks data is older than 10 minutes and may be stale.")

        options_count = sum(1 for position in positions if position.instrument_type.name == "OPTION")
        if options_count > 0:
            greeks_status = getattr(adapter, "last_greeks_status", {})
            cache_miss_count = int(greeks_status.get("cache_miss_count", 0))
            session_error = greeks_status.get("last_session_error")
            missing_greeks_details = greeks_status.get("missing_greeks_details") or []
            source_counts = Counter(
                getattr(position, "greeks_source", "none") for position in positions if position.instrument_type.name == "OPTION"
            )
            reason_counts = Counter(item.get("reason") or "unknown" for item in missing_greeks_details)

            st.caption(
                "Greeks diagnostics mode — "
                f"disable_cache={bool(greeks_status.get('disable_tasty_cache', adapter.disable_tasty_cache))}, "
                f"force_refresh_on_miss={bool(greeks_status.get('force_refresh_on_miss', adapter.force_refresh_on_miss))}"
            )
            st.write(
                {
                    "greeks_source_counts": dict(source_counts),
                    "missing_reason_counts": dict(reason_counts),
                }
            )

            if cache_miss_count > 0:
                st.warning(
                    f"Option Greeks missing for {cache_miss_count}/{options_count} option positions. "
                    f"Tastytrade cache/session issue detected."
                )
            if missing_greeks_details:
                st.subheader("Options Missing Greeks (IBKR-first diagnostics)")
                missing_df = pd.DataFrame(missing_greeks_details)
                st.dataframe(missing_df, width="stretch")
                download_cols = st.columns(2)
                with download_cols[0]:
                    st.download_button(
                        "Download Missing Greeks CSV",
                        missing_df.to_csv(index=False).encode("utf-8"),
                        file_name=f"missing_greeks_{account_id}.csv",
                        mime="text/csv",
                    )
                with download_cols[1]:
                    st.download_button(
                        "Download Missing Greeks JSON",
                        json.dumps(missing_greeks_details, indent=2).encode("utf-8"),
                        file_name=f"missing_greeks_{account_id}.json",
                        mime="application/json",
                    )
            if session_error:
                st.info(
                    "Tastytrade auth detail: "
                    f"{session_error}. "
                    "Set TASTYTRADE_REFRESH_TOKEN (OAuth, preferred), or use TASTYTRADE_REMEMBER_TOKEN / TASTYTRADE_2FA_CODE in .env and refresh."
                )

    metrics = st.columns(6)
    metrics[0].metric("Delta", f"{summary['total_delta']:.2f}")
    metrics[1].metric("Theta", f"{summary['total_theta']:.2f}")
    metrics[2].metric("Vega", f"{summary['total_vega']:.2f}")
    metrics[3].metric("Gamma", f"{summary['total_gamma']:.2f}")
    metrics[4].metric("SPX Delta", f"{summary['total_spx_delta']:.2f}")
    metrics[5].metric("Theta/Vega", f"{summary['theta_vega_ratio']:.3f}")

    iv_analysis: list[dict] = []
    if positions:
        iv_symbols = sorted(
            {
                str(position.underlying).upper()
                for position in positions
                if position.iv is not None and position.underlying
            }
        )
        historical_volatility = get_cached_historical_volatility(tuple(iv_symbols))
        st.session_state.setdefault("data_refresh_timestamps", {})["iv_hv"] = _safe_iso_now()
        iv_analysis = portfolio_tools.get_iv_analysis(positions, historical_volatility)

        st.subheader("IV vs HV Analysis")
        if iv_analysis:
            iv_df = pd.DataFrame(iv_analysis)
            sell_count = int(
                sum(
                    1
                    for row in iv_analysis
                    if str(row.get("signal", "")).startswith("strong_sell")
                    or str(row.get("signal", "")).startswith("moderate_sell")
                )
            )
            total_count = int(len(iv_analysis))
            iv_cols = st.columns(2)
            iv_cols[0].metric("Positions with IV > HV", f"{sell_count} of {total_count}")
            iv_cols[1].metric("Buy-edge candidates (IV < HV)", f"{sum(1 for row in iv_analysis if row.get('signal') == 'buy_edge')}")

            st.dataframe(
                iv_df.rename(
                    columns={
                        "iv": "IV",
                        "hv": "HV",
                        "spread": "IV-HV Spread",
                        "edge": "Edge",
                        "signal": "Signal",
                        "signal_color": "Signal Color",
                    }
                ),
                width="stretch",
            )
            st.caption(
                "IV > HV = sell edge (overpriced premium), IV < HV = buy edge (underpriced premium)."
            )
        else:
            st.info("IV/HV analysis unavailable for current positions (missing IV or insufficient price history).")

    if positions and show_positions_table:
        st.subheader("Portfolio Positions & Greeks")
        if ibkr_option_scaling:
            st.caption("Option Greeks in this table are multiplied by 100 for IBKR-style contract scaling.")
        position_df = build_positions_dataframe(positions, ibkr_option_scaling=ibkr_option_scaling)
        st.dataframe(position_df, width="stretch")

    st.subheader("Risk Compliance")
    if violations:
        st.error("One or more risk limits are violated.")
        st.dataframe(pd.DataFrame(violations), width="stretch")
    else:
        st.success("All regime limits are currently satisfied.")

    if positions:
        st.subheader("Gamma Risk by DTE")
        gamma_by_dte = portfolio_tools.get_gamma_risk_by_dte(positions)
        bucket_order = ["0-7", "8-30", "31-60", "60+"]
        bucket_values = [float(gamma_by_dte.get(bucket, 0.0)) for bucket in bucket_order]
        bucket_colors: list[str] = []
        for bucket, value in zip(bucket_order, bucket_values):
            if bucket == "0-7" and abs(value) > 5.0:
                bucket_colors.append("#e74c3c")
            elif bucket in {"0-7", "8-30"}:
                bucket_colors.append("#f39c12")
            else:
                bucket_colors.append("#2ecc71")

        gamma_fig = go.Figure(
            data=[
                go.Bar(
                    x=bucket_order,
                    y=bucket_values,
                    marker_color=bucket_colors,
                )
            ]
        )
        gamma_fig.update_layout(
            xaxis_title="DTE Bucket",
            yaxis_title="Gamma",
            height=320,
            margin=dict(l=20, r=20, t=20, b=20),
        )
        st.plotly_chart(gamma_fig, use_container_width=True)

        gamma_0_7 = float(gamma_by_dte.get("0-7", 0.0))
        if abs(gamma_0_7) > 5.0:
            st.warning("⚠️ High gamma in 0-7 DTE bucket. Taleb warns: 'Gamma risk explodes near expiration.'")

    st.subheader("Theta/Vega Profile")
    abs_vega = abs(float(summary.get("total_vega", 0.0)))
    abs_theta = abs(float(summary.get("total_theta", 0.0)))
    current_ratio = float(summary.get("theta_vega_ratio", 0.0))

    x_max = max(abs_vega * 1.6, 100.0)
    x_target = x_max * 0.6

    theta_vega_fig = go.Figure()
    theta_vega_fig.add_trace(
        go.Scatter(
            x=[0.0, x_max],
            y=[0.25 * 0.0, 0.25 * x_max],
            mode="lines",
            line=dict(color="rgba(46, 204, 113, 0.5)", width=1),
            showlegend=False,
            hoverinfo="skip",
        )
    )
    theta_vega_fig.add_trace(
        go.Scatter(
            x=[0.0, x_max],
            y=[0.40 * 0.0, 0.40 * x_max],
            mode="lines",
            line=dict(color="rgba(46, 204, 113, 0.5)", width=1),
            fill="tonexty",
            fillcolor="rgba(46, 204, 113, 0.15)",
            name="Target zone (0.25-0.40)",
            hoverinfo="skip",
        )
    )

    theta_vega_fig.add_trace(
        go.Scatter(
            x=[0.0, x_max],
            y=[0.20 * 0.0, 0.20 * x_max],
            mode="lines",
            line=dict(color="rgba(231, 76, 60, 0.45)", width=1, dash="dot"),
            showlegend=False,
            hoverinfo="skip",
        )
    )
    theta_vega_fig.add_trace(
        go.Scatter(
            x=[0.0, x_max],
            y=[0.50 * 0.0, 0.50 * x_max],
            mode="lines",
            line=dict(color="rgba(231, 76, 60, 0.45)", width=1, dash="dot"),
            showlegend=False,
            hoverinfo="skip",
        )
    )

    theta_vega_fig.add_trace(
        go.Scatter(
            x=[0.0, x_max],
            y=[0.33 * 0.0, 0.33 * x_max],
            mode="lines",
            line=dict(color="#4da3ff", width=2, dash="dash"),
            name="1:3 reference (0.33)",
            hoverinfo="skip",
        )
    )

    theta_vega_fig.add_trace(
        go.Scatter(
            x=[abs_vega],
            y=[abs_theta],
            mode="markers+text",
            marker=dict(size=11, color="#1f77b4"),
            text=[f"Current ({current_ratio:.3f})"],
            textposition="top center",
            name="Current portfolio",
        )
    )

    theta_vega_fig.update_layout(
        xaxis_title="Abs Vega (dollars)",
        yaxis_title="Abs Theta (dollars)",
        height=360,
        margin=dict(l=20, r=20, t=20, b=20),
        showlegend=True,
    )
    st.plotly_chart(theta_vega_fig, use_container_width=True)
    st.caption("Target zone: 0.25 ≤ |Theta|/|Vega| ≤ 0.40 (Sebastian 1:3 framework).")

    st.subheader("Market Data")
    st.write(
        {
            "VIX": round(vix_data["vix"], 2),
            "VIX3M": round(vix_data["vix3m"], 2),
            "TermStructure": round(vix_data["term_structure"], 3),
            "Backwardation": vix_data["is_backwardation"],
            "UpdatedAtUTC": datetime.utcnow().isoformat(),
        }
    )

    st.subheader("AI Assistant")
    user_prompt = st.text_input("Ask for a risk adjustment", placeholder="How should I reduce near-term gamma?")
    if user_prompt:
        tool_names = [tool["name"] for tool in TOOL_SCHEMAS]
        violation_count = len(violations)
        stance = "defensive" if violation_count > 0 or abs(float(summary.get("total_spx_delta", 0.0))) > 500 else "balanced"
        st.info(
            "Placeholder Copilot response\n\n"
            f"- Regime: {regime.name}\n"
            f"- Violations: {violation_count}\n"
            f"- SPX Delta: {summary['total_spx_delta']:.2f}\n"
            f"- Theta/Vega ratio: {summary['theta_vega_ratio']:.3f}\n"
            f"- Suggested stance: {stance}\n"
            f"- Available tools: {', '.join(tool_names)}"
        )

    with st.expander("Assistant configuration"):
        st.caption(AGENT_SYSTEM_PROMPT)
        st.json({"tool_schemas": TOOL_SCHEMAS})


if __name__ == "__main__":
    main()
