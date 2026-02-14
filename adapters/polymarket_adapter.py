from __future__ import annotations

from datetime import datetime
from typing import Any

import aiohttp


class PolymarketAdapter:
    """Fetches and parses Polymarket recession probabilities."""

    def __init__(self) -> None:
        """Initialize known Polymarket endpoints."""

        self.base_urls = [
            "https://gamma-api.polymarket.com/markets",
            "https://gamma-api.polymarket.com/events",
        ]

    async def get_recession_probability(self) -> dict[str, Any]:
        """Return recession probability payload with source and timestamp."""

        try:
            markets = await self._fetch_recession_markets()
            probability = self._extract_recession_probability(markets)
            return {
                "recession_probability": probability,
                "source": "polymarket",
                "timestamp": datetime.utcnow().isoformat(),
            }
        except Exception as exc:
            return {
                "recession_probability": None,
                "source": "unavailable",
                "timestamp": datetime.utcnow().isoformat(),
                "error": str(exc),
            }

    async def _fetch_recession_markets(self) -> list[dict[str, Any]]:
        current_year = datetime.utcnow().year
        collected: list[dict[str, Any]] = []

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            for url in self.base_urls:
                params = {
                    "limit": "200",
                    "active": "true",
                    "closed": "false",
                }
                payload = await self._fetch_json(session, url, params)
                markets = payload if isinstance(payload, list) else payload.get("data", []) if isinstance(payload, dict) else []
                for market in markets:
                    title = str(market.get("question") or market.get("title") or "").lower()
                    if "recession" not in title:
                        continue
                    if not self._market_matches_year(market, current_year):
                        continue
                    collected.append(market)

        return collected

    async def _fetch_json(self, session: aiohttp.ClientSession, url: str, params: dict[str, str]) -> Any:
        async with session.get(url, params=params) as response:
            response.raise_for_status()
            return await response.json()

    def _market_matches_year(self, market: dict[str, Any], current_year: int) -> bool:
        text = " ".join(
            [
                str(market.get("question") or ""),
                str(market.get("title") or ""),
                str(market.get("description") or ""),
                str(market.get("slug") or ""),
            ]
        )
        years = [year for year in range(current_year, current_year + 3) if str(year) in text]
        return bool(years) or ("this year" in text.lower())

    def _extract_recession_probability(self, markets: list[dict[str, Any]]) -> float | None:
        if not markets:
            return None

        candidates: list[float] = []
        for market in markets:
            prob = self._extract_yes_probability(market)
            if prob is not None:
                candidates.append(prob)

        if not candidates:
            return None

        return max(candidates)

    def _extract_yes_probability(self, market: dict[str, Any]) -> float | None:
        outcomes = market.get("outcomes")
        outcome_prices = market.get("outcomePrices")

        if isinstance(outcomes, str):
            outcomes = [item.strip().strip('"') for item in outcomes.strip("[]").split(",") if item.strip()]
        if isinstance(outcome_prices, str):
            outcome_prices = [item.strip().strip('"') for item in outcome_prices.strip("[]").split(",") if item.strip()]

        if isinstance(outcomes, list) and isinstance(outcome_prices, list):
            for idx, outcome in enumerate(outcomes):
                if str(outcome).strip().lower() != "yes":
                    continue
                if idx >= len(outcome_prices):
                    continue
                try:
                    value = float(outcome_prices[idx])
                    if value > 1.0:
                        value = value / 100.0
                    return max(0.0, min(1.0, value))
                except (TypeError, ValueError):
                    continue

        for key in ("yesPrice", "probability", "yes_probability"):
            if key not in market:
                continue
            try:
                value = float(market[key])
                if value > 1.0:
                    value = value / 100.0
                return max(0.0, min(1.0, value))
            except (TypeError, ValueError):
                continue

        return None
