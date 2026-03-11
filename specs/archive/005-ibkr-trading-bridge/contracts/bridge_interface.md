# Internal Contracts: 005 — IBKR Trading Bridge

This is an internal daemon — no HTTP endpoints are exposed. Contracts describe the Python interfaces.

---

## IBridgeBase ABC

```python
class IBridgeBase(ABC):
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def get_portfolio_greeks(self) -> dict:
        """
        Returns:
          {
            "delta": float,
            "gamma": float,
            "vega":  float,
            "theta": float,
            "underlying_price": float | None,
            "contract": "PORTFOLIO"
          }
        """
        ...

    @abstractmethod
    def is_connected(self) -> bool: ...
```

---

## bridge.database_manager

```python
async def ensure_bridge_schema(pool: asyncpg.Pool) -> None:
    """Creates portfolio_greeks and api_logs tables if they do not exist."""

async def write_portfolio_snapshot(
    breaker: DBCircuitBreaker,
    row: dict,  # keys: timestamp, contract, delta, gamma, vega, theta, underlying_price
) -> None:
    """Writes one row to portfolio_greeks via the circuit breaker."""

async def log_api_event(
    breaker: DBCircuitBreaker,
    api_mode: str,   # 'SOCKET' | 'PORTAL'
    message: str,
    status: str,     # 'info' | 'warning' | 'error'
) -> None:
    """Writes one row to api_logs via the circuit breaker."""
```

---

## Database Row Schemas

### portfolio_greeks insert payload

```json
{
  "timestamp": "2026-02-21T10:00:00+00:00",
  "contract": "PORTFOLIO",
  "delta": -12.3,
  "gamma": 0.05,
  "vega": -843.2,
  "theta": 312.1,
  "underlying_price": 6850.0
}
```

### api_logs insert payload

```json
{
  "timestamp": "2026-02-21T10:00:00+00:00",
  "api_mode": "SOCKET",
  "message": "Connected to IBKR Gateway at 127.0.0.1:7496",
  "status": "info"
}
```
