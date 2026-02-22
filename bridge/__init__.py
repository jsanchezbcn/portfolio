"""
bridge/
───────
IBKR trading bridge — connects to Interactive Brokers via SOCKET (ib_async)
or PORTAL (REST API) and persists portfolio-level Greeks to PostgreSQL every
5 seconds via a DBCircuitBreaker.

Public API:
  from bridge.ib_bridge import IBridgeBase, SocketBridge, PortalBridge, Watchdog
  from bridge.database_manager import ensure_bridge_schema, write_portfolio_snapshot, log_api_event
"""

from bridge.ib_bridge import IBridgeBase, PortalBridge, SocketBridge, Watchdog

__all__ = ["IBridgeBase", "SocketBridge", "PortalBridge", "Watchdog"]
