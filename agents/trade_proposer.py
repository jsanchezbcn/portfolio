"""
agents/trade_proposer.py
─────────────────────────
Feature 006: Trade Proposer — async monitoring loop.

Responsibilities:
    1. Every PROPOSER_INTERVAL seconds (default 300): fetch live portfolio
       Greeks from IBKRAdapter, run BreachDetector, generate candidates via
       ProposerEngine, persist top-3 to proposed_trades, and fire Option C
       notification when efficiency_score > threshold or regime == crisis_mode.
    2. --run-once flag: execute exactly one cycle and exit (CI testing).
    3. MOCK_BREACH=TRUE: skip live IBKR fetch; inject a synthetic
       neutral_volatility vega breach so the loop runs without gateway.

Usage::

    # Normal monitoring mode (runs forever)
    python -m agents.trade_proposer

    # Single-cycle CI mode (no gateway needed)
    MOCK_BREACH=TRUE python -m agents.trade_proposer --run-once

Environment variables:
    PROPOSER_INTERVAL           cycle interval in seconds (default 300)
    PROPOSER_NOTIFY_THRESHOLD   efficiency score cutoff for notifications (0.5)
    MOCK_BREACH                 TRUE → inject synthetic breach, skip live fetch
    IBKR_ACCOUNT_ID             override account ID (optional)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from urllib.parse import quote_plus
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Auto-load .env so PROPOSER_DB_URL and DB_* vars are available when run directly
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(PROJECT_ROOT / ".env")
except Exception:
    pass

from agents.proposer_engine import (
    BreachDetector,
    BreachEvent,
    ProposerEngine,
    RiskRegimeLoader,
)
from agent_tools.notification_dispatcher import NotificationDispatcher

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

PROPOSER_INTERVAL    = int(os.getenv("PROPOSER_INTERVAL",           "300"))
NOTIFY_THRESHOLD     = float(os.getenv("PROPOSER_NOTIFY_THRESHOLD", "0.5"))
MOCK_BREACH          = os.getenv("MOCK_BREACH", "").upper() in ("TRUE", "1", "YES")


# ── Synthetic breach for MOCK_BREACH mode ────────────────────────────────────

def _make_mock_greeks(account_id: str) -> dict[str, Any]:
    """Return a synthetic Greeks snapshot that triggers a vega breach in neutral_vol."""
    return {
        "vix":            18.0,
        "term_structure":  1.0,
        "recession_prob":  0.0,
        # $100k NLV → neutral_vol vega limit = -4800; inject -8000 (hard breach)
        "total_vega":  -8000.0,
        "spx_delta":      400.0,   # within limit
        "total_theta":    150.0,   # within limit
        "total_gamma":     60.0,   # within limit
        "account_id":   account_id,
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_adapter() -> Optional[Any]:
    """Return IBKRAdapter if available; None when MOCK_BREACH is set."""
    if MOCK_BREACH:
        return None
    try:
        from adapters.ibkr_adapter import IBKRAdapter
        return IBKRAdapter()
    except Exception as exc:
        logger.warning("IBKRAdapter unavailable (%s) — engine will run without margin simulation", exc)
        return None


def _get_session() -> Optional[Any]:
    """Return a live SQLModel/SQLAlchemy session, or None when DB unavailable."""
    db_url = os.getenv("PROPOSER_DB_URL", "")
    if not db_url:
        host = (os.getenv("DB_HOST") or "").split("#")[0].strip()
        port = (os.getenv("DB_PORT") or "5432").split("#")[0].strip()
        name = (os.getenv("DB_NAME") or "").split("#")[0].strip()
        user = (os.getenv("DB_USER") or "").split("#")[0].strip()
        pwd = (os.getenv("DB_PASS") or "").split("#")[0].strip()
        if host and name and user:
            auth = f"{quote_plus(user)}:{quote_plus(pwd)}" if pwd else quote_plus(user)
            db_url = f"postgresql+psycopg2://{auth}@{host}:{port}/{name}"
    if not db_url:
        logger.info("PROPOSER_DB_URL not set — proposed_trades persistence skipped")
        return None
    try:
        from sqlmodel import SQLModel, Session, create_engine
        from models.proposed_trade import ProposedTrade
        engine = create_engine(db_url)
        SQLModel.metadata.create_all(engine, tables=[ProposedTrade.__table__])
        return Session(engine)
    except Exception as exc:
        logger.warning("DB unavailable (%s) — proposed_trades persistence skipped", exc)
        return None


async def _fetch_greeks(adapter: Any, account_id: str) -> tuple[dict[str, Any], float]:
    """Fetch live Greeks snapshot and NLV from the IBKR adapter.

    Returns (greeks_snapshot, nlv).
    """
    try:
        # IBKRAdapter.get_portfolio_greeks() returns a dict with greeks
        greeks = await adapter.get_portfolio_greeks(account_id)
        nlv = float(greeks.get("nlv", 0.0))
        return greeks, nlv
    except Exception as exc:
        logger.error("Failed to fetch Greeks from IBKR: %s", exc)
        return {}, 0.0


async def _send_option_c_notification(
    dispatcher: NotificationDispatcher,
    candidates: list[Any],
    regime: str,
) -> None:
    """Fire Option C Telegram notification if threshold is exceeded."""
    top_score = max((c.efficiency_score for c in candidates), default=0.0)
    trigger_score = top_score > NOTIFY_THRESHOLD
    trigger_crisis = regime == "crisis_mode"

    if not (trigger_score or trigger_crisis):
        return

    reason = []
    if trigger_crisis:
        reason.append("⚠️ Crisis mode active — immediate hedge required")
    if trigger_score:
        reason.append(f"top efficiency score {top_score:.2f} > threshold {NOTIFY_THRESHOLD}")

    body_lines = [f"• {c.strategy_name} (score={c.efficiency_score:.2f})" for c in candidates[:3]]
    body = "\n".join(body_lines)

    try:
        await dispatcher.send_alert(
            title="🛡️ Trade Proposer: New Hedge Candidates",
            body=f"{', '.join(reason)}.\n\nTop proposals:\n{body}",
            urgency="red" if trigger_crisis else "yellow",
        )
        logger.info("Option C notification sent (regime=%s, top_score=%.2f)", regime, top_score)
    except Exception as exc:
        logger.warning("Notification failed: %s", exc)


# ── Core cycle ────────────────────────────────────────────────────────────────

async def run_cycle(
    *,
    adapter: Any,
    loader: RiskRegimeLoader,
    detector: BreachDetector,
    engine: ProposerEngine,
    dispatcher: NotificationDispatcher,
    account_id: str,
    nlv: float = 100_000.0,
) -> list[Any]:
    """Execute one complete proposer cycle.

    Returns:
        List of CandidateTrade objects persisted (may be empty).
    """
    # 1. Fetch / mock Greeks
    if MOCK_BREACH:
        greeks = _make_mock_greeks(account_id)
        logger.info("[MOCK_BREACH] injecting synthetic vega breach for account %s", account_id)
    else:
        greeks, nlv = await _fetch_greeks(adapter, account_id)
        if not greeks:
            logger.warning("Empty Greeks snapshot — skipping cycle")
            return []

    # 2. Detect breaches
    margin_used = float(greeks.get("margin_used", 0.0))
    breaches = detector.check(
        greeks,
        account_nlv=nlv,
        account_id=account_id,
        margin_used=margin_used,
    )

    if not breaches:
        logger.info("No risk breaches detected for %s", account_id)
        return []

    logger.info("%d breach(es) detected for %s: %s",
                len(breaches), account_id, [str(b) for b in breaches])

    # 3. Generate candidates
    atm_price = float(greeks.get("spx_price", 0.0))
    candidates = await engine.generate(
        breaches,
        account_id=account_id,
        nlv=nlv,
        atm_price=atm_price,
    )

    if not candidates:
        logger.info("No viable candidates generated for %s", account_id)
        return []

    logger.info("Generated %d candidate(s) for %s (top score=%.2f)",
                len(candidates), account_id, candidates[0].efficiency_score)

    # 4. Persist top-3
    session = _get_session()
    if session is not None:
        try:
            engine.persist_top3(account_id, candidates, session)
        finally:
            session.close()
    else:
        logger.info("No DB session — top-3 trades not persisted")

    # 5. Option C notification
    regime = breaches[0].regime if breaches else "neutral_volatility"
    await _send_option_c_notification(dispatcher, candidates, regime)

    return candidates


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    """Async entry point: monitoring loop or single cycle."""
    parser = argparse.ArgumentParser(description="Trade Proposer Agent")
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Execute one cycle and exit (for CI/testing)",
    )
    args, _ = parser.parse_known_args()

    # Setup
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    account_id = os.getenv("IBKR_ACCOUNT_ID", "DU_MOCK")
    adapter    = _get_adapter()
    loader     = RiskRegimeLoader()
    detector   = BreachDetector(loader)
    engine     = ProposerEngine(adapter=adapter, loader=loader)
    dispatcher = NotificationDispatcher()

    logger.info(
        "Trade Proposer starting — interval=%ds, mock=%s, run_once=%s, account=%s",
        PROPOSER_INTERVAL, MOCK_BREACH, args.run_once, account_id,
    )

    if args.run_once:
        candidates = await run_cycle(
            adapter=adapter,
            loader=loader,
            detector=detector,
            engine=engine,
            dispatcher=dispatcher,
            account_id=account_id,
        )
        logger.info("--run-once complete: %d candidates generated", len(candidates))
        return

    # Continuous loop
    while True:
        try:
            await run_cycle(
                adapter=adapter,
                loader=loader,
                detector=detector,
                engine=engine,
                dispatcher=dispatcher,
                account_id=account_id,
            )
        except Exception as exc:
            logger.error("Cycle error: %s", exc, exc_info=True)
        await asyncio.sleep(PROPOSER_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
