"""
scripts/seed_market_intel.py — Seed market_intel and signals tables with realistic data.
Run once to populate the DB so the dashboard panels show content.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env from project root
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass

from database.db_manager import DBManager

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("seed")

INTEL_ROWS = [
    ("AAPL", "manual_seed", "Apple reports strong Q1 iPhone sales; AI features driving upgrades. Analyst consensus remains bullish.", 0.72),
    ("SPY",  "manual_seed", "S&P 500 near all-time highs. Fed signals potential rate cuts mid-year; soft landing scenario gaining credibility.", 0.55),
    ("QQQ",  "manual_seed", "NASDAQ tech rally continues, led by AI infrastructure names. Some concern about stretched valuations at current levels.", 0.48),
    ("TSLA", "manual_seed", "Tesla faces margin pressure from EV price wars; FSD progress encouraging but regulatory hurdles remain.", -0.15),
    ("NVDA", "manual_seed", "NVIDIA demand for AI accelerators remains insatiable. Data center revenue guidance raised again; supply constraints easing.", 0.88),
    ("HPQ",  "manual_seed", "HP Inc. PC refresh cycle improving with AI PC demand. Commercial segment outperforming consumer division.", 0.31),
    ("DLR",  "manual_seed", "Digital Realty benefits from data center AI buildout; strong leasing activity in core markets. REIT outlook positive.", 0.45),
    ("ITOT", "manual_seed", "Broad US total market ETF. Diversified exposure with moderate bullish momentum from mega-cap tech leadership.", 0.38),
    ("AMSO", "manual_seed", "Small-cap position showing mixed signals. Sector rotation concerns with rising rates environment.", -0.05),
    ("BRD",  "manual_seed", "Fintech-adjacent holding. Digital payment volumes recovering; regulatory clarity improving for sector.", 0.22),
]

SIGNAL_ROWS = [
    {
        "signal_type": "put_call_parity",
        "legs_json": {
            "underlying": "SPX", "expiry": "2026-03-21", "strike": 5800.0,
            "direction": "long_call", "mispricing": 2.45, "call": 48.2, "put": 44.1, "threshold": 1.0,
        },
        "net_value": 2.45,
        "confidence": 0.73,
    },
    {
        "signal_type": "box_spread",
        "legs_json": {
            "underlying": "SPX", "expiry": "2026-03-21", "strikes": [5800.0, 5900.0],
            "net_credit": 101.3, "fair_value": 100.0, "legs": ["5800C/5900C", "5900P/5800P"],
        },
        "net_value": 1.3,
        "confidence": 0.61,
    },
    {
        "signal_type": "put_call_parity",
        "legs_json": {
            "underlying": "AAPL", "expiry": "2026-04-17", "strike": 220.0,
            "direction": "long_put", "mispricing": 1.12, "call": 8.5, "put": 6.9, "threshold": 1.0,
        },
        "net_value": 1.12,
        "confidence": 0.55,
    },
]


async def main() -> None:
    db = await DBManager.get_instance()

    # Seed market_intel
    for sym, src, content, score in INTEL_ROWS:
        await db.insert_market_intel(symbol=sym, source=src, content=content, sentiment_score=score)

    rows = await db.get_recent_market_intel(limit=20)
    logger.info("market_intel: %d rows written", len(rows))
    for r in rows:
        logger.info("  %-8s %+.2f  %s", r["symbol"], r["sentiment_score"] or 0.0, (r["content"] or "")[:60])

    # Seed signals
    for sig in SIGNAL_ROWS:
        await db.insert_signal(
            signal_type=sig["signal_type"],
            legs_json=sig["legs_json"],
            net_value=sig["net_value"],
            confidence=sig["confidence"],
        )

    sigs = await db.get_active_signals(limit=10)
    logger.info("\nsignals: %d rows written", len(sigs))
    for s in sigs:
        legs = s.get("legs_json") or {}
        if isinstance(legs, str):
            import json as _j; legs = _j.loads(legs)
        logger.info(
            "  %-20s conf=%.2f  %s/%s",
            s["signal_type"], s["confidence"] or 0,
            legs.get("underlying", "?"), legs.get("expiry", "?"),
        )

    await db.close()
    logger.info("\nDone — dashboard panels should now show data.")


if __name__ == "__main__":
    asyncio.run(main())
