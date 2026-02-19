"""
agents/llm_market_brief.py — LLM-generated market brief for options traders.

Uses GitHub Copilot SDK (gpt-4.1-mini by default — FREE via subscription)
to produce a short, opinionated market brief each hour (or on demand).

The brief is calibrated for a short-volatility / premium-collection trader
and covers:
  - Current regime and what it means for strategy selection
  - VIX term structure interpretation
  - Key opportunities or risks to watch
  - One concrete actionable idea

Output is persisted to ``market_intel`` (symbol="MARKET", source="llm_brief")
so the dashboard can display it without polling.

Environment variables:
  LLM_FAST_MODEL                : model name (default "gpt-4.1-mini")
  MARKET_BRIEF_INTERVAL_SECONDS : cadence in seconds (default 3600 = 1 hr)
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from agents.llm_client import async_llm_chat

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a concise, candid market strategist who specialises in SPX/VIX
options and premium-selling strategies (iron condors, strangles, jade lizards,
calendar spreads, backspreads). You trade at the portfolio level, not
individual stocks.

Write a brief for the trader's morning/mid-session check-in. Be opinionated
and specific. Do not hedge every sentence. If conditions favour premium
collection, say so. If they don't, say so clearly.

Output ONLY a JSON object (no markdown fences) with these fields:
  "headline"    : one sentence capturing the key takeaway
  "regime_read" : 1-2 sentences on what the current regime + VIX means today
  "opportunity" : 1-2 sentences on the best positioning opportunity right now
  "risk"        : 1 sentence on the main risk to watch
  "action"      : one concrete trade idea (e.g., "Consider adding a Feb SPX
                  5500/5450 put spread to hedge delta while collecting premium")
  "confidence"  : "high" | "medium" | "low"
"""


