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
import json
import logging
import os
import re
import subprocess
import uuid
from dataclasses import asdict
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
    strike: float = Field(description="Strike price")
    expiry: str = Field(description="Expiry date YYYYMMDD")
    right: str = Field(description="Option right (C or P)")
    sec_type: str = Field(description="OPT or FOP")
    exchange: str = Field(description="Exchange (SMART or CME)")


class GetChainParams(BaseModel):
    underlying: str = Field(description="Underlying symbol")
    expiry: str = Field(description="Expiry date YYYYMMDD")


class WhatIfOrderParams(BaseModel):
    legs: list[dict] = Field(description="List of order leg dicts with symbol, action, qty, sec_type, strike, right, expiry")


class GetRecentFillsParams(BaseModel):
    limit: int = Field(default=20, description="Max number of fills to return")


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

from agents.llm_client import async_list_models, async_llm_chat
from agents.llm_risk_auditor import LLMRiskAuditor
from agents.proposer_engine import BreachDetector, RiskRegimeLoader
from database.local_store import LocalStore
from desktop.config.preferences import load_preferences
from models.order import AITradeSuggestion, OptionRight, OrderAction, PortfolioGreeks, RiskBreach


def _default_trades_model() -> str:
    return (os.getenv("LLM_FAST_MODEL") or os.getenv("LLM_MODEL") or "gpt-5-mini").strip() or "gpt-5-mini"


