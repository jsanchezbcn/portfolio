"""models/order.py — Data models for the execution engine (003-algo-execution-platform).

Dataclasses used by ExecutionEngine, OrderBuilder, TradeJournal, and AIRiskAnalyst.
These are pure in-memory models; persistence is handled by database/local_store.py.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class OrderAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    MOC = "MOC"  # Market-on-Close — equities/ETFs only; not supported for options


class OptionRight(str, Enum):
    CALL = "C"
    PUT = "P"


class OrderStatus(str, Enum):
    """Finite-state machine for order lifecycle."""
    DRAFT = "DRAFT"          # Being built in the order builder
    SIMULATED = "SIMULATED"  # WhatIf simulation completed
    STAGED = "STAGED"        # Staged in broker but not transmitted
    PENDING = "PENDING"      # Submitted to broker, awaiting fill
    SUBMITTED = "SUBMITTED"  # Submitted to broker
    FILLED = "FILLED"        # Fully filled
    PARTIAL = "PARTIAL"      # Partially filled — remainder still PENDING
    PARTIAL_FILL = "PARTIAL_FILL" # Partially filled
    REJECTED = "REJECTED"    # Broker rejected the order
    CANCELLED = "CANCELLED"  # User or broker cancelled
    CANCELED = "CANCELED"    # User or broker cancelled


# Valid FSM transitions
_ALLOWED_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.DRAFT:      {OrderStatus.SIMULATED},
    OrderStatus.SIMULATED:  {OrderStatus.DRAFT, OrderStatus.PENDING},
    OrderStatus.PENDING:    {OrderStatus.FILLED, OrderStatus.PARTIAL, OrderStatus.REJECTED, OrderStatus.CANCELLED},
    OrderStatus.PARTIAL:    {OrderStatus.FILLED, OrderStatus.CANCELLED},
    OrderStatus.FILLED:     set(),
    OrderStatus.REJECTED:   set(),
    OrderStatus.CANCELLED:  set(),
}


def validate_status_transition(current: OrderStatus, new: OrderStatus) -> None:
    """Raise ValueError if the transition current→new is not allowed."""
    if new not in _ALLOWED_TRANSITIONS.get(current, set()):
        raise ValueError(
            f"Invalid order status transition: {current} → {new}. "
            f"Allowed from {current}: {_ALLOWED_TRANSITIONS[current]}"
        )


# ---------------------------------------------------------------------------
# Core data models
# ---------------------------------------------------------------------------


@dataclass
class OrderLeg:
    """One component of a multi-leg order (1–4 legs max)."""

    # Instrument identity
    symbol: str                         # e.g. "SPX   260321C05200" or "AAPL"
    action: OrderAction                 # BUY or SELL
    quantity: int                       # Positive integer; direction encoded in action

    # Option-specific (None for equities/futures)
    option_right: Optional[OptionRight] = None
    strike: Optional[float] = None
    expiration: Optional[date] = None
    conid: Optional[str] = None         # IBKR contract ID

    # Book-keeping
    fill_price: Optional[float] = None  # Populated after fill confirmation


@dataclass
class PortfolioGreeks:
    """Aggregate beta-weighted Greek snapshot for the full portfolio."""

    spx_delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # Convenience
    @property
    def delta_theta_ratio(self) -> Optional[float]:
        """Θ/Δ income-to-risk efficiency ratio. None when delta == 0."""
        if self.spx_delta == 0.0:
            return None
        return self.theta / self.spx_delta

    @property
    def sebastian_ratio(self) -> Optional[float]:
        """|Θ|/|V| — Sebastian ratio; target 0.25–0.40. None when vega or theta == 0."""
        if self.vega == 0.0 or self.theta == 0.0:
            return None
        return abs(self.theta) / abs(self.vega)


@dataclass
class SimulationResult:
    """Output of ExecutionEngine.simulate(): margin and projected post-trade state."""

    # WhatIf API fields (None when simulation produced an error)
    margin_requirement: Optional[float] = None  # Projected Initial Margin after the order (USD)
    equity_before: Optional[float] = None
    equity_after: Optional[float] = None

    # Computed post-trade Greeks (current portfolio + this order)
    post_trade_greeks: Optional[PortfolioGreeks] = None

    # Risk gate
    delta_breach: bool = False          # True if post_trade_greeks.spx_delta > max_portfolio_delta

    # Exchange error / timeout
    error: Optional[str] = None        # Set when simulation failed; order submission must be blocked


@dataclass
class RiskBreach:
    """A detected violation of a risk limit that triggers the AI analyst."""

    breach_type: str                    # e.g. "vega_floor_high_vix", "delta_cap", "gamma_0_7_dte"
    threshold_value: float             # The configured limit
    actual_value: float                # The current portfolio value that breached the limit
    regime: str                        # Regime at time of breach
    vix: float                         # VIX at time of breach
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class AITradeSuggestion:
    """One AI-generated remediation trade from LLMRiskAuditor.suggest_trades()."""

    suggestion_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    legs: list[OrderLeg] = field(default_factory=list)

    # Projected portfolio impact
    projected_delta_change: float = 0.0
    projected_theta_cost: float = 0.0  # Negative = theta earned; positive = theta spent

    # Narrative
    rationale: str = ""


@dataclass
class Order:
    """A pending or completed trade instruction (1–4 legs)."""

    ORDER_MAX_LEGS = 4

    order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    legs: list[OrderLeg] = field(default_factory=list)
    order_type: OrderType = OrderType.LIMIT
    status: OrderStatus = OrderStatus.DRAFT

    # Filled in after simulate()
    simulation_result: Optional[SimulationResult] = None

    # Filled in after submit()
    broker_order_id: Optional[str] = None
    submitted_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None

    # User context (from order builder textarea)
    user_rationale: str = ""

    # Rejection / error context
    rejection_reason: Optional[str] = None  # Populated when status == REJECTED

    # AI context (if trade originated from a suggestion)
    ai_suggestion_id: Optional[str] = None
    ai_rationale: Optional[str] = None

    def __post_init__(self) -> None:
        if len(self.legs) < 1:
            raise ValueError("Order must have at least 1 leg.")
        if len(self.legs) > self.ORDER_MAX_LEGS:
            raise ValueError(
                f"Order has {len(self.legs)} legs — maximum is {self.ORDER_MAX_LEGS}."
            )

    def transition_to(self, new_status: OrderStatus) -> None:
        """Advance order status with FSM validation."""
        validate_status_transition(self.status, new_status)
        self.status = new_status

    @property
    def is_multi_leg(self) -> bool:
        return len(self.legs) > 1

    @property
    def has_option_legs(self) -> bool:
        return any(leg.option_right is not None for leg in self.legs)


# ---------------------------------------------------------------------------
# Journal & snapshot records (stored in SQLite via local_store.py)
# ---------------------------------------------------------------------------


@dataclass
class TradeJournalEntry:
    """Full-fidelity record of one completed fill — maps to the trade_journal table."""

    # Identity (auto-generated if not supplied)
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=lambda: datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))

    # Broker context
    broker: str = ""                    # "IBKR" | "TASTYTRADE"
    account_id: str = ""
    broker_order_id: Optional[str] = None

    # Instrument summary
    underlying: str = ""               # "SPX", "AAPL", "/ES"
    strategy_tag: Optional[str] = None # "iron_condor" | "short_strangle" | "FLATTEN" etc.
    status: str = "FILLED"             # "FILLED" | "PARTIAL" | "CANCELLED"

    # Multi-leg payload (serialised JSON array when stored)
    legs_json: str = "[]"

    # Net cost (positive = debit paid, negative = credit received)
    net_debit_credit: Optional[float] = None

    # Market context at fill time
    vix_at_fill: Optional[float] = None
    spx_price_at_fill: Optional[float] = None
    regime: Optional[str] = None       # "LOW_VOL" | "MEDIUM_VOL" | "HIGH_VOL" | "CRISIS"

    # Full portfolio Greek snapshots (NOT just this trade's contribution)
    pre_greeks_json: str = "{}"        # serialised PortfolioGreeks
    post_greeks_json: str = "{}"

    # Rationale
    user_rationale: Optional[str] = None
    ai_rationale: Optional[str] = None
    ai_suggestion_id: Optional[str] = None


@dataclass
class AccountSnapshot:
    """15-minute time-series record — maps to the account_snapshots table."""

    snapshot_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    captured_at: str = field(default_factory=lambda: datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))

    account_id: str = ""
    broker: str = ""

    # Account value
    net_liquidation: Optional[float] = None
    cash_balance: Optional[float] = None

    # Aggregate portfolio Greeks (SPX beta-weighted)
    spx_delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None

    # Delta/Theta efficiency ratio (pre-computed, None when delta == 0)
    delta_theta_ratio: Optional[float] = None

    # Market context
    vix: Optional[float] = None
    spx_price: Optional[float] = None
    regime: Optional[str] = None
