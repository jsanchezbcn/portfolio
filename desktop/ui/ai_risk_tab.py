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
import os
import re
import subprocess
import uuid
from dataclasses import asdict
from datetime import date
from typing import Any


def _get_copilot_account() -> str:
    """Detect which GitHub Copilot account is currently configured."""
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=5,
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
from models.order import AITradeSuggestion, OptionRight, OrderAction, PortfolioGreeks, RiskBreach


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

        self._btn_context = QPushButton("📡 Refresh Context")
        top.addWidget(self._btn_context)

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
        self._btn_context.clicked.connect(self._on_refresh_context)
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
        self._btn_context.setEnabled(enabled)
        self._btn_audit.setEnabled(enabled)
        self._btn_suggest.setEnabled(enabled)
        self._btn_ask.setEnabled(enabled)

    def _load_model_defaults(self) -> None:
        default_model = (os.getenv("LLM_FAST_MODEL") or os.getenv("LLM_MODEL") or "gpt-5-mini").strip()
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
        self._append_chat(
            "assistant",
            f"🟢 **Connected**\n"
            f"- IBKR Account: `{account_id}`\n"
            f"- GitHub Copilot: `{gh_account}`\n"
            f"- Model: `{model}`\n\n"
            f"Click **📡 Refresh Context** to load your portfolio, then use "
            f"**🛡 Run Risk Audit** or **✨ Suggest Trades**.",
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
        return str(self._cmb_model.currentData() or "gpt-5-mini")

    @Slot()
    def _on_refresh_context(self) -> None:
        loop = asyncio.get_event_loop()
        loop.create_task(self._async_refresh_context())

    async def _async_refresh_context(self) -> None:
        self._lbl_status.setText("Refreshing context…")
        try:
            # ── Tool: positions ───────────────────────────────────────────
            self._log_tool_call("get_portfolio_positions", "fetching live positions + Greeks…")
            positions = await self._engine.refresh_positions()

            # ── Tool: account ─────────────────────────────────────────────
            self._log_tool_call("get_account_summary", "fetching account balances…")
            account = await self._engine.refresh_account()

            # ── Tool: open orders ─────────────────────────────────────────
            self._log_tool_call("get_open_orders", "fetching open orders from IB…")
            open_orders = await self._engine.get_open_orders()

            option_positions = [p for p in positions if p.sec_type in ("OPT", "FOP")]
            options_with_greeks = [
                p for p in option_positions
                if any(v is not None for v in (p.delta, p.gamma, p.theta, p.vega))
            ]
            greeks_coverage = (
                (len(options_with_greeks) / len(option_positions))
                if option_positions else 1.0
            )

            # ── Tool: market prices ───────────────────────────────────────
            self._log_tool_call("get_market_prices", "fetching ES + VIX snapshots…")
            es_snap = await self._engine.get_market_snapshot("ES", "FUT", "CME")
            try:
                vix_snap = await self._engine.get_market_snapshot("VIX", "IND", "CBOE")
                vix_value = float(vix_snap.last or vix_snap.close or 0.0)
            except Exception:
                vix_value = 20.0

            total_spx_delta = sum((p.spx_delta or 0.0) for p in positions)
            total_gamma = sum((p.gamma or 0.0) for p in positions)
            total_theta = sum((p.theta or 0.0) for p in positions)
            total_vega = sum((p.vega or 0.0) for p in positions)
            nlv = float(account.net_liquidation) if account else 0.0
            margin_used = float(account.init_margin) if account else 0.0

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

            scenario = self._cmb_scenario.currentText()
            scenario_forced = {
                "Low Volatility": "low_volatility",
                "Neutral Volatility": "neutral_volatility",
                "High Volatility": "high_volatility",
                "Crisis Mode": "crisis_mode",
            }.get(scenario)

            regime_name, limits = loader.get_effective_limits(
                vix=vix_value,
                term_structure=1.0,
                recession_prob=0.0,
                nlv=nlv,
            )
            if scenario_forced:
                regime_name = scenario_forced

            # ── Tool: risk breaches ───────────────────────────────────────
            self._log_tool_call("get_risk_breaches", f"checking limits for regime '{regime_name}'…")
            events = detector.check(
                greeks_snapshot,
                account_nlv=nlv,
                account_id=self._engine.account_id,
                margin_used=margin_used,
            )
            violations = [
                {
                    "metric": e.greek,
                    "current": e.current_value,
                    "limit": e.limit,
                    "message": f"distance={e.distance_to_target:.2f}",
                }
                for e in events
            ]

            # ── Tool: recent fills (DB, optional) ─────────────────────────
            recent_fills: list[dict] = []
            if self._engine._db_ok:
                try:
                    self._log_tool_call("get_recent_fills", "loading last 20 fills from DB…")
                    fill_rows = await self._engine._db.get_fills(
                        self._engine._account_id, limit=20
                    )
                    for row in fill_rows:
                        if isinstance(row, dict):
                            recent_fills.append(row)
                        else:
                            recent_fills.append(dict(row))
                except Exception as exc:
                    self._log_tool_call("get_recent_fills", f"unavailable: {exc}")

            # ── Tool: order log (DB, optional) ────────────────────────────
            order_log: list[dict] = []
            if self._engine._db_ok:
                try:
                    self._log_tool_call("get_order_log", "loading last 30 orders from DB…")
                    order_rows = await self._engine._db.get_orders(
                        self._engine._account_id, limit=30
                    )
                    for row in order_rows:
                        r = dict(row) if not isinstance(row, dict) else row
                        legs_raw = r.get("legs_json") or r.get("legs") or []
                        if isinstance(legs_raw, str):
                            try:
                                legs_raw = json.loads(legs_raw)
                            except Exception:
                                legs_raw = []
                        order_log.append({
                            "created_at": str(r.get("created_at") or "")[:19],
                            "status": r.get("status"),
                            "side": r.get("side"),
                            "order_type": r.get("order_type"),
                            "limit_price": r.get("limit_price"),
                            "filled_price": r.get("filled_price"),
                            "source": r.get("source"),
                            "rationale": str(r.get("rationale") or "")[:120],
                            "legs": legs_raw,
                        })
                except Exception as exc:
                    self._log_tool_call("get_order_log", f"unavailable: {exc}")

            # ── Tool: last known prices ───────────────────────────────────
            price_cache = dict(getattr(self._engine, "_last_price_cache", {}) or {})
            self._log_tool_call("get_last_prices", f"{len(price_cache)} symbols cached")

            self._context = {
                "summary": {
                    "total_spx_delta": total_spx_delta,
                    "total_gamma": total_gamma,
                    "total_theta": total_theta,
                    "total_vega": total_vega,
                    "position_count": len(positions),
                    "option_count": len(option_positions),
                    "options_with_greeks": len(options_with_greeks),
                    "greeks_coverage": greeks_coverage,
                    "theta_vega_ratio": (total_theta / total_vega) if total_vega else 0.0,
                    "theta_vega_zone": "unknown",
                },
                "regime_name": regime_name,
                "vix": vix_value,
                "term_structure": 1.0,
                "nlv": nlv,
                "violations": violations,
                "resolved_limits": limits,
                "account": asdict(account) if account else {},
                "positions": [asdict(p) for p in positions],
                "open_orders": [asdict(o) for o in open_orders],
                "recent_fills": recent_fills,
                "order_log": order_log,
                "last_prices": price_cache,
                "prices": {
                    "ES": asdict(es_snap),
                    "VIX": {"last": vix_value},
                },
            }
            self._lbl_status.setText(
                f"Context ready: {len(positions)} positions, {len(violations)} breaches, "
                f"Greeks {len(options_with_greeks)}/{len(option_positions)}, "
                f"{len(open_orders)} open orders"
            )
        except Exception as exc:
            self._lbl_status.setText(f"Context refresh failed: {exc}")

    @Slot()
    def _on_audit(self) -> None:
        loop = asyncio.get_event_loop()
        loop.create_task(self._async_audit())

    async def _async_audit(self) -> None:
        if not self._context:
            await self._async_refresh_context()
            if not self._context:
                return
        self._lbl_status.setText("Running audit…")
        try:
            auditor = LLMRiskAuditor(db=LocalStore())
            auditor._model = self.current_model
            result = await auditor.audit_now(
                summary=self._context["summary"],
                regime_name=self._context["regime_name"],
                vix=float(self._context["vix"]),
                term_structure=float(self._context["term_structure"]),
                nlv=float(self._context["nlv"] or 0.0),
                violations=list(self._context.get("violations") or []),
                resolved_limits=dict(self._context.get("resolved_limits") or {}),
            )
            self._append_chat("assistant", f"[Risk Audit] {result.get('headline','')}\n{result.get('body','')}")
            self._lbl_status.setText(f"Audit complete ({result.get('urgency', 'unknown')})")
        except Exception as exc:
            self._lbl_status.setText(f"Audit failed: {exc}")

    @Slot()
    def _on_suggest(self) -> None:
        loop = asyncio.get_event_loop()
        loop.create_task(self._async_suggest())

    async def _async_suggest(self) -> None:
        if not self._context:
            await self._async_refresh_context()
            if not self._context:
                return
        self._lbl_status.setText("Generating trade suggestions…")
        try:
            summary = self._context["summary"]
            coverage = float(summary.get("greeks_coverage") or 0.0)
            option_count = int(summary.get("option_count") or 0)
            with_greeks = int(summary.get("options_with_greeks") or 0)
            if option_count > 0 and coverage < 0.5:
                self._append_chat(
                    "assistant",
                    f"[Data Quality] Greeks coverage is too low ({with_greeks}/{option_count}). Refresh context before requesting suggestions.",
                )
                self._lbl_status.setText("Suggestion blocked: insufficient Greeks coverage")
                return

            pg = PortfolioGreeks(
                spx_delta=float(summary.get("total_spx_delta") or 0.0),
                gamma=float(summary.get("total_gamma") or 0.0),
                theta=float(summary.get("total_theta") or 0.0),
                vega=float(summary.get("total_vega") or 0.0),
            )
            breach_obj = None
            violations = self._context.get("violations") or []
            if violations:
                first = violations[0]
                breach_obj = RiskBreach(
                    breach_type=str(first.get("metric") or "risk"),
                    threshold_value=float(first.get("limit") or 0.0),
                    actual_value=float(first.get("current") or 0.0),
                    regime=str(self._context.get("regime_name") or "unknown"),
                    vix=float(self._context.get("vix") or 0.0),
                )

            auditor = LLMRiskAuditor(db=LocalStore())
            auditor._model = self.current_model
            theta_budget = max(0.0, abs(pg.theta) * 0.30)

            # Extract nearest active expiry from FOP/OPT positions
            positions_raw = self._context.get("positions") or []
            fop_expiries = sorted(
                {
                    p.get("expiration") or p.get("last_trade_date")
                    for p in positions_raw
                    if p.get("sec_type") in ("FOP", "OPT") and (p.get("expiration") or p.get("last_trade_date"))
                }
            )
            active_expiry = ""
            if fop_expiries:
                raw_exp = str(fop_expiries[0])
                # Normalise: remove dashes, keep YYYYMMDD
                active_expiry = raw_exp.replace("-", "")[:8]

            self._suggestions = await auditor.suggest_trades(
                portfolio_greeks=pg,
                vix=float(self._context.get("vix") or 0.0),
                regime=str(self._context.get("regime_name") or "unknown"),
                breach=breach_obj,
                theta_budget=theta_budget,
                active_expiry=active_expiry,
                underlying="MES",
            )
            self._render_suggestions()
            self._lbl_status.setText(f"Generated {len(self._suggestions)} suggestions")
        except Exception as exc:
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
        if not self._context:
            await self._async_refresh_context()
            if not self._context:
                return
        self._lbl_status.setText("Thinking…")
        try:
            # ── Assemble tool outputs shown to the LLM ────────────────────
            tools_context = {}
            tool_names = [
                ("tool:get_account_summary",    "account"),
                ("tool:get_portfolio_positions","positions"),
                ("tool:get_open_orders",        "open_orders"),
                ("tool:get_portfolio_greeks",   "summary"),
                ("tool:get_market_prices",      "prices"),
                ("tool:get_last_prices",        "last_prices"),
                ("tool:get_risk_breaches",      "violations"),
                ("tool:get_effective_limits",   "resolved_limits"),
                ("tool:get_recent_fills",       "recent_fills"),
                ("tool:get_order_log",          "order_log"),
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
            self._append_chat("tool", "\n".join(tool_log_lines))

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
                "- Reference open orders and recent fills when assessing current exposure.\n\n"
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
                f"Tool outputs (JSON):\n{json.dumps(tools_context, default=str)[:180_000]}\n\n"
                f"User question:\n{question}\n"
            )
            reply = await async_llm_chat(prompt, model=self.current_model, system=system, timeout=60.0)
            self._append_chat("assistant", reply or "(No response)")

            # ── Parse any inline trade proposals from the reply ────────────
            proposals = self._parse_trade_proposals_from_reply(reply or "")
            if proposals:
                self._append_chat(
                    "tool",
                    f"🔧 Parsed {len(proposals)} trade proposal(s) from response → added to suggestion table",
                )
                self._add_inline_trade_suggestions(proposals)

            self._lbl_status.setText("Ready")
        except Exception as exc:
            self._append_chat("assistant", f"Error: {exc}")
            self._lbl_status.setText("AI call failed")

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
