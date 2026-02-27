from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from urllib.parse import quote_plus
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import logging
import concurrent.futures
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as st_components

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.chdir(PROJECT_ROOT)

from adapters.ibkr_adapter import IBKRAdapter
from agent_config import AGENT_SYSTEM_PROMPT, TOOL_SCHEMAS
from core.market_data import MarketDataService
from dashboard.components.ibkr_login import render_ibkr_login_button
from dashboard.components.order_builder import render_order_builder
from dashboard.components.order_management import render_order_management
from dashboard.components.trade_dialog import (
    render_trade_dialog,
    render_submission_banner as render_trade_submission_banner,
    open_trade_dialog,
)
from agent_tools.market_data_tools import MarketDataTools
from agent_tools.portfolio_tools import PortfolioTools
from ibkr_portfolio_client import load_dotenv
from logging_config import setup_logging
from risk_engine.regime_detector import RegimeDetector


LOGGER = setup_logging("dashboard")

SNAPSHOT_INTERVAL_SECONDS = int(os.getenv("SNAPSHOT_INTERVAL_SECONDS", "900"))  # 15 min default
POSITIONS_FETCH_TIMEOUT_SECONDS = int(os.getenv("POSITIONS_FETCH_TIMEOUT_SECONDS", "25"))
GREEKS_FETCH_TIMEOUT_SECONDS = int(os.getenv("GREEKS_FETCH_TIMEOUT_SECONDS", "35"))
PORTFOLIO_REFRESH_SECONDS = int(os.getenv("PORTFOLIO_REFRESH_SECONDS", "30"))
WORKER_RESULT_MAX_AGE_SECONDS = int(os.getenv("WORKER_RESULT_MAX_AGE_SECONDS", "600"))
UI_AUTO_REFRESH_SECONDS = int(os.getenv("UI_AUTO_REFRESH_SECONDS", "30"))
NON_BLOCKING_DASHBOARD_LOAD = str(os.getenv("NON_BLOCKING_DASHBOARD_LOAD", "1")).strip().lower() in {"1", "true", "yes", "on"}
ACCOUNT_SUMMARY_TIMEOUT_SECONDS = float(os.getenv("ACCOUNT_SUMMARY_TIMEOUT_SECONDS", "8.0"))  # 8s — TWS socket needs more time than 2.5s


# ---------------------------------------------------------------------------
# Background snapshot logger (T060)
# ---------------------------------------------------------------------------

def _snapshot_loop(
    *,
    account_id: str,
    adapter: Any,
    portfolio_tools: Any,
    regime_detector: Any,
    interval: int,
) -> None:
    """Thread target: capture a portfolio snapshot every ``interval`` seconds.

    Threading (not asyncio) survives Streamlit reruns.
    Errors are logged but never crash the thread.
    """
    from database.local_store import LocalStore
    from models.order import AccountSnapshot
    from agent_tools.market_data_tools import MarketDataTools as _MDT

    store = LocalStore()
    logger = logging.getLogger(__name__ + ".snapshot_loop")

    while True:
        try:
            market_tools = _MDT()
            vix_info = market_tools.get_vix_data()
            vix = float(vix_info.get("vix", 0.0))

            # Prefer adapter.get_account_summary which uses TWS socket in SOCKET mode.
            summary_getter_adapter = getattr(adapter, "get_account_summary", None)
            summary_getter_client = getattr(getattr(adapter, "client", None), "get_account_summary", None)
            summary_getter = summary_getter_adapter or summary_getter_client
            ibkr_summary = summary_getter(account_id) if callable(summary_getter) else {}
            net_liq = None
            if isinstance(ibkr_summary, dict):
                raw = ibkr_summary.get("netliquidation")
                try:
                    net_liq = float(raw.get("amount")) if isinstance(raw, dict) else float(raw)
                except (TypeError, ValueError, AttributeError):
                    net_liq = None

            positions = getattr(adapter, "last_positions", None) or []
            if not positions:
                try:
                    positions = asyncio.run(adapter.fetch_positions(account_id))
                    if positions:
                        positions = asyncio.run(adapter.fetch_greeks(positions))
                        setattr(adapter, "last_positions", positions)
                except Exception as _pos_exc:
                    logger.debug("Snapshot loop fetch_positions/fetch_greeks failed: %s", _pos_exc)
            summary = portfolio_tools.get_portfolio_summary(positions)
            regime = regime_detector.detect_regime(vix=vix, term_structure=float(vix_info.get("term_structure", 1.0)))

            spx_info = market_tools.get_spx_data() or {}
            spx_price = None
            try:
                spx_price = float(spx_info.get("spx") or spx_info.get("last") or spx_info.get("close") or 0) or None
            except (TypeError, ValueError):
                spx_price = None

            captured_at = datetime.now(timezone.utc).isoformat()
            snap = AccountSnapshot(
                captured_at=captured_at,
                account_id=account_id,
                broker="IBKR",
                net_liquidation=net_liq,
                spx_delta=float(summary.get("total_spx_delta", 0.0)),
                gamma=float(summary.get("total_gamma", 0.0)),
                theta=float(summary.get("total_theta", 0.0)),
                vega=float(summary.get("total_vega", 0.0)),
                vix=vix,
                spx_price=spx_price,
                regime=getattr(regime, "name", str(regime)),
            )
            asyncio.run(store.capture_snapshot(snap))
            logger.info("Snapshot captured at %s (account=%s)", captured_at, account_id)
            # Record last success timestamp for dashboard indicator
            import streamlit as _st
            try:
                _st.session_state["_snapshot_last_at"] = captured_at
                _st.session_state["_snapshot_last_error"] = None
            except Exception:
                pass
        except Exception as exc:
            logger.warning("Snapshot loop error: %s", exc)
            try:
                import streamlit as _st
                _st.session_state["_snapshot_last_error"] = str(exc)[:120]
            except Exception:
                pass

        time.sleep(interval)


def _start_snapshot_logger(
    *,
    account_id: str,
    adapter: Any,
    portfolio_tools: Any,
    regime_detector: Any,
) -> None:
    """Start the background snapshot thread once per Streamlit session (T060)."""
    if st.session_state.get("_snapshot_thread_started"):
        return
    st.session_state["_snapshot_thread_started"] = True
    thread = threading.Thread(
        target=_snapshot_loop,
        kwargs=dict(
            account_id=account_id,
            adapter=adapter,
            portfolio_tools=portfolio_tools,
            regime_detector=regime_detector,
            interval=SNAPSHOT_INTERVAL_SECONDS,
        ),
        daemon=True,
        name="snapshot-logger",
    )
    thread.start()
    LOGGER.info("Snapshot logger thread started (interval=%ds)", SNAPSHOT_INTERVAL_SECONDS)


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
    return datetime.now(timezone.utc).isoformat()


def _age_minutes_from_iso(timestamp: str | None) -> float | None:
    """Return age in minutes from an ISO timestamp."""
    if not timestamp:
        return None
    try:
        ts = datetime.fromisoformat(timestamp)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_minutes = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
        return max(age_minutes, 0.0)
    except ValueError:
        return None


