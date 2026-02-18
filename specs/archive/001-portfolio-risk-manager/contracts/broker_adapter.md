# Contract: BrokerAdapter Interface

**Feature**: Portfolio Risk Management System  
**Date**: February 12, 2026

## Overview

The `BrokerAdapter` abstract base class defines the interface that all broker integrations must implement. This ensures consistent data handling regardless of the underlying broker API.

## Base Class Definition

```python
from abc import ABC, abstractmethod
from typing import List
from models.unified_position import UnifiedPosition

class BrokerAdapter(ABC):
    """Base class for broker adapters"""

    @abstractmethod
    async def fetch_positions(self, account_id: str) -> List[UnifiedPosition]:
        """
        Fetch and normalize positions for a given account.

        Args:
            account_id: Broker-specific account identifier

        Returns:
            List of UnifiedPosition objects

        Raises:
            ConnectionError: If broker API is unreachable
            AuthenticationError: If credentials are invalid
            ValueError: If account_id is invalid
        """
        pass

    @abstractmethod
    async def fetch_greeks(self, positions: List[UnifiedPosition]) -> List[UnifiedPosition]:
        """
        Enrich positions with Greeks data.

        Some brokers provide Greeks in the position data,
        others require separate API calls.

        Args:
            positions: List of positions potentially missing Greeks

        Returns:
            Same list with Greeks populated

        Raises:
            ConnectionError: If data provider is unreachable
        """
        pass
```

## Implementation Requirements

### fetch_positions()

**Input**:

- `account_id` (str): Account identifier in broker's format
  - IBKR: Typically "U1234567" format
  - Tastytrade: Account number string

**Output**:

- `List[UnifiedPosition]`: All positions in the account
  - Must include equity positions with delta = quantity
  - Must include option positions with contract details
  - Must calculate `days_to_expiration` if expiration present

**Responsibilities**:

1. Authenticate with broker API (use existing credentials/session)
2. Fetch raw position data
3. Transform each position to UnifiedPosition:
   - Map symbol/ticker
   - Determine instrument_type
   - Extract option details if applicable
   - Calculate DTE from expiration date
   - Set broker field to adapter source
4. Calculate SPX-weighted delta if beta data available
5. Return all positions (including zero-quantity for closed positions if relevant)

**Error Handling**:

- Return empty list if no positions (do not raise exception)
- Raise `ConnectionError` if API unreachable
- Raise `AuthenticationError` if session expired
- Log warnings for positions that cannot be parsed

**Performance**:

- Should complete within 2 seconds for typical account (< 100 positions)
- Use caching if broker API is slow
- Consider async/await for multiple accounts

### fetch_greeks()

**Input**:

- `positions` (List[UnifiedPosition]): Positions possibly missing Greeks

**Output**:

- `List[UnifiedPosition]`: Same list with Greeks populated

**Responsibilities**:

1. Identify which positions need Greeks (options only)
2. Fetch Greeks from broker or third-party data provider
3. Populate delta, gamma, theta, vega, iv fields
4. Multiply per-contract Greeks by quantity for position Greeks
5. Return updated position list

**Error Handling**:

- If Greeks unavailable, set to 0.0 and log warning
- Do not fail entire batch if one position fails
- Graceful degradation: partial data better than no data

**Performance**:

- Batch API calls where possible
- Use cached data if less than 5 minutes old
- Parallel fetching for multiple positions

## Concrete Implementations

### IBKRAdapter

**Source**: Wraps `ibkr_portfolio_client.py` (existing)

**fetch_positions()**:

- Calls `client.get_positions(account_id)`
- Uses `client._extract_option_details()` for option parsing
- Uses `client.calculate_spx_weighted_delta()` for beta adjustment

**fetch_greeks()**:

- Uses `client.get_tastytrade_option_greeks()` (existing cache)
- Falls back to zero if cache miss

**Special Handling**:

- IBKR contract IDs (conid) not exposed in UnifiedPosition
- Uses contract description in symbol field if no ticker

### TastytradeAdapter

**Source**: Wraps `tastytrade_options_fetcher.py` (existing)

**fetch_positions()**:

- Fetches positions via Tastytrade SDK
- Greeks often included in position response

**fetch_greeks()**:

- May be no-op if Greeks already populated
- Otherwise fetch from Tastytrade market data

**Special Handling**:

