from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class GreeksEstimate:
    delta: float
    gamma: float
    theta: float
    vega: float
    iv: float
    source: str = "estimated_bsm"


class GreeksEngine:
    """Local Black-Scholes fallback for missing option Greeks.

    The returned values are normalized to match the live IB greeks already used
    in the desktop app:
      - `delta` as unit delta per contract before quantity×multiplier scaling
      - `gamma` as unit gamma per contract before scaling
      - `theta` as daily theta
      - `vega` per 1 volatility-point move
    """

    def __init__(self, risk_free_rate: float = 0.01):
        self._risk_free_rate = float(risk_free_rate)

    def estimate(
        self,
        *,
        underlying_price: float,
        strike: float,
        expiry: date | None,
        right: str,
        iv: float,
        valuation_date: date | None = None,
    ) -> GreeksEstimate | None:
        s = float(underlying_price or 0.0)
        k = float(strike or 0.0)
        sigma = float(iv or 0.0)
        if s <= 0 or k <= 0 or sigma <= 0:
            return None

        t = self._time_to_expiry(expiry, valuation_date)
        if t <= 0:
            return None

        sqrt_t = math.sqrt(t)
        variance_term = sigma * sqrt_t
        if variance_term <= 0:
            return None

        d1 = (math.log(s / k) + (self._risk_free_rate + 0.5 * sigma * sigma) * t) / variance_term
        d2 = d1 - variance_term

        pdf_d1 = self._norm_pdf(d1)
        cdf_d1 = self._norm_cdf(d1)
        cdf_d2 = self._norm_cdf(d2)
        discount = math.exp(-self._risk_free_rate * t)

        right_up = str(right or "C").upper()
        if right_up == "P":
            delta = cdf_d1 - 1.0
            theta = (
                -(s * pdf_d1 * sigma) / (2.0 * sqrt_t)
                + self._risk_free_rate * k * discount * self._norm_cdf(-d2)
            ) / 365.0
        else:
            delta = cdf_d1
            theta = (
                -(s * pdf_d1 * sigma) / (2.0 * sqrt_t)
                - self._risk_free_rate * k * discount * cdf_d2
            ) / 365.0

        gamma = pdf_d1 / (s * variance_term)
        vega = (s * pdf_d1 * sqrt_t) / 100.0
        return GreeksEstimate(delta=delta, gamma=gamma, theta=theta, vega=vega, iv=sigma)

    @staticmethod
    def _time_to_expiry(expiry: date | None, valuation_date: date | None = None) -> float:
        if expiry is None:
            return 0.0
        today = valuation_date or date.today()
        days = max((expiry - today).days, 1)
        return days / 365.0

    @staticmethod
    def _norm_pdf(x: float) -> float:
        return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

    @staticmethod
    def _norm_cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))