class AIRiskTab(QWidget):
    """AI-driven risk chat + suggestion workflow."""

    suggestion_authorized = Signal(dict)  # {legs: [...], rationale: str, model: str}

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self._engine = engine
        self._context: dict[str, Any] = {}
        self._chat_history: list[tuple[str, str]] = []
        self._suggestions: list[AITradeSuggestion] = []
        self._setup_ui()
        self._connect_signals()

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
        row.addWidget(self._btn_whatif)
        row.addWidget(self._btn_authorize)
        layout.addLayout(row)

    def _connect_signals(self) -> None:
        self._btn_models.clicked.connect(self._on_refresh_models)
        self._btn_audit.clicked.connect(self._on_audit)
        self._btn_suggest.clicked.connect(self._on_suggest)
        self._btn_ask.clicked.connect(self._on_ask)
        self._btn_whatif.clicked.connect(self._on_whatif)
        self._btn_authorize.clicked.connect(self._on_authorize)
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
        bootstrap = [
            ("gpt-5-mini", "GPT-5 mini 🆓"),
            ("gpt-4.1", "GPT-4.1 🆓"),
            ("gpt-4o", "GPT-4o 🆓"),
            ("gpt-5", "GPT-5 💰"),
            ("o3", "o3 💰"),
        ]
        self._cmb_model.clear()
        for model_id, label in bootstrap:
            self._cmb_model.addItem(label, model_id)
        idx = self._cmb_model.findData(default_model)
        self._cmb_model.setCurrentIndex(idx if idx >= 0 else 0)

    @Slot()
    def _on_connected(self) -> None:
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

    @Slot()
    def _on_disconnected(self) -> None:
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
            current = self.current_model
            self._cmb_model.clear()
            for m in models:
                badge = "🆓" if m.get("is_free") else "💰"
                self._cmb_model.addItem(f"{m.get('name', m['id'])} {badge}", m["id"])
            idx = self._cmb_model.findData(current)
            self._cmb_model.setCurrentIndex(idx if idx >= 0 else 0)
            self._lbl_status.setText(f"Loaded {len(models)} models")
        except Exception as exc:
            self._lbl_status.setText(f"Model load failed: {exc}")

    @property
    def current_model(self) -> str:
        return str(self._cmb_model.currentData() or _default_trades_model())

    # ═══════════════════════════════════════════════════════════════════════
    # Tool handlers for LLM function calling
    # ═══════════════════════════════════════════════════════════════════════

    async def _tool_get_positions(self) -> list[dict[str, Any]]:
        """Fetch current portfolio positions with Greeks."""
        positions = await self._engine.refresh_positions()
        return [asdict(p) for p in positions]

    async def _tool_get_account(self) -> dict[str, Any]:
        """Fetch account summary (NLV, margins, cash)."""
        account = await self._engine.refresh_account()
        return asdict(account) if account else {}

    async def _tool_get_open_orders(self) -> list[dict[str, Any]]:
        """Fetch open orders from IB."""
        orders = await self._engine.get_open_orders()
        return [asdict(o) for o in orders]

    async def _tool_get_market_snapshot(self, symbol: str, sec_type: str, exchange: str) -> dict[str, Any]:
        """Fetch current market price snapshot for a symbol."""
        snap = await self._engine.get_market_snapshot(symbol, sec_type, exchange)
        return asdict(snap)

    async def _tool_get_bid_ask(
        self, symbol: str, strike: float, expiry: str, right: str, sec_type: str, exchange: str
    ) -> dict[str, Any]:
        """Fetch bid/ask for specific option contract."""
        # This would call chain data or specific contract lookup
        # For now, return cached price or empty
        price_cache = getattr(self._engine, "_last_price_cache", {}) or {}
        key = f"{symbol}_{strike}_{expiry}_{right}"
        if key in price_cache:
            return {"symbol": symbol, "strike": strike, "expiry": expiry, "right": right, "price": price_cache[key]}
        return {"symbol": symbol, "strike": strike, "expiry": expiry, "right": right, "price": None}

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

    async def _tool_get_recent_fills(self, limit: int = 20) -> list[dict[str, Any]]:
        """Fetch recent fills from database."""
        if not self._engine._db_ok:
            return []
        try:
            fill_rows = await self._engine._db.get_fills(self._engine._account_id, limit=limit)
            return [dict(row) if not isinstance(row, dict) else row for row in fill_rows]
        except Exception:
            return []

    async def _tool_get_risk_breaches(self) -> list[dict[str, Any]]:
        """Check current risk violations against regime limits."""
        from agents.proposer_engine import BreachDetector, RiskRegimeLoader

        positions = await self._engine.refresh_positions()
        account = await self._engine.refresh_account()

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
                params.symbol, params.strike, params.expiry, params.right, params.sec_type, params.exchange
            )

        @define_tool(description="Get options chain for underlying and expiry")
        async def get_chain(params: GetChainParams) -> list[dict]:
            return await self._tool_get_chain(params.underlying, params.expiry)

        @define_tool(description="Run WhatIf simulation for proposed trade legs")
        async def whatif_order(params: WhatIfOrderParams) -> dict:
            return await self._tool_whatif_order(params.legs)

        @define_tool(description="Get recent fills from database")
        async def get_recent_fills(params: GetRecentFillsParams) -> list[dict]:
            return await self._tool_get_recent_fills(params.limit)

        @define_tool(description="Check current risk violations against regime limits")
        async def get_risk_breaches(params: EmptyParams) -> list[dict]:
            return await self._tool_get_risk_breaches()

        return [
            get_positions,
            get_account,
            get_open_orders,
            get_market_snapshot,
            get_bid_ask,
            get_chain,
            whatif_order,
            get_recent_fills,
            get_risk_breaches,
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
            ("tool:get_market_prices", "prices"),
            ("tool:get_last_prices", "last_prices"),
            ("tool:get_risk_breaches", "violations"),
            ("tool:get_effective_limits", "resolved_limits"),
            ("tool:get_recent_fills", "recent_fills"),
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
        tool_log_lines.append("   tool:get_portfolio_state: structured summary")
        tool_log_lines.append("   tool:get_trades_view_state: UI state")
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
            auditor = LLMRiskAuditor(db=LocalStore())
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

            auditor = LLMRiskAuditor(db=LocalStore())
            auditor._model = self.current_model
            theta_budget = max(0.0, abs(pg.theta) * 0.30)

            # Extract nearest active expiry from FOP/OPT positions
            fop_expiries = sorted(
                {
                    p.get("expiration") or p.get("last_trade_date")
                    for p in positions_data
                    if p.get("sec_type") in ("FOP", "OPT") and (p.get("expiration") or p.get("last_trade_date"))
                }
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
                "- get_positions() → fetch current positions with Greeks\n"
                "- get_account() → fetch account balances and margins\n"
                "- get_open_orders() → fetch open orders\n"
                "- get_market_snapshot(symbol, sec_type, exchange) → fetch current market price\n"
                "- get_bid_ask(symbol, strike, expiry, right, sec_type, exchange) → fetch option bid/ask\n"
                "- get_chain(underlying, expiry) → fetch options chain\n"
                "- whatif_order(legs) → simulate trade P&L and Greeks impact\n"
                "- get_recent_fills(limit) → fetch recent fills from DB\n"
                "- get_risk_breaches() → check current risk violations\n\n"
                "IMPORTANT:\n"
                "- Always call functions to get fresh data — do not make assumptions about portfolio state\n"
                "- Prices are dynamic — call get_market_snapshot or get_bid_ask for current quotes\n"
                "- Contract resolution requires market hours — if WhatIf fails, explain gracefully\n\n"
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

            session = await client.create_session({
                "model": self.current_model,
                "streaming": True,
                "tools": tools,
                "system_message": {"content": system, "role": "system"},
                "infinite_sessions": {"enabled": False},
            })

            # Track streaming response and tool calls
            response_chunks: list[str] = []
            tool_calls: list[str] = []
            debug_tool_calls = bool(load_preferences().get("debug_tool_calls", True))

            def handle_event(event):
                if event.type == SessionEventType.ASSISTANT_MESSAGE_DELTA:
                    delta = event.data.delta_content
                    response_chunks.append(delta)
                elif event.type == SessionEventType.TOOL_EXECUTION_START:
                    tool_name = getattr(event.data, "tool_name", "unknown")
                    if debug_tool_calls:
                        logger.info("AI Risk tool start: %s", tool_name)
                        tool_calls.append(f"🔧 Calling tool: {tool_name}")
                elif event.type == SessionEventType.TOOL_EXECUTION_COMPLETE:
                    tool_name = getattr(event.data, "tool_name", "unknown")
                    if debug_tool_calls:
                        logger.info("AI Risk tool complete: %s", tool_name)
                        tool_calls.append(f"✓ Tool complete: {tool_name}")

            session.on(handle_event)

            # Send question and wait for full response
            await asyncio.wait_for(
                session.send_and_wait({"prompt": question}),
                timeout=90.0,
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
            self._append_chat("assistant", "Error: Request timed out after 90 seconds")
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

        legs = self._suggestion_to_engine_legs(suggestion)

        payload = {
            "legs": legs,
            "rationale": suggestion.rationale,
            "model": self.current_model,
        }
        self.suggestion_authorized.emit(payload)
        self._lbl_status.setText("Suggestion staged in Order Entry")

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
