# AI Strategy Tools Implementation

**Date**: March 9, 2026  
**Feature**: AI Risk Tab Strategy Analysis Tools  
**Status**: ✅ Complete & Tested

## Overview

Added three new tools to the AI Risk tab that expose portfolio strategies and provide intelligent suggestions for capital optimization and error detection. These tools complement the existing raw position data with higher-level strategy insights.

## New Tools

### 1. `get_strategy_snapshot`

**Purpose**: Expose portfolio strategies grouped by strategy type (spreads, iron condors, butterflies, etc.)

**Returns**:

```python
[
  {
    "association_id": "sha1_hash",
    "strategy_name": "Bull Call Spread",
    "strategy_family": "vertical",
    "underlying": "ES",
    "matched_by": "vertical_spread",
    "expiry_label": "Mar 20",
    "leg_count": 2,
    "leg_ids": [123, 456],
    "net_delta": 10.5,
    "net_gamma": 0.25,
    "net_theta": -2.3,
    "net_vega": 5.1,
    "net_spx_delta": 8.2,
    "market_value": -500.0,
    "unrealized_pnl": 150.0,
    "realized_pnl": 0.0
  },
  ...
]
```

**Use Cases**:

- LLM can understand portfolio in terms of actual strategies rather than raw legs
- Enables strategy-level risk analysis ("your iron condor is absorbing most of your margin")
- Better context for trade suggestions ("complete your half-spread by buying the protective leg")

### 2. `validate_strategies`

**Purpose**: Identify construction errors, incomplete spreads, and unusual risk characteristics

**Parameters**:

- `strategy_id` (optional): Validate specific strategy, or all if omitted

**Returns**:

```python
{
  "total_strategies": 5,
  "valid_count": 3,
  "issues_count": 2,
  "issues": [
    {
      "association_id": "abc123",
      "strategy_name": "Bull Call Spread",
      "underlying": "ES",
      "issues": [
        "Spread should have 2 legs but has 1",
        "High net delta 125.0 for vertical strategy"
      ]
    }
  ]
}
```

**Validation Checks**:

1. **Incomplete spreads**: Detects spreads with missing legs
2. **Unbalanced quantities**: Identifies non-standard leg ratios
3. **Excessive net delta**: Flags high directional exposure for neutral strategies
4. **Conflicting Greeks**: Detects unusual combinations (e.g., positive theta + large negative vega)
5. **Wrong leg count**: Validates iron condors (4 legs), butterflies (3 legs), etc.
6. **Calendar validation**: Ensures calendar/diagonal spreads have multiple expiries

**Use Cases**:

- Automatic portfolio health checks
- Pre-trade validation before adding new positions
- Identify accidental naked positions from partial closures

### 3. `optimize_capital`

**Purpose**: Suggest trade adjustments to reduce margin while maintaining similar exposure

**Parameters**:

- `underlying` (optional): Focus optimization on specific symbol
- `target_metric`: "margin" (reduce capital) or "delta_efficiency" (improve delta per dollar)

**Returns**:

```python
{
  "current_margin_used": 25000.0,
  "current_margin_pct": 25.0,
  "net_liquidation": 100000.0,
  "strategies_analyzed": 5,
  "suggestions_count": 3,
  "suggestions": [
    {
      "strategy_id": "abc123",
      "underlying": "ES",
      "current_strategy": "Short Call",
      "suggestion": "Convert to Bear Call Spread",
      "rationale": "Reduce margin by buying 5905C protection",
      "estimated_margin_reduction": "60-80%",
      "legs_to_add": [
        {
          "action": "BUY",
          "qty": 1,
          "symbol": "ES",
          "strike": 5905.0,
          "right": "C",
          "expiry": "20260320",
          "sec_type": "FOP"
        }
      ]
    }
  ]
}
```

**Optimization Strategies**:

1. **Convert naked options to spreads**: Reduces margin by 60-80%
2. **Complete partial spreads**: Cap risk/reward by adding missing legs
3. **Iron condor suggestions**: For high-gamma spreads, suggest adding complementary side
4. **Smart strike selection**: Suggests appropriate protection strikes based on underlying (e.g., +5 for /ES, +5% for stocks)

**Use Cases**:

- LLM can proactively suggest capital efficiency improvements
- User asks "how can I reduce margin use?" → immediate actionable suggestions
- Before adding new trades, optimize existing to free up capital

## Integration

**File**: `desktop/ui/ai_risk_tab.py`

**Parameter Models** (lines 89-97):

```python
class ValidateStrategyParams(BaseModel):
    strategy_id: str | None = Field(default=None)

class OptimizeCapitalParams(BaseModel):
    underlying: str | None = Field(default=None)
    target_metric: str = Field(default="margin")
```

**Tool Handlers** (lines 1089-1327):