def inject_custom_css() -> None:
    """Inject custom CSS for FinTech styling and accessibility (tabular numbers, refined fonts)."""
    st.markdown(
        """
        <style>
        /* Force tabular numbers for parity/alignment in metrics and tables */
        .stMetric, [data-testid="stMarkdownContainer"] code, .stDataFrame {
            font-variant-numeric: tabular-nums !important;
        }
        
        /* Refine metric styling for readability */
        [data-testid="stMetricValue"] {
            font-family: 'Fira Code', 'Roboto Mono', monospace !important;
            font-size: 1.8rem !important;
        }

        /* Customize Regime Banner for high visibility */
        .regime-banner {
            padding: 1rem;
            border-radius: 12px;
            color: white;
            font-size: 1.05rem;
            margin-bottom: 1.5rem;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .regime-item {
            margin: 0 0.5rem;
        }

        .regime-label {
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            opacity: 0.9;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_regime_banner(regime_name: str, vix_data: dict, macro_data: dict | None = None) -> None:
    color_map = {
        "low_volatility": "linear-gradient(90deg, #27ae60, #2ecc71)",
        "neutral_volatility": "linear-gradient(90deg, #2980b9, #3498db)",
        "high_volatility": "linear-gradient(90deg, #e67e22, #f39c12)",
        "crisis_mode": "linear-gradient(90deg, #c0392b, #e74c3c)",
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
        <div class='regime-banner' style='background: {color};'>
          <div class='regime-item'><span class='regime-label'>Regime:</span> <b>{regime_name.replace('_', ' ').title()}</b></div>
          <div class='regime-item'><span class='regime-label'>VIX:</span> <b>{vix_data['vix']:.2f}</b></div>
          <div class='regime-item'><span class='regime-label'>Term Structure:</span> <b>{vix_data['term_structure']:.3f}</b></div>
          <div class='regime-item'><span class='regime-label'>Recession:</span> <b>{recession_label}</b></div>
          <div class='regime-item' style='font-size: 0.8rem; opacity: 0.8;'>{macro_timestamp or 'N/A'} ({macro_source})</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption(
        f"Regime: {regime_name.replace('_', ' ').title()} | "
        f"VIX: {vix_data.get('vix', 0):.2f} | "
        f"Term Structure: {vix_data.get('term_structure', 0):.3f}"
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
                "⚠ Beta": "⚠ unavailable" if getattr(position, "beta_unavailable", False) else "",  # T019
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


# ── async helpers ──────────────────────────────────────────────────────────────

def _run_async(coro):
    """Run an async coroutine safely from any thread (Streamlit-safe).

    Always delegates to a fresh thread so ``asyncio.run()`` never hits an
    already-running loop (Tornado/Streamlit keep a loop in the main thread).
    """
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result(timeout=30)


def _resolve_proposer_db_url() -> str:
    explicit = (os.getenv("PROPOSER_DB_URL") or "").strip()
    if explicit:
        return explicit
    host = (os.getenv("DB_HOST") or "").split("#")[0].strip()
    port = (os.getenv("DB_PORT") or "5432").split("#")[0].strip()
    name = (os.getenv("DB_NAME") or "").split("#")[0].strip()
    user = (os.getenv("DB_USER") or "").split("#")[0].strip()
    pwd = (os.getenv("DB_PASS") or "").split("#")[0].strip()
    if host and name and user:
        auth = f"{quote_plus(user)}:{quote_plus(pwd)}" if pwd else quote_plus(user)
        return f"postgresql+psycopg2://{auth}@{host}:{port}/{name}"
    return ""


def _coerce_expiry_date(raw_value: Any):
    from datetime import date as _date
    if isinstance(raw_value, _date):
        return raw_value
    raw_text = str(raw_value or "").strip()
    if len(raw_text) == 8 and raw_text.isdigit():
        try:
            return datetime.strptime(raw_text, "%Y%m%d").date()
        except Exception:
            pass
    try:
        return datetime.fromisoformat(raw_text.replace("Z", "+00:00")).date()
    except Exception:
        return datetime.utcnow().date()


def _prefill_order_builder_from_legs(
    *,
    legs: list[dict[str, Any]],
    source_label: str,
    rationale: str = "",
) -> bool:
    """Stage prefill data into a non-widget session-state key.

    Widget keys (ob_n_legs_input, ob_action_N, …) cannot be modified after
    the Order Builder has already rendered them in the current Streamlit run.
    Instead we store the data under ``ob_prefill_data`` and let
    ``render_order_builder`` apply it at the TOP of the next run, before any
    widgets are instantiated.
    """
    if not legs:
        return False

    normalized: list[dict[str, Any]] = []
    for leg in legs[:4]:
        action = str(leg.get("action", "BUY")).upper()
        symbol = str(leg.get("symbol") or leg.get("underlying") or "SPX").upper()
        qty_raw = leg.get("quantity", leg.get("qty", 1))
        try:
            qty = max(1, int(float(qty_raw)))
        except Exception:
            qty = 1

        strike = leg.get("strike")
        right = leg.get("right")
        expiry_raw = leg.get("expiry") or leg.get("expiration")

        instrument_type = str(leg.get("instrument_type") or ("Option" if (strike is not None and right) else "Stock/ETF"))
        if instrument_type.lower() == "future":
            instrument_type = "Future"
        elif instrument_type.lower() not in {"option", "stock/etf"}:
            instrument_type = "Option" if (strike is not None and right) else "Stock/ETF"
        normalized.append(
            {
                "action": "SELL" if action == "SELL" else "BUY",
                "symbol": symbol,
                "qty": qty,
                "instrument_type": instrument_type,
                "strike": float(strike) if strike is not None else None,
                "right": str(right).upper() if right else None,
                "expiry": _coerce_expiry_date(expiry_raw) if expiry_raw else None,
                "conid": leg.get("conid") or leg.get("conId") or leg.get("contract_id") or leg.get("broker_id"),
            }
        )

    # Store all prefill data under a SINGLE non-widget key.
    # render_order_builder will pop this at the TOP of the next run and
    # write the individual ob_* widget keys before any widgets render.
    # NOTE: ob_approved and ob_rationale are widget keys — they MUST NOT be
    # set here (after their widgets have rendered).  They go into the staging
    # dict and are applied by _render_inner before widgets instantiate.
    st.session_state["ob_prefill_data"] = {
        "leg_count": len(normalized),
        "legs": normalized,
        "rationale": rationale,
        "reset_approved": True,   # tell render_order_builder to clear approval
    }
    st.session_state["ob_submit_result"] = None  # not a widget key — safe
    st.session_state["ob_force_expand"] = True    # persistent open flag for Order Builder
    # Open the inline Trade Ticket dialog (bid/ask + simulate + submit)
    open_trade_dialog(normalized, source_label, rationale)
    return True


def _render_order_draft_dialog() -> None:
    """Show a modal draft preview immediately after trade creation."""
    if not st.session_state.get("order_prefill_show_dialog"):
        return

    _draft_notice = st.session_state.get("order_prefill_notice") or "Draft created."
    _draft_source = st.session_state.get("order_prefill_source") or ""
    _draft_legs = st.session_state.get("order_prefill_legs") or []
    _draft_rationale = st.session_state.get("order_prefill_rationale") or ""

    @st.dialog("✅ Trade Draft Ready", width="large")
    def _show_dialog() -> None:
        st.success(_draft_notice)
        if _draft_source:
            st.caption(f"Source: {_draft_source}")

        if _draft_legs:
            rows = []
            for _leg in _draft_legs:
                rows.append(
                    {
                        "Action": _leg.get("action", "BUY"),
                        "Symbol": _leg.get("symbol", ""),
                        "Qty": int(float(_leg.get("qty", 1) or 1)),
                        "Type": _leg.get("instrument_type", "Option"),
                        "Strike": _leg.get("strike"),
                        "Right": _leg.get("right"),
                        "Expiry": str(_leg.get("expiry") or ""),
                    }
                )
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        if _draft_rationale:
            st.caption(f"Rationale: {_draft_rationale}")

        _c1, _c2 = st.columns(2)
        with _c1:
            if st.button("Go to Order Builder", use_container_width=True, key="order_draft_modal_go"):
                st.session_state["ob_force_expand"] = True
                st.session_state["order_prefill_show_dialog"] = False
                st.rerun()
        with _c2:
            if st.button("Close", use_container_width=True, key="order_draft_modal_close"):
                st.session_state["order_prefill_show_dialog"] = False
                st.rerun()

    _show_dialog()


def _render_order_draft_preview_block(*, key_prefix: str = "order_draft_preview") -> None:
    """Render a prominent inline preview of the currently staged order draft."""
    _prefill_notice = st.session_state.get("order_prefill_notice")
    _prefill_source = st.session_state.get("order_prefill_source", "")
    _prefill_legs = st.session_state.get("order_prefill_legs") or []
    _prefill_rationale = st.session_state.get("order_prefill_rationale", "")
    if not (_prefill_notice and _prefill_legs):
        return

    with st.container(border=True):
        st.success(_prefill_notice)
        st.caption(f"Source: {_prefill_source}")
        _draft_rows = []
        for _leg in _prefill_legs:
            _draft_rows.append(
                {
                    "Action": _leg.get("action", "BUY"),
                    "Symbol": _leg.get("symbol", ""),
                    "Qty": int(float(_leg.get("qty", 1) or 1)),
                    "Type": _leg.get("instrument_type", "Option"),
                    "Strike": _leg.get("strike"),
                    "Right": _leg.get("right"),
                    "Expiry": str(_leg.get("expiry") or ""),
                }
            )
        st.dataframe(pd.DataFrame(_draft_rows), use_container_width=True, hide_index=True)
        if _prefill_rationale:
            st.caption(f"Rationale: {_prefill_rationale}")

        _c1, _c2 = st.columns(2)
        with _c1:
            if st.button("Open in Order Builder", key=f"{key_prefix}_open"):
                st.session_state["ob_force_expand"] = True
                st.rerun()
        with _c2:
            if st.button("Clear Draft", key=f"{key_prefix}_clear"):
                for _k in (
                    "order_prefill_notice",
                    "order_prefill_source",
                    "order_prefill_legs",
                    "order_prefill_rationale",
                    "order_prefill_show_dialog",
                ):
                    st.session_state.pop(_k, None)
                st.rerun()


def _build_order_legs_from_signal(signal: dict[str, Any], default_underlying: str = "SPX") -> list[dict[str, Any]]:
    """Convert an arbitrage signal or proposed-trade legs_json into a list of order-leg dicts."""
    signal_type = str(signal.get("signal_type") or "").upper()
    legs_json = signal.get("legs_json") or {}
    if isinstance(legs_json, str):
        try:
            legs_json = json.loads(legs_json)
        except Exception:
            legs_json = {}

    # ── helpers ──────────────────────────────────────────────────────────────
    def _expand_right(r: Any) -> str:
        """Normalise P/C abbreviations to PUT/CALL."""
        s = str(r).upper().strip()
        if s.startswith("P"):
            return "PUT"
        if s.startswith("C"):
            return "CALL"
        return s

    def _dte_to_expiry(dte_raw: Any) -> str | None:
        """Convert an integer DTE to an ISO date string (today + dte days)."""
        try:
            dte = int(float(dte_raw))
            return (datetime.utcnow() + timedelta(days=dte)).strftime("%Y-%m-%d")
        except Exception:
            return None

    def _normalize_leg(raw_leg: dict[str, Any], fallback_symbol: str) -> dict[str, Any] | None:
        action = str(raw_leg.get("action") or raw_leg.get("side") or "BUY").upper()
        right = raw_leg.get("right") or raw_leg.get("option_type") or raw_leg.get("type")
        strike = raw_leg.get("strike") or raw_leg.get("strike_price")
        expiry_val = (raw_leg.get("expiry") or raw_leg.get("expiration")
                      or _dte_to_expiry(raw_leg.get("dte")))
        symbol_val = str(
            raw_leg.get("symbol")
            or raw_leg.get("underlying")
            or raw_leg.get("root")
            or fallback_symbol
            or "SPX"
        ).upper()

        qty_raw = raw_leg.get("quantity", raw_leg.get("qty", raw_leg.get("contracts", raw_leg.get("size", 1))))
        try:
            quantity_val = max(1, int(float(qty_raw or 1)))
        except Exception:
            quantity_val = 1

        if not right and strike is None:
            instrument_type = str(raw_leg.get("instrument_type") or raw_leg.get("asset_type") or "Stock/ETF")
            if instrument_type.lower() == "future":
                instrument_type = "Future"
            elif instrument_type.lower() not in {"stock/etf", "future", "option"}:
                instrument_type = "Stock/ETF"
            return {
                "action": "SELL" if action == "SELL" else "BUY",
                "symbol": symbol_val,
                "quantity": quantity_val,
                "instrument_type": instrument_type,
                "strike": None,
                "right": None,
                "expiry": expiry_val,
                "conid": raw_leg.get("conid") or raw_leg.get("conId") or raw_leg.get("contract_id") or raw_leg.get("broker_id"),
            }
        try:
            strike_val = float(strike) if strike is not None else None
        except Exception:
            strike_val = None

        return {
            "action": "SELL" if action == "SELL" else "BUY",
            "symbol": symbol_val,
            "quantity": quantity_val,
            "instrument_type": "Option",
            "strike": strike_val,
            "right": _expand_right(right) if right else None,
            "expiry": expiry_val,
            "conid": raw_leg.get("conid") or raw_leg.get("conId") or raw_leg.get("contract_id") or raw_leg.get("broker_id"),
        }

    def _extract_explicit_legs(obj: Any, fallback_symbol: str) -> list[dict[str, Any]]:
        legs: list[dict[str, Any]] = []
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict):
                    leg = _normalize_leg(item, fallback_symbol)
                    if leg:
                        legs.append(leg)
            return legs
        if isinstance(obj, dict):
            nested = obj.get("legs")
            if isinstance(nested, list):
                return _extract_explicit_legs(nested, fallback_symbol)
            nested_alt = obj.get("legs_json")
            if isinstance(nested_alt, list):
                return _extract_explicit_legs(nested_alt, fallback_symbol)
            for key in ("long_leg", "short_leg", "leg1", "leg2", "leg3", "leg4"):
                val = obj.get(key)
                if isinstance(val, dict):
                    leg = _normalize_leg(val, fallback_symbol)
                    if leg:
                        legs.append(leg)
            if not legs and all(k in obj for k in ("long_call_strike", "short_call_strike", "expiry")):
                try:
                    long_call = float(obj.get("long_call_strike"))
                    short_call = float(obj.get("short_call_strike"))
                    legs.extend(
                        [
                            {
                                "action": "BUY",
                                "symbol": fallback_symbol,
                                "quantity": 1,
                                "instrument_type": "Option",
                                "strike": long_call,
                                "right": "CALL",
                                "expiry": obj.get("expiry"),
                            },
                            {
                                "action": "SELL",
                                "symbol": fallback_symbol,
                                "quantity": 1,
                                "instrument_type": "Option",
                                "strike": short_call,
                                "right": "CALL",
                                "expiry": obj.get("expiry"),
                            },
                        ]
                    )
                except Exception:
                    pass
        return legs

    # ── 1. Explicit per-leg dicts (proposed-trade engine output) ──────────────
    if isinstance(legs_json, list):
        _explicit = _extract_explicit_legs(legs_json, default_underlying)
        if _explicit:
            return _explicit

    if not isinstance(legs_json, dict):
        return []

    _explicit = _extract_explicit_legs(legs_json, default_underlying)
    if _explicit:
        return _explicit

    # ── 2. Signal-type–specific parsers ──────────────────────────────────────
    symbol = str(legs_json.get("symbol") or default_underlying or "SPX").upper()
    # Accept both "expiry" and "expiration" keys
    expiry = legs_json.get("expiry") or legs_json.get("expiration")

    try:
        strike = float(legs_json.get("strike"))
    except Exception:
        strike = None

    # put_call_parity: direction field controls which side to buy
    if signal_type.startswith("PUT_CALL_PARITY"):
        direction = str(legs_json.get("direction") or "").lower()
        if strike is not None and expiry:
            if "call_overpriced" in signal_type or direction == "long_put":
                return [
                    {"action": "BUY", "symbol": symbol, "quantity": 1, "conid": None, "strike": strike, "right": "PUT", "expiry": expiry},
                    {"action": "SELL", "symbol": symbol, "quantity": 1, "conid": None, "strike": strike, "right": "CALL", "expiry": expiry},
                ]
            if "put_overpriced" in signal_type or direction == "long_call":
                return [
                    {"action": "BUY", "symbol": symbol, "quantity": 1, "conid": None, "strike": strike, "right": "CALL", "expiry": expiry},
                    {"action": "SELL", "symbol": symbol, "quantity": 1, "conid": None, "strike": strike, "right": "PUT", "expiry": expiry},
                ]
            # Fallback: generic parity trade — buy cheaper side
            put_px = float(legs_json.get("put") or 0)
            call_px = float(legs_json.get("call") or 0)
            if put_px < call_px:
                return [
                    {"action": "BUY", "symbol": symbol, "quantity": 1, "conid": None, "strike": strike, "right": "PUT", "expiry": expiry},
                    {"action": "SELL", "symbol": symbol, "quantity": 1, "conid": None, "strike": strike, "right": "CALL", "expiry": expiry},
                ]
            return [
                {"action": "BUY", "symbol": symbol, "quantity": 1, "conid": None, "strike": strike, "right": "CALL", "expiry": expiry},
                {"action": "SELL", "symbol": symbol, "quantity": 1, "conid": None, "strike": strike, "right": "PUT", "expiry": expiry},
            ]

    # box_spread: uses strikes list or lower_strike/upper_strike
    if signal_type == "BOX_SPREAD":
        strikes_list = legs_json.get("strikes") or []
        try:
            lower_strike = float(strikes_list[0]) if strikes_list else float(legs_json.get("lower_strike"))
            upper_strike = float(strikes_list[1]) if len(strikes_list) > 1 else float(legs_json.get("upper_strike"))
        except Exception:
            lower_strike = None
            upper_strike = None

        if lower_strike is not None and upper_strike is not None and expiry:
            return [
                {"action": "BUY",  "symbol": symbol, "quantity": 1, "conid": None, "strike": lower_strike, "right": "CALL", "expiry": expiry},
                {"action": "SELL", "symbol": symbol, "quantity": 1, "conid": None, "strike": upper_strike, "right": "CALL", "expiry": expiry},
                {"action": "SELL", "symbol": symbol, "quantity": 1, "conid": None, "strike": lower_strike, "right": "PUT",  "expiry": expiry},
                {"action": "BUY",  "symbol": symbol, "quantity": 1, "conid": None, "strike": upper_strike, "right": "PUT",  "expiry": expiry},
            ]

    return []


def _safe_float_or_none(value: Any) -> float | None:
    try:
        if value in (None, "", "N/A"):
            return None
        return float(value)
    except Exception:
        return None


def _with_live_option_quotes(legs: list[dict[str, Any]], market_data_service: Any) -> list[dict[str, Any]]:
    if not legs or market_data_service is None:
        return legs

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for leg in legs:
        symbol = str(leg.get("symbol") or leg.get("underlying") or "").upper().strip()
        expiry = str(leg.get("expiry") or leg.get("expiration") or "").strip()
        if not symbol or not expiry:
            continue
        grouped.setdefault((symbol, expiry), []).append(leg)

    quoted = [dict(leg) for leg in legs]
    for (symbol, expiry), _group_legs in grouped.items():
        try:
            chain = market_data_service.get_options_chain(symbol, expiry=expiry) or []
        except Exception:
            chain = []

        if not chain:
            continue

        lookup: dict[tuple[str, int], Any] = {}
        for row in chain:
            try:
                right = "CALL" if str(getattr(row, "option_type", "")).lower().startswith("c") else "PUT"
                strike_key = int(round(float(getattr(row, "strike", 0.0)) * 100))
                lookup[(right, strike_key)] = row
            except Exception:
                continue

        for q_leg in quoted:
            q_symbol = str(q_leg.get("symbol") or q_leg.get("underlying") or "").upper().strip()
            q_expiry = str(q_leg.get("expiry") or q_leg.get("expiration") or "").strip()
            if q_symbol != symbol or q_expiry != expiry:
                continue

            right = str(q_leg.get("right") or "").upper()
            strike = _safe_float_or_none(q_leg.get("strike"))
            if right not in {"CALL", "PUT"} or strike is None:
                continue
            row = lookup.get((right, int(round(strike * 100))))
            if not row:
                continue

            bid = _safe_float_or_none(getattr(row, "bid", None))
            ask = _safe_float_or_none(getattr(row, "ask", None))
            mid = _safe_float_or_none(getattr(row, "mid", None))
            if bid is not None:
                q_leg["bid"] = bid
            if ask is not None:
                q_leg["ask"] = ask
            if mid is not None:
                q_leg["mid"] = mid
            if bid is not None and ask is not None:
                q_leg["spread"] = ask - bid

    return quoted


def _estimate_combo_quote(legs: list[dict[str, Any]]) -> dict[str, Any]:
    total_legs = len(legs)
    quoted_legs = 0
    combo_bid = 0.0
    combo_ask = 0.0
    combo_mid = 0.0

    for leg in legs:
        action = str(leg.get("action", "BUY")).upper()
        is_sell = action == "SELL"
        qty_raw = leg.get("quantity", leg.get("qty", 1))
        try:
            qty = max(1, int(float(qty_raw)))
        except Exception:
            qty = 1

        bid = _safe_float_or_none(leg.get("bid"))
        ask = _safe_float_or_none(leg.get("ask"))
        if bid is None or ask is None:
            continue

        quoted_legs += 1
        if is_sell:
            combo_bid += qty * bid
            combo_ask += qty * ask
            combo_mid += qty * ((bid + ask) / 2.0)
        else:
            combo_bid -= qty * ask
            combo_ask -= qty * bid
            combo_mid -= qty * ((bid + ask) / 2.0)

    if quoted_legs == 0:
        return {
            "total_legs": total_legs,
            "quoted_legs": 0,
            "combo_bid": None,
            "combo_ask": None,
            "combo_mid": None,
            "combo_spread": None,
        }

    return {
        "total_legs": total_legs,
        "quoted_legs": quoted_legs,
        "combo_bid": combo_bid,
        "combo_ask": combo_ask,
        "combo_mid": combo_mid,
        "combo_spread": (combo_ask - combo_bid),
    }


def _capture_snapshot_from_summary(
    *,
    account_id: str,
    summary: dict[str, Any],
    ibkr_summary: dict[str, Any],
    vix_data: dict[str, Any],
    regime: Any,
    adapter: Any,
) -> None:
    try:
        from database.local_store import LocalStore
        from models.order import AccountSnapshot

        net_liq = None
        raw_nlv = ibkr_summary.get("netliquidation") if isinstance(ibkr_summary, dict) else None
        try:
            if isinstance(raw_nlv, dict):
                raw_amount = raw_nlv.get("amount")
                net_liq = float(raw_amount) if raw_amount not in (None, "", "N/A") else None
            elif raw_nlv not in (None, "", "N/A"):
                net_liq = float(raw_nlv)
        except Exception:
            net_liq = None

        spx_price = None
        try:
            spx_price = float(
                (getattr(adapter, "last_greeks_status", {}) or {}).get("spx_price")
                or summary.get("spx_price")
                or 0.0
            ) or None
        except Exception:
            spx_price = None

        snap = AccountSnapshot(
            captured_at=datetime.now(timezone.utc).isoformat(),
            account_id=account_id,
            broker="IBKR",
            net_liquidation=net_liq,
            spx_delta=float(summary.get("total_spx_delta", 0.0) or 0.0),
            gamma=float(summary.get("total_gamma", 0.0) or 0.0),
            theta=float(summary.get("total_theta", 0.0) or 0.0),
            vega=float(summary.get("total_vega", 0.0) or 0.0),
            vix=float(vix_data.get("vix", 0.0) or 0.0),
            spx_price=spx_price,
            regime=getattr(regime, "name", str(regime)),
        )
        _run_async(LocalStore().capture_snapshot(snap))
    except Exception as exc:
        LOGGER.debug("Foreground snapshot capture skipped: %s", exc)


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
    import concurrent.futures

    async def _fetch():
        try:
            from database.db_manager import DBManager
            db = DBManager()
            await db.connect()
            return await db.get_recent_market_intel(limit=20)
        except Exception as exc:
            LOGGER.debug("PostgreSQL market_intel unavailable (%s), using LocalStore", exc)
        try:
            from database.local_store import LocalStore
            db = LocalStore()
            return await db.get_recent_market_intel(limit=20)
        except Exception as exc2:
            LOGGER.debug("LocalStore market_intel also failed: %s", exc2)
            return []

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, _fetch()).result(timeout=15)
    except Exception as exc:
        LOGGER.debug("market_intel fetch skipped: %s", exc)
        return []


@st.cache_data(ttl=60)
def _fetch_active_signals_cached() -> list[dict]:
    """Fetch active arbitrage signals from the DB (cached 60 s)."""
    import concurrent.futures

    async def _fetch():
        try:
            from database.db_manager import DBManager
            db = DBManager()
            await db.connect()
            return await db.get_active_signals(limit=50)
        except Exception as exc:
            LOGGER.debug("signals fetch skipped: %s", exc)
            return []

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, _fetch()).result(timeout=15)
    except Exception as exc:
        LOGGER.debug("signals fetch skipped: %s", exc)
        return []


@st.cache_data(ttl=120)
def _fetch_llm_intel_cached(source: str, symbol: str | None = None) -> dict | None:
    """Return the latest market_intel row for a given LLM ``source`` tag (TTL 120 s)."""
    import concurrent.futures

    async def _fetch():
        row = None
        try:
            from database.db_manager import DBManager
            db = DBManager()
            await db.connect()
            rows = await db.get_market_intel_by_source(source, symbol=symbol, limit=1)
            row = rows[0] if rows else None
        except Exception as exc:
            LOGGER.debug("PostgreSQL llm_intel unavailable (%s), using LocalStore", exc)
        if row is None:
            try:
                from database.local_store import LocalStore
                db = LocalStore()
                rows = await db.get_market_intel_by_source(source, symbol=symbol, limit=1)
                row = rows[0] if rows else None
            except Exception as exc2:
                LOGGER.debug("LocalStore llm_intel also failed: %s", exc2)
        return row

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


# ── Worker job dispatch helpers ────────────────────────────────────────────────

def _get_sync_db_conn():
    """Return a synchronous psycopg2 connection using the same env vars as DBManager."""
    import psycopg2  # type: ignore[import]

    host = os.getenv("DB_HOST", "localhost").split("#")[0].strip()
    port = int(os.getenv("DB_PORT", "5432").split("#")[0].strip())
    dbname = os.getenv("DB_NAME", "portfolio_engine").split("#")[0].strip()
    user = os.getenv("DB_USER", "portfolio").split("#")[0].strip()
    password = os.getenv("DB_PASS", "").split("#")[0].strip()
    return psycopg2.connect(host=host, port=port, dbname=dbname, user=user, password=password, connect_timeout=5)


def _has_active_job(job_type: str) -> bool:
    """Return True if there is already a *recent* pending/running job of this type.

    Used to prevent job pile-up when the dashboard renders faster than workers complete.
    Stale rows (e.g., orphaned running jobs from old worker crashes) are ignored.
    """
    try:
        active_window_seconds = int(os.getenv("JOB_ACTIVE_WINDOW_SECONDS", "300"))
        conn = _get_sync_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM worker_jobs
                    WHERE job_type = %s
                      AND status IN ('pending', 'running')
                      AND updated_at >= NOW() - (%s || ' seconds')::INTERVAL
                    LIMIT 1
                    """,
                    (job_type, str(active_window_seconds)),
                )
                return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception:
        return False  # on error, allow dispatch


