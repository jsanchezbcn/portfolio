import asyncio
import logging
from typing import Dict, Any

from core.event_bus import get_event_bus

logger = logging.getLogger(__name__)

class ExecutionAgent:
    """
    Focuses on minimizing slippage, using algorithms like TWAP/VWAP, and managing broker API interactions.
    """
    def __init__(self):
        self.event_bus = get_event_bus()
        self.active_orders = {}

    async def start(self):
        """Start the execution agent."""
        await self.event_bus.start()
        await self.event_bus.subscribe("ORDER_SUBMITTED", self._on_order_submitted)
        await self.event_bus.subscribe("MARKET_DATA", self._on_market_data)
        logger.info("ExecutionAgent started and subscribed to ORDER_SUBMITTED and MARKET_DATA.")

    async def _on_order_submitted(self, data: dict):
        """Handle submitted orders and begin execution strategy."""
        order = data.get("order", {})
        if not order:
            return

        order_id = order.get("id")
        self.active_orders[order_id] = {
            "order": order,
            "status": "WORKING",
            "filled_quantity": 0,
            "remaining_quantity": order.get("quantity", 0),
            "strategy": order.get("execution_strategy", "MKT") # Default to Market
        }
        
        logger.info(f"ExecutionAgent working on order {order_id} with strategy {self.active_orders[order_id]['strategy']}")
        
        # In a real system, this would start a background task to manage the execution
        # For now, we just simulate a fill after a delay
        asyncio.create_task(self._simulate_execution(order_id))

    async def _on_market_data(self, data: dict):
        """Handle real-time market data to adjust execution algorithms."""
        # Update TWAP/VWAP calculations based on incoming ticks
        pass

    async def _simulate_execution(self, order_id: str):
        """Simulate the execution process."""
        await asyncio.sleep(2) # Simulate network delay
        
        if order_id not in self.active_orders:
            return
            
        order_info = self.active_orders[order_id]
        order = order_info["order"]
        
        # Simulate a full fill
        fill_event = {
            "order_id": order_id,
            "symbol": order.get("symbol"),
            "filled_quantity": order.get("quantity"),
            "fill_price": order.get("limit_price", 100), # Use limit price or default
            "status": "FILLED"
        }
        
        await self.event_bus.publish("ORDER_FILLED", fill_event)
        logger.info(f"Published ORDER_FILLED event for {order_id}")
        
        # Clean up
        del self.active_orders[order_id]

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    agent = ExecutionAgent()
    asyncio.run(agent.start())
    # Keep running
    asyncio.get_event_loop().run_forever()
