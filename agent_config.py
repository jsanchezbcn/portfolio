from __future__ import annotations

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