- `_tool_get_strategy_snapshot()`: Fetches from `engine.strategy_snapshot()`
- `_tool_validate_strategies()`: Runs 6 validation checks
- `_tool_optimize_capital()`: Analyzes strategies and generates 3 types of suggestions

**Tool Registration** (lines 1430-1465):
Added to `_create_tools_for_session()` return list

## Testing

**File**: `desktop/tests/test_ai_risk_strategy_tools.py`  
**Coverage**: 17 tests, all passing

### Test Categories

1. **Strategy Snapshot Tests** (3 tests)
   - Empty portfolio
   - Single strategy with Greeks
   - Multiple strategies

2. **Validation Tests** (7 tests)
   - All valid strategies
   - Incomplete spread detection
   - Unbalanced quantities
   - High net delta
   - Conflicting Greeks
   - Specific ID validation
   - Non-existent ID handling

3. **Capital Optimization Tests** (7 tests)
   - Empty portfolio
   - Naked short call → Bear call spread
   - Naked short put → Bull put spread
   - Incomplete spread completion
   - Underlying filter
   - High gamma → Iron condor suggestion
   - Margin metrics accuracy

### Test Results

```
✅ 17/17 tests passing (desktop/tests/test_ai_risk_strategy_tools.py)
✅ 63/63 tests passing (all AI risk tab tests)
✅ 82/82 tests passing (strategy + portfolio + table tests)
```

## Usage Examples

### 1. Portfolio Health Check

```
User: "Check my portfolio for any errors"
LLM calls: validate_strategies()
Response: "I found 2 issues: Your ES Bull Call Spread is incomplete (only has 1 leg),
and your SPY Iron Condor has unbalanced quantities (2 puts, 1 call)."
```

### 2. Capital Optimization Request

```
User: "I need to free up some margin, what can I do?"
LLM calls: optimize_capital()
Response: "I found 3 opportunities to reduce margin:
1. Convert your naked short ES 5900C to a bear call spread by buying 5905C → saves ~$15k
2. Complete your partial SPY bull put spread by selling the 580P → reduces margin 40%
3. Your MES position has high gamma; convert to an iron condor for more premium collection"
```

### 3. Strategy-Level Risk Analysis

```
User: "What's my biggest risk right now?"
LLM calls: get_strategy_snapshot() + get_portfolio_metrics()
Response: "Your largest exposure is the ES Iron Condor with -45 net SPX delta and
$25k margin (50% of your total margin use). The gamma is 35, which means small
moves in ES will significantly impact your delta."
```

## Technical Details

### Data Flow

```
IBEngine.refresh_positions()
  → StrategyReconstructor.reconstruct()
  → IBEngine._strategy_snapshot (cached)
  → AIRiskTab._tool_get_strategy_snapshot()
  → LLM receives structured strategy data
```

### Strategy Families Recognized

- `vertical`: Bull/bear call/put spreads
- `iron_structure`: Iron condors, iron butterflies
- `calendar`: Calendar spreads, diagonals
- `stock_combo`: Covered calls, collars
- `butterfly`: Call/put butterflies
- `short_option`: Naked calls/puts
- `long_option`: Long calls/puts
- `stock`: Stock positions
- `future`: Futures positions

### Performance

- **Strategy snapshot**: ~0.5ms (cached, updated on position refresh)
- **Validation**: ~5-10ms (CPU-bound, no I/O)
- **Optimization**: ~10-20ms (includes margin calculations)

## Future Enhancements

### Potential Additions

1. **Risk-adjusted optimization**: Consider Greeks when suggesting adjustments
2. **Historical performance**: Compare current strategies to past similar positions
3. **Implied volatility optimization**: Suggest adjustments based on IV skew
4. **Tax-aware suggestions**: Consider wash sale rules and tax lots
5. **Correlation analysis**: Identify redundant exposures across underlyings

### Integration Opportunities

- Expose via REST API for external tools
- Add to dashboard as "Strategy Health" widget
- Create automated alerts for validation failures
- Integrate with order entry for one-click suggestions

## Rollback Information

**If issues arise**, remove these sections from `desktop/ui/ai_risk_tab.py`:

1. Lines 89-97 (parameter models)
2. Lines 1089-1327 (tool handlers)
3. Lines 1430-1465 (tool registration - remove 3 tools from list)

**Delete test file**:

```bash
rm desktop/tests/test_ai_risk_strategy_tools.py
```

**Revert tool count**: Change line 574 in `test_ai_risk_tab_tools.py` from 17 back to 14

## Notes

- All tools use the existing `StrategyReconstructor` engine (already tested and validated)
- No database schema changes required (uses in-memory strategy cache)
- Tools are non-destructive (read-only analysis, no position modifications)
- Suggestions include all necessary leg details for easy execution
- Tool call logging integrated with existing AI Risk tab logging infrastructure
