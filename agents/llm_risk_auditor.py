"""
agents/llm_risk_auditor.py — LLM-powered live portfolio risk audit.

Uses GitHub Copilot SDK (gpt-4.1 by default — FREE via subscription) to
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
  LLM_MODEL                    : model name (default "gpt-4.1")
  RISK_AUDIT_INTERVAL_SECONDS  : cadence in seconds (default 300 = 5 min)
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from agents.llm_client import async_llm_chat
from models.order import (
    AITradeSuggestion,
    OrderAction,
    OrderLeg,
    PortfolioGreeks,
    RiskBreach,
)

logger = logging.getLogger(__name__)

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
You are a quantitative options risk analyst specializing in income strategies \
(SPX/ES iron condors, strangles, and spreads).

A portfolio risk breach has been detected. Your job is to suggest exactly 3 \
concrete remediation trades that would reduce the breach while preserving \
theta income within the given budget.

Rules:
- Respond ONLY with a valid JSON array of exactly 3 objects. No markdown fences, \
no extra text.
- Each object must have these fields:
    "legs": array of {symbol, action ("BUY"|"SELL"), quantity (positive int)}
    "projected_delta_change": float (negative = delta reduced)
    "projected_theta_cost": float (negative = theta earned; positive = theta spent)
    "rationale": string (1-2 sentences, cite specific Greek values)
- If action cannot be determined, omit the suggestion (return fewer objects).
- Never raise exceptions — always return valid JSON.
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
        interval_seconds: int = 300,
    ) -> None:
        self.db = db
        self.interval_seconds = interval_seconds
        self._scheduler: AsyncIOScheduler | None = None
        self._model = os.getenv("LLM_MODEL", "gpt-4.1")
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
    ) -> list[AITradeSuggestion]:
        """Return up to 3 AI-generated trades to remediate a risk breach.

        Args:
            portfolio_greeks: Current aggregate portfolio Greeks.
            vix:              Current VIX value.
            regime:           Active market regime name.
            breach:           The detected breach (or None for general audit).
            theta_budget:     Maximum theta that can be spent/risked.

        Returns:
            List of ``AITradeSuggestion`` (0–3 elements).  Never raises.
        """
        try:
            prompt = self._build_suggest_prompt(
                portfolio_greeks=portfolio_greeks,
                vix=vix,
                regime=regime,
                breach=breach,
                theta_budget=theta_budget,
            )
            raw = await async_llm_chat(
                prompt,
                model=self._model,
                system=_SUGGEST_SYSTEM_PROMPT,
                timeout=45.0,
            )
            return self._parse_suggestions(raw)
        except Exception as exc:
            logger.warning("suggest_trades() error: %s", exc, exc_info=True)
            return []

    # --- private helpers for suggest_trades ---

    @staticmethod
    def _build_suggest_prompt(
        *,
        portfolio_greeks: PortfolioGreeks,
        vix: float,
        regime: str,
        breach: Optional[RiskBreach],
        theta_budget: float,
    ) -> str:
        g = portfolio_greeks
        breach_section = (
            f"Breach Type:    {breach.breach_type}\n"
            f"Threshold:      {breach.threshold_value}\n"
            f"Actual Value:   {breach.actual_value}\n"
            f"Breach Regime:  {breach.regime}\n"
            f"Breach VIX:     {breach.vix}\n"
        ) if breach else "No specific breach — general portfolio optimisation requested.\n"

        return (
            f"## Portfolio Greeks\n"
            f"SPX Beta-Delta:   {g.spx_delta:.2f}\n"
            f"Gamma:            {g.gamma:.4f}\n"
            f"Daily Theta ($/d):{g.theta:.2f}\n"
            f"Vega ($):         {g.vega:.2f}\n\n"
            f"## Market Context\n"
            f"VIX:              {vix:.2f}\n"
            f"Regime:           {regime}\n\n"
            f"## Risk Breach\n"
            f"{breach_section}\n"
            f"## Constraints\n"
            f"Theta Budget (max to spend): {theta_budget:.2f}\n\n"
            f"Respond ONLY with a JSON array of exactly 3 suggestion objects "
            f"as described in the system prompt."
        )

    @staticmethod
    def _parse_suggestions(raw: str) -> list[AITradeSuggestion]:
        """Parse LLM JSON → list[AITradeSuggestion].  Returns [] on any error."""
        text = raw.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(l for l in lines if not l.startswith("```")).strip()
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
                    legs.append(
                        OrderLeg(
                            symbol=str(lr.get("symbol", "UNKNOWN")),
                            action=action,
                            quantity=int(lr.get("quantity", 1)),
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