- Tastytrade natively provides Greeks
- Expiration format may differ from IBKR

## Data Mapping Examples

### IBKR → UnifiedPosition

```python
# IBKR raw position
{
    'acctId': 'U1234567',
    'conid': 12345678,
    'contractDesc': 'AAPL 20MAR25 180 C',
    'position': 10,
    'mktPrice': 5.50,
    'mktValue': 5500,
    'currency': 'USD',
    'avgCost': 5.20,
    'unrealizedPnl': 300,
    'ticker': 'AAPL'
}

# Transformed to UnifiedPosition
UnifiedPosition(
    symbol='AAPL 20MAR25 180 C',
    instrument_type=InstrumentType.OPTION,
    broker='ibkr',
    quantity=10,
    avg_price=5.20,
    market_value=5500,
    unrealized_pnl=300,
    delta=0,  # Populated in fetch_greeks()
    gamma=0,
    theta=0,
    vega=0,
    spx_delta=0,  # Calculated after Greeks
    underlying='AAPL',
    strike=180.0,
    expiration=date(2025, 3, 20),
    option_type='call',
    iv=None,
    days_to_expiration=36
)
```

### Tastytrade → UnifiedPosition

```python
# Tastytrade raw position
{
    'symbol': 'AAPL  250320C00180000',
    'instrument-type': 'Equity Option',
    'quantity': 10,
    'average-open-price': '5.20',
    'mark': '5.50',
    'realized-day-gain': '300',
    'delta': '0.65',
    'gamma': '0.035',
    'theta': '-0.85',
    'vega': '1.20',
    'iv': '0.28'
}

# Transformed to UnifiedPosition
UnifiedPosition(
    symbol='AAPL 250320C00180000',
    instrument_type=InstrumentType.OPTION,
    broker='tastytrade',
    quantity=10,
    avg_price=5.20,
    market_value=5500,
    unrealized_pnl=300,
    delta=6.5,  # 0.65 × 10
    gamma=0.35,  # 0.035 × 10
    theta=-8.5,  # -0.85 × 10
    vega=12.0,  # 1.20 × 10
    spx_delta=0,  # Calculate via beta
    underlying='AAPL',
    strike=180.0,
    expiration=date(2025, 3, 20),
    option_type='call',
    iv=0.28,
    days_to_expiration=36
)
```

## Testing Contract

All adapter implementations must pass this test suite:

### Test: fetch_positions()

```python
async def test_fetch_positions(adapter: BrokerAdapter):
    """Verify positions are fetched and normalized"""
    positions = await adapter.fetch_positions('test_account')

    assert isinstance(positions, list)
    assert all(isinstance(p, UnifiedPosition) for p in positions)
    assert all(p.broker == adapter.broker_name for p in positions)

    # Check option details populated for options
    option_positions = [p for p in positions if p.instrument_type == InstrumentType.OPTION]
    for pos in option_positions:
        assert pos.underlying is not None
        assert pos.strike is not None
        assert pos.expiration is not None
        assert pos.option_type in ['call', 'put']
        assert pos.days_to_expiration is not None
```

### Test: fetch_greeks()

```python
async def test_fetch_greeks(adapter: BrokerAdapter):
    """Verify Greeks are populated"""
    # Create mock positions without Greeks
    mock_positions = [
        UnifiedPosition(
            symbol='TEST',
            instrument_type=InstrumentType.OPTION,
            broker=adapter.broker_name,
            quantity=10,
            avg_price=1.0,
            market_value=1000,
            unrealized_pnl=0,
            underlying='TEST',
            strike=100,
            expiration=date.today() + timedelta(days=30),
            option_type='call'
        )
    ]

    enriched = await adapter.fetch_greeks(mock_positions)

    assert len(enriched) == len(mock_positions)
    # Greeks should be populated (non-zero or at least set)
    for pos in enriched:
        assert pos.delta is not None
        assert pos.gamma is not None
        assert pos.theta is not None
        assert pos.vega is not None
```

### Test: Error Handling

```python
async def test_invalid_account(adapter: BrokerAdapter):
    """Verify graceful error handling"""
    with pytest.raises((ConnectionError, AuthenticationError, ValueError)):
        await adapter.fetch_positions('INVALID_ACCOUNT')
```

## Version History

- 2026-02-12: Initial contract definition
