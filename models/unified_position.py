from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


class InstrumentType(str, Enum):
    """Supported normalized instrument categories across brokers."""

    EQUITY = "EQUITY"
    OPTION = "OPTION"
    FUTURE = "FUTURE"
    FUTURE_OPTION = "FUTURE_OPTION"


class UnifiedPosition(BaseModel):
    """Normalized position payload used by risk and dashboard layers."""

    symbol: str
    instrument_type: InstrumentType
    broker: str
    quantity: float
    contract_multiplier: float = 1.0
    avg_price: float
    market_value: float
    unrealized_pnl: float

    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    spx_delta: float = 0.0

    underlying: str | None = None
    underlying_price: float | None = None  # Live price of the underlying (for options/futures: the index/stock price, NOT the option price)
    strike: float | None = None
    expiration: date | None = None
    option_type: Literal["call", "put"] | None = None
    iv: float | None = None
    days_to_expiration: int | None = None
    greeks_source: str = "none"
    beta_unavailable: bool = False  # True when no beta source found; SPX delta defaulted to assume beta=1.0

    # Broker-native identifier (conid for IBKR, OCC symbol for Tastytrade).
    # Used to look up live greeks via marketdata/snapshot without re-fetching positions.
    broker_id: str | None = None

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_option_fields(self) -> "UnifiedPosition":
        """Validate option-required fields and derive DTE when expiration is present."""

        if self.instrument_type == InstrumentType.OPTION:
            required = {
                "underlying": self.underlying,
                "strike": self.strike,
                "expiration": self.expiration,
                "option_type": self.option_type,
            }
            missing = [field for field, value in required.items() if value is None]
            if missing:
                raise ValueError(f"Option positions require fields: {', '.join(missing)}")

            if self.days_to_expiration is None and self.expiration is not None:
                self.days_to_expiration = (self.expiration - date.today()).days

        return self

    @property
    def dte_bucket(self) -> str:
        """Return DTE grouping used for gamma-risk aggregation."""

        if self.instrument_type != InstrumentType.OPTION or self.days_to_expiration is None:
            return "N/A"

        dte = self.days_to_expiration
        if dte <= 7:
            return "0-7"
        if dte <= 30:
            return "8-30"
        if dte <= 60:
            return "31-60"
        return "60+"


@dataclass
class BetaWeightedPosition:
    """A UnifiedPosition enriched with its SPX beta and computed SPX-equivalent delta.

    Produced by BetaWeighter.compute_spx_equivalent_delta(position).
    """

    position: UnifiedPosition
    beta: float                         # Beta value used for this position
    beta_source: str                    # "tastytrade" | "yfinance" | "beta_config" | "default"
    beta_unavailable: bool              # True → no source returned a beta; defaulted to 1.0
    spx_equivalent_delta: float        # (delta × qty × multiplier × beta × underlying_price) / spx_price
