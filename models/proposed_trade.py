"""
models/proposed_trade.py
─────────────────────────
SQLModel definition for the `proposed_trades` table.

This is the canonical source of truth — matches
specs/006-trade-proposer/contracts/proposed_trade.py and data-model.md.

Status lifecycle:
    Pending  → Approved   (human action via dashboard)
    Pending  → Rejected   (human action via dashboard)
    Pending  → Superseded (automatic — new batch replaces all Pending rows)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlmodel import Column, Field, JSON, SQLModel


class ProposedTrade(SQLModel, table=True):
    __tablename__ = "proposed_trades"

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: str = Field(index=True)

    # Human-readable name, e.g. "SPX Bear Put Spread 45 DTE"
    strategy_name: str

    # Multi-leg structure stored as JSON list of dicts:
    # [{"conId": 123, "symbol": "SPX", "action": "BUY", "quantity": 1,
    #   "strike": 5000, "expiry": "2025-06-20", "right": "P"}]
    legs_json: List[Dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
    )

    # P&L & margin impact from IBKR What-If API
    net_premium: float = 0.0          # credit (+) or debit (-)
    init_margin_impact: float = 0.0   # Initial Margin change
    maint_margin_impact: float = 0.0  # Maintenance Margin change
    margin_impact: float = 0.0        # == init_margin_impact (summary field)

    # Risk improvement projections
    efficiency_score: float = 0.0
    delta_reduction: float = 0.0      # projected β-weighted SPX delta change
    vega_reduction: float = 0.0       # projected portfolio vega change

    # Review state — defaults to Pending, set by ProposerEngine or dashboard
    status: str = Field(default="Pending")  # Pending | Approved | Rejected | Superseded

    # Human-readable rationale, e.g.:
    # "Corrects Vega breach (-8,000 vs -4,800 limit) in neutral_volatility. Score: 0.72"
    justification: str = ""

    created_at: datetime = Field(default_factory=datetime.utcnow)
