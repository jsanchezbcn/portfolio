"""desktop/ui/ai_risk_tab.py — AI Risk Assistant tab.

Provides:
- Model picker (default gpt-5-mini, premium options available)
- Portfolio-aware chat with live context (positions, margins, Greeks, prices,
  open orders, recent fills, order log)
- On-demand risk audit + AI remediation trade suggestions
- Inline trade proposals: the LLM can propose trades in JSON blocks that are
  automatically parsed and added to the suggestion table for one-click staging
- Authorized staging of AI suggestions into Order Entry
- Tool call transparency: every data fetch is logged in the chat stream
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
import time
import types
import uuid
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from pydantic import BaseModel, Field
from copilot.tools import define_tool

logger = logging.getLogger(__name__)


class EmptyParams(BaseModel):
    """Empty parameter model for tools that take no arguments."""


class GetMarketSnapshotParams(BaseModel):
    symbol: str = Field(description="Symbol (e.g. 'ES', 'AAPL')")
    sec_type: str = Field(description="Security type (STK, FUT, OPT, FOP, IND)")
    exchange: str = Field(description="Exchange (SMART, CME, CBOE)")


class GetBidAskParams(BaseModel):
    symbol: str = Field(description="Underlying symbol")
    strike: float | None = Field(default=None, description="Strike price for option contracts")
    expiry: str | None = Field(default=None, description="Expiry date YYYYMMDD for option contracts")
    right: str | None = Field(default=None, description="Option right (C or P) for option contracts")
    sec_type: str = Field(default="STK", description="STK, FUT, OPT, FOP, IND")
    exchange: str = Field(default="SMART", description="Exchange (SMART, CME, CBOE)")
    conid: int | None = Field(default=None, description="Optional conId for exact contract matching")
    multiplier: str | None = Field(default=None, description="Optional contract multiplier")


class GetChainParams(BaseModel):
    underlying: str = Field(description="Underlying symbol")
    expiry: str = Field(description="Expiry date YYYYMMDD")


class TradeLegParams(BaseModel):
    symbol: str = Field(description="Underlying root symbol, e.g. 'ES', 'MES', 'SPY', or 'AAPL'")
    action: str = Field(description="Leg side: 'BUY' or 'SELL'")
    qty: int = Field(description="Positive quantity. Use the field name 'qty', not 'quantity'.")
    sec_type: str = Field(default="FOP", description="Security type: FOP, OPT, FUT, or STK")
    exchange: str = Field(default="CME", description="Exchange, e.g. CME for /ES or SMART for equities")
    expiry: str | None = Field(default=None, description="Expiry as YYYYMMDD for options or futures")
    strike: float | None = Field(default=None, description="Strike price for option legs")
    right: str | None = Field(default=None, description="Option right: 'C' or 'P' for option legs")
    conid: int | None = Field(default=None, description="Optional exact IBKR conId if already known")
    multiplier: str | None = Field(default=None, description="Optional multiplier hint such as '50' for ES or '5' for MES")


class WhatIfOrderParams(BaseModel):
    legs: list[TradeLegParams] = Field(
        description=(
            "Trade legs for IBKR WhatIf simulation. Each leg must include symbol, action, qty, sec_type, exchange, "
            "and for option legs also expiry, strike, and right."
        )
    )


class GetTradeBidAskParams(BaseModel):
    legs: list[TradeLegParams] = Field(description="Trade legs to quote with symbol, action, qty, sec_type, exchange, and option fields when applicable")


class GetRecentFillsParams(BaseModel):
    limit: int = Field(default=20, description="Max number of fills to return")


class GetRecentMarketIntelParams(BaseModel):
    limit: int = Field(default=10, description="Max number of market-intel rows to return")


class AnalyzeTradeCandidateParams(BaseModel):
    legs: list[TradeLegParams] = Field(description="Candidate trade legs with symbol, action, qty, sec_type, exchange, and option fields when applicable")
    include_whatif: bool = Field(default=False, description="When true, also run a slower WhatIf simulation for the candidate")


class ValidateStrategyParams(BaseModel):
    strategy_id: str | None = Field(default=None, description="Optional specific strategy association_id to validate; if None, validates all strategies")


class OptimizeCapitalParams(BaseModel):
    underlying: str | None = Field(default=None, description="Optional underlying symbol to focus optimization on; if None, considers all underlyings")
    target_metric: str = Field(default="margin", description="Optimization target: 'margin' (reduce capital use) or 'delta_efficiency' (improve delta per dollar)")


@dataclass
class _ToolCacheEntry:
    value: Any
    fetched_at: float


def _get_copilot_account() -> str:
    """Detect which GitHub Copilot account is currently configured."""
    try:
        result = subprocess.run(
            ["gh", "auth", "status", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            timeout=1.0,
            env={**os.environ, "GH_PAGER": "cat", "NO_COLOR": "1"},
        )
        if result.returncode == 0:
            output = result.stdout
            for line in output.split("\n"):
                if "Logged in to" in line or "Login" in line or "Account" in line:
                    return line.strip()
            if "as" in output:
                parts = output.split("as")
                if len(parts) > 1:
                    return f"GitHub: {parts[1].split('(')[0].strip()}"
        return "GitHub Copilot (account unknown)"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        try:
            result = subprocess.run(
                ["git", "config", "user.name"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return f"GitHub: {result.stdout.strip()}"
        except Exception:
            pass
        return "GitHub Copilot (account detection failed)"

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QTextEdit,
    QComboBox,
    QTableWidget,
    QTableWidgetItem,
    QMessageBox,
)

from agents.llm_client import async_list_models, async_llm_chat, build_session_config, get_hardcoded_models
from agents.llm_risk_auditor import LLMRiskAuditor
from agents.proposer_engine import BreachDetector, RiskRegimeLoader
from desktop.config.preferences import load_preferences
from models.order import AITradeSuggestion, OptionRight, OrderAction, PortfolioGreeks, RiskBreach


def _default_trades_model() -> str:
    return (os.getenv("LLM_FAST_MODEL") or os.getenv("LLM_MODEL") or "gpt-5-mini").strip() or "gpt-5-mini"


_FUTURES_OPTION_UNDERLYINGS: frozenset[str] = frozenset({
    "ES", "MES", "NQ", "MNQ", "RTY", "M2K", "YM", "MYM",
})


class _NoopAuditStore:
    async def upsert_market_intel(self, **_: Any) -> str:
        return ""


class AIRiskTab(QWidget):
    """AI-driven risk chat + suggestion workflow."""

    _AI_REQUEST_INACTIVITY_TIMEOUT_SECONDS = 120.0
    _AI_REQUEST_MAX_WAIT_SECONDS = 420.0
    _POSITIONS_TTL_SECONDS = 60.0
    _GREEKS_TTL_SECONDS = 60.0
    _ACCOUNT_TTL_SECONDS = 30.0
    _OPEN_ORDERS_TTL_SECONDS = 15.0
    _RECENT_FILLS_TTL_SECONDS = 60.0
    _QUOTE_TTL_SECONDS = 10.0
    _TRADE_QUOTE_TTL_SECONDS = 10.0
    _MARKET_INTEL_TTL_SECONDS = 120.0
    _PRESET_GROUPS: dict[str, list[str]] = {
        "Risk Review": [
            "Summarize my current portfolio risk, the biggest breaches, and the top 3 actions to take now.",
            "Which positions contribute the most to SPX delta, gamma, theta, and vega right now?",
            "Explain my current theta/vega ratio, whether it is healthy, and what trade would improve it.",
        ],
        "Hedge Ideas": [
            "What is the most capital-efficient hedge for my current SPX delta using products I already trade?",
            "How should I reduce near-term gamma risk without destroying too much theta?",
            "If I had to fix the top breach with a single trade, what would you stage first and why?",
        ],
        "Execution": [
            "Find the best hedge candidate, then check its live bid/ask spread before recommending it.",
            "Which current positions look illiquid based on spread, and which should I avoid adjusting right now?",
            "Compare the natural price vs mid for the best remediation trade and explain the expected slippage.",
        ],
        "Trade Planning": [
            "Check portfolio metrics, open orders, and recent fills before suggesting a new trade.",
            "Propose three trade candidates, then compare their spread quality, theta cost, and likely execution quality.",
            "Before recommending anything, summarize current orders, fills, and stored market intel that should affect today's decision.",
        ],
    }

    suggestion_authorized = Signal(dict)  # {legs: [...], rationale: str, model: str}

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self._engine = engine
        self._context: dict[str, Any] = {}
        self._chat_history: list[tuple[str, str]] = []
        self._suggestions: list[AITradeSuggestion] = []
        self._tool_cache: dict[str, _ToolCacheEntry] = {}
        self._tool_inflight: dict[str, asyncio.Task] = {}
        self._ai_request_activity_event: asyncio.Event | None = None
        self._ai_request_tool_calls: list[str] | None = None
        self._ai_request_debug_tool_calls = False
        self._ai_request_active_tools: dict[str, dict[str, Any]] = {}
        self._ai_request_tool_seq = 0
        self._setup_ui()
        self._connect_signals()

    def _audit_store(self):
        store = getattr(self._engine, "_db", None)
        if store is not None and hasattr(store, "upsert_market_intel"):
            return store
        return _NoopAuditStore()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        top = QHBoxLayout()
        top.addWidget(QLabel("Model:"))
        self._cmb_model = QComboBox()
        self._cmb_model.setMinimumWidth(220)
        top.addWidget(self._cmb_model)

        self._btn_models = QPushButton("🔄 Refresh Models")
        top.addWidget(self._btn_models)

        top.addWidget(QLabel("Scenario:"))
        self._cmb_scenario = QComboBox()
        self._cmb_scenario.addItems([
            "Auto (live)",
            "Low Volatility",
            "Neutral Volatility",
            "High Volatility",
            "Crisis Mode",
        ])
        top.addWidget(self._cmb_scenario)

        self._btn_audit = QPushButton("🛡 Run Risk Audit")
        top.addWidget(self._btn_audit)

        self._btn_suggest = QPushButton("✨ Suggest Trades")
        top.addWidget(self._btn_suggest)

        top.addStretch()
        self._lbl_status = QLabel("Ready")
        self._lbl_status.setStyleSheet("color: #888;")
        top.addWidget(self._lbl_status)
        layout.addLayout(top)

        self._txt_chat = QTextEdit()
        self._txt_chat.setReadOnly(True)
        self._txt_chat.setPlaceholderText("AI conversation will appear here…")
        self._txt_chat.setMinimumHeight(230)
        layout.addWidget(self._txt_chat)

        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Quick asks:"))
        self._cmb_preset_group = QComboBox()
        self._cmb_preset_group.setMinimumWidth(160)
        preset_row.addWidget(self._cmb_preset_group)
        self._cmb_presets = QComboBox()
        self._cmb_presets.setMinimumWidth(420)
        preset_row.addWidget(self._cmb_presets, stretch=1)
        self._btn_use_preset = QPushButton("Use Prompt")
        preset_row.addWidget(self._btn_use_preset)
        layout.addLayout(preset_row)
        self._populate_preset_groups()

        ask_row = QHBoxLayout()
        self._txt_user = QTextEdit()
        self._txt_user.setMaximumHeight(70)
        self._txt_user.setPlaceholderText("Ask a follow-up (risk, Greeks, margin, positioning, hedge ideas)…")
        ask_row.addWidget(self._txt_user, stretch=1)
        self._btn_ask = QPushButton("💬 Ask AI")
        ask_row.addWidget(self._btn_ask)
        layout.addLayout(ask_row)

        layout.addWidget(QLabel("AI Trade Suggestions"))
        self._tbl = QTableWidget(0, 4)
        self._tbl.setHorizontalHeaderLabels(["Legs", "Δ Change", "Θ Cost", "Rationale"])
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.setAlternatingRowColors(True)
        self._tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._tbl.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._tbl.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._tbl, stretch=1)

        self._btn_authorize = QPushButton("✅ Authorize + Stage Selected Suggestion")
        self._btn_authorize.setEnabled(False)
        row = QHBoxLayout()
        self._btn_whatif = QPushButton("🔍 WhatIf Selected Suggestion")
        self._btn_whatif.setEnabled(False)
        self._btn_clear_suggestions = QPushButton("🧹 Clear Suggestions")
        self._btn_clear_suggestions.setEnabled(False)
        row.addWidget(self._btn_whatif)
        row.addWidget(self._btn_clear_suggestions)
        row.addWidget(self._btn_authorize)
        layout.addLayout(row)

    def _connect_signals(self) -> None:
        self._btn_models.clicked.connect(self._on_refresh_models)
        self._btn_audit.clicked.connect(self._on_audit)
        self._btn_suggest.clicked.connect(self._on_suggest)
        self._btn_ask.clicked.connect(self._on_ask)
        self._btn_whatif.clicked.connect(self._on_whatif)
        self._btn_clear_suggestions.clicked.connect(self._on_clear_suggestions)
        self._btn_authorize.clicked.connect(self._on_authorize)
        self._btn_use_preset.clicked.connect(self._on_use_preset)
        self._cmb_preset_group.currentIndexChanged.connect(self._on_preset_group_changed)
        self._engine.connected.connect(self._on_connected)
        self._engine.disconnected.connect(self._on_disconnected)
        self._engine.positions_updated.connect(self._on_positions_updated)
        self._engine.account_updated.connect(self._on_account_updated)
        self._engine.orders_updated.connect(self._on_orders_updated)
        self._engine.risk_updated.connect(self._on_risk_updated)

        self._set_controls_enabled(False)
        self._load_model_defaults()

    def _set_controls_enabled(self, enabled: bool) -> None:
        self._btn_audit.setEnabled(enabled)
        self._btn_suggest.setEnabled(enabled)
        self._btn_ask.setEnabled(enabled)

    def _load_model_defaults(self) -> None:
        default_model = _default_trades_model()
        self._populate_model_picker(get_hardcoded_models(), preferred_model=default_model)

    def _format_model_label(self, model: dict[str, Any]) -> str:
        name = str(model.get("name") or model.get("id") or "model")
        raw_multiplier = model.get("cost_multiplier")
        if model.get("is_free") and raw_multiplier in (None, 0, 0.0):
            suffix = "🆓"
        elif isinstance(raw_multiplier, (int, float)):
            suffix = f"{raw_multiplier:g}x"
        else:
            suffix = "💰"
        return f"{name} {suffix}"

    def _populate_model_picker(self, models: list[dict[str, Any]], *, preferred_model: str | None = None) -> None:
        current = preferred_model or self.current_model
        self._cmb_model.clear()
        for model in models:
            self._cmb_model.addItem(self._format_model_label(model), model["id"])
        idx = self._cmb_model.findData(current)
        self._cmb_model.setCurrentIndex(idx if idx >= 0 else 0)

    @Slot()
    def _on_connected(self) -> None:
        self._clear_tool_cache()
        self._set_controls_enabled(True)
        account_id = getattr(self._engine, "account_id", None) or getattr(self._engine, "_account_id", "unknown")
        model = self.current_model
        gh_account = _get_copilot_account()
        debug_tool_calls = bool(load_preferences().get("debug_tool_calls", True))
        debug_state = "ON" if debug_tool_calls else "OFF"
        self._append_chat(
            "assistant",
            f"🟢 **Connected**\n"
            f"- IBKR Account: `{account_id}`\n"
            f"- GitHub Copilot: `{gh_account}`\n"
            f"- Tool Debug Logs: `{debug_state}`\n"
            f"- Model: `{model}`\n\n"
            f"Use **🛡 Run Risk Audit** or **✨ Suggest Trades** to analyze your portfolio, "
            f"or ask me a question about risk, Greeks, margin, or positioning.",
        )
        self._lbl_status.setText(f"Connected — {account_id} | {gh_account}")
        asyncio.get_event_loop().create_task(self._async_refresh_models())
        asyncio.get_event_loop().create_task(self._async_refresh_context())

    @Slot()
    def _on_disconnected(self) -> None:
        self._clear_tool_cache()
        self._set_controls_enabled(False)
        self._lbl_status.setText("Disconnected")

    @Slot()
    def _on_refresh_models(self) -> None:
        loop = asyncio.get_event_loop()
        loop.create_task(self._async_refresh_models())

    async def _async_refresh_models(self) -> None:
        self._lbl_status.setText("Loading models…")
        try:
            models = await async_list_models()
            self._populate_model_picker(models)
            self._lbl_status.setText(f"Loaded {len(models)} models")
        except Exception as exc:
            self._lbl_status.setText(f"Model load failed: {exc}")

    @property
    def current_model(self) -> str:
        return str(self._cmb_model.currentData() or _default_trades_model())

    def _model_supports_tool_session(self, model: str | None = None) -> bool:
        session_config = build_session_config(model or self.current_model)
        return "provider" not in session_config

    def _clear_tool_cache(self) -> None:
        self._tool_cache.clear()

    def _engine_connected(self) -> bool:
        return getattr(self._engine, "is_connected", False) is True

    def _invalidate_tool_cache_prefixes(self, *prefixes: str) -> None:
        if not prefixes:
            return
        for key in list(self._tool_cache.keys()):
            if any(key == prefix or key.startswith(f"{prefix}:") for prefix in prefixes):
                self._tool_cache.pop(key, None)

    @Slot(object)
    def _on_positions_updated(self, _positions: Any) -> None:
        self._invalidate_tool_cache_prefixes("positions", "portfolio_metrics", "portfolio_greeks", "risk_breaches")

    @Slot(object)
    def _on_account_updated(self, _account: Any) -> None:
        self._invalidate_tool_cache_prefixes("account", "portfolio_metrics", "risk_breaches")

    @Slot(object)
    def _on_orders_updated(self, _orders: Any) -> None:
        self._invalidate_tool_cache_prefixes("open_orders")

    @Slot(object)
    def _on_risk_updated(self, _risk: Any) -> None:
        self._invalidate_tool_cache_prefixes("portfolio_metrics", "portfolio_greeks", "risk_breaches")

    def _populate_preset_groups(self) -> None:
        self._cmb_preset_group.blockSignals(True)
        self._cmb_preset_group.clear()
        for group in self._PRESET_GROUPS:
            self._cmb_preset_group.addItem(group, group)
        self._cmb_preset_group.blockSignals(False)
        self._populate_presets_for_group(self._current_preset_group())

    def _current_preset_group(self) -> str:
        return str(self._cmb_preset_group.currentData() or next(iter(self._PRESET_GROUPS.keys()), ""))

    def _populate_presets_for_group(self, group: str) -> None:
        prompts = list(self._PRESET_GROUPS.get(group, []))
        self._cmb_presets.clear()
        self._cmb_presets.addItem(f"Pick a {group.lower()} prompt…", "")
        for prompt in prompts:
            self._cmb_presets.addItem(prompt, prompt)

    @staticmethod
    def _normalize_cache_value(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: AIRiskTab._normalize_cache_value(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
        if isinstance(value, list):
            return [AIRiskTab._normalize_cache_value(v) for v in value]
        return value

    def _cache_key(self, prefix: str, payload: Any | None = None) -> str:
        if payload is None:
            return prefix
        normalized = self._normalize_cache_value(payload)
        try:
            encoded = json.dumps(normalized, default=str, sort_keys=True)
        except TypeError:
            encoded = repr(normalized)
        return f"{prefix}:{encoded}"

    async def _get_cached_or_fetch(self, key: str, *, ttl_seconds: float, producer) -> Any:
        now = time.monotonic()
        entry = self._tool_cache.get(key)
        if entry is not None and (now - entry.fetched_at) <= ttl_seconds:
            return entry.value
        inflight = self._tool_inflight.get(key)
        if inflight is not None:
            return await inflight

        async def _run_producer() -> Any:
            value = await producer()
            self._tool_cache[key] = _ToolCacheEntry(value=value, fetched_at=time.monotonic())
            return value

        task = asyncio.create_task(_run_producer())
        self._tool_inflight[key] = task
        try:
            return await task
        finally:
            if self._tool_inflight.get(key) is task:
                self._tool_inflight.pop(key, None)

    async def _get_positions_data(self, *, require_fresh_greeks: bool = False) -> list[Any]:
        ttl_seconds = self._GREEKS_TTL_SECONDS if require_fresh_greeks else self._POSITIONS_TTL_SECONDS
        key = "positions:fresh" if require_fresh_greeks else "positions:snapshot"
        return await self._get_cached_or_fetch(key, ttl_seconds=ttl_seconds, producer=self._engine.refresh_positions)

    async def _get_account_data(self) -> Any:
        async def producer() -> Any:
            if self._engine._db_ok:
                cached = await self._engine._db.get_cached_account_snapshot(
                    self._engine._account_id,
                    max_age_seconds=int(self._ACCOUNT_TTL_SECONDS),
                )
                if cached:
                    return types.SimpleNamespace(**cached)
            return await self._engine.refresh_account()

        return await self._get_cached_or_fetch(
            "account",
            ttl_seconds=self._ACCOUNT_TTL_SECONDS,
            producer=producer,
        )

    async def _get_open_orders_data(self) -> list[Any]:
        return await self._get_cached_or_fetch(
            "open_orders",
            ttl_seconds=self._OPEN_ORDERS_TTL_SECONDS,
            producer=self._engine.get_open_orders,
        )

    async def _get_recent_fills_data(self, *, limit: int) -> list[dict[str, Any]]:
        if not self._engine._db_ok:
            return []

        async def producer() -> list[dict[str, Any]]:
            rows = await self._engine._db.get_fills(self._engine._account_id, limit=limit)
            return [dict(row) if not isinstance(row, dict) else row for row in rows]

        return await self._get_cached_or_fetch(
            self._cache_key("recent_fills", {"limit": limit}),
            ttl_seconds=self._RECENT_FILLS_TTL_SECONDS,
            producer=producer,
        )

    def _portfolio_metrics_from_positions(self, positions: list[Any], account: Any | None) -> dict[str, Any]:
        native_total_delta = sum(float(getattr(p, "delta", 0.0) or 0.0) for p in positions)
        total_gamma = sum(float(getattr(p, "gamma", 0.0) or 0.0) for p in positions)
        total_theta = sum(float(getattr(p, "theta", 0.0) or 0.0) for p in positions)
        total_vega = sum(float(getattr(p, "vega", 0.0) or 0.0) for p in positions)
        total_spx_delta = sum(float(getattr(p, "spx_delta", 0.0) or 0.0) for p in positions)
        total_delta = total_spx_delta
        total_value = sum(float(getattr(p, "market_value", 0.0) or 0.0) for p in positions)
        gross_exposure = sum(abs(float(getattr(p, "market_value", 0.0) or 0.0)) for p in positions)
        options = [p for p in positions if getattr(p, "sec_type", "") in ("OPT", "FOP")]
        stocks = [p for p in positions if getattr(p, "sec_type", "") == "STK"]

        def top(metric: str) -> list[dict[str, Any]]:
            ranked = sorted(
                positions,
                key=lambda p: abs(float(getattr(p, metric, 0.0) or 0.0)),
                reverse=True,
            )
            return [
                {
                    "symbol": getattr(row, "symbol", None),
                    "sec_type": getattr(row, "sec_type", None),
                    "quantity": getattr(row, "quantity", None),
                    "expiry": getattr(row, "expiry", None),
                    metric: getattr(row, metric, None),
                }
                for row in ranked[:5]
            ]

        return {
            "total_positions": len(positions),
            "total_value": total_value,
            "total_spx_delta": total_spx_delta,
            "total_delta": total_delta,
            "total_native_delta": native_total_delta,
            "total_gamma": total_gamma,
            "total_theta": total_theta,
            "total_vega": total_vega,
            "theta_vega_ratio": (total_theta / total_vega) if total_vega else 0.0,
            "gross_exposure": gross_exposure,
            "net_exposure": total_value,
            "options_count": len(options),
            "stocks_count": len(stocks),
            "nlv": float(getattr(account, "net_liquidation", 0.0) or 0.0) if account else 0.0,
            "buying_power": float(getattr(account, "buying_power", 0.0) or 0.0) if account else 0.0,
            "init_margin": float(getattr(account, "init_margin", 0.0) or 0.0) if account else 0.0,
            "maint_margin": float(getattr(account, "maint_margin", 0.0) or 0.0) if account else 0.0,
            "top_spx_delta_positions": top("spx_delta"),
            "top_theta_positions": top("theta"),
            "top_vega_positions": top("vega"),
            "top_gamma_positions": top("gamma"),
        }

    async def _get_cached_positions_snapshot(self, *, max_age_seconds: int) -> list[Any] | None:
        if not self._engine._db_ok:
            return None
        cached = await self._engine._db.get_cached_positions(
            self._engine._account_id,
            max_age_seconds=max_age_seconds,
        )
        if not cached:
            return None
        return [types.SimpleNamespace(**row) for row in cached]

    async def _tool_get_portfolio_metrics(self) -> dict[str, Any]:
        if self._engine_connected():
            live_positions = self._engine.positions_snapshot()
            if live_positions:
                account = self._engine.account_snapshot()
                if account is None:
                    account = await self._get_account_data()
                return self._portfolio_metrics_from_positions(live_positions, account)

        if self._engine._db_ok:
            try:
                cached_metrics = await self._engine._db.get_cached_portfolio_metrics(
                    self._engine._account_id,
                    max_age_seconds=int(self._GREEKS_TTL_SECONDS),
                )
                cached_positions = await self._get_cached_positions_snapshot(
                    max_age_seconds=int(self._POSITIONS_TTL_SECONDS),
                )
                cached_account = await self._get_account_data()
                if cached_metrics and cached_positions is not None:
                    derived = self._portfolio_metrics_from_positions(cached_positions, cached_account)
                    return {
                        **cached_metrics,
                        "top_spx_delta_positions": derived["top_spx_delta_positions"],
                        "top_theta_positions": derived["top_theta_positions"],
                        "top_vega_positions": derived["top_vega_positions"],
                        "top_gamma_positions": derived["top_gamma_positions"],
                    }
            except Exception as exc:
                logger.warning("Cached portfolio metrics fetch failed: %s", exc)
        positions = await self._get_positions_data(require_fresh_greeks=True)
        account = await self._get_account_data()
        return self._portfolio_metrics_from_positions(positions, account)

    async def _tool_get_portfolio_greeks(self) -> dict[str, Any]:
        if self._engine_connected():
            live_positions = self._engine.positions_snapshot()
            if live_positions:
                option_positions = [p for p in live_positions if getattr(p, "sec_type", "") in ("OPT", "FOP")]
                options_with_greeks = [
                    p for p in option_positions
                    if any(getattr(p, greek, None) is not None for greek in ("delta", "gamma", "theta", "vega"))
                ]
                metrics = self._portfolio_metrics_from_positions(live_positions, None)
                return {
                    "total_spx_delta": metrics["total_spx_delta"],
                    "total_delta": metrics["total_delta"],
                    "total_gamma": metrics["total_gamma"],
                    "total_theta": metrics["total_theta"],
                    "total_vega": metrics["total_vega"],
                    "theta_vega_ratio": metrics["theta_vega_ratio"],
                    "option_positions": len(option_positions),
                    "options_with_greeks": len(options_with_greeks),
                    "greeks_coverage": (len(options_with_greeks) / len(option_positions)) if option_positions else 1.0,
                    "top_spx_delta_positions": metrics["top_spx_delta_positions"],
                    "top_theta_positions": metrics["top_theta_positions"],
                    "top_vega_positions": metrics["top_vega_positions"],
                    "top_gamma_positions": metrics["top_gamma_positions"],
                }

        if self._engine._db_ok:
            try:
                cached_greeks = await self._engine._db.get_cached_portfolio_greeks(
                    self._engine._account_id,
                    max_age_seconds=int(self._GREEKS_TTL_SECONDS),
                )
                positions = await self._get_cached_positions_snapshot(
                    max_age_seconds=int(self._POSITIONS_TTL_SECONDS),
                )
                if cached_greeks and positions is not None:
                    option_positions = [p for p in positions if getattr(p, "sec_type", "") in ("OPT", "FOP")]
                    options_with_greeks = [
                        p for p in option_positions
                        if any(getattr(p, greek, None) is not None for greek in ("delta", "gamma", "theta", "vega"))
                    ]
                    metrics = self._portfolio_metrics_from_positions(positions, None)
                    return {
                        "total_spx_delta": cached_greeks.get("total_spx_delta"),
                        "total_delta": cached_greeks.get("total_delta"),
                        "total_gamma": cached_greeks.get("total_gamma"),
                        "total_theta": cached_greeks.get("total_theta"),
                        "total_vega": cached_greeks.get("total_vega"),
                        "theta_vega_ratio": metrics["theta_vega_ratio"],
                        "option_positions": len(option_positions),
                        "options_with_greeks": len(options_with_greeks),
                        "greeks_coverage": (len(options_with_greeks) / len(option_positions)) if option_positions else 1.0,
                        "top_spx_delta_positions": metrics["top_spx_delta_positions"],
                        "top_theta_positions": metrics["top_theta_positions"],
                        "top_vega_positions": metrics["top_vega_positions"],
                        "top_gamma_positions": metrics["top_gamma_positions"],
                    }
            except Exception as exc:
                logger.warning("Cache fetch for Greeks failed: %s", exc)
        
        # Fall back to live data
        positions = await self._get_positions_data(require_fresh_greeks=True)
        option_positions = [p for p in positions if getattr(p, "sec_type", "") in ("OPT", "FOP")]
        options_with_greeks = [
            p for p in option_positions
            if any(getattr(p, greek, None) is not None for greek in ("delta", "gamma", "theta", "vega"))
        ]
        metrics = self._portfolio_metrics_from_positions(positions, None)
        return {
            "total_spx_delta": metrics["total_spx_delta"],
            "total_delta": metrics["total_delta"],
            "total_gamma": metrics["total_gamma"],
            "total_theta": metrics["total_theta"],
            "total_vega": metrics["total_vega"],
            "theta_vega_ratio": metrics["theta_vega_ratio"],
            "option_positions": len(option_positions),
            "options_with_greeks": len(options_with_greeks),
            "greeks_coverage": (len(options_with_greeks) / len(option_positions)) if option_positions else 1.0,
            "top_spx_delta_positions": metrics["top_spx_delta_positions"],
            "top_theta_positions": metrics["top_theta_positions"],
            "top_vega_positions": metrics["top_vega_positions"],
            "top_gamma_positions": metrics["top_gamma_positions"],
        }

    async def _tool_get_recent_market_intel(self, limit: int = 10) -> list[dict[str, Any]]:
        store = getattr(self._engine, "_db", None)
        if store is None or not hasattr(store, "get_recent_market_intel"):
            return []

        return await self._get_cached_or_fetch(
            self._cache_key("market_intel", {"limit": limit}),
            ttl_seconds=self._MARKET_INTEL_TTL_SECONDS,
            producer=lambda: store.get_recent_market_intel(limit=limit),
        )

    async def _tool_analyze_trade_candidate(self, legs: list[dict[str, Any]], *, include_whatif: bool = False) -> dict[str, Any]:
        """Bundle portfolio risk, liquidity, and optional simulation for a candidate trade."""
        portfolio_metrics, portfolio_greeks, breaches, trade_bid_ask, recent_market_intel = await asyncio.gather(
            self._tool_get_portfolio_metrics(),
            self._tool_get_portfolio_greeks(),
            self._tool_get_risk_breaches(),
            self._tool_get_trade_bid_ask(legs),
            self._tool_get_recent_market_intel(limit=5),
        )
        whatif_result = await self._tool_whatif_order(legs) if include_whatif else None
        return {
            "portfolio_metrics": portfolio_metrics,
            "portfolio_greeks": portfolio_greeks,
            "risk_breaches": breaches,
            "trade_bid_ask": trade_bid_ask,
            "recent_market_intel": recent_market_intel,
            "whatif": whatif_result,
            "cache_policy_seconds": {
                "positions": int(self._POSITIONS_TTL_SECONDS),
                "portfolio_greeks": int(self._GREEKS_TTL_SECONDS),
                "portfolio_metrics": int(self._GREEKS_TTL_SECONDS),
                "account": int(self._ACCOUNT_TTL_SECONDS),
                "open_orders": int(self._OPEN_ORDERS_TTL_SECONDS),
                "quotes": int(self._QUOTE_TTL_SECONDS),
                "trade_quotes": int(self._TRADE_QUOTE_TTL_SECONDS),
            },
        }

    async def _async_refresh_context(self) -> None:
        """Refresh lightweight summary context without preloading large data blobs."""
        self._lbl_status.setText("Refreshing AI context…")
        try:
            positions_data = await self._tool_get_positions()
            account_data = await self._tool_get_account()
            breaches_data = await self._tool_get_risk_breaches()

            total_spx_delta = sum(p.get("spx_delta", 0.0) or 0.0 for p in positions_data)
            total_gamma = sum(p.get("gamma", 0.0) or 0.0 for p in positions_data)
            total_theta = sum(p.get("theta", 0.0) or 0.0 for p in positions_data)
            total_vega = sum(p.get("vega", 0.0) or 0.0 for p in positions_data)
            option_positions = [p for p in positions_data if p.get("sec_type") in ("OPT", "FOP")]
            options_with_greeks = [
                p for p in option_positions
                if any(p.get(g) is not None for g in ("delta", "gamma", "theta", "vega"))
            ]
            greeks_coverage = (len(options_with_greeks) / len(option_positions)) if option_positions else 1.0
            nlv = float(account_data.get("net_liquidation", 0.0) or 0.0)

            try:
                vix_snap = await self._engine.get_market_snapshot("VIX", "IND", "CBOE")
                vix_value = float(vix_snap.last or vix_snap.close or 0.0)
            except Exception:
                vix_value = 20.0

            loader = RiskRegimeLoader()
            regime_name, limits = loader.get_effective_limits(
                vix=vix_value,
                term_structure=1.0,
                recession_prob=0.0,
                nlv=nlv,
            )

            self._context = {
                "summary": {
                    "total_spx_delta": total_spx_delta,
                    "total_gamma": total_gamma,
                    "total_theta": total_theta,
                    "total_vega": total_vega,
                    "position_count": len(positions_data),
                    "option_count": len(option_positions),
                    "options_with_greeks": len(options_with_greeks),
                    "greeks_coverage": greeks_coverage,
                    "theta_vega_ratio": (total_theta / total_vega) if total_vega else 0.0,
                },
                "regime_name": regime_name,
                "vix": vix_value,
                "nlv": nlv,
                "violations": breaches_data,
                "resolved_limits": limits,
                "account": account_data,
            }
            self._lbl_status.setText("AI context refreshed")
        except Exception as exc:
            self._lbl_status.setText(f"Context refresh failed: {exc}")

    def _set_ai_request_logging_context(
        self,
        *,
        activity_event: asyncio.Event,
        tool_calls: list[str],
        debug_tool_calls: bool,
    ) -> None:
        self._ai_request_activity_event = activity_event
        self._ai_request_tool_calls = tool_calls
        self._ai_request_debug_tool_calls = debug_tool_calls
        self._ai_request_active_tools = {}
        self._ai_request_tool_seq = 0

    def _clear_ai_request_logging_context(self) -> None:
        self._ai_request_activity_event = None
        self._ai_request_tool_calls = None
        self._ai_request_debug_tool_calls = False
        self._ai_request_active_tools = {}
        self._ai_request_tool_seq = 0

    def _signal_ai_request_activity(self) -> None:
        if self._ai_request_activity_event is not None:
            self._ai_request_activity_event.set()

    def _active_ai_tool_names(self) -> list[str]:
        names: list[str] = []
        for tool in self._ai_request_active_tools.values():
            name = str(tool.get("name") or "unknown")
            if name not in names:
                names.append(name)
        return names

    def _tool_payload_to_log_value(self, payload: Any) -> Any:
        if hasattr(payload, "model_dump"):
            return payload.model_dump(exclude_none=True)
        if isinstance(payload, dict):
            return {k: self._tool_payload_to_log_value(v) for k, v in payload.items()}
        if isinstance(payload, list):
            return [self._tool_payload_to_log_value(v) for v in payload]
        if isinstance(payload, tuple):
            return [self._tool_payload_to_log_value(v) for v in payload]
        return payload

    def _format_trade_leg_for_log(self, leg: dict[str, Any]) -> str:
        symbol = str(leg.get("symbol") or "?").upper()
        sec_type = str(leg.get("sec_type") or "STK").upper()
        exchange = str(leg.get("exchange") or ("CME" if sec_type in {"FUT", "FOP"} else "SMART")).upper()
        expiry = str(leg.get("expiry") or "").strip()
        strike = leg.get("strike")
        right = str(leg.get("right") or "").upper()
        action = str(leg.get("action") or "").upper()
        qty = leg.get("qty") or leg.get("quantity")

        option_descriptor = ""
        if strike is not None:
            strike_value = f"{float(strike):g}" if isinstance(strike, (int, float)) else str(strike)
            option_descriptor = strike_value
            if right:
                option_descriptor += right[:1]

        pieces: list[str] = []
        if action:
            pieces.append(action)
        if qty is not None:
            pieces.append(str(qty))
        pieces.append(symbol)
        if expiry:
            pieces.append(expiry)
        if option_descriptor:
            pieces.append(option_descriptor)
        pieces.append(sec_type)
        pieces.append(f"@{exchange}")
        return " ".join(pieces)

    def _summarize_trade_legs_for_log(self, legs: list[dict[str, Any]], *, max_legs: int = 4) -> str:
        if not legs:
            return "legs=[]"
        formatted = [self._format_trade_leg_for_log(leg) for leg in legs[:max_legs]]
        if len(legs) > max_legs:
            formatted.append(f"… +{len(legs) - max_legs} more")
        return " | ".join(formatted)

    def _summarize_tool_payload(self, tool_name: str, params: Any) -> str:
        payload = self._tool_payload_to_log_value(params)
        if not isinstance(payload, dict):
            return str(payload)[:220]

        if tool_name in {"whatif_order", "get_trade_bid_ask", "analyze_trade_candidate"}:
            parts: list[str] = []
            if tool_name == "analyze_trade_candidate":
                parts.append(f"include_whatif={bool(payload.get('include_whatif', False))}")
            raw_legs = payload.get("legs") or []
            if isinstance(raw_legs, list):
                parts.append(self._summarize_trade_legs_for_log(self._normalize_tool_legs(raw_legs)))
            return "; ".join(part for part in parts if part)

        if tool_name == "get_bid_ask":
            return self._format_trade_leg_for_log(payload)

        if tool_name == "get_market_snapshot":
            symbol = str(payload.get("symbol") or "?").upper()
            sec_type = str(payload.get("sec_type") or "STK").upper()
            exchange = str(payload.get("exchange") or "SMART").upper()
            return f"{symbol} {sec_type} @{exchange}"

        if tool_name == "get_chain":
            return f"{str(payload.get('underlying') or '?').upper()} expiry={payload.get('expiry') or '?'}"

        if tool_name in {"get_recent_fills", "get_recent_market_intel"}:
            return f"limit={payload.get('limit', '?')}"

        return json.dumps(payload, default=str)[:220]

    def _summarize_tool_result(self, result: Any) -> str:
        if isinstance(result, list):
            return f"items={len(result)}"
        if isinstance(result, dict):
            parts: list[str] = []
            status = result.get("status")
            if status is not None:
                parts.append(f"status={status}")
            for key in (
                "init_margin_change",
                "maint_margin_change",
                "equity_with_loan_change",
                "natural_net_debit",
                "mid_net_debit",
                "estimated_slippage_vs_mid",
                "net_liquidation",
                "total_spx_delta",
            ):
                if key in result and result.get(key) is not None:
                    value = result.get(key)
                    if isinstance(value, float):
                        parts.append(f"{key}={value:.4f}")
                    else:
                        parts.append(f"{key}={value}")
            if result.get("error"):
                parts.append(f"error={str(result.get('error'))[:120]}")
            if not parts:
                parts.append(f"keys={','.join(list(result.keys())[:5])}")
            return ", ".join(parts)
        return str(result)[:220]

    def _start_ai_tool_invocation(self, tool_name: str, detail: str) -> str:
        self._ai_request_tool_seq += 1
        call_id = f"{tool_name}#{self._ai_request_tool_seq}"
        self._ai_request_active_tools[call_id] = {
            "name": tool_name,
            "started_at": time.monotonic(),
            "detail": detail,
        }
        self._signal_ai_request_activity()
        logger.info("AI Risk tool start: %s%s", tool_name, f" | {detail}" if detail else "")
        if self._ai_request_debug_tool_calls and self._ai_request_tool_calls is not None:
            line = f"🔧 Calling tool: {tool_name}"
            if detail:
                line += f" — {detail}"
            self._ai_request_tool_calls.append(line)
        return call_id

    def _finish_ai_tool_invocation(self, call_id: str, *, result: Any = None, error: Exception | None = None) -> None:
        state = self._ai_request_active_tools.pop(call_id, {})
        tool_name = str(state.get("name") or call_id.split("#", 1)[0] or "unknown")
        started_at = float(state.get("started_at") or time.monotonic())
        elapsed = max(0.0, time.monotonic() - started_at)
        self._signal_ai_request_activity()

        if error is not None:
            logger.warning("AI Risk tool failed: %s (%.2fs) | %s", tool_name, elapsed, error)
            if self._ai_request_debug_tool_calls and self._ai_request_tool_calls is not None:
                self._ai_request_tool_calls.append(
                    f"✗ Tool failed: {tool_name} ({elapsed:.1f}s) — {error}"
                )
            return

        result_summary = self._summarize_tool_result(result)
        logger.info("AI Risk tool complete: %s (%.2fs)%s", tool_name, elapsed, f" | {result_summary}" if result_summary else "")
        if self._ai_request_debug_tool_calls and self._ai_request_tool_calls is not None:
            line = f"✓ Tool complete: {tool_name} ({elapsed:.1f}s)"
            if result_summary:
                line += f" — {result_summary}"
            self._ai_request_tool_calls.append(line)

    async def _run_logged_tool(self, tool_name: str, params: Any, runner: Any) -> Any:
        detail = self._summarize_tool_payload(tool_name, params)
        call_id = self._start_ai_tool_invocation(tool_name, detail)
        try:
            result = await runner()
        except Exception as exc:
            self._finish_ai_tool_invocation(call_id, error=exc)
            raise
        self._finish_ai_tool_invocation(call_id, result=result)
        return result

    # ═══════════════════════════════════════════════════════════════════════
    # Tool handlers for LLM function calling
    # ═══════════════════════════════════════════════════════════════════════

    async def _tool_get_positions(self) -> list[dict[str, Any]]:
        """Fetch current portfolio positions with Greeks."""
        # Prefer current in-memory snapshot while connected.
        if self._engine_connected():
            live_positions = self._engine.positions_snapshot()
            if live_positions:
                return [asdict(p) for p in live_positions]
            # Connected but no snapshot yet: force first live refresh.
            positions = await self._get_positions_data(require_fresh_greeks=False)
            return [asdict(p) for p in positions]

        # Try cache first (if < 60 seconds old)
        if self._engine._db_ok:
            try:
                cached = await self._engine._db.get_cached_positions(
                    self._engine._account_id,
                    max_age_seconds=60
                )
                if cached:
                    logger.debug("Using cached positions (%d rows)", len(cached))
                    return cached
            except Exception as exc:
                logger.warning("Cache fetch failed, using live data: %s", exc)
        
        # Fall back to live data
        positions = await self._get_positions_data(require_fresh_greeks=False)
        return [asdict(p) for p in positions]

    async def _tool_get_account(self) -> dict[str, Any]:
        """Fetch account summary (NLV, margins, cash)."""
        if self._engine_connected():
            current = self._engine.account_snapshot()
            if current is not None:
                return asdict(current) if hasattr(current, "__dataclass_fields__") else dict(vars(current))
        account = await self._get_account_data()
        if account is None:
            return {}
        if hasattr(account, "__dataclass_fields__"):
            return asdict(account)
        return dict(vars(account))

    async def _tool_get_open_orders(self) -> list[dict[str, Any]]:
        """Fetch open orders from IB."""
        orders = await self._get_open_orders_data()
        return [asdict(o) for o in orders]

    async def _tool_get_market_snapshot(self, symbol: str, sec_type: str, exchange: str) -> dict[str, Any]:
        """Fetch current market price snapshot for a symbol."""
        snap = await self._engine.get_market_snapshot(symbol, sec_type, exchange)
        return asdict(snap)

    async def _tool_get_bid_ask(
        self,
        symbol: str,
        strike: float | None,
        expiry: str | None,
        right: str | None,
        sec_type: str,
        exchange: str,
        *,
        conid: int | None = None,
        multiplier: str | None = None,
    ) -> dict[str, Any]:
        """Fetch bid/ask for stock, future, or option contract."""
        sec_type_upper = str(sec_type or "STK").upper()
        payload = {
            "symbol": symbol.upper(),
            "strike": strike,
            "expiry": expiry,
            "right": (right or "").upper() or None,
            "sec_type": sec_type_upper,
            "exchange": exchange,
            "conid": conid,
            "multiplier": multiplier,
        }

        async def producer() -> dict[str, Any]:
            if sec_type_upper in ("OPT", "FOP"):
                if strike is None or not expiry or not right:
                    return {
                        **payload,
                        "bid": None,
                        "ask": None,
                        "mid": None,
                        "spread": None,
                        "spread_pct": None,
                        "error": "Option quotes require strike, expiry, and right",
                    }
                leg = {
                    "symbol": symbol.upper(),
                    "strike": strike,
                    "expiry": expiry,
                    "right": (right or "").upper(),
                    "sec_type": sec_type_upper,
                    "exchange": exchange,
                    "action": "BUY",
                    "qty": 1,
                    "conid": conid,
                    "multiplier": multiplier,
                }
                quote = (await self._engine.get_bid_ask_for_legs([leg]))[0]
                bid = quote.get("bid")
                ask = quote.get("ask")
                mid = quote.get("mid")
            else:
                snap = await self._tool_get_market_snapshot(symbol, sec_type_upper, exchange)
                bid = snap.get("bid")
                ask = snap.get("ask")
                mid = round((float(bid) + float(ask)) / 2.0, 4) if bid is not None and ask is not None else (bid or ask or snap.get("last") or snap.get("close"))
            spread = round(float(ask) - float(bid), 4) if bid is not None and ask is not None else None
            spread_pct = round((spread / float(mid)) * 100.0, 4) if spread is not None and mid not in (None, 0) else None
            return {
                **payload,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "spread": spread,
                "spread_pct": spread_pct,
            }

        return await self._get_cached_or_fetch(
            self._cache_key("bid_ask", payload),
            ttl_seconds=self._QUOTE_TTL_SECONDS,
            producer=producer,
        )

    async def _tool_get_trade_bid_ask(self, legs: list[dict[str, Any]]) -> dict[str, Any]:
        """Fetch per-leg quotes and aggregate debit/credit estimates for a trade."""

        async def producer() -> dict[str, Any]:
            quotes = await self._engine.get_bid_ask_for_legs(legs)
            per_leg: list[dict[str, Any]] = []
            natural_net_debit = 0.0
            mid_net_debit = 0.0

            for leg, quote in zip(legs, quotes):
                qty = max(1.0, float(leg.get("qty") or leg.get("quantity") or 1.0))
                action = str(leg.get("action") or "BUY").upper()
                bid = quote.get("bid")
                ask = quote.get("ask")
                mid = quote.get("mid")
                spread = round(float(ask) - float(bid), 4) if bid is not None and ask is not None else None
                spread_pct = round((spread / float(mid)) * 100.0, 4) if spread is not None and mid not in (None, 0) else None
                natural_price = ask if action == "BUY" else bid
                mid_price = mid if mid is not None else natural_price
                sign = 1.0 if action == "BUY" else -1.0
                natural_net_debit += sign * float(natural_price or 0.0) * qty
                mid_net_debit += sign * float(mid_price or 0.0) * qty
                per_leg.append({
                    "leg": dict(leg),
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "spread": spread,
                    "spread_pct": spread_pct,
                    "natural_price_for_action": natural_price,
                    "mid_price_for_action": mid_price,
                })

            return {
                "legs": per_leg,
                "natural_net_debit": round(natural_net_debit, 4),
                "mid_net_debit": round(mid_net_debit, 4),
                "estimated_slippage_vs_mid": round(natural_net_debit - mid_net_debit, 4),
            }

        return await self._get_cached_or_fetch(
            self._cache_key("trade_bid_ask", legs),
            ttl_seconds=self._TRADE_QUOTE_TTL_SECONDS,
            producer=producer,
        )

    async def _tool_get_chain(self, underlying: str, expiry: str) -> list[dict[str, Any]]:
        """Fetch options chain for underlying and expiry."""
        symbol = str(underlying or "").upper()
        normalized_expiry = str(expiry or "").replace("-", "")[:8]

        def _serialize_rows(rows: list[Any]) -> list[dict[str, Any]]:
            result: list[dict[str, Any]] = []
            for row in rows:
                if getattr(row, "underlying", None) != symbol or getattr(row, "expiry", None) != normalized_expiry:
                    continue
                if hasattr(row, "__dataclass_fields__"):
                    result.append(asdict(row))
                else:
                    result.append(vars(row) if hasattr(row, "__dict__") else dict(row))
            return result

        cached_rows = _serialize_rows(self._engine.chain_snapshot() or [])
        if cached_rows:
            return cached_rows

        if len(normalized_expiry) != 8 or not normalized_expiry.isdigit():
            return []

        sec_type = "FOP" if symbol in _FUTURES_OPTION_UNDERLYINGS else "OPT"
        exchange = "CME" if sec_type == "FOP" else "SMART"
        expiry_date = date(
            int(normalized_expiry[:4]),
            int(normalized_expiry[4:6]),
            int(normalized_expiry[6:8]),
        )
        try:
            fetched_rows = await self._engine.get_chain(
                symbol,
                expiry=expiry_date,
                sec_type=sec_type,
                exchange=exchange,
                max_strikes=200,
            )
        except Exception:
            return []
        return _serialize_rows(fetched_rows or [])

    async def _tool_whatif_order(self, legs: list[dict[str, Any]]) -> dict[str, Any]:
        """Run WhatIf simulation for proposed trade legs."""
        try:
            result = await self._engine.whatif_order(legs, order_type="LIMIT", limit_price=None)
            return result
        except Exception as exc:
            return {"error": str(exc)}

    @staticmethod
    def _normalize_tool_legs(legs: list[Any]) -> list[dict[str, Any]]:
        """Normalize Copilot/Pydantic tool payloads into plain dicts for engine helpers."""
        normalized: list[dict[str, Any]] = []
        for leg in legs:
            if hasattr(leg, "model_dump"):
                normalized.append(dict(leg.model_dump(exclude_none=True)))
            elif isinstance(leg, dict):
                normalized.append(dict(leg))
            else:
                normalized.append(dict(vars(leg)))
        return normalized

    async def _tool_get_recent_fills(self, limit: int = 20) -> list[dict[str, Any]]:
        """Fetch recent fills from database."""
        try:
            return await self._get_recent_fills_data(limit=limit)
        except Exception:
            return []

    async def _tool_get_risk_breaches(self) -> list[dict[str, Any]]:
        """Check current risk violations against regime limits."""
        async def producer() -> list[dict[str, Any]]:
            from agents.proposer_engine import BreachDetector, RiskRegimeLoader

            positions = await self._get_positions_data(require_fresh_greeks=True)
            account = await self._get_account_data()

            total_spx_delta = sum((p.spx_delta or 0.0) for p in positions)
            total_gamma = sum((p.gamma or 0.0) for p in positions)
            total_theta = sum((p.theta or 0.0) for p in positions)
            total_vega = sum((p.vega or 0.0) for p in positions)
            nlv = float(account.net_liquidation) if account else 0.0
            margin_used = float(account.init_margin) if account else 0.0

            try:
                vix_snap = await self._engine.get_market_snapshot("VIX", "IND", "CBOE")
                vix_value = float(vix_snap.last or vix_snap.close or 0.0)
            except Exception:
                vix_value = 20.0

            greeks_snapshot = {
                "vix": vix_value,
                "term_structure": 1.0,
                "recession_prob": 0.0,
                "total_vega": total_vega,
                "spx_delta": total_spx_delta,
                "total_theta": total_theta,
                "total_gamma": total_gamma,
            }

            loader = RiskRegimeLoader()
            detector = BreachDetector(loader)

            events = detector.check(
                greeks_snapshot,
                account_nlv=nlv,
                account_id=self._engine.account_id,
                margin_used=margin_used,
            )

            return [
                {
                    "metric": e.greek,
                    "current": e.current_value,
                    "limit": e.limit,
                    "distance": e.distance_to_target,
                }
                for e in events
            ]

        return await self._get_cached_or_fetch(
            "risk_breaches",
            ttl_seconds=self._ACCOUNT_TTL_SECONDS,
            producer=producer,
        )

    async def _tool_get_strategy_snapshot(self) -> list[dict[str, Any]]:
        """Get current portfolio strategies with aggregated Greeks and P&L."""
        strategies = self._engine.strategy_snapshot()
        return [
            {
                "association_id": s.association_id,
                "strategy_name": s.strategy_name,
                "strategy_family": s.strategy_family,
                "underlying": s.underlying,
                "matched_by": s.matched_by,
                "expiry_label": s.expiry_label,
                "leg_count": len(s.legs),
                "leg_ids": s.leg_ids,
                "net_delta": s.net_delta,
                "net_gamma": s.net_gamma,
                "net_theta": s.net_theta,
                "net_vega": s.net_vega,
                "net_spx_delta": s.net_spx_delta,
                "market_value": s.net_mkt_value,
                "unrealized_pnl": s.net_upnl,
                "realized_pnl": s.net_rpnl,
            }
            for s in strategies
        ]

    async def _tool_validate_strategies(self, strategy_id: str | None = None) -> dict[str, Any]:
        """Validate strategy construction and identify potential errors."""
        strategies = self._engine.strategy_snapshot()
        
        if strategy_id:
            strategies = [s for s in strategies if s.association_id == strategy_id]
            if not strategies:
                return {"error": f"Strategy {strategy_id} not found"}
        
        issues: list[dict[str, Any]] = []
        valid_count = 0
        
        for strat in strategies:
            strategy_issues: list[str] = []
            
            # Check 1: Incomplete spreads (should have balanced legs)
            if strat.strategy_name in {"Bull Call Spread", "Bear Call Spread", "Bull Put Spread", "Bear Put Spread"}:
                if len(strat.legs) != 2:
                    strategy_issues.append(f"Spread should have 2 legs but has {len(strat.legs)}")
                else:
                    quantities = [abs(getattr(leg, "quantity", 0)) for leg in strat.legs]
                    if len(set(quantities)) > 1:
                        strategy_issues.append(f"Unbalanced spread quantities: {quantities}")
            
            # Check 2: Iron condors should have 4 legs
            if "Iron Condor" in strat.strategy_name and len(strat.legs) != 4:
                strategy_issues.append(f"Iron Condor should have 4 legs but has {len(strat.legs)}")
            
            # Check 3: Butterflies should have 3 legs
            if "Butterfly" in strat.strategy_name and len(strat.legs) != 3:
                strategy_issues.append(f"Butterfly should have 3 legs but has {len(strat.legs)}")
            
            # Check 4: Excessive net delta (might indicate incomplete hedge)
            if strat.net_delta is not None and abs(strat.net_delta) > 100:
                if strat.strategy_family not in {"stock", "future", "long_option"}:
                    strategy_issues.append(f"High net delta {strat.net_delta:.1f} for {strat.strategy_family} strategy")
            
            # Check 5: Conflicting Greeks (e.g., long theta with short vega is unusual)
            if strat.net_theta is not None and strat.net_vega is not None:
                if strat.net_theta > 0 and strat.net_vega < -50:
                    strategy_issues.append(f"Unusual: positive theta ({strat.net_theta:.2f}) with large negative vega ({strat.net_vega:.2f})")
            
            # Check 6: Calendar spreads should have different expiries
            if "Calendar" in strat.strategy_name or "Diagonal" in strat.strategy_name:
                expiries = set(getattr(leg, "expiry", None) for leg in strat.legs if hasattr(leg, "expiry"))
                if len(expiries) <= 1:
                    strategy_issues.append(f"Calendar/Diagonal should have multiple expiries but has {len(expiries)}")
            
            if strategy_issues:
                issues.append({
                    "association_id": strat.association_id,
                    "strategy_name": strat.strategy_name,
                    "underlying": strat.underlying,
                    "issues": strategy_issues,
                })
            else:
                valid_count += 1
        
        return {
            "total_strategies": len(strategies),
            "valid_count": valid_count,
            "issues_count": len(issues),
            "issues": issues,
        }

    async def _tool_optimize_capital(self, underlying: str | None = None, target_metric: str = "margin") -> dict[str, Any]:
        """Suggest trade adjustments to reduce capital use while maintaining similar exposure."""
        strategies = self._engine.strategy_snapshot()
        positions = await self._get_positions_data(require_fresh_greeks=False)
        account = await self._get_account_data()
        
        if underlying:
            strategies = [s for s in strategies if s.underlying.upper() == underlying.upper()]
        
        suggestions: list[dict[str, Any]] = []
        
        for strat in strategies:
            # Strategy 1: Convert naked options to spreads to reduce margin
            if strat.strategy_name in {"Long Call", "Long Put", "Short Call", "Short Put"}:
                if len(strat.legs) == 1:
                    leg = strat.legs[0]
                    strike = getattr(leg, "strike", None)
                    right = getattr(leg, "right", "")
                    expiry = getattr(leg, "expiry", "")
                    qty = abs(getattr(leg, "quantity", 0))
                    action = "Short" if str(strat.strategy_name).startswith("Short") else "Long"
                    
                    if strike and right and expiry and action == "Short":
                        # Suggest converting short naked to credit spread
                        if right.upper() == "C":
                            far_strike = strike + 5 if "ES" in strat.underlying else strike + (strike * 0.05)
                            suggestions.append({
                                "strategy_id": strat.association_id,
                                "underlying": strat.underlying,
                                "current_strategy": strat.strategy_name,
                                "suggestion": "Convert to Bear Call Spread",
                                "rationale": f"Reduce margin by buying {far_strike:.0f}C protection",
                                "estimated_margin_reduction": "60-80%",
                                "legs_to_add": [{
                                    "action": "BUY",
                                    "qty": int(qty),
                                    "symbol": strat.underlying,
                                    "strike": far_strike,
                                    "right": "C",
                                    "expiry": expiry,
                                    "sec_type": getattr(leg, "sec_type", "FOP"),
                                }],
                            })
                        elif right.upper() == "P":
                            far_strike = strike - 5 if "ES" in strat.underlying else strike - (strike * 0.05)
                            suggestions.append({
                                "strategy_id": strat.association_id,
                                "underlying": strat.underlying,
                                "current_strategy": strat.strategy_name,
                                "suggestion": "Convert to Bull Put Spread",
                                "rationale": f"Reduce margin by buying {far_strike:.0f}P protection",
                                "estimated_margin_reduction": "60-80%",
                                "legs_to_add": [{
                                    "action": "BUY",
                                    "qty": int(qty),
                                    "symbol": strat.underlying,
                                    "strike": far_strike,
                                    "right": "P",
                                    "expiry": expiry,
                                    "sec_type": getattr(leg, "sec_type", "FOP"),
                                }],
                            })
            
            # Strategy 2: Identify unbalanced spreads that can be completed
            if strat.strategy_name in {"Bull Call Spread", "Bear Call Spread", "Bull Put Spread", "Bear Put Spread"}:
                if len(strat.legs) == 1:
                    # Incomplete spread - suggest completion
                    leg = strat.legs[0]
                    strike = getattr(leg, "strike", None)
                    right = getattr(leg, "right", "")
                    expiry = getattr(leg, "expiry", "")
                    qty = abs(getattr(leg, "quantity", 0))
                    is_long = getattr(leg, "quantity", 0) > 0
                    
                    if strike and right and expiry:
                        if right.upper() == "C":
                            other_strike = strike + 5 if "ES" in strat.underlying else strike + (strike * 0.05)
                            other_action = "SELL" if is_long else "BUY"
                            suggestions.append({
                                "strategy_id": strat.association_id,
                                "underlying": strat.underlying,
                                "current_strategy": "Incomplete spread",
                                "suggestion": "Complete Call Spread",
                                "rationale": f"Complete spread by {other_action} {other_strike:.0f}C to cap risk/reward",
                                "estimated_margin_reduction": "40-60%" if not is_long else "N/A",
                                "legs_to_add": [{
                                    "action": other_action,
                                    "qty": int(qty),
                                    "symbol": strat.underlying,
                                    "strike": other_strike,
                                    "right": "C",
                                    "expiry": expiry,
                                    "sec_type": getattr(leg, "sec_type", "FOP"),
                                }],
                            })
                        elif right.upper() == "P":
                            other_strike = strike - 5 if "ES" in strat.underlying else strike - (strike * 0.05)
                            other_action = "SELL" if is_long else "BUY"
                            suggestions.append({
                                "strategy_id": strat.association_id,
                                "underlying": strat.underlying,
                                "current_strategy": "Incomplete spread",
                                "suggestion": "Complete Put Spread",
                                "rationale": f"Complete spread by {other_action} {other_strike:.0f}P to cap risk/reward",
                                "estimated_margin_reduction": "40-60%" if not is_long else "N/A",
                                "legs_to_add": [{
                                    "action": other_action,
                                    "qty": int(qty),
                                    "symbol": strat.underlying,
                                    "strike": other_strike,
                                    "right": "P",
                                    "expiry": expiry,
                                    "sec_type": getattr(leg, "sec_type", "FOP"),
                                }],
                            })
            
            # Strategy 3: High gamma exposure - suggest iron condor conversion
            if strat.net_gamma is not None and abs(strat.net_gamma) > 50:
                if strat.strategy_name in {"Bull Put Spread", "Bear Call Spread"}:
                    # Can convert to iron condor for more credit/less gamma
                    suggestions.append({
                        "strategy_id": strat.association_id,
                        "underlying": strat.underlying,
                        "current_strategy": strat.strategy_name,
                        "suggestion": "Convert to Iron Condor",
                        "rationale": f"Reduce gamma exposure ({strat.net_gamma:.1f}) and collect additional credit",
                        "estimated_margin_reduction": "Neutral (same margin, more premium)",
                        "legs_to_add": "Add complementary put/call spread on opposite side",
                    })
        
        # Calculate current margin utilization
        nlv = float(account.net_liquidation) if account else 0.0
        margin_used = float(account.init_margin) if account else 0.0
        margin_pct = (margin_used / nlv * 100.0) if nlv > 0 else 0.0
        
        return {
            "current_margin_used": margin_used,
            "current_margin_pct": round(margin_pct, 2),
            "net_liquidation": nlv,
            "strategies_analyzed": len(strategies),
            "suggestions_count": len(suggestions),
            "suggestions": suggestions,
        }

    def _create_tools_for_session(self) -> list:
        """Create Copilot SDK tool definitions that call instance methods."""
        # Define tools with closures over self
        @define_tool(description="Get current portfolio positions with Greeks and P&L")
        async def get_positions(params: EmptyParams) -> list[dict]:
            return await self._run_logged_tool("get_positions", params, self._tool_get_positions)

        @define_tool(description="Get aggregated portfolio Greeks with 1-minute cache freshness")
        async def get_portfolio_greeks(params: EmptyParams) -> dict:
            return await self._run_logged_tool("get_portfolio_greeks", params, self._tool_get_portfolio_greeks)

        @define_tool(description="Get aggregated portfolio metrics, exposures, and top risk contributors")
        async def get_portfolio_metrics(params: EmptyParams) -> dict:
            return await self._run_logged_tool("get_portfolio_metrics", params, self._tool_get_portfolio_metrics)

        @define_tool(description="Get account summary (NLV, margins, cash)")
        async def get_account(params: EmptyParams) -> dict:
            return await self._run_logged_tool("get_account", params, self._tool_get_account)

        @define_tool(description="Get open orders from IB")
        async def get_open_orders(params: EmptyParams) -> list[dict]:
            return await self._run_logged_tool("get_open_orders", params, self._tool_get_open_orders)

        @define_tool(description="Get current market price snapshot for a symbol")
        async def get_market_snapshot(params: GetMarketSnapshotParams) -> dict:
            return await self._run_logged_tool(
                "get_market_snapshot",
                params,
                lambda: self._tool_get_market_snapshot(params.symbol, params.sec_type, params.exchange),
            )

        @define_tool(description="Get bid/ask for specific option contract")
        async def get_bid_ask(params: GetBidAskParams) -> dict:
            return await self._run_logged_tool(
                "get_bid_ask",
                params,
                lambda: self._tool_get_bid_ask(
                    params.symbol,
                    params.strike,
                    params.expiry,
                    params.right,
                    params.sec_type,
                    params.exchange,
                    conid=params.conid,
                    multiplier=params.multiplier,
                ),
            )

        @define_tool(description="Get per-leg quotes and estimated net debit/credit for a multi-leg trade")
        async def get_trade_bid_ask(params: GetTradeBidAskParams) -> dict:
            return await self._run_logged_tool(
                "get_trade_bid_ask",
                params,
                lambda: self._tool_get_trade_bid_ask(self._normalize_tool_legs(params.legs)),
            )

        @define_tool(description="Get options chain for underlying and expiry")
        async def get_chain(params: GetChainParams) -> list[dict]:
            return await self._run_logged_tool(
                "get_chain",
                params,
                lambda: self._tool_get_chain(params.underlying, params.expiry),
            )

        @define_tool(description="Run an IBKR WhatIf simulation. Required payload: legs=[{symbol, action, qty, sec_type, exchange, expiry, strike, right}] with option fields included for FOP/OPT legs")
        async def whatif_order(params: WhatIfOrderParams) -> dict:
            return await self._run_logged_tool(
                "whatif_order",
                params,
                lambda: self._tool_whatif_order(self._normalize_tool_legs(params.legs)),
            )

        @define_tool(description="Get recent fills from database")
        async def get_recent_fills(params: GetRecentFillsParams) -> list[dict]:
            return await self._run_logged_tool(
                "get_recent_fills",
                params,
                lambda: self._tool_get_recent_fills(params.limit),
            )

        @define_tool(description="Check current risk violations against regime limits")
        async def get_risk_breaches(params: EmptyParams) -> list[dict]:
            return await self._run_logged_tool("get_risk_breaches", params, self._tool_get_risk_breaches)

        @define_tool(description="Get recent stored market intel, LLM briefs, and audit entries from Postgres")
        async def get_recent_market_intel(params: GetRecentMarketIntelParams) -> list[dict]:
            return await self._run_logged_tool(
                "get_recent_market_intel",
                params,
                lambda: self._tool_get_recent_market_intel(params.limit),
            )

        @define_tool(description="Bundle portfolio risk, trade spread quality, and optional WhatIf for a candidate trade")
        async def analyze_trade_candidate(params: AnalyzeTradeCandidateParams) -> dict:
            return await self._run_logged_tool(
                "analyze_trade_candidate",
                params,
                lambda: self._tool_analyze_trade_candidate(
                    self._normalize_tool_legs(params.legs),
                    include_whatif=params.include_whatif,
                ),
            )

        @define_tool(description="Get portfolio strategies with aggregated Greeks, grouped by strategy type (spreads, iron condors, etc.)")
        async def get_strategy_snapshot(params: EmptyParams) -> list[dict]:
            return await self._run_logged_tool("get_strategy_snapshot", params, self._tool_get_strategy_snapshot)

        @define_tool(description="Validate portfolio strategies and identify construction errors, incomplete spreads, or unusual risk characteristics")
        async def validate_strategies(params: ValidateStrategyParams) -> dict:
            return await self._run_logged_tool(
                "validate_strategies",
                params,
                lambda: self._tool_validate_strategies(params.strategy_id),
            )

        @define_tool(description="Suggest trade adjustments to reduce capital use while maintaining similar exposure. Can focus on specific underlying or optimize all positions.")
        async def optimize_capital(params: OptimizeCapitalParams) -> dict:
            return await self._run_logged_tool(
                "optimize_capital",
                params,
                lambda: self._tool_optimize_capital(params.underlying, params.target_metric),
            )

        return [
            get_positions,
            get_portfolio_greeks,
            get_portfolio_metrics,
            get_account,
            get_open_orders,
            get_market_snapshot,
            get_bid_ask,
            get_trade_bid_ask,
            get_chain,
            whatif_order,
            get_recent_fills,
            get_risk_breaches,
            get_recent_market_intel,
            analyze_trade_candidate,
            get_strategy_snapshot,
            validate_strategies,
            optimize_capital,
        ]

    # ═══════════════════════════════════════════════════════════════════════
    # End of tool handlers
    # ═══════════════════════════════════════════════════════════════════════

    def _top_positions_for_metric(self, metric: str, *, limit: int = 4) -> list[dict[str, Any]]:
        positions = [p for p in (self._context.get("positions") or []) if isinstance(p, dict)]
        ranked = sorted(
            positions,
            key=lambda p: abs(float(p.get(metric) or 0.0)),
            reverse=True,
        )
        result: list[dict[str, Any]] = []
        for row in ranked[:limit]:
            result.append({
                "symbol": row.get("symbol"),
                "sec_type": row.get("sec_type"),
                "quantity": row.get("quantity"),
                "expiry": row.get("expiry"),
                metric: row.get(metric),
            })
        return result

    def _build_chain_snapshot_summary(self, *, limit: int = 8) -> dict[str, Any]:
        rows = list(self._engine.chain_snapshot() or [])
        if not rows:
            return {"row_count": 0, "underlying": None, "expiry": None, "sample": []}

        sample = []
        for row in rows[:limit]:
            sample.append({
                "underlying": getattr(row, "underlying", None),
                "expiry": getattr(row, "expiry", None),
                "strike": getattr(row, "strike", None),
                "right": getattr(row, "right", None),
                "bid": getattr(row, "bid", None),
                "ask": getattr(row, "ask", None),
                "delta": getattr(row, "delta", None),
            })
        first = rows[0]
        return {
            "row_count": len(rows),
            "underlying": getattr(first, "underlying", None),
            "expiry": getattr(first, "expiry", None),
            "sample": sample,
        }

    def _build_market_focus_quotes(self, *, limit: int = 12) -> dict[str, Any]:
        chain_rows = list(self._engine.chain_snapshot() or [])
        option_quotes: list[dict[str, Any]] = []
        for row in chain_rows[:limit]:
            option_quotes.append({
                "underlying": getattr(row, "underlying", None),
                "expiry": getattr(row, "expiry", None),
                "strike": getattr(row, "strike", None),
                "right": getattr(row, "right", None),
                "bid": getattr(row, "bid", None),
                "ask": getattr(row, "ask", None),
                "last": getattr(row, "last", None),
                "delta": getattr(row, "delta", None),
            })

        last_prices = self._context.get("last_prices") or {}
        spot_last_prices = [
            {"symbol": str(symbol), "last": value}
            for symbol, value in sorted(last_prices.items())[:limit]
        ]

        return {
            "spot_last_prices": spot_last_prices,
            "option_bid_ask_samples": option_quotes,
        }

    def _serialize_suggestions(self, *, limit: int = 5) -> list[dict[str, Any]]:
        serialized: list[dict[str, Any]] = []
        for suggestion in self._suggestions[:limit]:
            serialized.append({
                "rationale": suggestion.rationale,
                "projected_delta_change": suggestion.projected_delta_change,
                "projected_theta_cost": suggestion.projected_theta_cost,
                "legs": [
                    {
                        "symbol": leg.symbol,
                        "action": leg.action.value,
                        "quantity": leg.quantity,
                        "strike": leg.strike,
                        "right": leg.option_right.value if leg.option_right else None,
                        "expiry": leg.expiration.strftime("%Y%m%d") if leg.expiration else None,
                    }
                    for leg in suggestion.legs
                ],
            })
        return serialized

    def _build_portfolio_state(self) -> dict[str, Any]:
        summary = dict(self._context.get("summary") or {})
        open_orders = [o for o in (self._context.get("open_orders") or []) if isinstance(o, dict)]
        fills = [f for f in (self._context.get("recent_fills") or []) if isinstance(f, dict)]
        positions = [p for p in (self._context.get("positions") or []) if isinstance(p, dict)]
        expiries = sorted(
            {
                str(p.get("expiry") or "").strip()
                for p in positions
                if p.get("sec_type") in ("OPT", "FOP") and p.get("expiry")
            }
        )
        return {
            "selected_scenario": self._cmb_scenario.currentText(),
            "copilot_profile": (os.getenv("GITHUB_COPILOT_ACTIVE_PROFILE") or "personal").strip() or "personal",
            "current_model": self.current_model,
            "regime_name": self._context.get("regime_name"),
            "vix": self._context.get("vix"),
            "nlv": self._context.get("nlv"),
            "headline_metrics": summary,
            "risk_breaches": list(self._context.get("violations") or []),
            "resolved_limits": dict(self._context.get("resolved_limits") or {}),
            "active_option_expiries": expiries[:8],
            "largest_spx_delta_positions": self._top_positions_for_metric("spx_delta"),
            "largest_theta_positions": self._top_positions_for_metric("theta"),
            "largest_vega_positions": self._top_positions_for_metric("vega"),
            "open_order_count": len(open_orders),
            "open_order_symbols": sorted({str(o.get("symbol") or "").upper() for o in open_orders if o.get("symbol")}),
            "recent_fill_symbols": sorted({str(f.get("symbol") or "").upper() for f in fills if f.get("symbol")}),
            "cached_price_symbols": sorted((self._context.get("last_prices") or {}).keys())[:12],
            "market_focus_quotes": self._build_market_focus_quotes(),
            "active_chain": self._build_chain_snapshot_summary(),
            "current_ai_suggestions": self._serialize_suggestions(),
        }

    def _build_tools_context(self) -> tuple[dict[str, Any], list[str]]:
        tools_context: dict[str, Any] = {}
        tool_names = [
            ("tool:get_account_summary", "account"),
            ("tool:get_portfolio_positions", "positions"),
            ("tool:get_open_orders", "open_orders"),
            ("tool:get_portfolio_greeks", "summary"),
            ("tool:get_portfolio_metrics", "summary"),
            ("tool:get_market_prices", "prices"),
            ("tool:get_last_prices", "last_prices"),
            ("tool:get_risk_breaches", "violations"),
            ("tool:get_effective_limits", "resolved_limits"),
            ("tool:get_recent_fills", "recent_fills"),
            ("tool:get_recent_market_intel", "market_intel"),
            ("tool:get_order_log", "order_log"),
        ]
        tool_log_lines = ["🔧 Tools dispatched to LLM:"]
        for tool_key, ctx_key in tool_names:
            value = self._context.get(ctx_key)
            tools_context[tool_key] = value
            if value is None:
                size_hint = "—"
            elif isinstance(value, list):
                size_hint = f"{len(value)} items"
            elif isinstance(value, dict):
                size_hint = f"{len(value)} keys"
            else:
                size_hint = "✓"
            tool_log_lines.append(f"   {tool_key}: {size_hint}")

        tools_context["tool:get_portfolio_state"] = self._build_portfolio_state()
        tools_context["tool:get_trades_view_state"] = {
            "selected_scenario": self._cmb_scenario.currentText(),
            "current_model": self.current_model,
            "copilot_profile": (os.getenv("GITHUB_COPILOT_ACTIVE_PROFILE") or "personal").strip() or "personal",
            "suggestion_count": len(self._suggestions),
        }
        tools_context["tool:get_bid_ask"] = {
            "available": True,
            "note": "Use for contract-level bid/ask/mid quotes (stock, future, option)",
        }
        tools_context["tool:get_trade_bid_ask"] = {
            "available": True,
            "note": "Use for per-leg quotes and natural-vs-mid trade cost",
        }
        tools_context["tool:get_market_snapshot"] = {
            "available": True,
            "note": "Use for latest last/bid/ask snapshots on underlyings and indices",
        }
        tools_context["tool:analyze_trade_candidate"] = {
            "available": True,
            "note": "Use for portfolio + spread quality + optional WhatIf validation",
        }
        tool_log_lines.append("   tool:get_portfolio_state: structured summary")
        tool_log_lines.append("   tool:get_trades_view_state: UI state")
        tool_log_lines.append("   tool:get_bid_ask: callable")
        tool_log_lines.append("   tool:get_trade_bid_ask: callable")
        tool_log_lines.append("   tool:get_market_snapshot: callable")
        tool_log_lines.append("   tool:analyze_trade_candidate: callable")
        return tools_context, tool_log_lines

    def _build_chat_request(self, question: str) -> tuple[str, str, dict[str, Any], list[str]]:
        tools_context, tool_log_lines = self._build_tools_context()
        recent_history = self._chat_history[-10:]
        history_text = "\n".join(
            f"{role}: {msg}" for role, msg in recent_history
            if role in ("user", "assistant")
        )
        system = (
            "You are the desktop AI risk copilot for an options portfolio manager. "
            "You have access to the full portfolio via the tool outputs below.\n\n"
            "CAPABILITIES:\n"
            "- Answer questions about positions, Greeks, margin, P&L, risk breaches.\n"
            "- Propose specific trades to adjust risk, hedge, or generate income.\n"
            "- Reference open orders, recent fills, and the active chain when assessing current exposure.\n"
            "- Prioritize the structured portfolio state summary, then cite supporting tool outputs.\n\n"
            "TRADE PROPOSAL FORMAT:\n"
            "When proposing any trade, you MUST output a JSON block in this exact format "
            "so it can be auto-loaded into Order Entry. Use triple-backtick json fences:\n"
            "```json\n"
            "{\"trade_proposal\": {\n"
            "  \"strategy\": \"short description\",\n"
            "  \"legs\": [\n"
            "    {\"symbol\": \"MES\", \"sec_type\": \"FOP\", \"exchange\": \"CME\","
            "     \"action\": \"SELL\", \"qty\": 1, \"strike\": 5700,"
            "     \"right\": \"C\", \"expiry\": \"YYYYMMDD\"}\n"
            "  ],\n"
            "  \"rationale\": \"reason\",\n"
            "  \"credit_per_contract\": 12.50,\n"
            "  \"pop_pct\": 80\n"
            "}}\n"
            "```\n"
            "sec_type must be FOP (ES/MES/NQ/MNQ options), OPT (equity options), "
            "STK (stocks), or FUT (futures). Use precise YYYYMMDD expiry dates.\n\n"
            "Always be concrete: use actual strikes, expiries, and quantities."
        )
        prompt = (
            f"Conversation so far:\n{history_text}\n\n"
            f"Portfolio state (prioritize this summary first):\n"
            f"{json.dumps(tools_context.get('tool:get_portfolio_state'), default=str)[:40_000]}\n\n"
            f"Tool outputs (JSON):\n{json.dumps(tools_context, default=str)[:180_000]}\n\n"
            f"User question:\n{question}\n"
        )
        return system, prompt, tools_context, tool_log_lines

    def _finalize_chat_reply(
        self,
        reply: str,
        *,
        tool_lines: list[str] | None = None,
        debug_empty_tool_message: bool = False,
    ) -> None:
        if tool_lines:
            self._append_chat("tool", "\n".join(tool_lines))
        elif debug_empty_tool_message:
            self._append_chat("tool", "ℹ️ Tool logging enabled, but no tools were invoked for this answer.")

        self._append_chat("assistant", reply or "(No response)")

        proposals = self._parse_trade_proposals_from_reply(reply or "")
        if proposals:
            self._append_chat(
                "tool",
                f"🔧 Parsed {len(proposals)} trade proposal(s) from response → added to suggestion table",
            )
            self._add_inline_trade_suggestions(proposals)

    async def _refresh_context_for_prompt_chat(self) -> None:
        positions_data, account_data, open_orders, portfolio_metrics, breaches_data, recent_fills = await asyncio.gather(
            self._tool_get_positions(),
            self._tool_get_account(),
            self._tool_get_open_orders(),
            self._tool_get_portfolio_metrics(),
            self._tool_get_risk_breaches(),
            self._tool_get_recent_fills(limit=10),
        )

        try:
            recent_market_intel = await self._tool_get_recent_market_intel(limit=5)
        except Exception:
            recent_market_intel = []

        nlv = float(account_data.get("net_liquidation", 0.0) or 0.0)
        try:
            vix_snap = await self._engine.get_market_snapshot("VIX", "IND", "CBOE")
            vix_value = float(vix_snap.last or vix_snap.close or 0.0)
        except Exception:
            vix_value = 20.0

        loader = RiskRegimeLoader()
        regime_name, limits = loader.get_effective_limits(
            vix=vix_value,
            term_structure=1.0,
            recession_prob=0.0,
            nlv=nlv,
        )

        last_prices = dict(self._context.get("last_prices") or {})
        prices = dict(self._context.get("prices") or {})
        for position in positions_data:
            symbol = str(position.get("symbol") or "").upper()
            if not symbol:
                continue
            last_value = position.get("last")
            if last_value is not None:
                last_prices[symbol] = last_value
                prices[symbol] = {"last": last_value}

        self._context = {
            **self._context,
            "summary": portfolio_metrics,
            "regime_name": regime_name,
            "vix": vix_value,
            "nlv": nlv,
            "violations": breaches_data,
            "resolved_limits": limits,
            "positions": positions_data,
            "account": account_data,
            "open_orders": open_orders,
            "recent_fills": recent_fills,
            "market_intel": recent_market_intel,
            "last_prices": last_prices,
            "prices": prices,
        }

    async def _async_answer_with_context_prompt(self, question: str) -> None:
        self._lbl_status.setText("Thinking…")
        await self._refresh_context_for_prompt_chat()
        system, prompt, _tools_context, tool_log_lines = self._build_chat_request(question)
        reply = await async_llm_chat(
            prompt,
            model=self.current_model,
            system=system,
            timeout=60.0,
        )
        self._finalize_chat_reply(reply, tool_lines=tool_log_lines)
        self._lbl_status.setText("Ready")

    async def _async_answer_with_tool_session(self, question: str) -> None:
        from copilot import CopilotClient
        from copilot.generated.session_events import SessionEventType

        client = None
        session = None
        try:
            client = CopilotClient({"log_level": "error"})
            await client.start()

            debug_tool_calls = bool(load_preferences().get("debug_tool_calls", True))
            activity_event = asyncio.Event()
            response_chunks: list[str] = []
            tool_calls: list[str] = []
            self._set_ai_request_logging_context(
                activity_event=activity_event,
                tool_calls=tool_calls,
                debug_tool_calls=debug_tool_calls,
            )

            tools = self._create_tools_for_session()
            system = (
                "You are the desktop AI risk copilot for an options portfolio manager. "
                "You have access to real-time portfolio data via function calls.\n\n"
                "CAPABILITIES:\n"
                "- get_positions() → fetch current portfolio positions (5-minute cache because positions change slowly)\n"
                "- get_portfolio_greeks() → fetch aggregated Greeks with 1-minute cache freshness\n"
                "- get_portfolio_metrics() → fetch portfolio totals, exposures, and top contributors\n"
                "- get_account() → fetch account balances and margins\n"
                "- get_open_orders() → fetch open orders\n"
                "- get_market_snapshot(symbol, sec_type, exchange) → fetch current market price\n"
                "- get_bid_ask(symbol, strike, expiry, right, sec_type, exchange) → fetch stock, future, or option bid/ask\n"
                "- get_trade_bid_ask(legs) → fetch per-leg quotes and aggregate debit/credit for a multi-leg trade\n"
                "- analyze_trade_candidate(legs, include_whatif) → bundle portfolio risk, spread quality, and optional simulation\n"
                "- get_chain(underlying, expiry) → fetch options chain\n"
                "- whatif_order(legs) → simulate IBKR margin impact for a candidate trade\n"
                "- get_recent_fills(limit) → fetch recent fills from DB\n"
                "- get_risk_breaches() → check current risk violations\n\n"
                "IMPORTANT:\n"
                "- Always call functions to get fresh data — do not make assumptions about portfolio state\n"
                "- Positions are cached for 5 minutes; Greeks and portfolio metrics are cached for 1 minute\n"
                "- Prices are dynamic — call get_market_snapshot, get_bid_ask, get_trade_bid_ask, or analyze_trade_candidate for current quotes\n"
                "- Contract resolution requires market hours — if WhatIf fails, explain gracefully\n"
                "- For whatif_order, pass legs=[{symbol, action, qty, sec_type, exchange, expiry, strike, right}]\n"
                "- For /ES and /MES options use sec_type='FOP', exchange='CME', and include expiry/strike/right on every option leg\n"
                "- Use the field name 'qty' rather than 'quantity' when calling tools\n\n"
                "TRADE PROPOSAL FORMAT:\n"
                "When proposing a trade, output JSON in triple-backtick fences:\n"
                "```json\n"
                "{\"trade_proposal\": {\n"
                "  \"strategy\": \"description\",\n"
                "  \"legs\": [{\"symbol\": \"MES\", \"action\": \"SELL\", \"qty\": 1, "
                "\"strike\": 5700, \"right\": \"C\", \"expiry\": \"YYYYMMDD\", "
                "\"sec_type\": \"FOP\", \"exchange\": \"CME\"}],\n"
                "  \"rationale\": \"reason\",\n"
                "  \"credit_per_contract\": 12.50,\n"
                "  \"pop_pct\": 80\n"
                "}}\n"
                "```"
            )

            session_config: Any = {
                "model": self.current_model,
                "streaming": True,
                "tools": tools,
                "system_message": {"content": system, "role": "system"},
                "infinite_sessions": {"enabled": False},
            }

            session = await client.create_session(session_config)

            def handle_event(event):
                if event.type == SessionEventType.ASSISTANT_MESSAGE_DELTA:
                    delta = event.data.delta_content
                    response_chunks.append(delta)
                    activity_event.set()
                elif event.type in {SessionEventType.TOOL_EXECUTION_START, SessionEventType.TOOL_EXECUTION_COMPLETE}:
                    activity_event.set()

            session.on(handle_event)

            await self._wait_for_session_response(
                session,
                {"prompt": question},
                inactivity_timeout=self._AI_REQUEST_INACTIVITY_TIMEOUT_SECONDS,
                max_total_timeout=self._AI_REQUEST_MAX_WAIT_SECONDS,
                activity_event=activity_event,
            )

            self._finalize_chat_reply(
                "".join(response_chunks),
                tool_lines=tool_calls,
                debug_empty_tool_message=debug_tool_calls,
            )
            self._lbl_status.setText("Ready")
        finally:
            self._clear_ai_request_logging_context()
            if session is not None:
                try:
                    await session.destroy()
                except Exception:
                    pass
            if client is not None:
                try:
                    await client.stop()
                except Exception:
                    pass

    @Slot()
    def _on_audit(self) -> None:
        loop = asyncio.get_event_loop()
        loop.create_task(self._async_audit())

    async def _async_audit(self) -> None:
        """Run risk audit using fresh data from tool handlers."""
        self._lbl_status.setText("Running audit…")
        try:
            # Fetch fresh data via tool handlers
            positions_data = await self._tool_get_positions()
            account_data = await self._tool_get_account()
            breaches_data = await self._tool_get_risk_breaches()

            # Compute summary metrics
            total_spx_delta = sum(p.get("spx_delta", 0.0) or 0.0 for p in positions_data)
            total_gamma = sum(p.get("gamma", 0.0) or 0.0 for p in positions_data)
            total_theta = sum(p.get("theta", 0.0) or 0.0 for p in positions_data)
            total_vega = sum(p.get("vega", 0.0) or 0.0 for p in positions_data)
            nlv = float(account_data.get("net_liquidation", 0.0) or 0.0)

            option_positions = [p for p in positions_data if p.get("sec_type") in ("OPT", "FOP")]
            options_with_greeks = [
                p for p in option_positions
                if any(p.get(g) is not None for g in ("delta", "gamma", "theta", "vega"))
            ]
            greeks_coverage = (len(options_with_greeks) / len(option_positions)) if option_positions else 1.0
            theta_vega_ratio = (total_theta / total_vega) if total_vega else 0.0

            summary_dict = {
                "total_spx_delta": total_spx_delta,
                "total_gamma": total_gamma,
                "total_theta": total_theta,
                "total_vega": total_vega,
                "position_count": len(positions_data),
                "option_count": len(option_positions),
                "options_with_greeks": len(options_with_greeks),
                "greeks_coverage": greeks_coverage,
                "theta_vega_ratio": theta_vega_ratio,
                "theta_vega_zone": "unknown",
            }

            # Determine regime and get limits
            from agents.proposer_engine import RiskRegimeLoader
            loader = RiskRegimeLoader()

            try:
                vix_snap = await self._engine.get_market_snapshot("VIX", "IND", "CBOE")
                vix_value = float(vix_snap.last or vix_snap.close or 0.0)
            except Exception:
                vix_value = 20.0

            regime_name, limits = loader.get_effective_limits(
                vix=vix_value,
                term_structure=1.0,
                recession_prob=0.0,
                nlv=nlv,
            )

            # Run LLM audit
            auditor = LLMRiskAuditor(db=self._audit_store())
            auditor._model = self.current_model
            result = await auditor.audit_now(
                summary=summary_dict,
                regime_name=regime_name,
                vix=vix_value,
                term_structure=1.0,
                nlv=nlv,
                violations=breaches_data,
                resolved_limits=limits,
            )
            self._append_chat("assistant", f"[Risk Audit] {result.get('headline','')}\n{result.get('body','')}")
            self._lbl_status.setText(f"Audit complete ({result.get('urgency', 'unknown')})")
        except Exception as exc:
            self._append_chat("assistant", f"Audit failed: {exc}")
            self._lbl_status.setText(f"Audit failed: {exc}")

    @Slot()
    def _on_suggest(self) -> None:
        loop = asyncio.get_event_loop()
        loop.create_task(self._async_suggest())

    async def _async_suggest(self) -> None:
        """Generate trade suggestions using fresh data from tool handlers."""
        self._lbl_status.setText("Generating trade suggestions…")
        try:
            # Fetch fresh data via tool handlers
            positions_data = await self._tool_get_positions()
            breaches_data = await self._tool_get_risk_breaches()

            # Compute summary metrics
            total_spx_delta = sum(p.get("spx_delta", 0.0) or 0.0 for p in positions_data)
            total_gamma = sum(p.get("gamma", 0.0) or 0.0 for p in positions_data)
            total_theta = sum(p.get("theta", 0.0) or 0.0 for p in positions_data)
            total_vega = sum(p.get("vega", 0.0) or 0.0 for p in positions_data)

            option_positions = [p for p in positions_data if p.get("sec_type") in ("OPT", "FOP")]
            options_with_greeks = [
                p for p in option_positions
                if any(p.get(g) is not None for g in ("delta", "gamma", "theta", "vega"))
            ]
            greeks_coverage = (len(options_with_greeks) / len(option_positions)) if option_positions else 1.0

            if option_positions and greeks_coverage < 0.5:
                self._append_chat(
                    "assistant",
                    f"[Data Quality] Greeks coverage is too low ({len(options_with_greeks)}/{len(option_positions)}). "
                    f"Request may be inaccurate.",
                )

            pg = PortfolioGreeks(
                spx_delta=total_spx_delta,
                gamma=total_gamma,
                theta=total_theta,
                vega=total_vega,
            )

            # Construct bre ach object if violations exist
            breach_obj = None
            if breaches_data:
                first = breaches_data[0]
                try:
                    vix_snap = await self._engine.get_market_snapshot("VIX", "IND", "CBOE")
                    vix_value = float(vix_snap.last or vix_snap.close or 0.0)
                except Exception:
                    vix_value = 20.0

                from agents.proposer_engine import RiskRegimeLoader
                loader = RiskRegimeLoader()
                regime_name, _ = loader.get_effective_limits(
                    vix=vix_value, term_structure=1.0, recession_prob=0.0,
                    nlv=sum(float(p.get("market_value", 0.0) or 0.0) for p in positions_data)
                )

                breach_obj = RiskBreach(
                    breach_type=str(first.get("metric", "risk")),
                    threshold_value=float(first.get("limit", 0.0)),
                    actual_value=float(first.get("current", 0.0)),
                    regime=regime_name,
                    vix=vix_value,
                )

            auditor = LLMRiskAuditor(db=self._audit_store())
            auditor._model = self.current_model
            theta_budget = max(0.0, abs(pg.theta) * 0.30)

            # Extract nearest active expiry from FOP/OPT positions
            fop_expiries = sorted(
                str(p.get("expiration") or p.get("last_trade_date"))
                for p in positions_data
                if p.get("sec_type") in ("FOP", "OPT") and (p.get("expiration") or p.get("last_trade_date"))
            )
            active_expiry = ""
            if fop_expiries:
                raw_exp = str(fop_expiries[0])
                active_expiry = raw_exp.replace("-", "")[:8]

            try:
                vix_snap = await self._engine.get_market_snapshot("VIX", "IND", "CBOE")
                vix_value = float(vix_snap.last or vix_snap.close or 0.0)
            except Exception:
                vix_value = 20.0

            from agents.proposer_engine import RiskRegimeLoader
            loader = RiskRegimeLoader()
            regime_name, _ = loader.get_effective_limits(
                vix=vix_value, term_structure=1.0, recession_prob=0.0,
                nlv=sum(float(p.get("market_value", 0.0) or 0.0) for p in positions_data)
            )

            self._suggestions = await auditor.suggest_trades(
                portfolio_greeks=pg,
                vix=vix_value,
                regime=regime_name,
                breach=breach_obj,
                theta_budget=theta_budget,
                active_expiry=active_expiry,
                underlying="MES",
                ib_engine=self._engine,
            )
            self._render_suggestions()
            self._lbl_status.setText(f"Generated {len(self._suggestions)} suggestions")
        except Exception as exc:
            self._append_chat("assistant", f"Suggestion failed: {exc}")
            self._lbl_status.setText(f"Suggestion generation failed: {exc}")

    def _render_suggestions(self) -> None:
        self._tbl.setRowCount(len(self._suggestions))
        for i, s in enumerate(self._suggestions):
            leg_parts = []
            for leg in s.legs:
                parts = [leg.action.value, str(leg.quantity), leg.symbol]
                if leg.strike is not None:
                    parts.append(f"@{leg.strike:.0f}")
                if leg.option_right is not None:
                    parts.append(leg.option_right.value)
                if leg.expiration is not None:
                    parts.append(leg.expiration.strftime("%Y%m%d"))
                leg_parts.append(" ".join(parts))
            legs_text = " | ".join(leg_parts) or "—"
            self._tbl.setItem(i, 0, QTableWidgetItem(legs_text))
            self._tbl.setItem(i, 1, QTableWidgetItem(f"{s.projected_delta_change:+.2f}"))
            self._tbl.setItem(i, 2, QTableWidgetItem(f"{s.projected_theta_cost:+.2f}"))
            self._tbl.setItem(i, 3, QTableWidgetItem((s.rationale or "").strip()))
        self._btn_authorize.setEnabled(len(self._suggestions) > 0)
        self._btn_whatif.setEnabled(len(self._suggestions) > 0)
        self._btn_clear_suggestions.setEnabled(len(self._suggestions) > 0)

    @Slot()
    def _on_clear_suggestions(self) -> None:
        if not self._suggestions and self._tbl.rowCount() == 0:
            return
        self._suggestions = []
        self._tbl.setRowCount(0)
        self._btn_authorize.setEnabled(False)
        self._btn_whatif.setEnabled(False)
        self._btn_clear_suggestions.setEnabled(False)
        self._lbl_status.setText("Suggestions cleared")
        self._append_chat("tool", "🧹 Cleared suggested trades")

    @Slot()
    def _on_whatif(self) -> None:
        row = self._tbl.currentRow()
        if row < 0 or row >= len(self._suggestions):
            return
        suggestion = self._suggestions[row]
        loop = asyncio.get_event_loop()
        loop.create_task(self._async_whatif_suggestion(suggestion))

    async def _async_whatif_suggestion(self, suggestion: AITradeSuggestion) -> None:
        self._lbl_status.setText("Running WhatIf for AI suggestion…")
        try:
            legs_payload = self._suggestion_to_engine_legs(suggestion)
            result = await self._engine.whatif_order(
                legs_payload,
                order_type="LIMIT",
                limit_price=None,
            )
            self._append_chat("assistant", f"[WhatIf] {json.dumps(result, default=str)}")
            self._lbl_status.setText("WhatIf complete")
        except Exception as exc:
            self._append_chat("assistant", f"[WhatIf] Error: {exc}")
            self._lbl_status.setText("WhatIf failed")

    @Slot()
    def _on_ask(self) -> None:
        question = self._txt_user.toPlainText().strip()
        if not question:
            return
        self._txt_user.clear()
        self._append_chat("user", question)
        loop = asyncio.get_event_loop()
        loop.create_task(self._async_ask(question))

    @Slot()
    def _on_use_preset(self) -> None:
        prompt = str(self._cmb_presets.currentData() or "").strip()
        if not prompt:
            return
        self._txt_user.setPlainText(prompt)
        self._txt_user.setFocus()

    @Slot()
    def _on_preset_group_changed(self) -> None:
        self._populate_presets_for_group(self._current_preset_group())

    async def _async_ask(self, question: str) -> None:
        """Answer questions using tool-calling when possible, with a prompt-context fallback."""
        if not self._model_supports_tool_session():
            try:
                await self._async_answer_with_context_prompt(question)
            except Exception as exc:
                self._append_chat("assistant", f"Error: {exc}")
                self._lbl_status.setText("AI call failed")
            return

        try:
            await self._async_answer_with_tool_session(question)
        except asyncio.TimeoutError as exc:
            logger.warning("AI Risk tool session timed out; retrying with direct prompt context (%s)", exc)
            self._append_chat("tool", "⚠️ Tool session timed out; retrying with direct portfolio-context prompt.")
            try:
                await self._async_answer_with_context_prompt(question)
            except Exception as fallback_exc:
                message = str(fallback_exc) or f"Request timed out after {self._AI_REQUEST_INACTIVITY_TIMEOUT_SECONDS:.0f} seconds of inactivity"
                self._append_chat("assistant", f"Error: {message}")
                self._lbl_status.setText("Timeout")
        except Exception as exc:
            self._append_chat("assistant", f"Error: {exc}")
            self._lbl_status.setText("AI call failed")

    # ── Tool call log & trade proposal helpers ────────────────────────────

    async def _wait_for_session_response(
        self,
        session: Any,
        payload: dict[str, Any],
        *,
        inactivity_timeout: float,
        max_total_timeout: float | None = None,
        activity_event: asyncio.Event,
    ) -> Any:
        """Wait for a session response, resetting the timer whenever activity occurs."""
        send_task = asyncio.create_task(session.send_and_wait(payload))
        started_at = time.monotonic()
        last_wait_log_at = 0.0
        try:
            while True:
                activity_task = asyncio.create_task(activity_event.wait())
                elapsed = time.monotonic() - started_at
                if max_total_timeout is not None:
                    remaining_total = max_total_timeout - elapsed
                    if remaining_total <= 0:
                        logger.warning("AI Risk session exceeded %.0fs overall wait", max_total_timeout)
                        activity_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await activity_task
                        send_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await send_task
                        raise asyncio.TimeoutError(
                            f"Request exceeded {max_total_timeout:.0f} seconds overall. Active tools: {', '.join(self._active_ai_tool_names()) or 'none'}"
                        )
                    wait_timeout = min(inactivity_timeout, remaining_total)
                else:
                    wait_timeout = inactivity_timeout

                done, _pending = await asyncio.wait(
                    {send_task, activity_task},
                    timeout=wait_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if send_task in done:
                    if not activity_task.done():
                        activity_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await activity_task
                    return await send_task

                if activity_task in done:
                    activity_event.clear()
                    continue

                active_tools = self._active_ai_tool_names()
                activity_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await activity_task

                if active_tools:
                    now = time.monotonic()
                    if now - last_wait_log_at >= 10.0:
                        logger.info(
                            "AI Risk session still waiting after %.0fs; active tool(s): %s",
                            now - started_at,
                            ", ".join(active_tools),
                        )
                        last_wait_log_at = now
                    continue

                logger.warning("AI Risk session timed out after %.0fs of inactivity", inactivity_timeout)
                send_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await send_task
                raise asyncio.TimeoutError(f"Request timed out after {inactivity_timeout:.0f} seconds of inactivity")
        finally:
            if not send_task.done():
                send_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await send_task

    def _log_tool_call(self, tool_name: str, detail: str = "") -> None:
        """Append a tool-call log line to the chat (dimmed system role)."""
        msg = f"🔧 [{tool_name}]"
        if detail:
            msg += f" {detail}"
        self._append_chat("tool", msg)

    def _parse_trade_proposals_from_reply(self, reply: str) -> list[dict]:
        """Scan LLM reply for ```json {...trade_proposal...} ``` blocks.

        Returns a list of raw trade-proposal dicts (may be empty).
        """
        proposals: list[dict] = []
        # Match ```json ... ``` or ```trade_proposal ... ``` blocks
        pattern = re.compile(
            r"```(?:json|trade_proposal)?\s*(\{.*?\})\s*```",
            re.DOTALL | re.IGNORECASE,
        )
        for match in pattern.finditer(reply):
            raw = match.group(1).strip()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            # Support both {"trade_proposal": {...}} and bare proposal dicts
            if "trade_proposal" in parsed:
                proposal = parsed["trade_proposal"]
            elif "legs" in parsed:
                proposal = parsed
            else:
                continue
            if isinstance(proposal.get("legs"), list) and proposal["legs"]:
                proposals.append(proposal)
        return proposals

    def _add_inline_trade_suggestions(self, proposals: list[dict]) -> None:
        """Convert raw proposal dicts → AITradeSuggestion + add to table."""
        from models.order import OrderLeg

        new_suggestions: list[AITradeSuggestion] = []
        for p in proposals:
            raw_legs = p.get("legs") or []
            order_legs: list[OrderLeg] = []
            for lg in raw_legs:
                action_str = str(lg.get("action", "BUY")).upper()
                right_str = str(lg.get("right", "") or "").upper()
                right = OptionRight.CALL if right_str.startswith("C") else (
                    OptionRight.PUT if right_str.startswith("P") else None
                )
                expiry_raw = lg.get("expiry")
                expiry_date: date | None = None
                if expiry_raw:
                    try:
                        s = str(expiry_raw).replace("-", "")[:8]
                        expiry_date = date(int(s[:4]), int(s[4:6]), int(s[6:8]))
                    except Exception:
                        expiry_date = None
                order_legs.append(OrderLeg(
                    symbol=str(lg.get("symbol", "?")).upper(),
                    action=OrderAction.BUY if action_str == "BUY" else OrderAction.SELL,
                    quantity=max(1, int(float(lg.get("qty") or lg.get("quantity") or 1))),
                    option_right=right,
                    strike=float(lg.get("strike") or 0) or None,
                    expiration=expiry_date,
                    conid=str(lg.get("conid") or "") or None,
                ))
            if not order_legs:
                continue
            credit = float(p.get("credit_per_contract") or 0)
            pop = float(p.get("pop_pct") or 0)
            strategy = str(p.get("strategy") or "AI Chat Proposal")
            rationale_text = (
                str(p.get("rationale") or "")
                + (f" | Credit: ${credit:.2f}/contract" if credit else "")
                + (f" | PoP: {pop:.0f}%" if pop else "")
            ).strip(" |")
            new_suggestions.append(AITradeSuggestion(
                suggestion_id=str(uuid.uuid4()),
                legs=order_legs,
                projected_delta_change=0.0,
                projected_theta_cost=-credit if credit else 0.0,
                rationale=f"[{strategy}] {rationale_text}",
            ))
        if new_suggestions:
            self._suggestions = new_suggestions + list(self._suggestions)
            self._render_suggestions()

    def _append_chat(self, role: str, text: str) -> None:
        if not text:
            return
        self._chat_history.append((role, text))
        if role == "tool":
            # Tool logs: grey, small, collapsible visually via indentation
            self._txt_chat.append(f'<span style="color:#888; font-size:11px;">{text}</span>')
        elif role == "user":
            self._txt_chat.append(f"🧑 {text}")
        elif role == "system":
            self._txt_chat.append(f'<span style="color:#e67e22; font-weight:bold;">⚡ {text}</span>')
        else:
            self._txt_chat.append(f"🤖 {text}")

    _FOP_MULTIPLIERS = {"ES": "50", "MES": "5", "NQ": "20", "MNQ": "2",
                         "RTY": "50", "M2K": "5", "YM": "5", "MYM": "0.5"}

    def _suggestion_to_engine_legs(self, suggestion: AITradeSuggestion) -> list[dict[str, Any]]:
        legs = []
        for leg in suggestion.legs:
            sec_type = "FOP" if leg.symbol in {"ES", "MES", "NQ", "MNQ"} else "OPT"
            exchange = "CME" if sec_type == "FOP" else "SMART"
            expiry = leg.expiration.strftime("%Y%m%d") if isinstance(leg.expiration, date) else ""
            right = leg.option_right.value if isinstance(leg.option_right, OptionRight) else None
            legs.append(
                {
                    "symbol": leg.symbol,
                    "action": leg.action.value if isinstance(leg.action, OrderAction) else str(leg.action),
                    "qty": int(leg.quantity),
                    "sec_type": sec_type,
                    "exchange": exchange,
                    "strike": leg.strike,
                    "right": right,
                    "expiry": expiry,
                    "conid": leg.conid,
                    "multiplier": self._FOP_MULTIPLIERS.get(leg.symbol.upper(), ""),
                }
            )
        return legs

    async def _run_risk_validation_for_legs(self, legs: list[dict[str, Any]]) -> dict[str, Any]:
        analysis = await self._tool_analyze_trade_candidate(legs, include_whatif=True)
        whatif = analysis.get("whatif") if isinstance(analysis, dict) else None
        metrics_raw = analysis.get("portfolio_metrics") if isinstance(analysis, dict) else {}
        metrics = metrics_raw if isinstance(metrics_raw, dict) else {}
        breaches = analysis.get("risk_breaches") if isinstance(analysis, dict) else []

        verdict = "pass"
        reasons: list[str] = []

        if not isinstance(whatif, dict) or whatif.get("status") != "success":
            verdict = "fail"
            reasons.append(str((whatif or {}).get("error") or "WhatIf validation failed"))
        else:
            nlv = float(metrics.get("nlv") or 0.0)
            init_margin = float(metrics.get("init_margin") or 0.0)
            maint_margin = float(metrics.get("maint_margin") or 0.0)
            init_change = float(whatif.get("init_margin_change") or 0.0)
            maint_change = float(whatif.get("maint_margin_change") or 0.0)
            projected_init = init_margin + init_change
            projected_maint = maint_margin + maint_change

            if breaches:
                verdict = "warn"
                reasons.append(f"{len(breaches)} active risk breach(es) detected pre-trade")
            if nlv > 0 and projected_maint / nlv > 0.65:
                verdict = "warn"
                reasons.append("Projected maintenance margin exceeds 65% of NLV")

            analysis["projected_margin"] = {
                "init_margin": projected_init,
                "maint_margin": projected_maint,
                "nlv": nlv,
            }

        return {
            "status": verdict,
            "reasons": reasons,
            "analysis": analysis,
        }

    @Slot()
    def _on_authorize(self) -> None:
        row = self._tbl.currentRow()
        if row < 0 or row >= len(self._suggestions):
            return
        suggestion = self._suggestions[row]

        msg = QMessageBox.question(
            self,
            "Authorize AI Suggestion",
            "Create an order draft from the selected AI suggestion?\n"
            "You will still need to submit manually from Order Entry.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if msg != QMessageBox.StandardButton.Yes:
            return

        loop = asyncio.get_event_loop()
        loop.create_task(self._async_authorize_suggestion(suggestion))

    def _stage_authorized_suggestion(self, suggestion: AITradeSuggestion, validation: dict[str, Any], status: str) -> None:
        legs = self._suggestion_to_engine_legs(suggestion)
        payload = {
            "legs": legs,
            "rationale": suggestion.rationale,
            "model": self.current_model,
            "risk_validation": validation,
        }
        self.suggestion_authorized.emit(payload)
        if status == "warn":
            self._append_chat("assistant", "[Risk Validation] ⚠️ Staged with warnings after confirmation")
            self._lbl_status.setText("Suggestion staged (risk warnings acknowledged)")
        else:
            self._append_chat("assistant", "[Risk Validation] ✅ Passed — suggestion staged")
            self._lbl_status.setText("Suggestion staged in Order Entry")

    async def _async_authorize_suggestion(self, suggestion: AITradeSuggestion) -> None:
        self._lbl_status.setText("Validating suggestion with risk agent…")
        legs = self._suggestion_to_engine_legs(suggestion)
        validation = await self._run_risk_validation_for_legs(legs)
        status = str(validation.get("status") or "fail")
        reasons = validation.get("reasons") or []

        if status == "fail":
            reason_text = "; ".join(str(r) for r in reasons) or "Risk validation failed"
            self._append_chat("assistant", f"[Risk Validation] ❌ {reason_text}")
            self._lbl_status.setText("Risk validation failed")
            return

        if status == "warn":
            warning_text = "\n".join(f"• {r}" for r in reasons) if reasons else "• Risk warnings detected"
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Risk Validation Warning")
            box.setText(
                "Risk-agent validation returned warnings:\n"
                f"{warning_text}\n\n"
                "Stage this suggestion anyway?"
            )
            box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            box.setDefaultButton(QMessageBox.StandardButton.No)

            def _on_warning_choice(button) -> None:
                if box.standardButton(button) == QMessageBox.StandardButton.Yes:
                    self._stage_authorized_suggestion(suggestion, validation, "warn")
                else:
                    self._lbl_status.setText("Staging cancelled")

            box.buttonClicked.connect(_on_warning_choice)
            box.open()
            return

        self._stage_authorized_suggestion(suggestion, validation, status)

    # ── Agent runner signal handlers ──────────────────────────────────────

    @Slot(dict)
    def on_risk_alert(self, payload: dict) -> None:
        """Receive risk breach alert from AgentRunner and display in the chat."""
        severity = payload.get("severity", "warning")
        metric   = payload.get("label", payload.get("metric", "?"))
        current  = payload.get("current", 0)
        limit    = payload.get("limit", 0)
        message  = payload.get("message", "")
        color = "#e74c3c" if severity == "critical" else "#e67e22"
        self._append_chat(
            "system",
            f"⚠️ RISK ALERT [{metric}]: current={current:+.2f}, limit={limit:+.2f}\n{message}",
        )
        self._lbl_status.setText(f"⚠️ Risk breach: {metric}")
        self._lbl_status.setStyleSheet(f"color: {color}; font-weight: bold;")

    @Slot(dict)
    def on_arb_signal(self, payload: dict) -> None:
        """Receive arbitrage signal from AgentRunner."""
        message = payload.get("message", "")
        edge    = payload.get("edge", 0)
        self._append_chat("system", f"🎯 ARB SIGNAL (${abs(edge):.2f} edge):\n{message}")
        self._lbl_status.setText(f"Arb signal detected — ${abs(edge):.2f} edge")
        self._lbl_status.setStyleSheet("color: #27ae60; font-weight: bold;")

    @Slot(dict)
    def on_trade_suggestion(self, payload: dict) -> None:
        """Receive trade suggestion from AgentRunner and display in the chat."""
        strategy  = payload.get("strategy", "?")
        pop       = payload.get("pop_pct", 0)
        credit    = payload.get("credit_per_contract", 0)
        rationale = payload.get("rationale", "")
        self._append_chat(
            "system",
            f"💡 TRADE IDEA [{strategy}] PoP={pop:.0f}% Credit=${credit:.0f}/contract\n{rationale}",
        )
        self._lbl_status.setText(f"New trade suggestion: {strategy}")
        self._lbl_status.setStyleSheet("color: #2980b9; font-weight: bold;")
