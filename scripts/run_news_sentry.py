"""
scripts/run_news_sentry.py — One-shot runner to populate market_intel via NewsSentry.

Usage:
  python scripts/run_news_sentry.py [SYMBOL ...]

If no symbols are provided, uses a default watchlist.
Requires: NEWS_API_KEY, NEWS_API_SECRET (for Alpaca), and GitHub Copilot CLI auth.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import os

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env from project root
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass

from database.db_manager import DBManager
from agents.news_sentry import NewsSentry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("run_news_sentry")

DEFAULT_SYMBOLS = ["AAPL", "SPY", "QQQ", "TSLA", "NVDA", "HPQ", "DLR", "ITOT"]


async def main(symbols: list[str]) -> None:
    logger.info("Connecting to database...")
    db = await DBManager.get_instance()

    sentry = NewsSentry(symbols=symbols, db=db, interval_seconds=900)
    logger.info("Running NewsSentry for %d symbols: %s", len(symbols), symbols)

    for sym in symbols:
        logger.info("  → Fetching + scoring sentiment for %s", sym)
        await sentry.fetch_and_score(sym)
        logger.info("  ✓ Done: %s", sym)

    # Show what was written
    rows = await db.get_recent_market_intel(limit=len(symbols) * 2)
    logger.info("\nStored %d market_intel rows:", len(rows))
    for row in rows:
        logger.info(
            "  [%s] %s | score=%.2f | %s",
            row.get("created_at", "?"),
            row.get("symbol", "?"),
            row.get("sentiment_score") or 0.0,
            (row.get("content") or "")[:80],
        )

    await db.close()
    logger.info("Done.")


if __name__ == "__main__":
    symbols = sys.argv[1:] or DEFAULT_SYMBOLS
    asyncio.run(main(symbols))