def _dispatch_job(job_type: str, payload: dict | None = None) -> str | None:
    """Enqueue a background worker job; return the job_id or None on error.

    Skips insertion if a pending or running job already exists for this type,
    preventing pile-up when the Streamlit render loop fires faster than workers complete.
    Uses synchronous psycopg2 to avoid asyncpg pool / event-loop sharing issues
    that occur when asyncio.run() is called repeatedly from different threads.
    """
    import json as _json

    if _has_active_job(job_type):
        LOGGER.debug("dispatch_job(%s) skipped — active job already queued", job_type)
        return None

    try:
        conn = _get_sync_db_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO worker_jobs (job_type, payload, status, created_at, updated_at)
                        VALUES (%s, %s::JSONB, 'pending', NOW(), NOW())
                        RETURNING id::TEXT
                        """,
                        (job_type, _json.dumps(payload or {})),
                    )
                    row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception as exc:
        LOGGER.warning("dispatch_job(%s) failed: %s", job_type, exc)
        return None


def _get_job_result(job_type: str, max_age_seconds: float = 60) -> dict | None:
    """Return the latest completed result for job_type if younger than max_age_seconds.

    Uses synchronous psycopg2 to avoid asyncpg pool / event-loop sharing issues.
    """
    import json as _json

    try:
        conn = _get_sync_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT result
                    FROM worker_jobs
                    WHERE job_type = %s
                      AND status = 'done'
                      AND updated_at >= NOW() - (%s || ' seconds')::INTERVAL
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (job_type, str(int(max_age_seconds))),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if row is None or row[0] is None:
            return None
        result = row[0]
        if isinstance(result, str):
            result = _json.loads(result)
        return result
    except Exception as exc:
        LOGGER.warning("get_job_result(%s) failed: %s", job_type, exc)
        return None


def _positions_from_dicts(position_dicts: list[dict]) -> list:
    """Reconstruct UnifiedPosition instances from serialized dicts."""
    from models.unified_position import UnifiedPosition

    model_fields = getattr(UnifiedPosition, "model_fields", {})
    pos_fields = set(model_fields.keys()) if model_fields else set()
    positions = []
    for d in position_dicts:
        try:
            filtered = {k: v for k, v in d.items() if not pos_fields or k in pos_fields}
            positions.append(UnifiedPosition(**filtered))
        except Exception as exc:
            LOGGER.debug("position deserialization skipped: %s", exc)
    return positions


def _fetch_account_summary_fast(adapter: Any, account_id: str) -> dict[str, object]:
    """Fetch account summary with a strict timeout to avoid blocking UI render."""
    fn = getattr(adapter, "get_account_summary", None) or getattr(getattr(adapter, "client", None), "get_account_summary", None)
    if not callable(fn):
        return {}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(fn, account_id)
            payload = future.result(timeout=ACCOUNT_SUMMARY_TIMEOUT_SECONDS)
            return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        LOGGER.debug("Fast account summary fetch failed/timeout for %s: %s", account_id, exc)
        return {}


def _render_trade_proposer_queue(
    *,
    summary: dict,
    ibkr_summary: dict,
    vix_data: dict,
    macro_data: dict | None,
    regime: Any,
    adapter: Any,
    account_id: str,
) -> None:
    """Trade Proposer Queue — generate and display capital-efficient trade proposals."""
    st.subheader("🛡️ Trade Proposer Queue")
    _proposal_header_cols = st.columns([2, 3])
    with _proposal_header_cols[0]:
        if st.button("✨ Suggest New Trades", key="suggest_new_trades_btn"):
            _nlv_for_proposer: float = 100_000.0
            try:
                _raw_nlv = ibkr_summary.get("netliquidation") if isinstance(ibkr_summary, dict) else None
                if isinstance(_raw_nlv, dict):
                    _v = _raw_nlv.get("amount")
                    if _v not in (None, "", "N/A"):
                        _nlv_for_proposer = float(_v)
                elif _raw_nlv not in (None, "", "N/A"):
                    _nlv_for_proposer = float(_raw_nlv)
            except Exception:
                pass

            _greeks_for_proposer = {
                "vix": float(vix_data.get("vix") or 0.0),
                "term_structure": float(vix_data.get("term_structure") or 1.0),
                "recession_prob": float(macro_data.get("recession_probability") or 0.0) if isinstance(macro_data, dict) else 0.0,
                "total_vega": float(summary.get("total_vega") or 0.0),
                "spx_delta": float(summary.get("total_spx_delta") or 0.0),
                "total_theta": float(summary.get("total_theta") or 0.0),
                "total_gamma": float(summary.get("total_gamma") or 0.0),
                "spx_price": float(adapter.last_greeks_status.get("spx_price", 0.0) or st.session_state.get("last_spx_price", 0.0) or 0.0),
                "account_id": account_id,
            }
            with st.spinner("Analyzing portfolio for risk-improving trades…"):
                try:
                    from agents.proposer_engine import BreachDetector, ProposerEngine, RiskRegimeLoader
                    from sqlmodel import SQLModel as _SM, Session as _Sess, create_engine as _ce
                    from models.proposed_trade import ProposedTrade as _PT
                    _p_loader = RiskRegimeLoader()
                    _p_detector = BreachDetector(_p_loader)
                    _p_engine = ProposerEngine(adapter=None, loader=_p_loader)
                    _p_breaches = _p_detector.check(
                        _greeks_for_proposer, account_nlv=_nlv_for_proposer,
                        account_id=account_id, margin_used=0.0,
                    )
                    if not _p_breaches:
                        st.success("✅ No risk breaches detected.")
                    else:
                        _p_candidates = _run_async(_p_engine.generate(
                            _p_breaches, account_id=account_id, nlv=_nlv_for_proposer,
                            atm_price=float(_greeks_for_proposer.get("spx_price", 0.0)),
                        ))
                        if _p_candidates:
                            _p_db_url = _resolve_proposer_db_url()
                            if _p_db_url:
                                _p_eng2 = _ce(_p_db_url)
                                _SM.metadata.create_all(_p_eng2, tables=[_PT.__table__])
                                with _Sess(_p_eng2) as _p_sess:
                                    _p_engine.persist_top3(account_id, _p_candidates, _p_sess)
                                st.success(f"✅ {len(_p_candidates)} candidate(s) generated.")
                            else:
                                st.warning("No DB configured.")
                            st.rerun()
                        else:
                            st.info("No viable candidates for current Greeks.")
                except Exception as _p_exc:
                    st.error(f"Proposer failed: {_p_exc}")
    with _proposal_header_cols[1]:
        st.caption("Uses live Greeks + regime to generate top 3 capital-efficient trades.")

    try:
        _proposed_rows: list[dict] = []
        _db_url = _resolve_proposer_db_url()
        if _db_url:
            async def _fetch_proposed() -> list[dict]:
                try:
                    from sqlmodel import SQLModel, Session, create_engine, select, or_ as _or
                    from models.proposed_trade import ProposedTrade
                    _eng = create_engine(_db_url)
                    SQLModel.metadata.create_all(_eng, tables=[ProposedTrade.__table__])
                    with Session(_eng) as _sess:
                        stmt = select(ProposedTrade).where(
                            _or(ProposedTrade.status == "Pending", ProposedTrade.status == "Superseded")
                        ).order_by(ProposedTrade.created_at.desc()).limit(9)
                        rows = []
                        for r in _sess.exec(stmt).all():
                            legs = r.legs_json or []
                            leg_summary = "; ".join(
                                f"{l.get('action','?')} {l.get('right','?')}{l.get('strike','?')} "
                                f"bid={l.get('bid', '?')} ask={l.get('ask', '?')}"
                                for l in legs if l.get("conId", 0) != 0 or l.get("bid") is not None
                            ) or "—"
                            rows.append({
                                "id": r.id, "strategy": r.strategy_name, "status": r.status,
                                "score": round(r.efficiency_score, 3), "net_premium": round(r.net_premium, 2),
                                "justification": r.justification, "legs_json": legs,
                                "legs_bid_ask": leg_summary, "created_at": str(r.created_at)[:19],
                                "account": r.account_id,
                            })
                        return rows
                except Exception:
                    return []

            _proposed_rows = _run_async(_fetch_proposed())

        if _proposed_rows:
            _pending_rows = [r for r in _proposed_rows if r.get("status") == "Pending"]
            _superseded_rows = [r for r in _proposed_rows if r.get("status") != "Pending"]

            def _render_proposal_card(row: dict, idx: int) -> None:
                _pid = row.get("id", idx)
                _strat = row.get("strategy", "Unknown")
                _score = row.get("score", 0.0)
                _prem = row.get("net_premium", 0.0)
                _just = row.get("justification", "")
                _legs = _build_order_legs_from_signal(
                    {"signal_type": "", "legs_json": row.get("legs_json", [])}, default_underlying="SPX",
                ) or [lg for lg in row.get("legs_json", []) if isinstance(lg, dict)]
                _ts = row.get("created_at", "")[:16]
                _stat = row.get("status", "")

                with st.container(border=True):
                    _card_cols = st.columns([1, 5])
                    with _card_cols[0]:
                        if st.button("🚀 Create\nOrder", key=f"prop_create_{_pid}_{idx}",
                                     use_container_width=True):
                            if _legs and _prefill_order_builder_from_legs(
                                legs=_legs, source_label=f"proposal #{_pid} — {_strat}",
                                rationale=str(_just or ""),
                            ):
                                st.rerun()
                        _ab_cols = st.columns(2)
                        with _ab_cols[0]:
                            if st.button("✅", key=f"prop_approve_{_pid}_{idx}"):
                                async def _approve_card(pid: int = _pid) -> None:
                                    from sqlmodel import Session, create_engine
                                    from models.proposed_trade import ProposedTrade
                                    with Session(create_engine(_db_url)) as _s:
                                        _r = _s.get(ProposedTrade, pid)
                                        if _r:
                                            _r.status = "Approved"
                                            _s.add(_r); _s.commit()
                                _run_async(_approve_card()); st.rerun()
                        with _ab_cols[1]:
                            if st.button("❌", key=f"prop_reject_{_pid}_{idx}"):
                                async def _reject_card(pid: int = _pid) -> None:
                                    from sqlmodel import Session, create_engine
                                    from models.proposed_trade import ProposedTrade
                                    with Session(create_engine(_db_url)) as _s:
                                        _r = _s.get(ProposedTrade, pid)
                                        if _r:
                                            _r.status = "Rejected"
                                            _s.add(_r); _s.commit()
                                _run_async(_reject_card()); st.rerun()
                    with _card_cols[1]:
                        _badge = "🟢 Pending" if _stat == "Pending" else "⬜ Previous"
                        st.markdown(f"**{_strat}** {_badge} Score: `{_score:.3f}` Net: `${_prem:.2f}` _{_ts}_")
                        if _just:
                            st.caption(f"📋 {_just}")
                        if _legs:
                            _pq = _estimate_combo_quote(_legs)
                            if _pq.get("combo_bid") is not None:
                                st.caption(f"Quote — Bid {float(_pq['combo_bid']):.2f} | Ask {float(_pq['combo_ask']):.2f} | Mid {float(_pq['combo_mid']):.2f}")

            if _pending_rows:
                st.caption(f"**{len(_pending_rows)} pending** — click 🚀 to stage in Order Builder")
                for _i, _row in enumerate(_pending_rows):
                    _render_proposal_card(_row, _i)
            if _superseded_rows:
                with st.expander(f"Previous ({len(_superseded_rows)})", expanded=not _pending_rows):
                    for _i, _row in enumerate(_superseded_rows):
                        _render_proposal_card(_row, 1000 + _i)
        else:
            if _db_url:
                st.info("No proposals found. Click **✨ Suggest New Trades**.")
            else:
                st.caption("Set `PROPOSER_DB_URL` in `.env` to enable.")
    except Exception as _tp_exc:
        if type(_tp_exc).__module__.startswith("streamlit"):
            raise
        LOGGER.warning("Trade Proposer Queue failed: %s", _tp_exc, exc_info=True)
        st.caption(f"Trade Proposer Queue unavailable: {_tp_exc}")


def render_portfolio_content() -> None:
    """Render all dashboard content.

    Called by both the standalone ``main()`` entry-point and the multi-page
    navigation wrapper in ``dashboard/main.py``.  Does NOT call
    ``st.set_page_config`` — that must be done exactly once by the caller.

    Layout order (after overhaul):
      1. Account summary + Portfolio Greeks  (top)
      2. Positions split: Futures/Stocks | Options (stale highlighting, Buy/Sell/Roll)
      3. Risk Compliance + suggested trades
      4. Arbitrage Signals (sorted by fill probability, with commissions)
      5. Options Chain (TWS-style)
      6. Order Builder + Open Orders
      7. AI Assistant (full tool access)
    """
    LOGGER.info("Starting dashboard run")
    inject_custom_css()

    adapter, portfolio_tools, market_tools, regime_detector = get_services()
    api_mode = os.getenv("IB_API_MODE", "PORTAL").split("#")[0].strip().upper()

    st.title("Portfolio Risk Manager")
    # NOTE: Live metric panels use @st.fragment(run_every=...) instead of full-page reload.
    # This lets greeks/positions refresh every 30 s without freezing the entire page.
    st.sidebar.header("Inputs")
    reload_accounts = st.sidebar.button("Reload Accounts")
    if api_mode != "SOCKET":
        render_ibkr_login_button(adapter)

    # ── Client Portal restart (hidden in SOCKET/TWS mode) ─────────────────────
    if api_mode != "SOCKET":
        with st.sidebar.expander("Client Portal Controls", expanded=False):
            gateway_alive = adapter.client.check_gateway_status()
            st.markdown(
                f"**Gateway status:** {'🟢 Running' if gateway_alive else '🔴 Down'}"
            )
            col_restart, col_stop = st.columns(2)
            with col_restart:
                if st.button("Restart Portal", help="Stop + restart the IBKR Client Portal gateway"):
                    _job_id = _dispatch_job("restart_gateway", {})
                    if _job_id:
                        st.info("⏳ Gateway restart dispatched to worker. Check back in ~30s.")
                        st.markdown("[Open login page](https://localhost:5001)")
                    else:
                        st.error("Could not dispatch restart job. Is the worker running?")
            with col_stop:
                if st.button("Stop Portal", help="Gracefully stop the gateway process"):
                    adapter.client.stop_gateway()
                    st.warning("Portal stopped.")

            # Show last 60 lines of the gateway log
            log_path = PROJECT_ROOT / ".clientportal.log"
            if log_path.exists():
                try:
                    lines = log_path.read_text(errors="replace").splitlines()
                    tail = "\n".join(lines[-60:]) if len(lines) > 60 else "\n".join(lines)
                    st.text_area("Gateway log (last 60 lines)", value=tail, height=200)
                except Exception:
                    st.caption("Log file unreadable.")
            else:
                st.caption("No gateway log found yet.")

    if api_mode == "SOCKET":
        # In TWS socket mode load accounts directly from env — no Client Portal REST call needed.
        if "ibkr_accounts" not in st.session_state or reload_accounts:
            _env_accts = [
                a.strip()
                for a in os.getenv("IB_ACCOUNTS", "").split(",")
                if a.strip() and not a.strip().startswith("DU")
            ]
            if not _env_accts:
                import glob as _glob
                _env_accts = sorted({
                    Path(f).stem.replace(".positions_snapshot_", "")
                    for f in _glob.glob(str(PROJECT_ROOT / ".positions_snapshot_*.json"))
                    if not Path(f).stem.replace(".positions_snapshot_", "").startswith("DU")
                })
            st.session_state["ibkr_accounts"] = [{"accountId": a} for a in _env_accts]
    else:
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
        if api_mode == "SOCKET":
            st.error("No IBKR accounts found. Set IB_ACCOUNTS in .env (e.g. IB_ACCOUNTS=U1234567).")
        else:
            st.error("No IBKR accounts available from gateway. Use 'Sign in to IBKR' in the sidebar, then click 'Reload Accounts'.")
        st.stop()

    account_id = st.sidebar.selectbox("IBKR Account", options=account_options, index=0)
    refresh = st.sidebar.button("Refresh")
    ibkr_option_scaling = st.sidebar.checkbox("IBKR-style option scaling (x100)", value=False)
    use_cached_fallback = st.sidebar.checkbox("Use latest cached portfolio if IBKR unavailable", value=True)
    ibkr_only_mode = st.sidebar.checkbox("IBKR-only mode (no external Greeks)", value=True)

    # ── LLM model picker ──────────────────────────────────────────────
    # ── Flatten Risk sidebar shortcut (T069: accessible within 2 clicks) ─
    if st.sidebar.button("🚨 Flatten Risk", key="sidebar_flatten_risk_btn",
                         help="Buy-to-close all short options — confirmation required"):
        st.session_state["_sidebar_flatten_requested"] = True

    with st.sidebar.expander("🤖 AI / LLM Settings", expanded=False):
        _all_models = _get_available_models()
        _model_labels = [f"{m['name']} {'🆓' if m['is_free'] else '💰'}" for m in _all_models]
        _model_ids = [m["id"] for m in _all_models]
        _default_model_idx = next((i for i, m in enumerate(_all_models) if m["id"] == "gpt-4.1"), 0)
        _sel_idx = st.selectbox(
            "Model",
            options=range(len(_model_labels)),
            format_func=lambda i: _model_labels[i],
            index=_default_model_idx,
            key="llm_model_picker",
        )
        selected_llm_model: str = _model_ids[_sel_idx] if _model_ids else "gpt-4.1"

    with st.sidebar.expander("🔬 Greeks Diagnostics", expanded=False):
        disable_tasty_cache = st.checkbox(
            "Disable Tasty cache (live fetch only)",
            value=bool(getattr(adapter, "disable_tasty_cache", False)),
        )
        force_refresh_on_miss = st.checkbox(
            "Force live fetch on cache miss",
            value=bool(getattr(adapter, "force_refresh_on_miss", True)),
            disabled=disable_tasty_cache,
        )
    # ibkr_only_mode mirrors CLI --ibkr-only: disable Tastytrade, use IBKR snapshot only.
    # The adapter flags must be set BEFORE fetch_greeks is called below.
    if ibkr_only_mode:
        adapter.disable_tasty_cache = True
        adapter.force_refresh_on_miss = False
    else:
        adapter.disable_tasty_cache = bool(disable_tasty_cache)
        adapter.force_refresh_on_miss = bool(force_refresh_on_miss)

    if refresh:
        get_cached_vix_data.clear()
        get_cached_macro_data.clear()
        get_cached_historical_volatility.clear()
        _fetch_market_intel_cached.clear()
        _fetch_active_signals_cached.clear()
        _fetch_llm_intel_cached.clear()

    if account_id is None:
        st.error("Unable to resolve a valid IBKR account ID from gateway response.")
        st.stop()

    with st.spinner("Loading portfolio and market data…"):
        data_refresh = st.session_state.setdefault("data_refresh_timestamps", {})
        positions = st.session_state.get("positions")
        if not isinstance(positions, list):
            positions = []
        previous_positions_for_account = positions
        previous_account = st.session_state.get("selected_account")
        fallback_saved_at = None
        st.session_state["greeks_refresh_fallback"] = False
        st.session_state["greeks_refresh_fallback_reason"] = ""

        # ── Worker-based greeks fetch ─────────────────────────────────────────
        # Try to get positions+greeks from the latest completed worker job.
        # This avoids blocking the Streamlit render thread on heavy I/O.
        _worker_result = _get_job_result("fetch_greeks", max_age_seconds=WORKER_RESULT_MAX_AGE_SECONDS)
        _worker_positions = (
            _positions_from_dicts(_worker_result["positions"])
            if _worker_result and _worker_result.get("positions")
            else None
        )

        if _worker_positions:
            # Fresh result from worker — no blocking needed
            positions = _worker_positions
            data_refresh["positions"] = _safe_iso_now()
            data_refresh["greeks"] = _safe_iso_now()
            st.session_state["positions"] = positions
            st.session_state["selected_account"] = account_id
            st.session_state["fallback_saved_at"] = None
            fallback_saved_at = None
            # Propagate SPX price from worker result so the SPX delta panel can use it
            _worker_spx = float(_worker_result.get("spx_price") or 0.0)
            if _worker_spx > 0:
                st.session_state["last_spx_price"] = _worker_spx
                adapter.last_greeks_status["spx_price"] = _worker_spx
            # Dispatch a new job if the result is getting stale or refresh was requested
            _stale = _get_job_result("fetch_greeks", max_age_seconds=PORTFOLIO_REFRESH_SECONDS) is None
            if refresh or _stale or previous_account != account_id:
                _dispatch_job("fetch_greeks", {"account_id": account_id, "ibkr_only": ibkr_only_mode})
        else:
            # No fresh worker result — dispatch job for future renders, then
            # fall back to the blocking path so the page still loads with data.
            _dispatch_job("fetch_greeks", {"account_id": account_id, "ibkr_only": ibkr_only_mode})

            if use_cached_fallback and (positions is None or previous_account != account_id) and not refresh:
                cached_positions, fallback_saved_at = load_positions_snapshot(account_id)
                if cached_positions:
                    positions = cached_positions
                    data_refresh["positions"] = fallback_saved_at or _safe_iso_now()
                    data_refresh["greeks"] = fallback_saved_at or _safe_iso_now()
                    st.session_state["positions"] = positions
                    st.session_state["selected_account"] = account_id
                    st.session_state["fallback_saved_at"] = fallback_saved_at

            should_block_for_live_fetch = refresh or not NON_BLOCKING_DASHBOARD_LOAD

            if should_block_for_live_fetch and (positions is None or previous_account != account_id or refresh):
                fetched_positions = []
                try:
                    fetched_positions = _run_async(
                        asyncio.wait_for(
                            adapter.fetch_positions(account_id),
                            timeout=POSITIONS_FETCH_TIMEOUT_SECONDS,
                        )
                    )
                except TimeoutError:
                    LOGGER.warning(
                        "Timed out fetching positions for %s after %ss; using fallback path",
                        account_id,
                        POSITIONS_FETCH_TIMEOUT_SECONDS,
                    )
                except Exception as exc:
                    LOGGER.warning("Position fetch failed for %s: %s", account_id, exc)
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
                if positions is None:
                    positions = []
                    st.session_state["positions"] = positions
                    st.session_state["selected_account"] = account_id
                fallback_saved_at = st.session_state.get("fallback_saved_at")

            # Enrich with greeks only when we chose the blocking fetch path.
            if should_block_for_live_fetch and positions:
                try:
                    positions = _run_async(
                        asyncio.wait_for(
                            adapter.fetch_greeks(positions),
                            timeout=GREEKS_FETCH_TIMEOUT_SECONDS,
                        )
                    )
                except TimeoutError:
                    LOGGER.warning(
                        "Timed out enriching Greeks for %s after %ss; keeping positions without new Greeks",
                        account_id,
                        GREEKS_FETCH_TIMEOUT_SECONDS,
                    )
                except Exception as exc:
                    LOGGER.warning("Greeks enrichment failed for %s: %s", account_id, exc)
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
        # Strict-timeout account summary fetch so UI sections render quickly.
        summary_payload = _fetch_account_summary_fast(adapter, account_id)
        ibkr_summary: dict[str, object] = summary_payload if isinstance(summary_payload, dict) else {}

    summary = portfolio_tools.get_portfolio_summary(positions)
    violations = portfolio_tools.check_risk_limits(summary, regime)

    # Persist live summary into LocalStore on UI refresh cadence so charts/DB stay current.
    _now_ts = time.time()
    _last_snapshot_ui_ts = float(st.session_state.get("_snapshot_ui_capture_ts", 0.0) or 0.0)
    if (_now_ts - _last_snapshot_ui_ts) >= float(max(5, PORTFOLIO_REFRESH_SECONDS)):
        _capture_snapshot_from_summary(
            account_id=account_id,
            summary=summary,
            ibkr_summary=ibkr_summary,
            vix_data=vix_data,
            regime=regime,
            adapter=adapter,
        )
        st.session_state["_snapshot_ui_capture_ts"] = _now_ts

    # T060: Start background snapshot logger (once per session)
    _start_snapshot_logger(
        account_id=account_id,
        adapter=adapter,
        portfolio_tools=portfolio_tools,
        regime_detector=regime_detector,
    )

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

        # T061: Last snapshot indicator
        _snap_at = st.session_state.get("_snapshot_last_at")
        _snap_err = st.session_state.get("_snapshot_last_error")
        if _snap_err:
            st.caption(f"Snapshot logger error: {_snap_err[:60]}")
        elif _snap_at:
            _snap_age = _age_minutes_from_iso(_snap_at)
            _snap_age_str = f" ({_snap_age:.1f} min old)" if _snap_age is not None else ""
            st.caption(f"Last snapshot: {_snap_at[11:19]} UTC{_snap_age_str}")
        else:
            st.caption("Snapshot logger: starting… (first snapshot in 15 min)")

    if ibkr_only_mode:
        st.caption("IBKR-only mode: Greeks from IBKR snapshot only.")


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

    # ── Greeks diagnostics (collapsed) ───────────────────────────────────
    if positions:
        options_count = sum(1 for position in positions if position.instrument_type.name == "OPTION")
        if options_count > 0:
            greeks_status = getattr(adapter, "last_greeks_status", {})
            cache_miss_count = int(greeks_status.get("cache_miss_count", 0))
            session_error = greeks_status.get("last_session_error")
            missing_greeks_details = greeks_status.get("missing_greeks_details") or []

            with st.expander("🔬 Greeks Diagnostics", expanded=False):
                source_counts = Counter(
                    getattr(position, "greeks_source", "none") for position in positions if position.instrument_type.name == "OPTION"
                )
                reason_counts = Counter(item.get("reason") or "unknown" for item in missing_greeks_details)
                st.caption(
                    "Greeks diagnostics — "
                    f"disable_cache={bool(greeks_status.get('disable_tasty_cache', adapter.disable_tasty_cache))}, "
                    f"force_refresh_on_miss={bool(greeks_status.get('force_refresh_on_miss', adapter.force_refresh_on_miss))}"
                )
                st.write({"greeks_source_counts": dict(source_counts), "missing_reason_counts": dict(reason_counts)})

                if cache_miss_count > 0:
                    st.warning(f"Option Greeks missing for {cache_miss_count}/{options_count} option positions.")
                if missing_greeks_details:
                    missing_df = pd.DataFrame(missing_greeks_details)
                    st.dataframe(missing_df, width="stretch")
                    dl_cols = st.columns(2)
                    with dl_cols[0]:
                        st.download_button("Download Missing Greeks CSV", missing_df.to_csv(index=False).encode("utf-8"),
                                           file_name=f"missing_greeks_{account_id}.csv", mime="text/csv")
                    with dl_cols[1]:
                        st.download_button("Download Missing Greeks JSON", json.dumps(missing_greeks_details, indent=2).encode("utf-8"),
                                           file_name=f"missing_greeks_{account_id}.json", mime="application/json")
                if session_error:
                    st.info(f"Tastytrade auth detail: {session_error}.")

    # ── Persist ibkr_summary for fragment access across periodic reruns ──────
    st.session_state["_live_ibkr_summary"] = ibkr_summary

    # ── Build ExecutionEngine once (available to fragment + sections 3-7) ─────
    try:
        from core.execution import ExecutionEngine
        from database.local_store import LocalStore as _LS
        _exec_engine = ExecutionEngine(
            ibkr_gateway_client=adapter.client,
            local_store=_LS(),
            beta_weighter=adapter._beta_weighter,
        )
    except Exception as _ee_exc:
        LOGGER.warning("Could not build ExecutionEngine: %s", _ee_exc)
        _exec_engine = None

    # ══════════════════════════════════════════════════════════════════════
    # ██  SECTIONS 1+2 — LIVE METRICS + POSITIONS (auto-refresh every 30 s) ██
    # Uses @st.fragment so only this panel reruns — no full-page reload.
    # ══════════════════════════════════════════════════════════════════════
    @st.fragment(run_every=f"{PORTFOLIO_REFRESH_SECONDS}s")
    def _live_metrics_panel() -> None:
        """Greeks + positions panel that auto-refreshes every PORTFOLIO_REFRESH_SECONDS
        without blocking or reloading the rest of the page."""

        # ── 1. Poll latest background-worker result ───────────────────────
        _wr = _get_job_result("fetch_greeks", max_age_seconds=WORKER_RESULT_MAX_AGE_SECONDS)
        if _wr and _wr.get("positions"):
            _fresh_pos = _positions_from_dicts(_wr["positions"])
            if _fresh_pos:
                st.session_state["positions"] = _fresh_pos
                _spx = float(_wr.get("spx_price") or 0.0)
                if _spx > 0:
                    adapter.last_greeks_status["spx_price"] = _spx
                    st.session_state["last_spx_price"] = _spx

        # ── 2. Keep worker job fresh ──────────────────────────────────────
        _ibkr_only = bool(st.session_state.get("ibkr_only_mode", True))
        if _get_job_result("fetch_greeks", max_age_seconds=PORTFOLIO_REFRESH_SECONDS) is None:
            _dispatch_job("fetch_greeks", {"account_id": account_id, "ibkr_only": _ibkr_only})

        # ── 3. Read latest data ───────────────────────────────────────────
        _positions = st.session_state.get("positions") or []
        _summary = portfolio_tools.get_portfolio_summary(_positions)
        _ibkr_sum = st.session_state.get("_live_ibkr_summary") or {}

        # ── Section 1: Account Summary + Portfolio Greeks ─────────────────
        st.header("📊 Account & Portfolio Greeks")

        def _to_float(value: object) -> float | None:  # noqa: E306
            try:
                if isinstance(value, dict):
                    amount = value.get("amount")
                    return float(amount) if amount not in (None, "", "N/A") else None
                return float(str(value).replace(",", "")) if value not in (None, "", "N/A") else None
            except (TypeError, ValueError):
                return None

        # Risk-first account metrics
        risk_cols = st.columns(4)
        margin_usage_pct = 0.0
        if _ibkr_sum:
            net_liq = _to_float(_ibkr_sum.get("netliquidation"))
            buying_power = _to_float(_ibkr_sum.get("buyingpower"))
            maint_margin = _to_float(_ibkr_sum.get("maintmarginreq"))
            excess_liq = _to_float(_ibkr_sum.get("excessliquidity"))
            if net_liq and maint_margin and net_liq > 0:
                margin_usage_pct = (maint_margin / net_liq) * 100
        else:
            net_liq = buying_power = maint_margin = excess_liq = None

        risk_cols[0].metric("Net Liquidation", f"${net_liq:,.0f}" if net_liq else "N/A")
        risk_cols[1].metric("Buying Power", f"${buying_power:,.0f}" if buying_power else "N/A")
        risk_cols[2].metric(
            "Margin Usage",
            f"{margin_usage_pct:.1f}%",
            delta="High" if margin_usage_pct > 50 else "Safe",
            delta_color="inverse" if margin_usage_pct > 50 else "normal",
        )
        risk_cols[3].metric("Excess Liquidity", f"${excess_liq:,.0f}" if excess_liq else "N/A")

        # Portfolio Greeks row
        _total_spx_delta = float(_summary.get("total_spx_delta", 0.0))
        _total_vega = float(_summary.get("total_vega", 0.0))
        _theta_vega_ratio = float(_summary.get("theta_vega_ratio", 0.0))

        greek_cols = st.columns(6)
        greek_cols[0].metric("SPX β-Δ", f"{_total_spx_delta:.1f}",
                             delta="Directional" if abs(_total_spx_delta) > 100 else "Neutral",
                             delta_color="inverse" if abs(_total_spx_delta) > 100 else "normal")
        greek_cols[1].metric("Delta", f"{_summary['total_delta']:.2f}")
        greek_cols[2].metric("Theta", f"{_summary['total_theta']:.2f}")
        greek_cols[3].metric("Vega", f"{_total_vega:.1f}",
                             delta="Short Vol" if _total_vega < -1000 else None,
                             delta_color="inverse" if _total_vega < -1000 else "normal")
        greek_cols[4].metric("Gamma", f"{_summary['total_gamma']:.4f}")
        greek_cols[5].metric("Θ/V Ratio", f"{_theta_vega_ratio:.3f}")

        _equity_positions = [
            p for p in _positions
            if getattr(p.instrument_type, "name", "") in {"EQUITY", "STOCK", "ETF"}
        ]
        if _equity_positions:
            _eq_spx_delta = sum(float(getattr(p, "spx_delta", 0.0) or 0.0) for p in _equity_positions)
            _beta_missing = sum(1 for p in _equity_positions if bool(getattr(p, "beta_unavailable", False)))
            st.caption(
                f"Equity SPX Δ: {_eq_spx_delta:.2f} across {len(_equity_positions)} stock position(s)"
                f" | beta_unavailable: {_beta_missing}"
            )

        # Additional high-signal risk diagnostics
        try:
            _gross_exposure = sum(abs(float(getattr(p, "market_value", 0.0) or 0.0)) for p in _positions)
            _net_exposure = sum(float(getattr(p, "market_value", 0.0) or 0.0) for p in _positions)
            _by_symbol: dict[str, float] = {}
            for _p in _positions:
                _sym = str(getattr(_p, "underlying", "") or getattr(_p, "symbol", "") or "").upper()
                _by_symbol[_sym] = _by_symbol.get(_sym, 0.0) + abs(float(getattr(_p, "market_value", 0.0) or 0.0))
            _top_name, _top_val = ("—", 0.0)
            if _by_symbol:
                _top_name, _top_val = max(_by_symbol.items(), key=lambda kv: kv[1])
            _top_pct = (_top_val / _gross_exposure * 100.0) if _gross_exposure > 0 else 0.0
            _beta_missing_cnt = sum(1 for _p in _positions if bool(getattr(_p, "beta_unavailable", False)))

            _extra_cols = st.columns(4)
            _extra_cols[0].metric("Gross Exposure", f"${_gross_exposure:,.0f}")
            _extra_cols[1].metric("Net Exposure", f"${_net_exposure:,.0f}")
            _extra_cols[2].metric("Top Concentration", f"{_top_name} {_top_pct:.1f}%")
            _extra_cols[3].metric("β Missing", f"{_beta_missing_cnt}")
        except Exception:
            pass

        _spx_price_for_display = (
            adapter.last_greeks_status.get("spx_price", 0.0)
            or st.session_state.get("last_spx_price", 0.0)
            or 0.0
        )
        if not _spx_price_for_display or _spx_price_for_display <= 0:
            st.error("⛔ **SPX price unavailable** — SPX delta cannot be computed.")

        # ── Section 2: Positions split ────────────────────────────────────
        if _positions:
            from dashboard.components.positions_view import render_positions_split
            _ibkr_scaling = bool(st.session_state.get("ibkr_option_scaling", False))
            render_positions_split(
                positions=_positions,
                ibkr_option_scaling=_ibkr_scaling,
                adapter=adapter,
                account_id=account_id,
                exec_engine=_exec_engine,
                prefill_order_fn=_prefill_order_builder_from_legs,
            )

        # ── refresh timestamp indicator ───────────────────────────────────
        st.caption(
            f"🔄 Metrics auto-refresh every {PORTFOLIO_REFRESH_SECONDS}s · "
            f"last update: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
        )

    _live_metrics_panel()  # triggers on load AND every PORTFOLIO_REFRESH_SECONDS

    # ══════════════════════════════════════════════════════════════════════
    # ██  SECTION 3 — RISK COMPLIANCE + TRADE SUGGESTIONS  ██
    # ══════════════════════════════════════════════════════════════════════
    from dashboard.components.risk_compliance_view import render_risk_compliance
    render_risk_compliance(
        violations=violations,
        summary=summary,
        regime=regime,
        positions=positions,
        adapter=adapter,
        account_id=account_id,
        ibkr_summary=ibkr_summary,
        vix_data=vix_data,
        prefill_order_fn=_prefill_order_builder_from_legs,
    )

    # Trade Proposer Queue (capitalefficient suggestions)
    _render_trade_proposer_queue(
        summary=summary,
        ibkr_summary=ibkr_summary,
        vix_data=vix_data,
        macro_data=macro_data,
        regime=regime,
        adapter=adapter,
        account_id=account_id,
    )

    _render_order_draft_preview_block(key_prefix="order_draft_preview_top")

    # ══════════════════════════════════════════════════════════════════════
    # ██  SECTION 4 — ARBITRAGE SIGNALS (sorted by fill prob)  ██
    # ══════════════════════════════════════════════════════════════════════
    # MarketDataService for live quotes
    _mds_key = "_market_data_service"
    if _mds_key not in st.session_state or st.session_state[_mds_key] is None:
        try:
            st.session_state[_mds_key] = MarketDataService(
                ibkr_client=adapter.client,
                tastytrade_fetcher=adapter.client.options_cache,
            )
        except Exception as _mds_exc:
            LOGGER.warning("Could not build MarketDataService: %s", _mds_exc)
            st.session_state[_mds_key] = None
    _market_data_svc = st.session_state.get(_mds_key)

    _active_signals = _fetch_active_signals_cached()
    from dashboard.components.arb_signals_view import render_arb_signals
    render_arb_signals(
        signals=_active_signals,
        build_order_legs_fn=_build_order_legs_from_signal,
        with_live_quotes_fn=_with_live_option_quotes,
        prefill_order_fn=_prefill_order_builder_from_legs,
        estimate_combo_quote_fn=_estimate_combo_quote,
        market_data_svc=_market_data_svc,
    )

    # ══════════════════════════════════════════════════════════════════════
    # ██  SECTION 5 — OPTIONS BOOK  ██
    # ══════════════════════════════════════════════════════════════════════
    try:
        from dashboard.components.options_book_view import render_options_book
        render_options_book(
            adapter=adapter,
            summary=summary,
            prefill_order_fn=_prefill_order_builder_from_legs,
            symbols=["SPX", "/ES", "ES", "SPY", "MES", "QQQ"],
        )
    except Exception as _chain_exc:
        LOGGER.warning("Options book panel failed: %s", _chain_exc, exc_info=True)
        st.warning(f"⚠️ Options book panel error: {_chain_exc}")

    # ══════════════════════════════════════════════════════════════════════
    # ██  SECTION 6 — ORDER BUILDER + OPEN ORDERS  ██
    # ══════════════════════════════════════════════════════════════════════

    _current_greeks = None
    try:
        from models.order import PortfolioGreeks as _PG
        _current_greeks = _PG(
            spx_delta=float(summary.get("total_spx_delta", 0.0)),
            gamma=float(summary.get("total_gamma", 0.0)),
            theta=float(summary.get("total_theta", 0.0)),
            vega=float(summary.get("total_vega", 0.0)),
        )
    except Exception:
        pass

    _regime_key = getattr(regime, "name", "neutral_volatility").lower()
    _regime_map = {"low_volatility": "low_volatility", "neutral_volatility": "neutral_volatility",
                   "high_volatility": "high_volatility", "crisis_mode": "crisis_mode"}
    _regime_key = _regime_map.get(_regime_key, "neutral_volatility")

    # ── Anchor so "Create Order" can scroll the browser here ─────────────
    # If a draft was just created, scroll to the Order Builder expander
    if st.session_state.pop("_scroll_to_order_builder", False):
        # Use st_components.html to execute real JS (st.markdown <script> is sandboxed)
        st_components.html(
            """
            <script>
            (function() {
                // scrollIntoView on the anchor will scroll the nearest scrollable parent (section.stMain)
                function scrollToBuilder() {
                    var anchor = window.parent.document.getElementById('order-builder-anchor');
                    if (anchor) {
                        anchor.scrollIntoView({ behavior: 'smooth', block: 'start' });
                    } else {
                        // Fallback: scroll the stMain container to 55% of its height
                        var scroller = window.parent.document.querySelector('section.stMain');
                        if (scroller) {
                            scroller.scrollTo({ top: scroller.scrollHeight * 0.55, behavior: 'smooth' });
                        }
                    }
                }
                // Delay to ensure page has finished rendering
                setTimeout(scrollToBuilder, 600);
            })();
            </script>
            """,
            height=0,
            scrolling=False,
        )
    st.markdown('<a id="order-builder-anchor"></a>', unsafe_allow_html=True)

    # ── Trade Ticket Dialog: inline bid/ask, simulate, submit ─────────────
    render_trade_submission_banner()
    render_trade_dialog(
        execution_engine=_exec_engine,
        account_id=account_id,
        current_portfolio_greeks=_current_greeks,
        regime=_regime_key,
        market_data_service=_market_data_svc,
    )

    render_order_builder(
        execution_engine=_exec_engine,
        account_id=account_id,
        current_portfolio_greeks=_current_greeks,
        regime=_regime_key,
        market_data_service=_market_data_svc,
    )

    render_order_management(ibkr_gateway_client=adapter.client, account_id=account_id)

    # Flatten Risk
    try:
        from dashboard.components.flatten_risk import render_flatten_risk
        render_flatten_risk(execution_engine=_exec_engine, account_id=account_id, positions=positions)
    except Exception as _fr_exc:
        LOGGER.warning("Flatten Risk panel failed: %s", _fr_exc)

    # Trade Journal
    try:
        from dashboard.components.trade_journal_view import render_trade_journal
        from database.local_store import LocalStore as _LSJ
        render_trade_journal(_LSJ())
    except Exception as _tj_exc:
        LOGGER.warning("Trade journal panel failed: %s", _tj_exc)

    # ══════════════════════════════════════════════════════════════════════
    # ██  SECTION 7 — AI ASSISTANT (full tool access)  ██
    # ══════════════════════════════════════════════════════════════════════
    st.subheader("🤖 AI Assistant")

    # AI Insights side-by-side (risk audit + market brief)
    _urgency_color = {"green": "success", "yellow": "warning", "red": "error"}
    _urgency_emoji = {"green": "✅", "yellow": "⚠️", "red": "🚨"}
    _ai_col1, _ai_col2 = st.columns(2)
    with _ai_col1:
        st.markdown("**🔍 Live Risk Audit**")
        _audit = _fetch_llm_intel_cached("llm_risk_audit", symbol="PORTFOLIO")
        if _audit:
            _urg = _audit.get("urgency", "green")
            getattr(st, _urgency_color.get(_urg, "info"))(f"{_urgency_emoji.get(_urg, '')} {_audit.get('headline', '')}")
            if _audit.get("body"):
                st.write(_audit["body"])
            for _s in (_audit.get("suggestions") or []):
                st.markdown(f"- {_s}")
            st.caption(f"Updated: {_audit.get('_created_at', 'unknown')}")
        else:
            st.info("No risk audit available.")
    with _ai_col2:
        st.markdown("**📊 Market Brief**")
        _brief = _fetch_llm_intel_cached("llm_market_brief")
        if _brief:
            _urg = _brief.get("urgency", "green")
            getattr(st, _urgency_color.get(_urg, "info"))(f"{_urgency_emoji.get(_urg, '')} {_brief.get('headline', '')}")
            if _brief.get("body"):
                st.write(_brief["body"])
            for _s in (_brief.get("suggestions") or []):
                st.markdown(f"- {_s}")
            st.caption(f"Updated: {_brief.get('_created_at', 'unknown')}")
        else:
            st.info("No market brief available.")

    if st.button("📰 Refresh Brief", help="Request a fresh LLM market brief"):
        _brief_payload = {
            "vix": float(vix_data.get("vix", 20.0)),
            "vix3m": float(vix_data.get("vix3m") or 21.0),
            "term_structure": float(vix_data.get("term_structure", 1.05)),
            "regime_name": regime.name if hasattr(regime, "name") else str(regime),
            "portfolio_summary": summary,
        }
        _bid = _dispatch_job("llm_brief", _brief_payload)
        if _bid:
            _fetch_llm_intel_cached.clear()
            st.info("⏳ Brief requested — refresh in ~30s.")
        else:
            st.warning("Could not dispatch brief job.")

    # Chat with full context
    user_prompt = st.text_input("Ask for a risk adjustment", placeholder="How should I reduce near-term gamma?")
    if user_prompt:
        violation_count = len(violations)
        _pos_lines = [
            f"  {_p.symbol} qty={_p.quantity} delta={float(_p.delta or 0):.2f} "
            f"theta={float(_p.theta or 0):.2f} vega={float(_p.vega or 0):.2f} "
            f"gamma={float(_p.gamma or 0):.4f} spx_delta={float(_p.spx_delta or 0):.2f} "
            f"type={_p.instrument_type.name} strike={_p.strike} exp={_p.expiration} "
            f"greeks_source={getattr(_p, 'greeks_source', 'none')}"
            for _p in (positions or [])[:50]
        ]
        context_block = (
            f"Portfolio snapshot (IBKR-only={ibkr_only_mode}):\n"
            f"  Regime: {regime.name}\n"
            f"  VIX: {vix_data.get('vix','?')}, VIX3M: {vix_data.get('vix3m','?')}, "
            f"Term structure: {vix_data.get('term_structure','?')}\n"
            f"  Account: NLV={net_liq}, BuyingPower={buying_power}, MarginUsed={maint_margin}\n"
            f"  SPX Delta: {summary.get('total_spx_delta', 0):.2f}, "
            f"Delta: {summary.get('total_delta', 0):.2f}, "
            f"Theta: {summary.get('total_theta', 0):.2f}, "
            f"Vega: {summary.get('total_vega', 0):.2f}, "
            f"Gamma: {summary.get('total_gamma', 0):.4f}\n"
            f"  Theta/Vega: {summary.get('theta_vega_ratio', 0):.3f}\n"
            f"  Violations ({violation_count}): {'; '.join(str(v) for v in violations[:5])}\n"
            f"\nAll positions ({len(positions or [])}):\n" + "\n".join(_pos_lines)
        )
        full_prompt = context_block + "\n\nUser question: " + user_prompt
        with st.spinner("Thinking…"):
            try:
                from agents.llm_client import async_llm_chat
                _reply = _run_async(async_llm_chat(full_prompt, model=selected_llm_model, timeout=45.0))
                st.markdown(_reply or "*(no response)*")
            except Exception as _exc:
                st.warning(f"LLM unavailable ({_exc})")
                st.info(
                    f"- Regime: {regime.name}\n- Violations: {violation_count}\n"
                    f"- SPX Delta: {summary['total_spx_delta']:.2f}\n"
                    f"- Theta/Vega: {summary['theta_vega_ratio']:.3f}"
                )

    with st.expander("Assistant configuration"):
        st.caption(AGENT_SYSTEM_PROMPT)
        st.json({"tool_schemas": TOOL_SCHEMAS})

    # ══════════════════════════════════════════════════════════════════════
    # ██  SECONDARY PANELS (collapsible)  ██
    # ══════════════════════════════════════════════════════════════════════

    # IV vs HV Analysis
    if positions:
        with st.expander("📉 IV vs HV Analysis", expanded=False):
            iv_symbols = sorted({str(p.underlying).upper() for p in positions if p.iv is not None and p.underlying})
            historical_volatility = get_cached_historical_volatility(tuple(iv_symbols))
            st.session_state.setdefault("data_refresh_timestamps", {})["iv_hv"] = _safe_iso_now()
            iv_analysis = portfolio_tools.get_iv_analysis(positions, historical_volatility)
            if iv_analysis:
                iv_df = pd.DataFrame(iv_analysis)
                st.dataframe(iv_df, use_container_width=True)
                st.caption("IV > HV = sell edge | IV < HV = buy edge")
            else:
                st.info("IV/HV analysis unavailable.")

    # Market Intelligence
    with st.expander("📰 Market Intelligence", expanded=False):
        intel_rows = _fetch_market_intel_cached()
        if intel_rows:
            intel_df = pd.DataFrame(intel_rows)
            _display_cols = [c for c in ["created_at", "symbol", "source", "headline", "sentiment_score"] if c in intel_df.columns]
            st.dataframe(intel_df[_display_cols] if _display_cols else intel_df, use_container_width=True)
        else:
            st.info("No market intelligence rows.")
        _portfolio_symbols = list({p.symbol for p in (positions or []) if p.symbol})
        if st.button("📰 Fetch News Now", help="Fetch news for portfolio symbols"):
            with st.spinner("Fetching…"):
                try:
                    from agents.news_sentry import NewsSentry
                    _run_async(NewsSentry().fetch_and_score(_portfolio_symbols or ["SPY", "QQQ"]))
                    _fetch_market_intel_cached.clear()
                    st.success("Done")
                    st.rerun()
                except Exception as _exc:
                    st.warning(f"Failed: {_exc}")

    # Historical Charts
    try:
        from dashboard.components.historical_charts import render_historical_charts
        from database.local_store import LocalStore as _LSH
        render_historical_charts(_LSH())
    except Exception as _hc_exc:
        LOGGER.warning("Historical charts failed: %s", _hc_exc)

    # Render-complete marker — tests wait for this to confirm full render.
    st.markdown(
        "<span data-testid='render-complete' style='font-size:1px;opacity:0;line-height:0'>·</span>",
        unsafe_allow_html=True,
    )


def main() -> None:
    """Standalone entry-point — sets page config then renders dashboard."""
    st.set_page_config(page_title="Portfolio Risk Manager", page_icon="📊", layout="wide")
    render_portfolio_content()


if __name__ == "__main__":
    main()
