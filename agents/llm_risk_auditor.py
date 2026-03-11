"""
agents/llm_risk_auditor.py — LLM-powered live portfolio risk audit.

Uses GitHub Copilot SDK (gpt-5-mini by default — FREE via subscription) to
continuously audit portfolio Greeks against the active market regime and
generate natural-language explanations of risks and actionable suggestions.

What the LLM does:
  - Interprets Greek profile in plain English (e.g., "Your theta income is
    well below regime minimum. At current pace you are not compensated for
    the risk you're taking.")
  - Explains what a VIX/regime combination means for the strategy mix.
  - Flags unusual Greek combinations (e.g., positive vega + positive delta
    in crisis mode) that rules cannot catch.
  - Proposes 1-3 specific adjustments ranked by urgency.

The audit result is written to the ``market_intel`` table so it surfaces
on the dashboard without any polling overhead.

Environment variables:
    LLM_MODEL                    : model name (default "gpt-5-mini")
  RISK_AUDIT_INTERVAL_SECONDS  : cadence in seconds (default 300 = 5 min)
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from agents.llm_client import async_llm_chat
from datetime import date as _date

from models.order import (
    AITradeSuggestion,
    OrderAction,
    OrderLeg,
    OptionRight,
    PortfolioGreeks,
    RiskBreach,
)

logger = logging.getLogger(__name__)


def _default_risk_model() -> str:
    return (os.getenv("LLM_FAST_MODEL") or os.getenv("LLM_MODEL") or "gpt-5-mini").strip() or "gpt-5-mini"

_SYSTEM_PROMPT = """\
You are a professional options portfolio risk manager specialising in
index-volatility strategies (SPX/ES options, iron condors, strangles,
backspreads). Your job is to audit a portfolio snapshot and give the trader
a concise, actionable risk assessment.

