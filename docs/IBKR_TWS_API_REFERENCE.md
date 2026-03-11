# IBKR TWS API Reference Summary

> Source: https://ibkrcampus.com/campus/ibkr-api-page/twsapi-doc/
> Downloaded: 2026-02-26

## Overview

The TWS API is a TCP Socket Protocol API for connecting to Trader Workstation (TWS)
or IB Gateway. It supports Python, Java, C++, C#, and VB. We use **ib_async**
(third-party library) which wraps the official `ibapi` protocol.

**Connection defaults**: TWS Live=7496, TWS Paper=7497, IBG Live=4001, IBG Paper=4002.
Max 32 simultaneous client connections.

---

## Architecture

- **EClient**: Sends requests to TWS
- **EWrapper**: Receives responses from TWS
- **EReader**: Reads from socket, adds messages to queue
- ib_async wraps both into a single `IB` class with async/await

---

## Connection

```python
# ib_async style (what we use)
from ib_async import IB
ib = IB()
await ib.connectAsync("127.0.0.1", 7496, clientId=30, timeout=15)
print(ib.isConnected())
print(ib.managedAccounts())
ib.disconnect()
```

**Important**: `nextValidID` callback signals connection is ready.
Error 502 = TWS not running or wrong port.
Error 504 = Not connected.

---

## Account & Portfolio Data

### Account Summary

```python
# reqAccountSummary — subscribes, updates every 3 minutes
tags = await ib.accountSummaryAsync()
# Returns: account, tag, value, currency
```

**Key Tags**: `NetLiquidation`, `TotalCashValue`, `BuyingPower`, `InitMarginReq`,
`MaintMarginReq`, `UnrealizedPnL`, `RealizedPnL`, `AvailableFunds`,
`ExcessLiquidity`, `Cushion`, `GrossPositionValue`, `EquityWithLoanValue`

### Positions

```python
positions = ib.positions()
# Returns: account, contract, position, avgCost
```

### PnL

```python
# Account-level PnL
ib.reqPnL(reqId, accountId, "")
# Position-level PnL
ib.reqPnLSingle(reqId, accountId, "", conId)
# Returns: dailyPnL, unrealizedPnL, realizedPnL
```

---

## Contracts (Financial Instruments)

### Contract Types

| SecType | Description    |
| ------- | -------------- |
| STK     | Stock          |
| OPT     | Option         |
| FUT     | Future         |
| FOP     | Futures Option |
| CASH    | Forex          |
| BAG     | Combo/Spread   |
| IND     | Index          |
| BOND    | Bond           |
| CMDTY   | Commodity      |

### Contract Examples

```python
from ib_async import Stock, Option, Future, FuturesOption

# Stock
stock = Stock("AAPL", "SMART", "USD")

# Option
opt = Option("SPY", "20260320", 550, "C", "SMART", "USD")

# Future
fut = Future("ES", "202603", "CME")

# Futures Option (FOP)
fop = FuturesOption("ES", "20260320", 5500, "C", "CME", "USD")
```

### Contract Details

```python
details = await ib.reqContractDetailsAsync(contract)
# Returns: conId, symbol, localSymbol, tradingClass, exchange, primaryExchange,
#          minTick, orderTypes, validExchanges, underConId, etc.
```

### Option Chains

```python
chains = await ib.reqSecDefOptParamsAsync(
    underlyingSymbol="ES",
    futFopExchange="",
    underlyingSecType="FUT",
    underlyingConId=conId
)
# Returns: exchange, underlyingConId, tradingClass, multiplier, expirations, strikes
```

### Qualify Contracts

```python
qualified = await ib.qualifyContractsAsync(*contracts)
# Fills in conId and validates contracts — essential before orders
```

---

## Market Data

### Live Streaming (L1 Top of Book)

