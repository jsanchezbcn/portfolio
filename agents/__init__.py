"""
agents — autonomous background agents for the AI trading system.

Agents:
- NewsSentry: fetches news, scores sentiment via LLM, writes to market_intel
- ArbHunter: scans option chains for box-spread and put-call parity violations
"""
