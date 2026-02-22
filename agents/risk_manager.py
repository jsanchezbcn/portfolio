import asyncio
import logging
import yaml
from pathlib import Path
from typing import Dict, Any, List

from core.event_bus import get_event_bus

logger = logging.getLogger(__name__)

class RiskManagerAgent:
    """
    Monitors portfolio Greeks against predefined limits and proposes hedging orders.
    """
    def __init__(self, config_path: str = "config/risk_matrix.yaml"):
        self.config_path = Path(config_path)
        self.risk_matrix = self._load_config()
        self.event_bus = get_event_bus()
        self.current_regime = "neutral_volatility" # Default, can be updated by Market Intelligence Agent

    def _load_config(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            logger.warning(f"Risk matrix config not found at {self.config_path}. Using defaults.")
            return {}
        with open(self.config_path, "r") as f:
            return yaml.safe_load(f)

    async def start(self):
        """Start the risk manager agent."""
        await self.event_bus.start()
        await self.event_bus.subscribe("PORTFOLIO_UPDATED", self._on_portfolio_updated)
        await self.event_bus.subscribe("REGIME_CHANGED", self._on_regime_changed)
        logger.info("RiskManagerAgent started and subscribed to PORTFOLIO_UPDATED.")

    async def _on_regime_changed(self, data: dict):
        """Handle regime change events from Market Intelligence Agent."""
        new_regime = data.get("regime")
        if new_regime and new_regime in self.risk_matrix.get("regimes", {}):
            self.current_regime = new_regime
            logger.info(f"Risk regime updated to: {self.current_regime}")

    async def _on_portfolio_updated(self, data: dict):
        """Handle portfolio updates and check risk limits."""
        portfolio = data.get("portfolio", {})
        greeks = portfolio.get("greeks", {})
        nlv = portfolio.get("net_liquidation_value", 0)

        if not greeks or nlv <= 0:
            return

        limits = self._get_current_limits(nlv)
        breaches = self._check_limits(greeks, limits)

        if breaches:
            logger.warning(f"Risk limits breached: {breaches}")
            await self._propose_hedge(breaches, greeks)

    def _get_current_limits(self, nlv: float) -> Dict[str, float]:
        """Calculate effective limits based on current regime and NLV."""
        regime_config = self.risk_matrix.get("regimes", {}).get(self.current_regime, {})
        base_limits = regime_config.get("limits", {})

        # Calculate effective limits based on NLV
        effective_limits = {
            "min_theta": base_limits.get("min_daily_theta_pct_nlv", 0) * nlv,
            "max_negative_vega": base_limits.get("max_negative_vega_pct_nlv", 0) * nlv,
            "max_delta": base_limits.get("max_spx_delta_pct_nlv", 0) * nlv,
            "max_gamma": base_limits.get("max_gamma_pct_nlv", 0) * nlv,
        }
        return effective_limits

    def _check_limits(self, greeks: Dict[str, float], limits: Dict[str, float]) -> List[str]:
        """Check if current Greeks breach the effective limits."""
        breaches = []
        
        # Delta check (absolute value)
        if abs(greeks.get("spx_delta", 0)) > limits.get("max_delta", float('inf')):
            breaches.append(f"Delta ({greeks.get('spx_delta')}) exceeds limit ({limits.get('max_delta')})")
            
        # Vega check (negative vega limit)
        if greeks.get("vega", 0) < limits.get("max_negative_vega", -float('inf')):
            breaches.append(f"Vega ({greeks.get('vega')}) exceeds negative limit ({limits.get('max_negative_vega')})")
            
        # Theta check (minimum theta)
        if greeks.get("theta", 0) < limits.get("min_theta", -float('inf')):
            breaches.append(f"Theta ({greeks.get('theta')}) is below minimum ({limits.get('min_theta')})")
            
        return breaches

    async def _propose_hedge(self, breaches: List[str], current_greeks: Dict[str, float]):
        """Propose a hedging order to mitigate risk breaches."""
        # Simple heuristic for now: if delta is too high, propose selling SPY/SPX
        # In a real system, this would use the risk-management skill to find optimal hedges
        
        hedge_proposal = {
            "reason": "Risk limit breach",
            "breaches": breaches,
            "current_greeks": current_greeks,
            "suggested_action": "Review portfolio delta and vega exposure."
        }
        
        # If delta is the main issue, suggest a delta hedge
        delta = current_greeks.get("spx_delta", 0)
        if any("Delta" in b for b in breaches):
            action = "SELL" if delta > 0 else "BUY"
            hedge_proposal["suggested_hedge"] = {
                "symbol": "SPY",
                "action": action,
                "quantity": abs(int(delta / 100)), # Rough SPY equivalent
                "type": "MKT"
            }
            
        await self.event_bus.publish("HEDGE_PROPOSED", hedge_proposal)
        logger.info(f"Published HEDGE_PROPOSED event: {hedge_proposal}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    agent = RiskManagerAgent()
    asyncio.run(agent.start())
    # Keep running
    asyncio.get_event_loop().run_forever()
