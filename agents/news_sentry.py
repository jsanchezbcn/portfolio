"""
agents/news_sentry.py — User Story 2: Automated sentiment score.

NewsSentry fetches financial news headlines for a list of symbols on a
configurable interval (default 15 minutes), scores sentiment via an LLM,
and persists a SentimentRecord to the market_intel table.

Environment variables:
  NEWS_PROVIDER      : "alpaca" (default) | "finnhub" | "newsapi" | "gnews"
  NEWS_API_KEY       : API key for the selected provider
  NEWS_API_SECRET    : Secret key (Alpaca only)
  FINNHUB_API_KEY    : Finnhub key (when NEWS_PROVIDER=finnhub)
  NEWSAPI_KEY        : NewsAPI.org key (when NEWS_PROVIDER=newsapi)
  GNEWS_TOKEN        : GNews token (when NEWS_PROVIDER=gnews)
  LLM_MODEL          : Model name for sentiment scoring (default "gpt-4.1")
  NEWS_INTERVAL_SECONDS : Scheduler interval in seconds (default 900)

Requires GitHub Copilot CLI to be installed and authenticated.

Free-tier notes:
  - Alpaca  : free with paper-trading account, ~200 req/min
  - Finnhub : free tier 60 req/min
  - NewsAPI : free 100 req/day (developer plan)
  - GNews   : free 100 req/day

If multiple providers are configured the sentry tries them in priority order
(controlled by NEWS_PROVIDER) and falls back gracefully on empty results.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from agents.llm_client import async_llm_chat

logger = logging.getLogger(__name__)


class NewsSentry:
    """Fetches news and scores sentiment for a list of symbols.

    Args:
        symbols: Tickers to monitor (e.g. ``["AAPL", "TSLA"]``).
        db:      Initialised DBManager instance.
        interval_seconds: Scheduler interval in seconds (default 900 = 15 min).
    """

    def __init__(
        self,
        *,
        symbols: list[str],
        db: Any,
        interval_seconds: int = 900,
    ) -> None:
        self.symbols = symbols
        self.db = db
        self.interval_seconds = interval_seconds
        self._scheduler: AsyncIOScheduler | None = None
        self._model = os.getenv("LLM_MODEL", "gpt-4.1")
        self._news_provider = os.getenv("NEWS_PROVIDER", "alpaca").lower()
        self._news_api_key = os.getenv("NEWS_API_KEY", "")
        # Provider-specific keys (can supplement NEWS_API_KEY)
        self._finnhub_key = os.getenv("FINNHUB_API_KEY", "") or self._news_api_key
        self._newsapi_key = os.getenv("NEWSAPI_KEY", "") or self._news_api_key
        self._gnews_token = os.getenv("GNEWS_TOKEN", "") or self._news_api_key

    # ------------------------------------------------------------------ #
    # T022 — _fetch_news                                                   #
    # ------------------------------------------------------------------ #

    async def _fetch_news(self, symbol: str) -> list[str]:
        """Fetch recent headlines for *symbol* from the configured news provider.

        Returns a list of headline strings (may be empty).
        Raises httpx.HTTPError on network/HTTP failures (caller handles).
        """
        dispatch = {
            "finnhub": self._fetch_finnhub,
            "newsapi": self._fetch_newsapi,
            "gnews": self._fetch_gnews,
        }
        fetcher = dispatch.get(self._news_provider, self._fetch_alpaca)
        headlines = await fetcher(symbol)
        # Fallback chain: if primary returned nothing, try a secondary free source
        if not headlines and self._news_provider not in ("newsapi", "gnews"):
            if self._newsapi_key:
                logger.debug("Falling back to newsapi for %s", symbol)
                headlines = await self._fetch_newsapi(symbol)
            elif self._gnews_token:
                logger.debug("Falling back to gnews for %s", symbol)
                headlines = await self._fetch_gnews(symbol)
        return headlines

    async def _fetch_alpaca(self, symbol: str) -> list[str]:
        url = "https://data.alpaca.markets/v1beta1/news"
        headers = {
            "APCA-API-KEY-ID": self._news_api_key,
            "APCA-API-SECRET-KEY": os.getenv("NEWS_API_SECRET", ""),
        }
        params = {"symbols": symbol, "limit": 20}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
        news_items = data.get("news", data) if isinstance(data, dict) else data
        return [item.get("headline", "") for item in news_items if item.get("headline")]

    async def _fetch_finnhub(self, symbol: str) -> list[str]:
        import datetime

        url = "https://finnhub.io/api/v1/company-news"
        today = datetime.date.today()
        week_ago = today - datetime.timedelta(days=7)
        params = {
            "symbol": symbol,
            "from": week_ago.isoformat(),
            "to": today.isoformat(),
            "token": self._finnhub_key,
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            items: list[dict[str, Any]] = resp.json()
        return [item.get("headline", "") for item in items if item.get("headline")]

    async def _fetch_newsapi(self, symbol: str) -> list[str]:
        """Fetch headlines from NewsAPI.org — 100 req/day free tier."""
        if not self._newsapi_key:
            logger.debug("NEWSAPI_KEY not set, skipping newsapi for %s", symbol)
            return []
        url = "https://newsapi.org/v2/everything"
        params = {
            "q": symbol,
            "apiKey": self._newsapi_key,
            "sortBy": "publishedAt",
            "pageSize": 20,
            "language": "en",
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        articles: list[dict[str, Any]] = data.get("articles", [])
        return [
            item.get("title", "")
            for item in articles
            if item.get("title") and item["title"] != "[Removed]"
        ]

    async def _fetch_gnews(self, symbol: str) -> list[str]:
        """Fetch headlines from GNews.io — 100 req/day free tier."""
        if not self._gnews_token:
            logger.debug("GNEWS_TOKEN not set, skipping gnews for %s", symbol)
            return []
        url = "https://gnews.io/api/v4/search"
        params = {
            "q": symbol,
            "token": self._gnews_token,
            "max": 10,
            "lang": "en",
            "sortby": "publishedAt",
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        articles: list[dict[str, Any]] = data.get("articles", [])
        return [item.get("title", "") for item in articles if item.get("title")]

    # ------------------------------------------------------------------ #
    # T023 — _score_sentiment                                              #
    # ------------------------------------------------------------------ #

    async def _score_sentiment(
        self, headlines: list[str], symbol: str
    ) -> tuple[float | None, str]:
        """Score headlines with an LLM; return (score, summary).

        Score is clamped to [-1.0, +1.0].
        Summary is ≤ 50 words.
        Returns (None, "no_news") if called with an empty list (safety guard).
        """
        if not headlines:
            return None, "no_news"

        bullet_list = "\n".join(f"- {h}" for h in headlines[:20])
        prompt = (
            f"You are a financial sentiment analyst. "
            f"Given the following recent news headlines for {symbol}, "
            f"return a JSON object with exactly two fields:\n"
            f'  "score": a float between -1.0 (very bearish) and +1.0 (very bullish)\n'
            f'  "summary": a string of at most 50 words summarising the sentiment\n\n'
            f"Headlines:\n{bullet_list}\n\n"
            f"Respond with ONLY the JSON object, no other text."
        )

        try:
            raw = await async_llm_chat(prompt, model=self._model, timeout=45.0)
            raw = raw.strip()
            # Remove markdown code fences (```json...``` or ```...```) properly
            _fence = re.match(r'^```(?:json)?\s*([\s\S]*?)\s*```$', raw)
            if _fence:
                raw = _fence.group(1).strip()
            if not raw:
                logger.warning("LLM returned empty response for %s", symbol)
                return None, "scoring_error"
            parsed = json.loads(raw)
            score: float = float(parsed["score"])
            summary: str = str(parsed.get("summary", ""))
            # Clamp to [-1, +1]
            score = max(-1.0, min(1.0, score))
            # Truncate summary to 50 words
            words = summary.split()
            if len(words) > 50:
                summary = " ".join(words[:50])
            return score, summary
        except json.JSONDecodeError as json_exc:
            logger.warning("LLM sentiment JSON parse failed for %s: %s — raw=%r", symbol, json_exc, raw[:200] if 'raw' in dir() else 'N/A')
            return None, "scoring_error"
        except Exception:
            logger.exception("LLM sentiment scoring failed for %s", symbol)
            return None, "scoring_error"

    # ------------------------------------------------------------------ #
    # T024 — fetch_and_score (orchestration)                              #
    # ------------------------------------------------------------------ #

    async def fetch_and_score(self, symbol: str) -> None:
        """Orchestrate fetch → score → DB write for *symbol*.

        Any exception is caught and logged; the scheduler tick continues.
        """
        try:
            headlines = await self._fetch_news(symbol)

            if not headlines:
                await self.db.insert_market_intel(
                    symbol=symbol,
                    source=self._news_provider,
                    content="no_news",
                    sentiment_score=None,
                )
                logger.info("No news for %s — stored no_news record", symbol)
                return

            score, summary = await self._score_sentiment(headlines, symbol)

            # Clamp score defensively (in case _score_sentiment returns raw LLM value)
            if score is not None:
                score = max(-1.0, min(1.0, score))

            await self.db.insert_market_intel(
                symbol=symbol,
                source=self._news_provider,
                content=summary,
                sentiment_score=score,
            )
            logger.info(
                "Sentiment stored for %s: score=%.2f summary=%r",
                symbol,
                score if score is not None else float("nan"),
                summary,
            )
        except Exception:
            logger.exception("fetch_and_score failed for %s — skipping", symbol)

    # ------------------------------------------------------------------ #
    # T026 — start / stop                                                  #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Start the APScheduler interval job for all configured symbols."""
        self._scheduler = AsyncIOScheduler()
        for symbol in self.symbols:
            self._scheduler.add_job(
                self.fetch_and_score,
                trigger="interval",
                seconds=self.interval_seconds,
                args=[symbol],
                id=f"news_sentry_{symbol}",
                replace_existing=True,
                max_instances=1,
            )
        self._scheduler.start()
        logger.info(
            "NewsSentry started: symbols=%s interval=%ds",
            self.symbols,
            self.interval_seconds,
        )

    def stop(self) -> None:
        """Gracefully shut down the scheduler."""
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            logger.info("NewsSentry stopped")


# ------------------------------------------------------------------ #
# Entry point: python -m agents.news_sentry [symbol ...]              #
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    import asyncio
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from database.local_store import LocalStore

    symbols = sys.argv[1:] or ["SPY", "QQQ", "AAPL", "TSLA", "NVDA"]
    db = LocalStore()
    sentry = NewsSentry(symbols=symbols, db=db, interval_seconds=900)

    async def _main() -> None:
        print(f"Running one-shot fetch+score for {symbols}…")
        for sym in symbols:
            print(f"  Fetching {sym}…")
            await sentry.fetch_and_score(sym)
        rows = await db.get_recent_market_intel(limit=len(symbols) + 5)
        print(f"\n{len(rows)} records in local store:")
        for r in rows:
            score = r.get("sentiment_score")
            score_str = f"{score:+.2f}" if score is not None else "  n/a"
            print(f"  [{score_str}] {r['symbol']:<6} {r['source']:<10} {r['content'][:80]}")

    asyncio.run(_main())