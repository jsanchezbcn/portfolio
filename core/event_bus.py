import asyncio
import json
import logging
from typing import Callable, Dict, List, Any

import asyncpg

logger = logging.getLogger(__name__)

class EventBus:
    """
    Centralized event bus using PostgreSQL LISTEN/NOTIFY.
    """
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._conn: asyncpg.Connection | None = None
        self._pool: asyncpg.Pool | None = None
        self._callbacks: Dict[str, List[Callable[[dict], Any]]] = {}
        self._running = False

    async def start(self):
        """Start the event bus connection and pool."""
        if self._running:
            return
        
        self._pool = await asyncpg.create_pool(self.dsn)
        self._conn = await asyncpg.connect(self.dsn)
        self._running = True
        logger.info("EventBus started.")

    async def stop(self):
        """Stop the event bus."""
        self._running = False
        if self._conn:
            for channel in self._callbacks.keys():
                await self._conn.remove_listener(channel, self._on_notify)
            await self._conn.close()
            self._conn = None
        if self._pool:
            await self._pool.close()
            self._pool = None
        logger.info("EventBus stopped.")

    async def subscribe(self, channel: str, callback: Callable[[dict], Any]):
        """Subscribe to a channel."""
        if not self._running:
            raise RuntimeError("EventBus is not running. Call start() first.")
        
        if channel not in self._callbacks:
            self._callbacks[channel] = []
            await self._conn.add_listener(channel, self._on_notify)
            logger.info(f"Subscribed to channel: {channel}")
            
        self._callbacks[channel].append(callback)

    async def publish(self, channel: str, payload: dict):
        """Publish a message to a channel."""
        if not self._running:
            raise RuntimeError("EventBus is not running. Call start() first.")
        
        payload_str = json.dumps(payload)
        async with self._pool.acquire() as conn:
            await conn.execute(f"NOTIFY {channel}, '{payload_str}'")
            logger.debug(f"Published to {channel}: {payload_str}")

    def _on_notify(self, conn: asyncpg.Connection, pid: int, channel: str, payload: str):
        """Internal callback for PostgreSQL NOTIFY."""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            logger.error(f"Failed to decode payload on channel {channel}: {payload}")
            return

        callbacks = self._callbacks.get(channel, [])
        for callback in callbacks:
            try:
                # Schedule the callback as a task so it doesn't block the listener
                asyncio.create_task(self._invoke_callback(callback, data))
            except Exception as e:
                logger.error(f"Error scheduling callback for channel {channel}: {e}")

    async def _invoke_callback(self, callback: Callable[[dict], Any], data: dict):
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(data)
            else:
                callback(data)
        except Exception as e:
            logger.error(f"Error in callback for event: {e}")

# Global instance
_event_bus: EventBus | None = None

def get_event_bus(dsn: str | None = None) -> EventBus:
    global _event_bus
    if _event_bus is None:
        import os
        dsn = dsn or os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/portfolio")
        _event_bus = EventBus(dsn)
    return _event_bus