```python
ticker = ib.reqMktData(contract, genericTickList="", snapshot=False)
# Returns continuous updates: bid, ask, last, volume, etc.
await asyncio.sleep(2)  # wait for data
print(ticker.bid, ticker.ask, ticker.last)
ib.cancelMktData(contract)
```

### Snapshot

```python
ticker = ib.reqMktData(contract, genericTickList="", snapshot=True)
```

### Generic Tick Types (comma-separated string)

| ID  | Data                     |
| --- | ------------------------ |
| 100 | Option Volume            |
| 101 | Option Open Interest     |
| 104 | Historical Volatility    |
| 106 | Implied Volatility       |
| 165 | 13/26/52 week high/low   |
| 225 | Regulatory Imbalance     |
| 232 | Mark Price               |
| 233 | RT Volume                |
| 236 | Shortable                |
| 256 | Inventory                |
| 292 | News                     |
| 293 | Trade Count              |
| 411 | RT Historical Volatility |
| 456 | IB Dividends             |
| 577 | ETF NAV                  |
| 588 | Futures Open Interest    |
| 595 | Short-Term Volume        |

### Option Greeks (returned via tickOptionComputation)

- Tick 10 = Bid greeks
- Tick 11 = Ask greeks
- Tick 12 = Last greeks
- Tick 13 = Model greeks (most reliable)
- Fields: impliedVolatility, delta, gamma, vega, theta, optPrice, undPrice

### Market Data Types

| Type | Name           | Description                            |
| ---- | -------------- | -------------------------------------- |
| 1    | Live           | Real-time data (requires subscription) |
| 2    | Frozen         | Last recorded data at market close     |
| 3    | Delayed        | 15-20 min delayed (free)               |
| 4    | Delayed Frozen | Delayed + frozen                       |

```python
ib.reqMarketDataType(3)  # Switch to delayed data
```

---

## Historical Data

```python
bars = await ib.reqHistoricalDataAsync(
    contract,
    endDateTime="",          # empty = now
    durationStr="1 W",       # 1 W, 1 D, 1 M, 1 Y, etc.
    barSizeSetting="1 day",  # 1 secs, 5 secs, 1 min, 1 hour, 1 day, etc.
    whatToShow="TRADES",     # TRADES, MIDPOINT, BID, ASK, etc.
    useRTH=True,             # Regular Trading Hours only
    formatDate=1,
)
```

### Duration Strings

`S` (seconds), `D` (days), `W` (weeks), `M` (months), `Y` (years)

### Bar Sizes

1/5/10/15/30 secs, 1/2/3/5/10/15/20/30 mins, 1/2/3/4/8 hours, 1 day, 1W, 1M

### whatToShow Values

TRADES, MIDPOINT, BID, ASK, BID_ASK, ADJUSTED_LAST,
HISTORICAL_VOLATILITY, OPTION_IMPLIED_VOLATILITY, FEE_RATE, SCHEDULE

---

## Real-Time Bars (5-second)

```python
bars = ib.reqRealTimeBars(contract, 5, "TRADES", useRTH=False)
# Returns: time, open, high, low, close, volume, wap, count
```

---

## Tick-by-Tick Data

```python
# Types: "Last", "AllLast", "BidAsk", "MidPoint"
ib.reqTickByTickData(reqId, contract, "Last", 0, True)
```

Max subscriptions = 5% of total market data lines.

---

## Orders

### Order Types

LMT, MKT, STP, STP_LMT, MOC, LOC, MIT, LIT, TRAIL, TRAIL_LIMIT,
REL (pegged), MIDPX (midpoint), and more.

### Place Order

```python
from ib_async import LimitOrder, MarketOrder

order = LimitOrder("BUY", 1, 100.50)
trade = ib.placeOrder(contract, order)
# trade.orderStatus: status, filled, remaining, avgFillPrice
```

### WhatIf (Margin Impact Simulation)

```python
order = LimitOrder("BUY", 1, 100.50)
order.whatIf = True
trade = ib.placeOrder(contract, order)
# orderStatus: initMarginChange, maintMarginChange, equityWithLoanChange
```

