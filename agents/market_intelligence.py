import asyncio
import logging
from typing import Dict, Any

from core.event_bus import get_event_bus

logger = logging.getLogger(__name__)

class MarketIntelligenceAgent:
    """
    Analyzes news, sentiment, and macroeconomic data to adjust the overall portfolio risk regime.
    """
    def __init__(self):
        self.event_bus = get_event_bus()
        self.current_regime = "neutral_volatility"
        self.vix_level = 15.0
        self.term_structure = 1.0

    async def start(self):
        """Start the market intelligence agent."""
        await self.event_bus.start()
        await self.event_bus.subscribe("MARKET_DATA", self._on_market_data)
        await self.event_bus.subscribe("NEWS_ALERT", self._on_news_alert)
        logger.info("MarketIntelligenceAgent started and subscribed to MARKET_DATA and NEWS_ALERT.")

    async def _on_market_data(self, data: dict):
        """Handle real-time market data to update VIX and term structure."""
        symbol = data.get("symbol")
        if symbol == "VIX":
            self.vix_level = data.get("last_price", self.vix_level)
            await self._evaluate_regime()
        elif symbol == "VIX_TERM_STRUCTURE":
            self.term_structure = data.get("value", self.term_structure)
            await self._evaluate_regime()

    async def _on_news_alert(self, data: dict):
        """Handle breaking news alerts that might trigger a regime change."""
        sentiment = data.get("sentiment", "neutral")
        impact = data.get("impact", "low")
        
        if sentiment == "negative" and impact == "high":
            logger.warning(f"High impact negative news received: {data.get('headline')}")
            # Temporarily spike VIX internally to force a regime evaluation
            self.vix_level += 5.0
            await self._evaluate_regime()

    async def _evaluate_regime(self):
        """Evaluate current market conditions and determine the appropriate risk regime."""
        new_regime = self.current_regime
        
        if self.vix_level > 22:
            new_regime = "high_volatility"
        elif self.vix_level < 15 and self.term_structure > 1.10:
            new_regime = "low_volatility"
        else:
            new_regime = "neutral_volatility"
            
        if new_regime != self.current_regime:
            logger.info(f"Regime change detected: {self.current_regime} -> {new_regime}")
            self.current_regime = new_regime
            
            regime_event = {
                "regime": self.current_regime,
                "vix_level": self.vix_level,
                "term_structure": self.term_structure,
                "reason": "Market conditions updated"
            }
            
            await self.event_bus.publish("REGIME_CHANGED", regime_event)
            logger.info(f"Published REGIME_CHANGED event: {regime_event}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    agent = MarketIntelligenceAgent()
    asyncio.run(agent.start())
    # Keep running
    asyncio.get_event_loop().run_forever()
