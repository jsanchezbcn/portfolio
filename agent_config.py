from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency in some environments
    load_dotenv = None


@dataclass(slots=True)
class StreamingEnvironmentConfig:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_pass: str
    db_pool_min: int
    db_pool_max: int
    db_command_timeout: float
    ibkr_ws_url: str
    ibkr_heartbeat_seconds: int
    ibkr_reconnect_max_backoff_seconds: int
    stream_flush_interval_seconds: float
    stream_flush_batch_size: int


def load_streaming_environment(env_file: str = ".env") -> StreamingEnvironmentConfig:
    if load_dotenv is not None:
        load_dotenv(env_file, override=False)

    return StreamingEnvironmentConfig(
        db_host=os.getenv("DB_HOST", "localhost"),
        db_port=int(os.getenv("DB_PORT", "5432")),
        db_name=os.getenv("DB_NAME", "portfolio_engine"),
        db_user=os.getenv("DB_USER", "portfolio"),
        db_pass=os.getenv("DB_PASS", ""),
        db_pool_min=int(os.getenv("DB_POOL_MIN", "1")),
        db_pool_max=int(os.getenv("DB_POOL_MAX", "10")),
        db_command_timeout=float(os.getenv("DB_COMMAND_TIMEOUT", "10")),
        ibkr_ws_url=os.getenv("IBKR_WS_URL", "wss://localhost:5000/v1/api/ws"),
        ibkr_heartbeat_seconds=int(os.getenv("IBKR_HEARTBEAT_SECONDS", "60")),
        ibkr_reconnect_max_backoff_seconds=int(os.getenv("IBKR_RECONNECT_MAX_BACKOFF_SECONDS", "30")),
        stream_flush_interval_seconds=float(os.getenv("STREAM_FLUSH_INTERVAL_SECONDS", "1.0")),
        stream_flush_batch_size=int(os.getenv("STREAM_FLUSH_BATCH_SIZE", "50")),
    )

AGENT_SYSTEM_PROMPT = """
You are a portfolio risk assistant with full access to trading tools, grounded in three principles:
- Natenberg: compare implied vs historical volatility to identify relative premium edge.
- Sebastian (insurance model): prioritize stable theta collection with controlled vega.
- Taleb: treat near-expiration gamma concentration as a nonlinear tail-risk amplifier.

You have access to the full portfolio including per-position Greeks, account capital (NLV, buying power,
margin), market data (VIX, term structure, macro), options chains, and order submission.
Always reason from current regime context, portfolio Greek totals, and explicit risk-limit status.
Offer practical adjustment ideas (size, structure, expiry distribution) and can pre-fill orders.
Commission schedule: /ES=$1.40, /MES=$0.47, stock options=$0.65 per contract.
""".strip()

# GitHub Copilot function-calling schemas used by the dashboard assistant.
# Full tool access for the AI agent — includes portfolio, capital, market data, and order tools.
TOOL_SCHEMAS = [
    {
        "name": "get_portfolio_summary",
        "description": "Return aggregated portfolio Greeks (delta, theta, vega, gamma, SPX delta) and ratio metrics (theta/vega).",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_portfolio_positions",
        "description": "Return all portfolio positions with per-position Greeks, instrument type, strike, expiration, greeks_source, and staleness.",
        "parameters": {
            "type": "object",
            "properties": {
                "instrument_type": {
                    "type": "string",
                    "description": "Filter by type: OPTION, STOCK, FUTURE, or ALL.",
                    "enum": ["OPTION", "STOCK", "FUTURE", "ALL"],
                },
            },
        },
    },
    {
        "name": "get_account_capital",
        "description": "Return account capital details: net liquidation value, buying power, maintenance margin, excess liquidity, margin usage %.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "check_risk_limits",
        "description": "Evaluate current portfolio metrics against active regime limits. Returns list of violations.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_gamma_risk_by_dte",
        "description": "Return portfolio gamma grouped by DTE buckets (0-7, 8-30, 31-60, 60+).",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_iv_analysis",
        "description": "Return IV vs HV analysis for portfolio positions — identifies sell/buy edge candidates.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_vix_data",
        "description": "Return latest VIX, VIX3M, term structure, backwardation flag used by regime detection.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_macro_data",
        "description": "Return macro indicators including recession probability, source, and timestamp.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_options_chain",
        "description": "Fetch options chain for an underlying symbol and expiration. Returns strikes with bid/ask/mid/delta for puts and calls.",
        "parameters": {
            "type": "object",
            "properties": {
                "underlying": {
                    "type": "string",
                    "description": "Underlying symbol (e.g., ES, MES, SPY).",
                },
                "expiry": {
                    "type": "string",
                    "description": "Expiration date in YYYY-MM-DD format.",
                },
                "strikes_each_side": {
                    "type": "integer",
                    "description": "Number of strikes above and below ATM to include.",
                    "default": 6,
                },
            },
            "required": ["underlying", "expiry"],
        },
    },
    {
        "name": "get_market_quote",
        "description": "Get live bid/ask/mid/last quote for a symbol (stock, ETF, or future).",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Ticker symbol (e.g., SPY, ES, MES).",
                },
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_arbitrage_signals",
        "description": "Return active arbitrage signals sorted by fill probability, with commission-adjusted net edge.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "suggest_adjustment",
        "description": "Propose risk-aware adjustments based on regime and current exposures. Returns specific trade suggestions with legs.",
        "parameters": {
            "type": "object",
            "properties": {
                "objective": {
                    "type": "string",
                    "description": "User objective, e.g. 'reduce SPX delta', 'cut near-term gamma', 'improve theta/vega ratio'.",
                },
            },
            "required": ["objective"],
        },
    },
    {
        "name": "create_order_draft",
        "description": "Pre-fill the Order Builder with specified legs. User must still approve before submission.",
        "parameters": {
            "type": "object",
            "properties": {
                "legs": {
                    "type": "array",
                    "description": "Array of order legs.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["BUY", "SELL"]},
                            "symbol": {"type": "string"},
                            "quantity": {"type": "integer"},
                            "strike": {"type": "number"},
                            "right": {"type": "string", "enum": ["CALL", "PUT"]},
                            "expiry": {"type": "string", "description": "YYYY-MM-DD"},
                        },
                        "required": ["action", "symbol", "quantity"],
                    },
                },
                "rationale": {
                    "type": "string",
                    "description": "Journal entry explaining the trade rationale.",
                },
            },
            "required": ["legs"],
        },
    },
    {
        "name": "get_risk_regime",
        "description": "Return current market regime (low_volatility, neutral_volatility, high_volatility, crisis_mode) with VIX/term-structure context.",
        "parameters": {"type": "object", "properties": {}},
    },
]