### Combo / Spread Orders

```python
from ib_async import Contract, ComboLeg

bag = Contract()
bag.symbol = "ES"
bag.secType = "BAG"
bag.exchange = "CME"
bag.currency = "USD"
bag.comboLegs = [
    ComboLeg(conId=leg1_conId, ratio=1, action="BUY", exchange="CME"),
    ComboLeg(conId=leg2_conId, ratio=1, action="SELL", exchange="CME"),
]
```

### Open Orders

```python
orders = await ib.reqAllOpenOrdersAsync()
# Returns: order, contract, orderStatus
```

### Cancel Order

```python
ib.cancelOrder(order)
ib.reqGlobalCancel()  # Cancel all
```

---

## News

```python
# Available providers
providers = await ib.reqNewsProvidersAsync()

# Historical news
headlines = await ib.reqHistoricalNewsAsync(
    conId, providerCodes="BZ+FLY", startDateTime="", endDateTime="", totalResults=10
)

# News articles
article = await ib.reqNewsArticleAsync(providerCode, articleId)
```

---

## Market Scanner

```python
params = await ib.reqScannerParametersAsync()  # XML with all available scanners

# Subscribe to scanner
sub = ScannerSubscription(
    instrument="STK", locationCode="STK.US.MAJOR",
    scanCode="TOP_PERC_GAIN", numberOfRows=25
)
results = await ib.reqScannerSubscriptionAsync(sub)
```

---

## Error Codes (Key Ones)

| Code | Meaning                                      |
| ---- | -------------------------------------------- |
| 100  | Max message rate exceeded                    |
| 101  | Max tickers reached                          |
| 200  | No security definition found                 |
| 201  | Order rejected                               |
| 202  | Order cancelled                              |
| 502  | Can't connect (wrong port / TWS not running) |
| 504  | Not connected                                |
| 1100 | Connectivity lost to IB servers              |
| 1101 | Connectivity restored — data lost (resubmit) |
| 1102 | Connectivity restored — data maintained      |
| 2104 | Market data farm OK (informational)          |
| 2106 | HMDS data farm OK (informational)            |
| 2158 | Sec-def data farm OK (informational)         |

---

## Pacing Limits

- Historical data: max 60 requests in 10 minutes
- Identical historical requests: min 15 seconds apart
- Market data: ~50 messages/second max
- Order placement: no hard limit, but respect 50 msg/s

---

## Best Practices

1. Use **Offline TWS** to prevent auto-update breaking API version sync
2. Set **"Never Lock TWS"** + **Auto Restart** in Global Config → Lock and Exit
3. Set **memory allocation to 4000 MB** for API workloads
4. Enable **API message log file** for debugging (Global Config → API → Settings)
5. Use **client ID 0** to bind manual TWS orders
6. Always **qualify contracts** before placing orders
7. Handle **1100/1101/1102** reconnection events properly
8. Filter **2104/2106/2158** as informational, not errors

---

## ib_async (Third-Party Library We Use)

Recognized by IB as a third-party wrapper. Key differences from official `ibapi`:

- Async/await native (no EReader thread needed)
- `IB` class combines EClient + EWrapper
- `connectAsync()` instead of `connect()` + `run()`
- Methods return data directly instead of via callbacks
- Works with asyncio event loops (including qasync for PySide6)

---

## Port Configuration (Our Setup)

| Component              | Port | Client IDs |
| ---------------------- | ---- | ---------- |
| Streamlit positions    | 4001 | 11         |
| Streamlit greeks       | 4001 | 12         |
| Streamlit acct summary | 4001 | 13         |
| Streamlit WhatIf       | 4001 | 15         |
| Streamlit chain        | 4001 | 19         |
| Streamlit chain matrix | 4001 | 20         |
| Desktop app            | 4001 | 30         |
