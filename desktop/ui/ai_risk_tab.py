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

from agents.llm_client import async_list_models, async_llm_chat, get_hardcoded_models
from agents.llm_risk_auditor import LLMRiskAuditor
from agents.proposer_engine import BreachDetector, RiskRegimeLoader
from desktop.config.preferences import load_preferences
from models.order import AITradeSuggestion, OptionRight, OrderAction, PortfolioGreeks, RiskBreach


def _default_trades_model() -> str:
    return (os.getenv("LLM_FAST_MODEL") or os.getenv("LLM_MODEL") or "gpt-5-mini").strip() or "gpt-5-mini"


class _NoopAuditStore:
    async def upsert_market_intel(self, **_: Any) -> str:
        return ""


class AIRiskTab(QWidget):
    """AI-driven risk chat + suggestion workflow."""

    _POSITIONS_TTL_SECONDS = 300.0
    _GREEKS_TTL_SECONDS = 60.0
    _ACCOUNT_TTL_SECONDS = 60.0
    _OPEN_ORDERS_TTL_SECONDS = 30.0
    _RECENT_FILLS_TTL_SECONDS = 60.0
    _QUOTE_TTL_SECONDS = 30.0
    _TRADE_QUOTE_TTL_SECONDS = 30.0
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

    def _clear_tool_cache(self) -> None:
        self._tool_cache.clear()

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
        value = await producer()
        self._tool_cache[key] = _ToolCacheEntry(value=value, fetched_at=now)
        return value

    async def _get_positions_data(self, *, require_fresh_greeks: bool = False) -> list[Any]:
        ttl_seconds = self._GREEKS_TTL_SECONDS if require_fresh_greeks else self._POSITIONS_TTL_SECONDS
        key = "positions:fresh" if require_fresh_greeks else "positions:snapshot"
        return await self._get_cached_or_fetch(key, ttl_seconds=ttl_seconds, producer=self._engine.refresh_positions)

    async def _get_account_data(self) -> Any:
        return await self._get_cached_or_fetch(
            "account",
            ttl_seconds=self._ACCOUNT_TTL_SECONDS,
            producer=self._engine.refresh_account,
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
        total_delta = sum(float(getattr(p, "delta", 0.0) or 0.0) for p in positions)
        total_gamma = sum(float(getattr(p, "gamma", 0.0) or 0.0) for p in positions)
        total_theta = sum(float(getattr(p, "theta", 0.0) or 0.0) for p in positions)
        total_vega = sum(float(getattr(p, "vega", 0.0) or 0.0) for p in positions)
        total_spx_delta = sum(float(getattr(p, "spx_delta", 0.0) or 0.0) for p in positions)
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

    async def _tool_get_portfolio_metrics(self) -> dict[str, Any]:
        positions = await self._get_positions_data(require_fresh_greeks=True)
        account = await self._get_account_data()
        return self._portfolio_metrics_from_positions(positions, account)

    async def _tool_get_portfolio_greeks(self) -> dict[str, Any]:
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
                "quotes": int(self._QUOTE_TTL_SECONDS),
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

    # ═══════════════════════════════════════════════════════════════════════
    # Tool handlers for LLM function calling
    # ═══════════════════════════════════════════════════════════════════════

    async def _tool_get_positions(self) -> list[dict[str, Any]]:
        """Fetch current portfolio positions with Greeks."""
        positions = await self._get_positions_data(require_fresh_greeks=False)
        return [asdict(p) for p in positions]

    async def _tool_get_account(self) -> dict[str, Any]:
        """Fetch account summary (NLV, margins, cash)."""
        account = await self._get_account_data()
        return asdict(account) if account else {}

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
        rows = self._engine.chain_snapshot() or []
        result = []
        for row in rows:
            if getattr(row, "underlying", None) == underlying and getattr(row, "expiry", None) == expiry:
                # Handle both dataclass and SimpleNamespace/mock objects
                if hasattr(row, "__dataclass_fields__"):
                    result.append(asdict(row))
                else:
                    result.append(vars(row) if hasattr(row, "__dict__") else dict(row))
        return result

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

    def _create_tools_for_session(self) -> list:
        """Create Copilot SDK tool definitions that call instance methods."""
        # Define tools with closures over self
        @define_tool(description="Get current portfolio positions with Greeks and P&L")
        async def get_positions(params: EmptyParams) -> list[dict]:
            return await self._tool_get_positions()

        @define_tool(description="Get aggregated portfolio Greeks with 1-minute cache freshness")
        async def get_portfolio_greeks(params: EmptyParams) -> dict:
            return await self._tool_get_portfolio_greeks()

        @define_tool(description="Get aggregated portfolio metrics, exposures, and top risk contributors")
        async def get_portfolio_metrics(params: EmptyParams) -> dict:
            return await self._tool_get_portfolio_metrics()

        @define_tool(description="Get account summary (NLV, margins, cash)")
        async def get_account(params: EmptyParams) -> dict:
            return await self._tool_get_account()

        @define_tool(description="Get open orders from IB")
        async def get_open_orders(params: EmptyParams) -> list[dict]:
            return await self._tool_get_open_orders()

        @define_tool(description="Get current market price snapshot for a symbol")
        async def get_market_snapshot(params: GetMarketSnapshotParams) -> dict:
            return await self._tool_get_market_snapshot(params.symbol, params.sec_type, params.exchange)

        @define_tool(description="Get bid/ask for specific option contract")
        async def get_bid_ask(params: GetBidAskParams) -> dict:
            return await self._tool_get_bid_ask(
                params.symbol,
                params.strike,
                params.expiry,
                params.right,
                params.sec_type,
                params.exchange,
                conid=params.conid,
                multiplier=params.multiplier,
            )

        @define_tool(description="Get per-leg quotes and estimated net debit/credit for a multi-leg trade")
        async def get_trade_bid_ask(params: GetTradeBidAskParams) -> dict:
            return await self._tool_get_trade_bid_ask(self._normalize_tool_legs(params.legs))

        @define_tool(description="Get options chain for underlying and expiry")
        async def get_chain(params: GetChainParams) -> list[dict]:
            return await self._tool_get_chain(params.underlying, params.expiry)

        @define_tool(description="Run an IBKR WhatIf simulation. Required payload: legs=[{symbol, action, qty, sec_type, exchange, expiry, strike, right}] with option fields included for FOP/OPT legs")
        async def whatif_order(params: WhatIfOrderParams) -> dict:
            return await self._tool_whatif_order(self._normalize_tool_legs(params.legs))

        @define_tool(description="Get recent fills from database")
        async def get_recent_fills(params: GetRecentFillsParams) -> list[dict]:
            return await self._tool_get_recent_fills(params.limit)

        @define_tool(description="Check current risk violations against regime limits")
        async def get_risk_breaches(params: EmptyParams) -> list[dict]:
            return await self._tool_get_risk_breaches()

        @define_tool(description="Get recent stored market intel, LLM briefs, and audit entries from Postgres")
        async def get_recent_market_intel(params: GetRecentMarketIntelParams) -> list[dict]:
            return await self._tool_get_recent_market_intel(params.limit)

        @define_tool(description="Bundle portfolio risk, trade spread quality, and optional WhatIf for a candidate trade")
        async def analyze_trade_candidate(params: AnalyzeTradeCandidateParams) -> dict:
            return await self._tool_analyze_trade_candidate(
                self._normalize_tool_legs(params.legs),
                include_whatif=params.include_whatif,
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
        """Use Copilot SDK with function calling to answer questions on-demand."""
        self._lbl_status.setText("Thinking…")
        from copilot import CopilotClient
        from copilot.generated.session_events import SessionEventType

        client = None
        session = None
        try:
            # Create SDK client and session with tools
            client = CopilotClient({"log_level": "error"})
            await client.start()

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

            # Track streaming response and tool calls
            response_chunks: list[str] = []
            tool_calls: list[str] = []
            pending_tool_calls: list[str] = []
            debug_tool_calls = bool(load_preferences().get("debug_tool_calls", True))
            activity_event = asyncio.Event()

            def handle_event(event):
                if event.type == SessionEventType.ASSISTANT_MESSAGE_DELTA:
                    delta = event.data.delta_content
                    response_chunks.append(delta)
                    activity_event.set()
                elif event.type == SessionEventType.TOOL_EXECUTION_START:
                    tool_name = getattr(event.data, "tool_name", "unknown")
                    activity_event.set()
                    if debug_tool_calls:
                        logger.info("AI Risk tool start: %s", tool_name)
                        pending_tool_calls.append(tool_name)
                        tool_calls.append(f"🔧 Calling tool: {tool_name}")
                elif event.type == SessionEventType.TOOL_EXECUTION_COMPLETE:
                    tool_name = getattr(event.data, "tool_name", None) or getattr(event.data, "name", None)
                    activity_event.set()
                    if not tool_name and pending_tool_calls:
                        tool_name = pending_tool_calls.pop(0)
                    tool_name = tool_name or "unknown"
                    if debug_tool_calls:
                        logger.info("AI Risk tool complete: %s", tool_name)
                        tool_calls.append(f"✓ Tool complete: {tool_name}")

            session.on(handle_event)

            # Send question and wait for full response
            await self._wait_for_session_response(
                session,
                {"prompt": question},
                inactivity_timeout=90.0,
                activity_event=activity_event,
            )

            # Log tool calls if any
            if tool_calls:
                self._append_chat("tool", "\n".join(tool_calls))
            elif debug_tool_calls:
                self._append_chat("tool", "ℹ️ Tool logging enabled, but no tools were invoked for this answer.")

            # Assemble and display response
            reply = "".join(response_chunks)
            self._append_chat("assistant", reply or "(No response)")

            # Parse any inline trade proposals from the reply
            proposals = self._parse_trade_proposals_from_reply(reply or "")
            if proposals:
                self._append_chat(
                    "tool",
                    f"🔧 Parsed {len(proposals)} trade proposal(s) from response → added to suggestion table",
                )
                self._add_inline_trade_suggestions(proposals)

            self._lbl_status.setText("Ready")

        except asyncio.TimeoutError:
            self._append_chat("assistant", "Error: Request timed out after 90 seconds of inactivity")
            self._lbl_status.setText("Timeout")
        except Exception as exc:
            self._append_chat("assistant", f"Error: {exc}")
            self._lbl_status.setText("AI call failed")
        finally:
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

    # ── Tool call log & trade proposal helpers ────────────────────────────

    async def _wait_for_session_response(
        self,
        session: Any,
        payload: dict[str, Any],
        *,
        inactivity_timeout: float,
        activity_event: asyncio.Event,
    ) -> Any:
        """Wait for a session response, resetting the timer whenever activity occurs."""
        send_task = asyncio.create_task(session.send_and_wait(payload))
        try:
            while True:
                activity_task = asyncio.create_task(activity_event.wait())
                done, _pending = await asyncio.wait(
                    {send_task, activity_task},
                    timeout=inactivity_timeout,
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

                logger.warning("AI Risk session timed out after %.0fs of inactivity", inactivity_timeout)
                activity_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await activity_task
                send_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await send_task
                raise asyncio.TimeoutError(f"No AI session activity for {inactivity_timeout:.0f} seconds")
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
