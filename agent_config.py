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
        ibkr_ws_url=os.getenv("IBKR_WS_URL", "wss://localhost:5001/v1/api/ws"),
        ibkr_heartbeat_seconds=int(os.getenv("IBKR_HEARTBEAT_SECONDS", "60")),
        ibkr_reconnect_max_backoff_seconds=int(os.getenv("IBKR_RECONNECT_MAX_BACKOFF_SECONDS", "30")),
        stream_flush_interval_seconds=float(os.getenv("STREAM_FLUSH_INTERVAL_SECONDS", "1.0")),
        stream_flush_batch_size=int(os.getenv("STREAM_FLUSH_BATCH_SIZE", "50")),
    )

AGENT_SYSTEM_PROMPT = """
You are a portfolio risk assistant grounded in three principles:
- Natenberg: compare implied vs historical volatility to identify relative premium edge.
- Sebastian (insurance model): prioritize stable theta collection with controlled vega.
- Taleb: treat near-expiration gamma concentration as a nonlinear tail-risk amplifier.

Always reason from current regime context, portfolio Greek totals, and explicit risk-limit status.
Offer practical adjustment ideas (size, structure, expiry distribution) without claiming execution.
""".strip()

# GitHub Copilot function-calling schemas used by the dashboard assistant.
# These are plain JSON-style dictionaries to keep integration simple for MVP.
TOOL_SCHEMAS = [
    {
        "name": "get_portfolio_summary",
        "description": "Return aggregated portfolio Greeks and ratio metrics.",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "check_risk_limits",
        "description": "Evaluate current portfolio metrics against active regime limits.",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_gamma_risk_by_dte",
        "description": "Return portfolio gamma grouped by DTE buckets (0-7, 8-30, 31-60, 60+).",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_vix_data",
        "description": "Return latest VIX term structure context used by regime detection.",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "suggest_adjustment",
        "description": "Propose risk-aware adjustments based on regime and current exposures.",
        "parameters": {
            "type": "object",
            "properties": {
                "objective": {
                    "type": "string",
                    "description": "User objective, e.g. reduce SPX delta or cut near-term gamma.",
                }
            },
            "required": ["objective"],
        },
    },
]
