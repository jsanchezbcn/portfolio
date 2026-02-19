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


def _run_async(coro):
    """Run an async coroutine from synchronous Streamlit context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result(timeout=10)
        return loop.run_until_complete(coro)
    except Exception:
        return asyncio.run(coro)


@st.cache_data(ttl=3600)
def _get_available_models() -> list[dict]:
    """Fetch available LLM models from Copilot SDK (cached 1 h)."""
    try:
        from agents.llm_client import async_list_models
        result = _run_async(async_list_models())
        return result if result else []
    except Exception:
        return [
            {"id": "gpt-4.1",     "name": "GPT-4.1",    "is_free": True},
            {"id": "gpt-4o",      "name": "GPT-4o",      "is_free": True},
            {"id": "gpt-4o-mini", "name": "GPT-4o mini", "is_free": True},
        ]


@st.cache_data(ttl=60)
def _fetch_market_intel_cached() -> list[dict]:
    """Fetch recent market_intel rows from the DB (cached 60 s).

    Tries PostgreSQL (DBManager) first; falls back to local SQLite (LocalStore).
    """
    async def _fetch():
        # Try PostgreSQL first
        try:
            from database.db_manager import DBManager
            db = DBManager()
            await db.connect()
            return await db.get_recent_market_intel(limit=20)
        except Exception as exc:
            LOGGER.debug("PostgreSQL market_intel unavailable (%s), using LocalStore", exc)
        # Fallback: SQLite local store
        try:
            from database.local_store import LocalStore
            db = LocalStore()
            return await db.get_recent_market_intel(limit=20)
        except Exception as exc2:
            LOGGER.debug("LocalStore market_intel also failed: %s", exc2)
            return []

    import concurrent.futures
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, _fetch()).result(timeout=15)
    except Exception as exc:
        LOGGER.debug("market_intel fetch skipped: %s", exc)
        return []


@st.cache_data(ttl=60)
def _fetch_active_signals_cached() -> list[dict]:
    """Fetch active arbitrage signals from the DB (cached 60 s)."""
    try:
        from database.db_manager import DBManager

        async def _fetch():
            db = DBManager()
            await db.connect()
            return await db.get_active_signals(limit=50)

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, _fetch()).result(timeout=15)
    except Exception as exc:
        LOGGER.debug("signals fetch skipped: %s", exc)
        return []


@st.cache_data(ttl=120)
def _fetch_llm_intel_cached(source: str, symbol: str | None = None) -> dict | None:
    """Return the latest market_intel row for a given LLM ``source`` tag.

    TTL is 120 s so the dashboard shows near-live results without hammering the DB.
    Returns a parsed dict when content is JSON, otherwise ``{"headline": content}``.
    Tries PostgreSQL first; falls back to local SQLite.
    """
    async def _fetch():
        row = None
        # Try PostgreSQL first
        try:
            from database.db_manager import DBManager
            db = DBManager()
            await db.connect()
            rows = await db.get_market_intel_by_source(source, symbol=symbol, limit=1)
            row = rows[0] if rows else None
        except Exception as exc:
            LOGGER.debug("PostgreSQL llm_intel unavailable (%s), using LocalStore", exc)
        # Fallback: SQLite
        if row is None:
            try:
                from database.local_store import LocalStore
                db = LocalStore()
                rows = await db.get_market_intel_by_source(source, symbol=symbol, limit=1)
                row = rows[0] if rows else None
            except Exception as exc2:
                LOGGER.debug("LocalStore llm_intel also failed: %s", exc2)
        return row

    import concurrent.futures
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            row = pool.submit(asyncio.run, _fetch()).result(timeout=15)
        if row is None:
            return None
        content = row.get("content", "")
        try:
            parsed = json.loads(content) if content else {}
        except (ValueError, TypeError):
            parsed = {"headline": content}
        parsed["_created_at"] = str(row.get("created_at", ""))[:19]
        return parsed
    except Exception as exc:
        LOGGER.debug("llm_intel fetch skipped (source=%s): %s", source, exc)
        return None



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
        # Gateway unavailable — fall back to cached position snapshots for offline mode
        import glob as _glob
        _snap_files = _glob.glob(str(PROJECT_ROOT / ".positions_snapshot_*.json"))
        _cached_account_ids = [
            Path(f).name.replace(".positions_snapshot_", "").replace(".json", "")
            for f in _snap_files
        ]
        if _cached_account_ids:
            st.warning(
                "⚠️ IBKR gateway not available — using **cached snapshot** (read-only mode).  \n"
                "Start the gateway and click **Reload Accounts** to go live."
            )
            account_options = sorted(_cached_account_ids)
        else:
            st.error(
                "No IBKR accounts available from gateway. "
                "Use 'Sign in to IBKR' in the sidebar, then click 'Reload Accounts'."
            )
            st.stop()

    account_id = st.sidebar.selectbox("IBKR Account", options=account_options, index=0)
    refresh = st.sidebar.button("Refresh")
    show_positions_table = st.sidebar.checkbox("Show per-position Greeks", value=True)
    ibkr_option_scaling = st.sidebar.checkbox("IBKR-style option scaling (x100)", value=False)
    use_cached_fallback = st.sidebar.checkbox("Use latest cached portfolio if IBKR unavailable", value=True)
    ibkr_only_mode = st.sidebar.checkbox("IBKR-only mode (no external Greeks)", value=False)

    # LLM Model picker
    st.sidebar.subheader("🤖 AI / LLM Settings")
    _all_models = _get_available_models()
    _model_labels = [
        f"{m['name']} {'🆓' if m['is_free'] else '💰'}" for m in _all_models
    ]
    _model_ids = [m["id"] for m in _all_models]
    _default_model_idx = next(
        (i for i, m in enumerate(_all_models) if m["id"] == "gpt-4.1"), 0
    )
    _sel_idx = st.sidebar.selectbox(
        "Model",
        options=range(len(_model_labels)),
        format_func=lambda i: _model_labels[i],
        index=_default_model_idx,
        key="llm_model_picker",
    )
    selected_llm_model: str = _model_ids[_sel_idx]

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
        _fetch_market_intel_cached.clear()
        _fetch_active_signals_cached.clear()

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
            # Fetch Greeks only when positions are actually refreshed (not on every render)
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

            positions = st.session_state["positions"] = positions
            st.session_state["selected_account"] = account_id
            st.session_state["fallback_saved_at"] = fallback_saved_at
        else:
            fallback_saved_at = st.session_state.get("fallback_saved_at")
            # Use already-loaded positions (with Greeks) from session state
            positions = st.session_state.get("positions") or positions

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
    # Extract NLV early so risk limits can be scaled by portfolio size.
    # ibkr_summary may not be populated yet; fall back to None gracefully.
    try:
        _nlv_for_risk = float(ibkr_summary.get("netliquidation")) if ibkr_summary else None
    except (TypeError, ValueError):
        _nlv_for_risk = None
    violations = portfolio_tools.check_risk_limits(summary, regime, nlv=_nlv_for_risk)

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

    # Issue 15: Dedicated Risk Status panel — traffic-light for every guardrail
    with st.container():
        st.subheader("Risk Status")
        rs_cols = st.columns(4)

        # Column 0: Greek limit compliance
        with rs_cols[0]:
            st.caption("Greek Limits")
            if violations:
                st.error(f"⛔ {len(violations)} violation(s)")
            else:
                st.success("✓ All limits OK")

        # Column 1: DTE expiry risk
        with rs_cols[1]:
            st.caption("DTE Expiry Risk")
            if positions:
                _dte_alerts = portfolio_tools.check_dte_expiry_risk(positions)
                _crit_dte = [a for a in _dte_alerts if a["level"] == "CRITICAL"]
                _warn_dte = [a for a in _dte_alerts if a["level"] == "WARNING"]
                if _crit_dte:
                    st.error(f"⛔ {len(_crit_dte)} expiry critical")
                elif _warn_dte:
                    st.warning(f"⚠ {len(_warn_dte)} expiry warning(s)")
                else:
                    st.success("✓ No DTE risk")
            else:
                st.info("No positions")

        # Column 2: Vega concentration
        with rs_cols[2]:
            st.caption("Concentration")
            if positions:
                _conc = portfolio_tools.check_concentration_risk(positions, regime)
                if _conc:
                    st.warning(f"⚠ {len(_conc)} concentration issue(s)")
                else:
                    st.success("✓ Concentration OK")
            else:
                st.info("No positions")

        # Column 3: Daily drawdown (requires set_start_of_day_net_liq to be called)
        with rs_cols[3]:
            st.caption("Daily Drawdown")
            _current_nl: float | None = None
            if ibkr_summary:
                def _to_float_safe(value: object) -> float | None:
                    try:
                        if isinstance(value, dict):
                            a = value.get("amount")
                            return float(a) if a not in (None, "", "N/A") else None
                        return float(str(value).replace(",", "")) if value not in (None, "", "N/A") else None
                    except (TypeError, ValueError):
                        return None
                _current_nl = _to_float_safe(ibkr_summary.get("netliquidation"))
            if _current_nl is not None:
                _dd = portfolio_tools.check_daily_drawdown(_current_nl)
                if _dd:
                    st.error(f"⛔ {_dd['loss_pct']:.1%} drawdown")
                else:
                    st.success("✓ Drawdown OK")
            else:
                st.info("Net liq N/A")

        # Show violation details when any exist
        if violations:
            with st.expander("Active Risk Violations", expanded=True):
                for _v in violations:
                    st.write(
                        f"• **{_v.get('metric', '?')}**: {_v.get('message', '')} "
                        f"(current: `{_v.get('current', '?')}`, limit: `{_v.get('limit', '?')}`)"
                    )

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

    # ------------------------------------------------------------------ #
    # NewsSentry — Market Intelligence panel                              #
    # ------------------------------------------------------------------ #
    st.subheader("Market Intelligence (Sentiment)")
    if refresh:
        _fetch_market_intel_cached.clear()
    market_intel_rows = _fetch_market_intel_cached()
    if market_intel_rows:
        intel_df = pd.DataFrame(market_intel_rows)
        # Format display columns
        display_cols = [c for c in ["symbol", "source", "sentiment_score", "content", "created_at"] if c in intel_df.columns]
        intel_df = intel_df[display_cols]
        if "sentiment_score" in intel_df.columns:
            intel_df["sentiment_score"] = intel_df["sentiment_score"].apply(
                lambda x: f"{float(x):.2f}" if x is not None else "N/A"
            )
        if "created_at" in intel_df.columns:
            intel_df["created_at"] = intel_df["created_at"].apply(
                lambda x: str(x)[:19] if x is not None else ""
            )
        st.dataframe(intel_df, use_container_width=True)
        # Aggregate sentiment per symbol
        if "sentiment_score" in market_intel_rows[0]:
            scored = [r for r in market_intel_rows if r.get("sentiment_score") is not None]
            if scored:
                by_symbol: dict[str, list[float]] = {}
                for r in scored:
                    sym = str(r.get("symbol", "?"))
                    try:
                        by_symbol.setdefault(sym, []).append(float(r["sentiment_score"]))
                    except (TypeError, ValueError):
                        pass
                if by_symbol:
                    avg_scores = {sym: sum(scores) / len(scores) for sym, scores in by_symbol.items()}
                    sentiment_cols = st.columns(min(len(avg_scores), 4))
                    for idx, (sym, avg) in enumerate(avg_scores.items()):
                        color = "🟢" if avg > 0.1 else ("🔴" if avg < -0.1 else "🟡")
                        sentiment_cols[idx % len(sentiment_cols)].metric(f"{color} {sym}", f"{avg:.2f}")
    else:
        st.info(
            "No market intelligence data available. "
            "Use the **Fetch News Now** button, or run `python -m agents.news_sentry`."
        )

    # "Fetch News Now" button — always visible in the news section
    _news_col, _ = st.columns([1, 3])
    with _news_col:
        if st.button("📰 Fetch News Now", key="btn_fetch_news"):
            # Derive symbols from current portfolio positions (fallback: broad ETFs)
            try:
                _raw_syms = [str(p.symbol).split()[0].upper() for p in positions if getattr(p, "symbol", None)]
                _news_symbols = sorted(set(_raw_syms)) or ["SPY", "QQQ", "NVDA", "AAPL"]
            except Exception:
                _news_symbols = ["SPY", "QQQ", "NVDA", "AAPL"]

            with st.spinner(f"Fetching & scoring news for {_news_symbols}…"):
                try:
                    async def _do_news():
                        from agents.news_sentry import NewsSentry
                        from database.local_store import LocalStore
                        _db = LocalStore()
                        _sentry = NewsSentry(symbols=_news_symbols, db=_db)
                        for _sym in _news_symbols:
                            await _sentry.fetch_and_score(_sym)

                    _run_async(_do_news())
                    _fetch_market_intel_cached.clear()
                    st.success(f"News fetched for: {', '.join(_news_symbols)}")
                    st.rerun()
                except Exception as _ne:
                    st.error(f"News fetch failed: {_ne}")

    # ------------------------------------------------------------------ #
    # AI Insights — LLM Risk Audit + Market Brief                        #
    # ------------------------------------------------------------------ #
    st.subheader("🤖 AI Insights")
    if refresh:
        _fetch_llm_intel_cached.clear()

    _ai_audit_col, _ai_brief_col = st.columns(2)

    # --- Risk Audit (LLMRiskAuditor, source="llm_risk_audit") -----------
    with _ai_audit_col:
        st.markdown("**Live Risk Audit** *(gpt-4.1 · 5-min cadence)*")
        audit_data = _fetch_llm_intel_cached("llm_risk_audit", symbol="PORTFOLIO")
        if audit_data:
            urgency = audit_data.get("urgency", "green").lower()
            _urgency_emoji = {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(urgency, "⚪")
            _urgency_color = {"green": "success", "yellow": "warning", "red": "error"}.get(urgency, "info")
            getattr(st, _urgency_color)(f"{_urgency_emoji} **{audit_data.get('headline', 'Audit result')}**")
            if audit_data.get("body"):
                st.caption(audit_data["body"])
            suggestions = audit_data.get("suggestions") or []
            if isinstance(suggestions, list) and suggestions:
                with st.expander("Suggestions", expanded=urgency == "red"):
                    for _s in suggestions:
                        st.markdown(f"• {_s}")
            st.caption(f"Generated: {audit_data.get('_created_at', 'unknown')}")
        else:
            st.info("No audit available yet. Start LLMRiskAuditor or use the button below.")

        # On-demand audit button
        if st.button("🔍 Audit Now", key="btn_audit_now"):
            with st.spinner("Running LLM risk audit…"):
                try:
                    from agents.llm_risk_auditor import LLMRiskAuditor

                    async def _do_audit():
                        try:
                            from database.db_manager import DBManager
                            db = DBManager()
                            await db.connect()
                        except Exception:
                            from database.local_store import LocalStore
                            db = LocalStore()
                        auditor = LLMRiskAuditor(db=db)
                        return await auditor.audit_now(
                            summary=summary,
                            regime_name=regime.name,
                            vix=vix_data["vix"],
                            term_structure=vix_data["term_structure"],
                            nlv=_nlv_for_risk,
                            violations=violations,
                            resolved_limits=None,
                        )

                    _audit_result = _run_async(_do_audit())
                    _fetch_llm_intel_cached.clear()
                    st.rerun()
                except Exception as _ae:
                    st.error(f"Audit failed: {_ae}")

    # --- Market Brief (LLMMarketBrief, source="llm_brief") --------------
    with _ai_brief_col:
        st.markdown("**Market Brief** *(gpt-4.1-mini · 1-hr cadence)*")
        brief_data = _fetch_llm_intel_cached("llm_brief", symbol="MARKET")
        if brief_data:
            _tone = brief_data.get("confidence", "medium")
            _tone_emoji = {"high": "💪", "medium": "🤔", "low": "😐"}.get(_tone, "")
            st.info(f"{_tone_emoji} **{brief_data.get('headline', 'Market brief')}**")
            if brief_data.get("regime_read"):
                st.caption(brief_data["regime_read"])
            if brief_data.get("opportunity"):
                st.success(f"**Opportunity:** {brief_data['opportunity']}")
            if brief_data.get("risk"):
                st.warning(f"**Watch:** {brief_data['risk']}")
            if brief_data.get("action"):
                with st.expander("💡 Trade Idea"):
                    st.markdown(brief_data["action"])
            st.caption(f"Generated: {brief_data.get('_created_at', 'unknown')}")
        else:
            st.info("No brief available yet. Start LLMMarketBrief or use the button below.")

        # On-demand brief button
        if st.button("📰 Refresh Brief", key="btn_refresh_brief"):
            with st.spinner("Generating market brief…"):
                try:
                    from agents.llm_market_brief import LLMMarketBrief

                    async def _do_brief():
                        try:
                            from database.db_manager import DBManager
                            db = DBManager()
                            await db.connect()
                        except Exception:
                            from database.local_store import LocalStore
                            db = LocalStore()
                        brief_agent = LLMMarketBrief(db=db)
                        return await brief_agent.brief_now(
                            vix=vix_data["vix"],
                            vix3m=vix_data.get("vix3m", vix_data["vix"]),
                            term_structure=vix_data["term_structure"],
                            regime_name=regime.name,
                            recession_probability=macro_data.get("recession_probability"),
                            portfolio_summary=summary,
                            nlv=_nlv_for_risk,
                        )

                    _run_async(_do_brief())
                    _fetch_llm_intel_cached.clear()
                    st.rerun()
                except Exception as _be:
                    st.error(f"Brief failed: {_be}")

    # ------------------------------------------------------------------ #
    # ArbHunter — Active Arbitrage Signals panel                          #
    # ------------------------------------------------------------------ #
    st.subheader("Arbitrage Signals")
    if refresh:
        _fetch_active_signals_cached.clear()
    active_signals = _fetch_active_signals_cached()
    if active_signals:
        signals_df = pd.DataFrame(active_signals)
        display_sig_cols = [c for c in ["signal_type", "net_value", "confidence", "status", "detected_at"] if c in signals_df.columns]
        signals_df = signals_df[display_sig_cols]
        if "net_value" in signals_df.columns:
            signals_df["net_value"] = signals_df["net_value"].apply(
                lambda x: f"${float(x):.2f}" if x is not None else "N/A"
            )
        if "confidence" in signals_df.columns:
            signals_df["confidence"] = signals_df["confidence"].apply(
                lambda x: f"{float(x):.2f}" if x is not None else "N/A"
            )
        if "detected_at" in signals_df.columns:
            signals_df["detected_at"] = signals_df["detected_at"].apply(
                lambda x: str(x)[:19] if x is not None else ""
            )
        st.dataframe(signals_df, use_container_width=True)
        pcp_count = sum(1 for r in active_signals if r.get("signal_type", "").upper() == "PUT_CALL_PARITY")
        box_count = sum(1 for r in active_signals if r.get("signal_type", "").upper() == "BOX_SPREAD")
        sig_cols = st.columns(2)
        sig_cols[0].metric("Put-Call Parity Signals", pcp_count)
        sig_cols[1].metric("Box Spread Signals", box_count)
    else:
        st.info(
            "No active arbitrage signals. "
            "Run ArbHunter.scan() against an option chain to detect opportunities."
        )

    # ------------------------------------------------------------------ #
    # AI Assistant                                                        #
    # ------------------------------------------------------------------ #
    st.subheader("AI Assistant")
    user_prompt = st.text_input("Ask for a risk adjustment", placeholder="How should I reduce near-term gamma?")
    if user_prompt:
        violation_count = len(violations)
        stance = "defensive" if violation_count > 0 or abs(float(summary.get("total_spx_delta", 0.0))) > 500 else "balanced"

        # Build per-position context (top 12 option positions)
        _opt_positions = [p for p in positions if getattr(p, "instrument_type", None) and p.instrument_type.name == "OPTION"][:12]
        _equity_positions = [p for p in positions if getattr(p, "instrument_type", None) and p.instrument_type.name == "EQUITY"][:5]
        _futures_positions = [p for p in positions if getattr(p, "instrument_type", None) and p.instrument_type.name == "FUTURE"][:5]

        def _pos_line(p) -> str:
            dte = getattr(p, "days_to_expiration", None)
            dte_s = f"DTE={dte}" if dte is not None else ""
            return (
                f"  {str(p.symbol)[:28]:<28} qty={p.quantity:+6.1f} {dte_s:<8} "
                f"Δ={p.delta:+.3f} θ={p.theta:+.3f} ν={p.vega:+.3f} "
                f"src={getattr(p, 'greeks_source', '?')}"
            )

        _opt_block = "\n".join(_pos_line(p) for p in _opt_positions) or "  (none)"
        _eq_block  = "\n".join(f"  {str(p.symbol)[:20]:<20} qty={p.quantity:+.1f} Δ={p.delta:+.3f}" for p in _equity_positions) or "  (none)"
        _fut_block = "\n".join(f"  {str(p.symbol)[:20]:<20} qty={p.quantity:+.1f} Δ={p.delta:+.3f}" for p in _futures_positions) or "  (none)"

        # Full context block for AI
        context_block = (
            f"Account: {account_id}\n"
            f"Regime: {regime.name}\n"
            f"VIX: {vix_data['vix']:.2f} | VIX3M: {vix_data.get('vix3m', 'N/A')} | Term Structure: {vix_data['term_structure']:.3f}\n"
            f"Portfolio aggregate — Delta: {summary['total_delta']:.2f} | Theta: {summary['total_theta']:.2f} | "
            f"Vega: {summary['total_vega']:.2f} | Gamma: {summary['total_gamma']:.2f}\n"
            f"SPX Delta: {summary['total_spx_delta']:.2f} | Theta/Vega ratio: {summary['theta_vega_ratio']:.3f}\n"
            f"Position counts — Options: {len(_opt_positions)} shown (of {sum(1 for p in positions if getattr(p,'instrument_type',None) and p.instrument_type.name=='OPTION')}) | "
            f"Equity: {len(_equity_positions)} | Futures: {len(_futures_positions)}\n"
            f"Option positions (qty, DTE, Greeks per contract × qty):\n{_opt_block}\n"
            f"Equity positions:\n{_eq_block}\n"
            f"Futures positions:\n{_fut_block}\n"
            f"Risk violations ({violation_count}): "
            + (", ".join(f"{v.get('metric','?')}: {v.get('message','')}" for v in violations) if violations else "none")
            + f"\nSuggested stance: {stance}"
        )

        # Use GitHub Copilot SDK (CopilotClient) — same pattern as news_sentry.py and explain_performance.py
        llm_response: str | None = None
        llm_model = selected_llm_model
        full_prompt = (
            f"System: {AGENT_SYSTEM_PROMPT}\n\n"
            f"Current portfolio context:\n{context_block}\n\n"
            f"User question: {user_prompt}"
        )
        try:
            from agents.llm_client import async_llm_chat
            with st.spinner("Asking AI assistant..."):
                llm_response = _run_async(
                    async_llm_chat(full_prompt, model=llm_model, timeout=45.0)
                ) or None
        except Exception as llm_exc:
            LOGGER.warning("LLM call failed: %s", llm_exc)
            llm_response = None

        if llm_response:
            st.info(llm_response)
        else:
            # Graceful fallback — structured context response
            tool_names = [tool["name"] for tool in TOOL_SCHEMAS]
            st.info(
                "**Portfolio context** (GitHub Copilot CLI required for AI responses)\n\n"
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
