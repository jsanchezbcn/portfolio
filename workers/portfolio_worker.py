"""portfolio_worker.py — background job executor for the portfolio dashboard.

Polls the PostgreSQL ``worker_jobs`` table for pending jobs and executes them
in the worker process so that the Streamlit UI never blocks on heavy I/O.

Supported job types
-------------------
fetch_greeks    payload: {"account_id": str, "ibkr_only": bool}
                result: {"positions": [...], "summary": {...}, "spx_price": float}

llm_brief       payload: {"vix": float, "vix3m": float, "term_structure": float,
                           "regime_name": str, "recession_probability": float|null,
                           "portfolio_summary": dict|null, "nlv": float|null}
                result: the dict returned by LLMMarketBrief.brief_now()

llm_audit       payload: {"summary": dict, "regime_name": str, "vix": float,
                           "term_structure": float, "nlv": float|null,
                           "violations": list, "resolved_limits": dict|null}
                result: the dict returned by LLMRiskAuditor.audit_now()

restart_gateway payload: {}
                result: {"success": bool}

Usage
-----
    python workers/portfolio_worker.py --worker-id worker-1
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Make sure project root is on sys.path when run directly
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from database.db_manager import DBManager

LOGGER = logging.getLogger("portfolio_worker")

# How long to sleep between polling cycles when no jobs are pending
_POLL_INTERVAL = 2.0
# How often (in poll cycles) to run job cleanup
_CLEANUP_EVERY = 300  # ~10 minutes at 2s intervals

# ---------------------------------------------------------------------------
# JSON-safe serializer for dataclass objects (positions)
# ---------------------------------------------------------------------------

def _json_default(obj: Any) -> Any:
    """Custom encoder helper for types json can't handle natively."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    # Handle enums (e.g. InstrumentType)
    if hasattr(obj, "value"):
        return obj.value
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def _positions_to_dicts(positions: list) -> list[dict]:
    """Serialize a list of UnifiedPosition dataclasses to plain dicts."""
    import json

    result = []
    for pos in positions:
        d = dataclasses.asdict(pos)
        # Re-serialize through json to apply our default handler
        d = json.loads(json.dumps(d, default=_json_default))
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Job handlers
# ---------------------------------------------------------------------------

async def _handle_fetch_greeks(payload: dict[str, Any]) -> dict[str, Any]:
    """Fetch positions + greeks for the given account."""
    from adapters.ibkr_adapter import IBKRAdapter
    from agent_tools.portfolio_tools import PortfolioTools

    account_id: str = payload["account_id"]
    ibkr_only: bool = payload.get("ibkr_only", True)

    adapter = IBKRAdapter()
    if ibkr_only:
        adapter.disable_tasty_cache = True
        adapter.force_refresh_on_miss = False

    try:
        positions = await adapter.fetch_positions(account_id)
    except Exception as exc:
        raise RuntimeError(f"fetch_positions failed: {exc}") from exc

    if positions:
        positions = await adapter.fetch_greeks(positions)

    tools = PortfolioTools()
    summary = tools.get_portfolio_summary(positions)
    greeks_status = getattr(adapter, "last_greeks_status", {})
    spx_price = float(greeks_status.get("spx_price") or 0.0)

    return {
        "positions": _positions_to_dicts(positions),
        "summary": summary,
        "spx_price": spx_price,
    }


async def _handle_llm_brief(payload: dict[str, Any]) -> dict[str, Any]:
    """Generate an LLM market brief and persist it to market_intel."""
    from agents.llm_market_brief import LLMMarketBrief

    db = await DBManager.get_instance()
    agent = LLMMarketBrief(db=db)

    result = await agent.brief_now(
        vix=float(payload.get("vix", 20.0)),
        vix3m=float(payload.get("vix3m", 21.0)),
        term_structure=float(payload.get("term_structure", 1.05)),
        regime_name=str(payload.get("regime_name", "unknown")),
        recession_probability=payload.get("recession_probability"),
        portfolio_summary=payload.get("portfolio_summary"),
        nlv=payload.get("nlv"),
    )
    return result or {}


