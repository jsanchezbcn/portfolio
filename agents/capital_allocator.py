import asyncio
import logging
from typing import Dict, Any

from core.event_bus import get_event_bus

logger = logging.getLogger(__name__)

class CapitalAllocatorAgent:
    """
    Determines optimal position size based on Kelly Criterion, Risk Parity, or fixed fractional risk.
    """
    def __init__(self):
        self.event_bus = get_event_bus()
        self.max_position_size_pct = 0.05 # 5% max per position
        self.kelly_fraction = 0.5 # Half-Kelly

    async def start(self):
        """Start the capital allocator agent."""
        await self.event_bus.start()
        await self.event_bus.subscribe("ORDER_STAGED", self._on_order_staged)
        logger.info("CapitalAllocatorAgent started and subscribed to ORDER_STAGED.")

    async def _on_order_staged(self, data: dict):
        """Handle staged orders and determine optimal size."""
        order = data.get("order", {})
        portfolio = data.get("portfolio", {})
        
        if not order or not portfolio:
            return

        nlv = portfolio.get("net_liquidation_value", 0)
        if nlv <= 0:
            logger.warning("Cannot allocate capital: NLV is zero or unknown.")
            return

        # Calculate optimal size
        optimal_size = self._calculate_optimal_size(order, nlv)
        
        # If the requested size is larger than optimal, we might want to adjust it
        # For now, we just log it and publish an ALLOCATION_PROPOSED event
        allocation_proposal = {
            "order_id": order.get("id"),
            "symbol": order.get("symbol"),
            "requested_quantity": order.get("quantity"),
            "optimal_quantity": optimal_size,
            "reason": "Kelly Criterion / Fixed Fractional Risk"
        }
        
        await self.event_bus.publish("ALLOCATION_PROPOSED", allocation_proposal)
        logger.info(f"Published ALLOCATION_PROPOSED event: {allocation_proposal}")

    def _calculate_optimal_size(self, order: Dict[str, Any], nlv: float) -> int:
        """Calculate optimal position size based on risk parameters."""
        # Simplified Kelly Criterion / Fixed Fractional Risk
        # In a real system, this would use historical win rate and payoff ratio
        
        # Max capital allowed for this position
        max_capital = nlv * self.max_position_size_pct
        
        # Estimated margin requirement or cost per unit
        # This is a placeholder. Real implementation needs actual margin impact.
        estimated_cost_per_unit = order.get("limit_price", 100) * 100 # Assuming options multiplier 100
        
        if estimated_cost_per_unit <= 0:
            return order.get("quantity", 1)
            
        max_quantity = int(max_capital / estimated_cost_per_unit)
        
        # Return the smaller of requested quantity or max allowed quantity
        requested_qty = order.get("quantity", 1)
        return min(requested_qty, max(1, max_quantity))

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    agent = CapitalAllocatorAgent()
    asyncio.run(agent.start())
    # Keep running
    asyncio.get_event_loop().run_forever()