class LLMMarketBrief:
    """Generates periodic market briefs using an LLM.

    Args:
        db: Initialised DBManager instance.
        interval_seconds: How often to run, in seconds (default 3600).
    """

    def __init__(
        self,
        *,
        db: Any,
        interval_seconds: int = 3600,
    ) -> None:
        self.db = db
        self.interval_seconds = interval_seconds
        self._scheduler: AsyncIOScheduler | None = None
        self._model = os.getenv("LLM_FAST_MODEL", "gpt-4.1-mini")
        self._latest_market_data: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_market_data(
        self,
        *,
        vix: float,
        vix3m: float,
        term_structure: float,
        regime_name: str,
        recession_probability: float | None,
        portfolio_summary: dict[str, Any] | None = None,
        nlv: float | None = None,
    ) -> None:
        """Push fresh market data to be used in the next brief cycle.

        Synchronous — safe to call from any thread (e.g. dashboard loop).
        """
        self._latest_market_data = {
            "vix": round(vix, 2),
            "vix3m": round(vix3m, 2) if vix3m else None,
            "term_structure": round(term_structure, 4),
            "regime_name": regime_name,
            "recession_probability": recession_probability,
            "portfolio_summary": portfolio_summary,
            "nlv": round(nlv, 0) if nlv else None,
        }

    # ------------------------------------------------------------------
    # Scheduler lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._scheduler = AsyncIOScheduler()
        self._scheduler.add_job(
            self._run_brief,
            "interval",
            seconds=self.interval_seconds,
            id="llm_market_brief",
            replace_existing=True,
            max_instances=1,
        )
        self._scheduler.start()
        logger.info(
            "LLMMarketBrief started (model=%s interval=%ds)",
            self._model,
            self.interval_seconds,
        )

    async def stop(self) -> None:
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Core generation logic
    # ------------------------------------------------------------------

    async def _run_brief(self) -> None:
        data = self._latest_market_data
        if data is None:
            logger.debug("LLMMarketBrief: no market data yet, skipping")
            return
        try:
            result = await self._generate(data)
            await self._persist(data, result)
        except Exception as exc:
            logger.warning("LLMMarketBrief error: %s", exc, exc_info=True)

    async def _generate(self, data: dict[str, Any]) -> dict[str, Any]:
        prompt = self._build_prompt(data)
        raw = await async_llm_chat(prompt, model=self._model, timeout=60.0)
        return self._parse_response(raw)

    def _build_prompt(self, data: dict[str, Any]) -> str:
        recession_str = (
            f"{data['recession_probability']:.1%}"
            if data.get("recession_probability") is not None
            else "unknown"
        )

        ts_description = (
            "contango (normal)" if data["term_structure"] > 1.0
            else "backwardation (stressed)" if data["term_structure"] < 1.0
            else "flat"
        )

        portfolio_section = ""
        if data.get("portfolio_summary"):
            s = data["portfolio_summary"]
            nlv_str = f"${data['nlv']:,.0f}" if data.get("nlv") else "unknown"
            portfolio_section = f"""
## Current Portfolio Snapshot
NLV:            {nlv_str}
SPX Delta:      {s.get('total_spx_delta', 0):.1f}
Theta ($/day):  {s.get('total_theta', 0):.2f}
Vega:           {s.get('total_vega', 0):.2f}
Theta/Vega:     {s.get('theta_vega_ratio', 0):.3f} ({s.get('theta_vega_zone', '?')})
Positions:      {s.get('position_count', 0)}
"""

        return f"""{_SYSTEM_PROMPT}

## Market Data
VIX:                  {data['vix']}
VIX3M:                {data.get('vix3m', 'n/a')}
Term Structure (V/V3): {data['term_structure']} ({ts_description})
Regime:               {data['regime_name'].replace('_', ' ').title()}
Recession Probability: {recession_str} (from Polymarket prediction markets)
{portfolio_section}
Generate the market brief now. Return ONLY the JSON object.
"""

    @staticmethod
    def _parse_response(raw: str) -> dict[str, Any]:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(l for l in lines if not l.startswith("```")).strip()
        try:
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise ValueError("Not a dict")
            return {
                "headline": str(parsed.get("headline", "Market brief generated.")),
                "regime_read": str(parsed.get("regime_read", "")),
                "opportunity": str(parsed.get("opportunity", "")),
                "risk": str(parsed.get("risk", "")),
                "action": str(parsed.get("action", "")),
                "confidence": str(parsed.get("confidence", "medium")),
            }
        except Exception:
            return {
                "headline": "Market brief (raw)",
                "regime_read": raw[:300] if raw else "",
                "opportunity": "",
                "risk": "",
                "action": "",
                "confidence": "low",
            }

    async def _persist(self, data: dict[str, Any], result: dict[str, Any]) -> None:
        content = {**result, "vix": data["vix"], "regime": data["regime_name"]}
        try:
            await self.db.upsert_market_intel(
                symbol="MARKET",
                source="llm_brief",
                sentiment_score=None,
                summary=result["headline"],
                raw_data=content,
            )
            logger.info(
                "LLM market brief stored — regime=%s confidence=%s",
                data["regime_name"],
                result["confidence"],
            )
        except Exception as exc:
            logger.warning("LLMMarketBrief persist error: %s", exc)

    # ------------------------------------------------------------------
    # On-demand brief (for dashboard "Refresh Brief" button)
    # ------------------------------------------------------------------

    async def brief_now(
        self,
        *,
        vix: float,
        vix3m: float,
        term_structure: float,
        regime_name: str,
        recession_probability: float | None,
        portfolio_summary: dict[str, Any] | None = None,
        nlv: float | None = None,
    ) -> dict[str, Any]:
        """Run an immediate brief and return the result dict."""
        data = {
            "vix": round(vix, 2),
            "vix3m": round(vix3m, 2) if vix3m else None,
            "term_structure": round(term_structure, 4),
            "regime_name": regime_name,
            "recession_probability": recession_probability,
            "portfolio_summary": portfolio_summary,
            "nlv": round(nlv, 0) if nlv else None,
        }
        result = await self._generate(data)
        await self._persist(data, result)
        return result


# ------------------------------------------------------------------ #
# Entry point: python -m agents.llm_market_brief                     #
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from database.local_store import LocalStore

    db = LocalStore()
    agent = LLMMarketBrief(db=db)

    async def _main() -> None:
        print("Generating market brief (on-demand)…")
        result = await agent.brief_now(
            vix=18.5,
            vix3m=20.0,
            term_structure=20.0 / 18.5,
            regime_name="low_vol",
            recession_probability=0.15,
            portfolio_summary=None,
            nlv=None,
        )
        import json
        print(json.dumps(result, indent=2))

    asyncio.run(_main())