async def _handle_llm_audit(payload: dict[str, Any]) -> dict[str, Any]:
    """Run an LLM risk audit and persist it to market_intel."""
    from agents.llm_risk_auditor import LLMRiskAuditor
    from risk_engine.regime_detector import RegimeDetector

    db = await DBManager.get_instance()
    regime_detector = RegimeDetector()
    agent = LLMRiskAuditor(db=db, regime_detector=regime_detector)

    result = await agent.audit_now(
        summary=payload.get("summary", {}),
        regime_name=str(payload.get("regime_name", "unknown")),
        vix=float(payload.get("vix", 20.0)),
        term_structure=float(payload.get("term_structure", 1.05)),
        nlv=payload.get("nlv"),
        violations=payload.get("violations", []),
        resolved_limits=payload.get("resolved_limits"),
    )
    return result or {}


async def _handle_restart_gateway(_payload: dict[str, Any]) -> dict[str, Any]:
    """Restart the IBKR gateway process."""
    from adapters.ibkr_adapter import IBKRAdapter

    adapter = IBKRAdapter()
    try:
        ok = await asyncio.to_thread(adapter.client.restart_gateway)
    except Exception as exc:
        LOGGER.error("restart_gateway exception: %s", exc)
        ok = False
    return {"success": bool(ok)}


_JOB_HANDLERS = {
    "fetch_greeks": _handle_fetch_greeks,
    "llm_brief": _handle_llm_brief,
    "llm_audit": _handle_llm_audit,
    "restart_gateway": _handle_restart_gateway,
}


# ---------------------------------------------------------------------------
# Main worker loop
# ---------------------------------------------------------------------------

async def run_worker(worker_id: str) -> None:
    """Poll for pending jobs and execute them until interrupted."""
    db = await DBManager.get_instance()
    LOGGER.info("[%s] Worker started — polling every %.1fs", worker_id, _POLL_INTERVAL)

    cycle = 0
    while True:
        try:
            job = await db.claim_next_job(worker_id)
        except Exception as exc:
            LOGGER.warning("[%s] claim_next_job error: %s", worker_id, exc)
            await asyncio.sleep(_POLL_INTERVAL)
            continue

        if job is None:
            await asyncio.sleep(_POLL_INTERVAL)
        else:
            job_id: str = job["id"]
            job_type: str = job["job_type"]
            payload: dict = job.get("payload") or {}
            LOGGER.info("[%s] Claimed job %s type=%s", worker_id, job_id, job_type)

            handler = _JOB_HANDLERS.get(job_type)
            if handler is None:
                err = f"Unknown job type: {job_type!r}"
                LOGGER.warning("[%s] %s", worker_id, err)
                await db.fail_job(job_id, err)
            else:
                try:
                    result = await handler(payload)
                    await db.complete_job(job_id, result)
                    LOGGER.info("[%s] Completed job %s type=%s", worker_id, job_id, job_type)
                except Exception as exc:
                    error_msg = f"{type(exc).__name__}: {exc}"
                    LOGGER.error("[%s] Job %s failed: %s", worker_id, job_id, error_msg)
                    await db.fail_job(job_id, error_msg)

        cycle += 1
        if cycle % _CLEANUP_EVERY == 0:
            try:
                deleted = await db.cleanup_old_jobs(max_age_hours=24)
                if deleted:
                    LOGGER.info("[%s] Cleaned up %d old jobs", worker_id, deleted)
            except Exception as exc:
                LOGGER.debug("[%s] cleanup error: %s", worker_id, exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Portfolio background worker")
    parser.add_argument(
        "--worker-id",
        default=f"worker-{os.getpid()}",
        help="Unique identifier for this worker instance",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        asyncio.run(run_worker(args.worker_id))
    except KeyboardInterrupt:
        LOGGER.info("[%s] Worker stopped by user.", args.worker_id)


if __name__ == "__main__":
    main()
