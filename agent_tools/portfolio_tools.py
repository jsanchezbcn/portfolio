from __future__ import annotations

from collections import defaultdict

from models.unified_position import UnifiedPosition
from risk_engine.regime_detector import MarketRegime


class PortfolioTools:
    """Portfolio-level analytics and risk checks."""

    def __init__(self) -> None:
        """Initialize internal memoized summary state."""

        self._last_summary: dict | None = None

    def get_portfolio_summary(self, positions: list[UnifiedPosition]) -> dict:
        """Aggregate portfolio Greeks and compute Theta/Vega diagnostics."""

        totals = {
            "total_delta": sum(position.delta for position in positions),
            "total_gamma": sum(position.gamma for position in positions),
            "total_theta": sum(position.theta for position in positions),
            "total_vega": sum(position.vega for position in positions),
            "total_spx_delta": sum(position.spx_delta for position in positions),
            "position_count": len(positions),
        }

        abs_vega = abs(totals["total_vega"])
        theta_vega_ratio = abs(totals["total_theta"]) / abs_vega if abs_vega else 0.0
        totals["theta_vega_ratio"] = theta_vega_ratio

        if 0.25 <= theta_vega_ratio <= 0.40:
            totals["theta_vega_zone"] = "green"
        elif theta_vega_ratio < 0.20 or theta_vega_ratio > 0.50:
            totals["theta_vega_zone"] = "red"
        else:
            totals["theta_vega_zone"] = "neutral"

        self._last_summary = totals
        return totals

    def check_risk_limits(self, summary: dict, regime: MarketRegime) -> list[dict]:
        """Compare summary metrics with active regime limits and return violations."""

        violations: list[dict] = []

        if abs(summary["total_spx_delta"]) > regime.limits.max_beta_delta:
            violations.append(
                {
                    "metric": "SPX Delta",
                    "current": summary["total_spx_delta"],
                    "limit": regime.limits.max_beta_delta,
                    "message": "Directional risk exceeds regime limit",
                }
            )

        if summary["total_vega"] < regime.limits.max_negative_vega:
            violations.append(
                {
                    "metric": "Vega",
                    "current": summary["total_vega"],
                    "limit": regime.limits.max_negative_vega,
                    "message": "Short volatility exposure exceeds limit",
                }
            )

        if summary["total_theta"] < regime.limits.min_daily_theta:
            violations.append(
                {
                    "metric": "Theta",
                    "current": summary["total_theta"],
                    "limit": regime.limits.min_daily_theta,
                    "message": "Daily theta collection below minimum",
                }
            )

        if abs(summary["total_gamma"]) > regime.limits.max_gamma:
            violations.append(
                {
                    "metric": "Gamma",
                    "current": summary["total_gamma"],
                    "limit": regime.limits.max_gamma,
                    "message": "Gamma exposure exceeds regime limit",
                }
            )

        return violations

    def get_gamma_risk_by_dte(self, positions: list[UnifiedPosition]) -> dict[str, float]:
        """Group gamma exposure by DTE bucket."""

        grouped: dict[str, float] = defaultdict(float)
        for position in positions:
            grouped[position.dte_bucket] += position.gamma
        return dict(grouped)

    def get_iv_analysis(self, positions: list[UnifiedPosition], historical_volatility: dict[str, float]) -> list[dict]:
        """Compute IV-vs-HV signals for option positions with available volatility data."""

        analysis: list[dict] = []
        for position in positions:
            if position.iv is None or not position.underlying:
                continue
            hv = historical_volatility.get(position.underlying)
            if hv is None:
                continue
            spread = position.iv - hv
            if position.iv > hv and spread >= 0.15:
                edge = "sell_strong"
                signal = "strong_sell_edge"
                color = "green"
            elif position.iv > hv and spread >= 0.10:
                edge = "sell_moderate"
                signal = "moderate_sell_edge"
                color = "light_blue"
            elif position.iv < hv:
                edge = "buy"
                signal = "buy_edge"
                color = "blue"
            else:
                edge = "neutral"
                signal = "neutral"
                color = "neutral"

            analysis.append(
                {
                    "symbol": position.symbol,
                    "underlying": position.underlying,
                    "iv": position.iv,
                    "hv": hv,
                    "spread": spread,
                    "edge": edge,
                    "signal": signal,
                    "signal_color": color,
                }
            )
        return analysis