Rules:
- Be direct. Avoid hedged language like "may" or "could" when the data is clear.
- Reference specific Greek values in your response.
- If a limit is breached, say so explicitly and label it URGENT.
- Propose concrete adjustments (e.g., "Sell 1 SPX iron condor with 30 DTE
  to add ~$X theta without increasing delta beyond Y").
- Keep the total response under 200 words.
- Output JSON with keys: "headline" (1 sentence), "body" (≤ 150 words),
  "urgency" ("green" | "yellow" | "red"), "suggestions" (list of ≤3 strings).
"""


_SUGGEST_SYSTEM_PROMPT = """\
You are a quantitative options risk analyst for a trader who ONLY trades ES and MES \
futures options (FOP) on CME. We do NOT trade SPX options.

A portfolio risk breach has been detected. Your job is to suggest exactly 3 \
concrete remediation trades using ES or MES futures options that would reduce \
the breach while preserving theta income within the given budget.

Product reference:
  ES  = E-mini S&P 500 Futures Option, exchange CME, multiplier 50, sec_type FOP
  MES = Micro E-mini S&P 500 Futures Option, exchange CME, multiplier 5, sec_type FOP
  Use MES for smaller adjustments (< 20 deltas), ES for larger (>= 20 deltas).

Commission schedule (IB Pro rate, per contract per side):
  ES  FOP: ~$1.40/side → ~$2.80 round-trip per contract
  MES FOP: ~$0.47/side → ~$0.94 round-trip per contract
  Delta equivalence: 1 ES contract = 10 MES contracts (multiplier ratio 50:5)
  Cost for equivalent delta:
    1 ES round-trip  = $2.80
    10 MES round-trip = $9.40  (3.4× more expensive for identical delta exposure)
  Fee efficiency rule:
    → PREFER ES when the target quantity is ≥ 1 ES-equivalent (i.e. ≥ 10 MES legs
      for a single-leg trade or ≥ 5 MES per leg in a multi-leg spread).
    → ONLY use MES when fine-tuning delta by < 10 contracts (sub-ES granularity).
    If your suggestion would require 10 or more MES contracts, convert to ES instead.

Rules:
- Respond ONLY with a valid JSON array of exactly 3 objects. No markdown fences, \
no extra text.
- Each object must have these top-level fields:
    "legs": array of leg objects (see below)
    "projected_delta_change": float (negative = delta reduced)
    "projected_theta_cost": float (negative = theta earned; positive = theta spent)
    "rationale": string (1-2 sentences, cite specific Greek values)
- Each leg object MUST have ALL of these fields:
    "symbol":   "ES" or "MES"
    "sec_type": "FOP"
    "exchange": "CME"
    "action":   "BUY" or "SELL"
    "quantity": positive integer
    "strike":   float (option strike price, e.g. 5850.0)
    "right":    "C" for call or "P" for put
    "expiry":   "YYYYMMDD" string matching the active expiry provided in the prompt
- Never use SPX, SPXW, SPY, or any equity option symbol.
- If action cannot be determined, omit the suggestion (return fewer objects).
- Never raise exceptions — always return valid JSON.

Available execution/planning tools in the host app (do not invent new tools):
    - get_bid_ask_for_legs(legs): returns live bid/ask/mid for specific legs (can be slow: ~2-10s)
    - get_chain(symbol, expiry, strikes_each_side): returns options chain around ATM (can be slow: ~5-15s)
        - whatif_order(legs): IBKR margin simulation for a candidate trade (can be very slow: up to ~90s)
            Required leg schema: {symbol, action, qty, sec_type, exchange, expiry, strike, right}

Latency guidance:
    - Prefer chain + bid/ask data first for liquidity screening.
    - Use WhatIf only when needed for final candidate validation due to latency.
    - If tool data is missing/stale, state assumptions briefly in rationale.
"""


class LLMRiskAuditor:
    """Periodically audits portfolio risk using an LLM.

    Args:
        db: Initialised DBManager instance.
        interval_seconds: How often to run, in seconds (default 300).
    """

    def __init__(
        self,
        *,
        db: Any,
        regime_detector: Any | None = None,
        interval_seconds: int = 300,
    ) -> None:
        self.db = db
        self.regime_detector = regime_detector
        self.interval_seconds = interval_seconds
        self._scheduler: AsyncIOScheduler | None = None
        self._model = _default_risk_model()
        # Latest snapshot pushed externally by the dashboard/streaming pipeline
        self._latest_context: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Public API — called by the dashboard to push fresh context
    # ------------------------------------------------------------------

    def update_context(
        self,
        *,
        summary: dict[str, Any],
        regime_name: str,
        vix: float,
        term_structure: float,
        nlv: float | None,
        violations: list[dict[str, Any]],
        resolved_limits: dict[str, Any] | None = None,
    ) -> None:
        """Push a fresh portfolio snapshot for the next audit cycle.

        This is intentionally synchronous (no await) so the dashboard can
        call it from any thread.
        """
        self._latest_context = {
            "summary": summary,
            "regime_name": regime_name,
            "vix": round(vix, 2),
            "term_structure": round(term_structure, 4),
            "nlv": round(nlv, 0) if nlv else None,
            "violations": violations,
            "resolved_limits": resolved_limits,
        }

    # ------------------------------------------------------------------
    # Scheduler lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._scheduler = AsyncIOScheduler()
        self._scheduler.add_job(
            self._run_audit,
            "interval",
            seconds=self.interval_seconds,
            id="llm_risk_audit",
            replace_existing=True,
            max_instances=1,
        )
        self._scheduler.start()
        logger.info(
            "LLMRiskAuditor started (model=%s interval=%ds)",
            self._model,
            self.interval_seconds,
        )

    async def stop(self) -> None:
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Core audit logic
    # ------------------------------------------------------------------

    async def _run_audit(self) -> None:
        ctx = self._latest_context
        if ctx is None:
            logger.debug("LLMRiskAuditor: no context yet, skipping")
            return

        try:
            result = await self._audit(ctx)
            await self._persist(ctx, result)
        except Exception as exc:
            logger.warning("LLMRiskAuditor error: %s", exc, exc_info=True)

    async def _audit(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Build prompt, call LLM, return parsed result dict."""
        prompt = self._build_prompt(ctx)
        raw = await async_llm_chat(prompt, model=self._model, timeout=60.0)
        return self._parse_response(raw)

    def _build_prompt(self, ctx: dict[str, Any]) -> str:
        s = ctx["summary"]
        limits = ctx.get("resolved_limits") or {}
        violations = ctx.get("violations") or []

        violation_text = (
            "\n".join(
                f"  BREACH {v['metric']}: current={v['current']:.2f} limit={v['limit']:.2f} — {v.get('message','')}"
                for v in violations
            )
            if violations
            else "  None"
        )

        limit_text = (
            "\n".join(f"  {k}: {round(v, 2) if isinstance(v, float) else v}" for k, v in limits.items()
                      if k not in ("nlv_used", "vix_scaler", "ts_scaler", "is_nlv_scaled",
                                   "max_single_underlying_vega_pct", "max_position_contracts",
                                   "allowed_strategies"))
            if limits
            else "  Not available"
        )

        nlv_str = f"${ctx['nlv']:,.0f}" if ctx["nlv"] else "unknown"

        return f"""{_SYSTEM_PROMPT}

## Portfolio Snapshot

Account NLV:       {nlv_str}
Regime:            {ctx['regime_name'].replace('_', ' ').title()}
VIX:               {ctx['vix']}
Term Structure:    {ctx['term_structure']} (>1.0 = contango, <1.0 = backwardation)

### Portfolio Greeks (portfolio-level totals)
SPX Beta-Delta:    {s.get('total_spx_delta', 0):.2f}
Total Gamma:       {s.get('total_gamma', 0):.4f}
Daily Theta ($/d): {s.get('total_theta', 0):.2f}
Total Vega ($):    {s.get('total_vega', 0):.2f}
Position Count:    {s.get('position_count', 0)}
Theta/Vega Ratio:  {s.get('theta_vega_ratio', 0):.3f} ({s.get('theta_vega_zone', 'unknown')})

### Effective Risk Limits (scaled for NLV + market conditions)
{limit_text}

### Active Violations
{violation_text}

Please audit this snapshot and return ONLY a JSON object (no markdown fences)
with the keys described in the system prompt.
"""

    @staticmethod
    def _parse_response(raw: str) -> dict[str, Any]:
        """Extract JSON from the LLM response; fall back gracefully."""
        # Strip markdown fences if present
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(
                line for line in lines
                if not line.startswith("```")
            ).strip()
        try:
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise ValueError("Not a dict")
            return {
                "headline": str(parsed.get("headline", "Risk audit complete.")),
                "body": str(parsed.get("body", raw)),
                "urgency": str(parsed.get("urgency", "green")),
                "suggestions": list(parsed.get("suggestions", [])),
            }
        except Exception:
            return {
                "headline": "LLM risk audit (parsing fallback)",
                "body": raw[:500] if raw else "No response",
                "urgency": "yellow",
                "suggestions": [],
            }

    async def _persist(self, ctx: dict[str, Any], result: dict[str, Any]) -> None:
        """Write the audit result to market_intel."""
        content = {
            "headline": result["headline"],
            "body": result["body"],
            "urgency": result["urgency"],
            "suggestions": result["suggestions"],
            "vix": ctx["vix"],
            "regime": ctx["regime_name"],
            "nlv": ctx["nlv"],
            "violations_count": len(ctx.get("violations") or []),
        }
        try:
            await self.db.upsert_market_intel(
                symbol="PORTFOLIO",
                source="llm_risk_audit",
                sentiment_score=None,
                summary=result["headline"],
                raw_data=content,
            )
            logger.info(
                "LLM risk audit stored — urgency=%s violations=%d",
                result["urgency"],
                content["violations_count"],
            )
        except Exception as exc:
            logger.warning("LLMRiskAuditor persist error: %s", exc)

    # ------------------------------------------------------------------
    # On-demand audit (for dashboard "Audit Now" button)
    # ------------------------------------------------------------------

    async def audit_now(
        self,
        *,
        summary: dict[str, Any],
        regime_name: str,
        vix: float,
        term_structure: float,
        nlv: float | None,
        violations: list[dict[str, Any]],
        resolved_limits: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run an immediate audit and return the result dict.

        Does NOT require the scheduler to be running.
        """
        ctx = {
            "summary": summary,
            "regime_name": regime_name,
            "vix": round(vix, 2),
            "term_structure": round(term_structure, 4),
            "nlv": round(nlv, 0) if nlv else None,
            "violations": violations,
            "resolved_limits": resolved_limits,
        }
        result = await self._audit(ctx)
        await self._persist(ctx, result)
        return result

    # ------------------------------------------------------------------
    # T048-T050: Trade suggestion (AI remediation recommendations)
    # ------------------------------------------------------------------

    async def suggest_trades(
        self,
        *,
        portfolio_greeks: PortfolioGreeks,
        vix: float,
        regime: str,
        breach: Optional[RiskBreach],
        theta_budget: float,
        active_expiry: str = "",
        underlying: str = "MES",
        ib_engine: Any = None,
    ) -> list[AITradeSuggestion]:
        """Return up to 3 AI-generated trades to remediate a risk breach.

        Args:
            portfolio_greeks: Current aggregate portfolio Greeks.
            vix:              Current VIX value.
            regime:           Active market regime name.
            breach:           The detected breach (or None for general audit).
            theta_budget:     Maximum theta that can be spent/risked.
            active_expiry:    Active expiry to use for suggestions (YYYYMMDD string).
            underlying:       Underlying symbol to use (default "MES").
            ib_engine:        Optional IBEngine instance for fetching live bid/ask spreads.

        Returns:
            List of ``AITradeSuggestion`` (0–3 elements).  Never raises.
        """
        try:
            # Pre-fetch live market data (bid/ask spreads, underlying price) if engine available
            market_data_context = ""
            if ib_engine is not None and active_expiry:
                try:
                    market_data_context = await self._fetch_market_data_context(
                        ib_engine=ib_engine,
                        underlying=underlying,
                        active_expiry=active_expiry,
                    )
                except Exception as exc:
                    logger.debug("Failed to fetch market data context: %s", exc)

            prompt = self._build_suggest_prompt(
                portfolio_greeks=portfolio_greeks,
                vix=vix,
                regime=regime,
                breach=breach,
                theta_budget=theta_budget,
                active_expiry=active_expiry,
                underlying=underlying,
                market_data_context=market_data_context,
            )
            raw = await async_llm_chat(
                prompt,
                model=self._model,
                system=_SUGGEST_SYSTEM_PROMPT,
                timeout=45.0,
            )
            parsed = self._rank_and_filter_suggestions(
                self._parse_suggestions(raw),
                breach=breach,
                theta_budget=theta_budget,
                active_expiry=active_expiry,
                underlying=underlying,
            )
            if parsed:
                return parsed
            logger.warning("suggest_trades: empty/invalid LLM output, using deterministic fallback suggestions")
            return self._fallback_suggestions(
                portfolio_greeks=portfolio_greeks,
                breach=breach,
                theta_budget=theta_budget,
                active_expiry=active_expiry,
                underlying=underlying,
            )
        except Exception as exc:
            logger.warning("suggest_trades() error: %s", exc, exc_info=True)
            return self._fallback_suggestions(
                portfolio_greeks=portfolio_greeks,
                breach=breach,
                theta_budget=theta_budget,
                active_expiry=active_expiry,
                underlying=underlying,
            )

    # --- private helpers for suggest_trades ---

    async def _fetch_market_data_context(
        self,
        *,
        ib_engine: Any,
        underlying: str,
        active_expiry: str,
    ) -> str:
        """Fetch live bid/ask spreads and underlying price for the LLM prompt.
        
        Returns formatted string with:
        - Underlying price
        - ATM strike region
        - Bid/ask spreads for ~5 strikes around ATM (calls and puts)
        
        If any fetch fails, returns empty string (non-blocking).
        """
        try:
            # 1. Get underlying price
            snap = await ib_engine.get_market_snapshot(underlying)
            underlying_price = float(
                getattr(snap, "last", None)
                or getattr(snap, "close", None)
                or 0.0
            )
            if not underlying_price:
                return ""
            
            # 2. Round to nearest 25 for ATM strike
            atm_strike = round(underlying_price / 25) * 25
            
            # 3. Fetch options chain (±3 strikes around ATM)
            if len(active_expiry) == 8:
                expiry_date = _date(int(active_expiry[:4]), int(active_expiry[4:6]), int(active_expiry[6:8]))
            else:
                return ""
            
            chain = await ib_engine.get_chain(
                underlying=underlying,
                expiry=expiry_date,
                max_strikes=14,
            )
            if not chain:
                return ""
            
            grouped: dict[float, dict[str, Any]] = {}
            for row in chain:
                strike = float(getattr(row, "strike", 0.0) or 0.0)
                if strike <= 0:
                    continue
                entry = grouped.setdefault(strike, {"C": None, "P": None})
                right = str(getattr(row, "right", "") or "").upper()
                if right in ("C", "P"):
                    entry[right] = row

            closest_strikes = sorted(grouped.keys(), key=lambda strike: abs(strike - atm_strike))[:7]
            closest_strikes.sort()

            # 4. Build bid/ask table
            lines = [
                f"\\n## Live Market Data (Expiry {active_expiry})",
                f"Underlying {underlying} price: ${underlying_price:.2f}",
                f"ATM strike: {atm_strike}",
                "\\nOptions Chain (Bid/Ask/Mid):",
                "Strike | Call Bid | Call Ask | Call Mid | Put Bid | Put Ask | Put Mid",
                "-------|----------|----------|----------|---------|---------|--------",
            ]
            
            for strike in closest_strikes:
                call = grouped[strike].get("C")
                put = grouped[strike].get("P")
                call_bid = float(getattr(call, "bid", 0.0) or 0.0)
                call_ask = float(getattr(call, "ask", 0.0) or 0.0)
                call_mid = float(getattr(call, "last", 0.0) or 0.0) or ((call_bid + call_ask) / 2.0 if call_bid and call_ask else 0.0)
                put_bid = float(getattr(put, "bid", 0.0) or 0.0)
                put_ask = float(getattr(put, "ask", 0.0) or 0.0)
                put_mid = float(getattr(put, "last", 0.0) or 0.0) or ((put_bid + put_ask) / 2.0 if put_bid and put_ask else 0.0)
                
                lines.append(
                    f"{strike:>6.0f} | {call_bid:>8.2f} | {call_ask:>8.2f} | "
                    f"{call_mid:>8.2f} | {put_bid:>7.2f} | {put_ask:>7.2f} | {put_mid:>7.2f}"
                )
            
            lines.append("\\nUse these bid/ask spreads to ensure your suggested trades are realistic.")
            lines.append("Pick strikes with tight spreads (low bid/ask difference) for better execution probability.")
            
            return "\\n".join(lines)
            
        except Exception as exc:
            logger.debug("_fetch_market_data_context failed: %s", exc)
            return ""

    @staticmethod
    def _build_suggest_prompt(
        *,
        portfolio_greeks: PortfolioGreeks,
        vix: float,
        regime: str,
        breach: Optional[RiskBreach],
        theta_budget: float,
        active_expiry: str = "",
        underlying: str = "MES",
        market_data_context: str = "",
    ) -> str:
        g = portfolio_greeks
        heuristics = LLMRiskAuditor._build_trade_selection_heuristics(
            portfolio_greeks=portfolio_greeks,
            breach=breach,
            theta_budget=theta_budget,
            active_expiry=active_expiry,
            underlying=underlying,
        )
        breach_section = (
            f"Breach Type:    {breach.breach_type}\n"
            f"Threshold:      {breach.threshold_value}\n"
            f"Actual Value:   {breach.actual_value}\n"
            f"Breach Regime:  {breach.regime}\n"
            f"Breach VIX:     {breach.vix}\n"
        ) if breach else "No specific breach — general portfolio optimisation requested.\n"

        expiry_hint = (
            f"Active expiry to use for suggestions: {active_expiry}\n"
            if active_expiry else
            "No active expiry in portfolio — pick the nearest front-month expiry.\n"
        )

        market_section = market_data_context if market_data_context else ""

        return (
            f"## Portfolio Greeks\n"
            f"SPX Beta-Delta:   {g.spx_delta:.2f}\n"
            f"Gamma:            {g.gamma:.4f}\n"
            f"Daily Theta ($/d):{g.theta:.2f}\n"
            f"Vega ($):         {g.vega:.2f}\n\n"
            f"## Market Context\n"
            f"VIX:              {vix:.2f}\n"
            f"Regime:           {regime}\n\n"
            f"## Active Instrument\n"
            f"Preferred underlying: {underlying}\n"
            f"{expiry_hint}"
            f"{market_section}"
            f"## Risk Breach\n"
            f"{breach_section}\n"
            f"## Constraints\n"
            f"Theta Budget (max to spend): {theta_budget:.2f}\n\n"
            f"## Execution Heuristics\n"
            f"{heuristics}\n\n"
            f"## Available Tools (host app)\n"
            f"- get_bid_ask_for_legs: live bid/ask/mid for candidate legs (slow: ~2-10s)\n"
            f"- get_chain: broader options chain around ATM for expiry (slow: ~5-15s)\n"
            f"- whatif_order: IBKR margin/risk simulation for final candidates (very slow: up to ~90s)\n"
            f"  Required legs format: {{symbol, action, qty, sec_type, exchange, expiry, strike, right}}\n"
            f"Use slow tools only when they materially improve trade quality.\n\n"
            f"Respond ONLY with a JSON array of exactly 3 suggestion objects "
            f"as described in the system prompt. Each leg MUST include symbol, "
            f"sec_type, exchange, action, quantity, strike, right, and expiry fields."
        )

    @staticmethod
    def _parse_suggestions(raw: str) -> list[AITradeSuggestion]:
        """Parse LLM JSON → list[AITradeSuggestion].  Returns [] on any error."""
        text = raw.strip() if raw else ""
        # Guard against empty response (Copilot SDK sometimes returns "")
        if not text:
            logger.warning("suggest_trades: LLM returned empty response — fallback required")
            return []
        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(l for l in lines if not l.startswith("""`""")).strip()
        if not text:
            logger.warning("suggest_trades: LLM response was only markdown fences — fallback required")
            return []
        try:
            parsed = json.loads(text)
            if not isinstance(parsed, list):
                logger.warning("suggest_trades: LLM returned non-list JSON")
                return []
            results: list[AITradeSuggestion] = []
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                legs_raw = item.get("legs", [])
                legs: list[OrderLeg] = []
                for lr in legs_raw:
                    if not isinstance(lr, dict):
                        continue
                    try:
                        action = OrderAction(lr.get("action", "BUY").upper())
                    except ValueError:
                        action = OrderAction.BUY
                    # Parse option-specific fields
                    strike_raw = lr.get("strike")
                    strike = float(strike_raw) if strike_raw is not None else None
                    right_raw = str(lr.get("right") or "").upper().strip()
                    right_map = {"C": "C", "CALL": "C", "P": "P", "PUT": "P"}
                    right_val = right_map.get(right_raw)
                    option_right: Optional[OptionRight] = None
                    if right_val:
                        try:
                            option_right = OptionRight(right_val)
                        except ValueError:
                            pass
                    expiry_raw = str(lr.get("expiry") or "").replace("-", "").strip()
                    expiration = None
                    if len(expiry_raw) == 8:
                        try:
                            expiration = _date(
                                int(expiry_raw[:4]),
                                int(expiry_raw[4:6]),
                                int(expiry_raw[6:8]),
                            )
                        except (ValueError, OverflowError):
                            pass
                    legs.append(
                        OrderLeg(
                            symbol=str(lr.get("symbol", "MES")),
                            action=action,
                            quantity=int(lr.get("quantity", 1)),
                            option_right=option_right,
                            strike=strike,
                            expiration=expiration,
                        )
                    )
                results.append(
                    AITradeSuggestion(
                        legs=legs,
                        projected_delta_change=float(item.get("projected_delta_change", 0.0)),
                        projected_theta_cost=float(item.get("projected_theta_cost", 0.0)),
                        rationale=str(item.get("rationale", "")),
                    )
                )
            return results
        except Exception as exc:
            logger.warning("suggest_trades: parse error — %s", exc)
            return []

    @staticmethod
    def _build_trade_selection_heuristics(
        *,
        portfolio_greeks: PortfolioGreeks,
        breach: Optional[RiskBreach],
        theta_budget: float,
        active_expiry: str,
        underlying: str,
    ) -> str:
        target = str(getattr(breach, "breach_type", "") or "").lower()
        delta = float(portfolio_greeks.spx_delta or 0.0)
        gamma = float(portfolio_greeks.gamma or 0.0)
        theta = float(portfolio_greeks.theta or 0.0)
        vega = float(portfolio_greeks.vega or 0.0)
        product = "ES" if abs(delta) >= 20 else (underlying if underlying in ("ES", "MES") else "MES")
        guidance = [
            f"Use {product} as the default product unless a finer adjustment requires MES.",
            f"Keep expiry on {active_expiry or 'the nearest active expiry'} unless a longer-dated hedge is clearly safer.",
            f"Do not exceed theta spend of {theta_budget:.2f}; prefer negative theta_cost values when possible.",
        ]

        if "delta" in target or abs(delta) >= 80:
            if delta > 0:
                guidance.append("Portfolio delta is too long; prefer trades with negative projected_delta_change, such as short calls or long puts.")
            else:
                guidance.append("Portfolio delta is too short; prefer trades with positive projected_delta_change, such as short puts or long calls.")
        if "gamma" in target or abs(gamma) >= 0.1:
            guidance.append("If gamma risk is elevated, avoid adding near-dated short gamma unless the structure is clearly defined-risk and liquid.")
        if "vega" in target or abs(vega) >= 2500:
            if vega < 0:
                guidance.append("Portfolio is short vega; avoid blindly selling more premium. Favor trades that reduce short-vol concentration or add some long optionality.")
            else:
                guidance.append("Portfolio is long vega; prefer short-premium or lower-vega structures if spreads are tight.")
        if theta < 0:
            guidance.append("Current theta is negative, so avoid paying theta unless it clearly resolves the primary breach.")

        return "\n".join(f"- {line}" for line in guidance)

    @staticmethod
    def _rank_and_filter_suggestions(
        suggestions: list[AITradeSuggestion],
        *,
        breach: Optional[RiskBreach],
        theta_budget: float,
        active_expiry: str,
        underlying: str,
    ) -> list[AITradeSuggestion]:
        if not suggestions:
            return []

        target = str(getattr(breach, "breach_type", "") or "").lower()
        preferred_symbols = {underlying.upper(), "ES", "MES"}
        expiry_compact = active_expiry.replace("-", "") if active_expiry else ""
        ranked: list[tuple[float, AITradeSuggestion]] = []

        for suggestion in suggestions:
            if not suggestion.legs:
                continue
            if any((leg.symbol or "").upper() not in preferred_symbols for leg in suggestion.legs):
                continue
            if theta_budget > 0 and suggestion.projected_theta_cost > (theta_budget * 1.25):
                continue

            score = 0.0
            if expiry_compact and all(
                leg.expiration and leg.expiration.strftime("%Y%m%d") == expiry_compact
                for leg in suggestion.legs
            ):
                score += 3.0
            if any((leg.symbol or "").upper() == underlying.upper() for leg in suggestion.legs):
                score += 1.5
            if suggestion.projected_theta_cost <= theta_budget:
                score += 1.0
            if suggestion.projected_theta_cost <= 0:
                score += 1.0
            if "delta" in target:
                if breach and float(getattr(breach, "actual_value", 0.0) or 0.0) > float(getattr(breach, "threshold_value", 0.0) or 0.0):
                    if suggestion.projected_delta_change < 0:
                        score += 4.0
                elif suggestion.projected_delta_change > 0:
                    score += 4.0
            rationale = (suggestion.rationale or "").lower()
            if "spread" in rationale or "liquid" in rationale or "execution" in rationale:
                score += 0.5
            ranked.append((score, suggestion))

        ranked.sort(key=lambda item: item[0], reverse=True)
        return [suggestion for _score, suggestion in ranked[:3]]

    @staticmethod
    def _fallback_suggestions(
        *,
        portfolio_greeks: PortfolioGreeks,
        breach: Optional[RiskBreach],
        theta_budget: float,
        active_expiry: str = "",
        underlying: str = "MES",
    ) -> list[AITradeSuggestion]:
        target = (breach.breach_type.lower() if breach and breach.breach_type else "")
        spx_delta = float(portfolio_greeks.spx_delta or 0.0)
        vega = float(portfolio_greeks.vega or 0.0)
        theta = float(portfolio_greeks.theta or 0.0)
        max_theta_spend = max(1.0, float(theta_budget or 0.0))

        # Parse active_expiry to a date for OrderLeg.expiration
        expiration: Optional[_date] = None
        exp_str = active_expiry.replace("-", "").strip()
        if len(exp_str) == 8:
            try:
                expiration = _date(int(exp_str[:4]), int(exp_str[4:6]), int(exp_str[6:8]))
            except (ValueError, OverflowError):
                pass

        # Use ES for large moves, MES for small adjustments
        sym = underlying if underlying in ("ES", "MES") else "MES"

        if "delta" in target or abs(spx_delta) > 80:
            action = OrderAction.SELL if spx_delta > 0 else OrderAction.BUY
            right = OptionRight.CALL if spx_delta > 0 else OptionRight.PUT
            # Estimate an ATM-ish strike from delta (rough heuristic ~5850 for MES)
            atm = 5850.0
            rationale = (
                f"Portfolio SPX delta {spx_delta:+.1f} exceeds target; sell a {sym} "
                f"{right.value} to reduce directional exposure (approx. strike {atm:.0f})."
            )
            projected_delta_change = -20.0 if spx_delta > 0 else 20.0
            projected_theta_cost = min(max_theta_spend, 20.0)
            base_leg = OrderLeg(
                symbol=sym, action=action, quantity=1,
                option_right=right, strike=atm, expiration=expiration,
            )
        elif "vega" in target or abs(vega) > 5000:
            action = OrderAction.BUY if vega < 0 else OrderAction.SELL
            right = OptionRight.CALL if vega > 0 else OptionRight.PUT
            atm = 5850.0
            rationale = (
                f"Portfolio vega {vega:+.0f} is elevated; adjust {sym} options exposure "
                f"to pull vega toward neutral with controlled theta spend."
            )
            projected_delta_change = -8.0 if vega > 0 else 8.0
            projected_theta_cost = min(max_theta_spend, 35.0)
            base_leg = OrderLeg(
                symbol=sym, action=action, quantity=1,
                option_right=right, strike=atm, expiration=expiration,
            )
        else:
            action = OrderAction.SELL if theta < 0 else OrderAction.BUY
            right = OptionRight.PUT
            atm = 5800.0
            rationale = (
                f"Conservative {sym} put adjustment to improve risk balance; "
                f"current theta {theta:+.1f}."
            )
            projected_delta_change = 0.0
            projected_theta_cost = min(max_theta_spend, 15.0)
            base_leg = OrderLeg(
                symbol=sym, action=action, quantity=1,
                option_right=right, strike=atm, expiration=expiration,
            )

        base = AITradeSuggestion(
            legs=[base_leg],
            projected_delta_change=projected_delta_change,
            projected_theta_cost=projected_theta_cost,
            rationale=rationale,
        )
        return [base]



if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from database.local_store import LocalStore

    db = LocalStore()
    auditor = LLMRiskAuditor(db=db)

    async def _main() -> None:
        print("Running on-demand risk audit (demo context)…")
        result = await auditor.audit_now(
            summary={
                "total_spx_delta": -12.5,
                "total_gamma": -0.003,
                "total_theta": 450.0,
                "total_vega": -8500.0,
                "theta_vega_ratio": -0.053,
                "theta_vega_zone": "good",
                "position_count": 7,
            },
            regime_name="low_vol",
            vix=18.5,
            term_structure=1.08,
            nlv=150_000.0,
            violations=[],
        )
        import json
        print(json.dumps(result, indent=2))

    asyncio.run(_main())