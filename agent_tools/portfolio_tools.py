from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Any, Awaitable, Callable

from agent_tools.alert_dispatcher import AlertDispatcher, build_default_dispatcher
from core.processor import DataProcessor
from database.db_manager import DBManager, TradeEntry
from models.unified_position import InstrumentType, UnifiedPosition
from risk_engine.regime_detector import MarketRegime

LOGGER = logging.getLogger(__name__)


class PortfolioTools:
    """Portfolio-level analytics and risk checks."""

    def __init__(self, db_manager: DBManager | None = None, processor: DataProcessor | None = None) -> None:
        """Initialize internal memoized summary state."""

        self._last_summary: dict | None = None
        self.db_manager = db_manager
        self.processor = processor
        # Issue 7: track start-of-day net liquidation for daily drawdown guardrail
        self._start_of_day_net_liq: float | None = None
        # Issue 4: pluggable alert dispatcher (default: log only; Slack if SLACK_WEBHOOK_URL set)
        self.dispatcher: AlertDispatcher = build_default_dispatcher()

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

    def check_risk_limits(
        self,
        summary: dict,
        regime: MarketRegime,
        positions: list[UnifiedPosition] | None = None,
    ) -> list[dict]:
        """Compare summary metrics with active regime limits and return violations.

        Args:
            summary: Output of :meth:`get_portfolio_summary`.
            regime: Active :class:`MarketRegime`.
            positions: Optional list of positions — enables the per-position size check
                (Issue 6) when provided.

        Returns:
            List of violation dicts.  Each dict has ``metric``, ``current``,
            ``limit``, and ``message`` keys.  Also dispatches via
            :attr:`dispatcher` when violations are found.
        """

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

        # Issue 13: regime-aware vega violation message
        if summary["total_vega"] < regime.limits.max_negative_vega:
            if regime.name == "crisis_mode":
                vega_message = "Portfolio must be vega-neutral or long in crisis mode"
            else:
                vega_message = "Short volatility exposure exceeds limit"
            violations.append(
                {
                    "metric": "Vega",
                    "current": summary["total_vega"],
                    "limit": regime.limits.max_negative_vega,
                    "message": vega_message,
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

        # Issue 6: per-position contract size check
        if positions is not None:
            for pos in positions:
                if abs(pos.quantity) > regime.limits.max_position_contracts:
                    violations.append(
                        {
                            "metric": "Position Size",
                            "symbol": pos.symbol,
                            "current": pos.quantity,
                            "limit": regime.limits.max_position_contracts,
                            "message": (
                                f"{pos.symbol} has {abs(pos.quantity):.0f} contracts"
                                f" (limit {regime.limits.max_position_contracts})"
                            ),
                        }
                    )

        # Issue 4: auto-dispatch whenever violations are found
        if violations:
            self.dispatcher.dispatch(f"Risk check [{regime.name}]", violations)

        return violations

    def get_gamma_risk_by_dte(self, positions: list[UnifiedPosition]) -> dict[str, float]:
        """Group gamma exposure by DTE bucket."""

        grouped: dict[str, float] = defaultdict(float)
        for position in positions:
            grouped[position.dte_bucket] += position.gamma
        return dict(grouped)

    # -------------------------------------------------------------------------
    # Issue 1: Pre-trade simulation guardrail
    # -------------------------------------------------------------------------
    def simulate_trade_impact(
        self,
        positions: list[UnifiedPosition],
        proposed: UnifiedPosition,
        regime: MarketRegime,
    ) -> dict[str, Any]:
        """Simulate adding *proposed* to the portfolio and check limit compliance.

        Returns::

            {
                'ok': bool,                   # True when no limits are breached
                'violations': list[dict],     # Same schema as check_risk_limits
                'projected': dict,            # Projected portfolio summary
            }
        """
        combined = positions + [proposed]
        projected = self.get_portfolio_summary(combined)
        violations = self.check_risk_limits(projected, regime, positions=combined)
        return {"ok": len(violations) == 0, "violations": violations, "projected": projected}

    # -------------------------------------------------------------------------
    # Issue 3: DTE expiry alarm (gamma spike protection)
    # -------------------------------------------------------------------------
    def check_dte_expiry_risk(self, positions: list[UnifiedPosition]) -> list[dict[str, Any]]:
        """Return DTE expiry alerts for short options with near-term expiration.

        Levels:
            ``CRITICAL`` — DTE ≤ 2 (extreme gamma risk)
            ``WARNING``  — DTE ≤ 5 (elevated gamma risk)
        """
        alerts: list[dict[str, Any]] = []
        for position in positions:
            if position.instrument_type != InstrumentType.OPTION:
                continue
            if position.quantity >= 0:  # long positions don't carry short-gamma risk
                continue
            dte = position.days_to_expiration
            if dte is None:
                continue
            if dte <= 2:
                alerts.append(
                    {
                        "level": "CRITICAL",
                        "symbol": position.symbol,
                        "dte": dte,
                        "quantity": position.quantity,
                        "message": (
                            f"Short option {position.symbol} expires in {dte} DTE — extreme gamma risk"
                        ),
                    }
                )
                LOGGER.critical(
                    "SHORT OPTION EXPIRY RISK: %s | DTE=%d | qty=%s",
                    position.symbol,
                    dte,
                    position.quantity,
                )
            elif dte <= 5:
                alerts.append(
                    {
                        "level": "WARNING",
                        "symbol": position.symbol,
                        "dte": dte,
                        "quantity": position.quantity,
                        "message": (
                            f"Short option {position.symbol} expires in {dte} DTE — elevated gamma risk"
                        ),
                    }
                )
        return alerts

    # -------------------------------------------------------------------------
    # Issue 5: Per-underlying vega concentration limit
    # -------------------------------------------------------------------------
    def check_concentration_risk(
        self,
        positions: list[UnifiedPosition],
        regime: MarketRegime,
    ) -> list[dict[str, Any]]:
        """Check whether a single underlying dominates total vega exposure."""
        violations: list[dict[str, Any]] = []
        total_abs_vega = sum(abs(pos.vega) for pos in positions)
        if total_abs_vega == 0.0:
            return violations

        vega_by_underlying: dict[str, float] = defaultdict(float)
        for position in positions:
            if position.underlying:
                vega_by_underlying[position.underlying] += position.vega

        max_pct = regime.limits.max_single_underlying_vega_pct
        for underlying, underlying_vega in vega_by_underlying.items():
            pct = abs(underlying_vega) / total_abs_vega
            if pct > max_pct:
                violations.append(
                    {
                        "metric": "Vega Concentration",
                        "underlying": underlying,
                        "pct": round(pct, 4),
                        "limit_pct": max_pct,
                        "message": (
                            f"{underlying} represents {pct:.1%} of total vega"
                            f" (limit {max_pct:.0%})"
                        ),
                    }
                )
        return violations

    # -------------------------------------------------------------------------
    # Issue 7: Daily P&L drawdown guardrail
    # -------------------------------------------------------------------------
    def set_start_of_day_net_liq(self, net_liq: float) -> None:
        """Record today's opening net liquidation value for drawdown tracking."""
        self._start_of_day_net_liq = float(net_liq)
        LOGGER.info("Start-of-day net liquidation set to %.2f", self._start_of_day_net_liq)

    def check_daily_drawdown(
        self,
        current_net_liq: float,
        max_loss_pct: float = 0.03,
    ) -> dict[str, Any] | None:
        """Return a violation dict when daily drawdown exceeds *max_loss_pct*, else None."""
        if self._start_of_day_net_liq is None or self._start_of_day_net_liq <= 0:
            return None
        loss_pct = (self._start_of_day_net_liq - current_net_liq) / self._start_of_day_net_liq
        if loss_pct > max_loss_pct:
            violation: dict[str, Any] = {
                "metric": "Daily Drawdown",
                "loss_pct": round(loss_pct, 4),
                "limit_pct": max_loss_pct,
                "start_net_liq": self._start_of_day_net_liq,
                "current_net_liq": current_net_liq,
                "message": (
                    f"Daily P&L drawdown {loss_pct:.1%} exceeds {max_loss_pct:.0%} limit"
                    " — new trades blocked"
                ),
            }
            LOGGER.critical(
                "DAILY DRAWDOWN BREACH: %.1f%% (limit %.0f%%) | start=%.2f current=%.2f",
                loss_pct * 100,
                max_loss_pct * 100,
                self._start_of_day_net_liq,
                current_net_liq,
            )
            return violation
        return None

    def get_iv_analysis(
        self,
        positions: list[UnifiedPosition],
        historical_volatility: dict[str, float],
        vix: float | None = None,
    ) -> list[dict]:
        """Compute IV-vs-HV signals for option positions with available volatility data.

        Issue 14: When *vix* is provided thresholds scale with the volatility environment
        so a 10-point IV premium in VIX-15 is treated as a stronger signal than in VIX-35.
        """
        # Issue 14: VIX-relative thresholds
        if vix is not None and vix > 0:
            strong_threshold = 0.10 + (vix / 100.0)
            moderate_threshold = strong_threshold * 0.65  # ~65% of the strong threshold
        else:
            strong_threshold = 0.15
            moderate_threshold = 0.10

        analysis: list[dict] = []
        for position in positions:
            if position.iv is None or not position.underlying:
                continue
            hv = historical_volatility.get(position.underlying)
            if hv is None:
                continue
            spread = position.iv - hv
            if position.iv > hv and spread >= strong_threshold:
                edge = "sell_strong"
                signal = "strong_sell_edge"
                color = "green"
            elif position.iv > hv and spread >= moderate_threshold:
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

    async def query_greek_snapshots(
        self,
        *,
        broker: str | None = None,
        account_id: str | None = None,
        contract_key: str | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if self.db_manager is None:
            raise RuntimeError("DB manager is not configured")
        return await self.db_manager.fetch_snapshots(
            broker=broker,
            account_id=account_id,
            contract_key=contract_key,
            from_time=from_time,
            to_time=to_time,
            limit=limit,
        )

    async def record_trade(self, payload: dict[str, Any]) -> None:
        if self.db_manager is None:
            raise RuntimeError("DB manager is not configured")

        trade = TradeEntry(
            broker=str(payload["broker"]),
            account_id=str(payload["accountId"]),
            symbol=str(payload["symbol"]),
            contract_key=payload.get("contractKey"),
            action=str(payload["action"]),
            quantity=float(payload["quantity"]),
            price=float(payload["price"]) if payload.get("price") is not None else None,
            strategy_tag=payload.get("strategyTag"),
            metadata=payload.get("metadata") or {},
        )
        await self.db_manager.insert_trade(trade)

    def get_streaming_status(self) -> dict[str, Any]:
        if self.processor is None:
            return {"sessions": []}
        return {"sessions": self.processor.get_stream_sessions()}

    def start_streaming(
        self,
        *,
        brokers: list[str],
        stream_starters: dict[str, Callable[[], Awaitable[None]]],
    ) -> None:
        if self.processor is None:
            raise RuntimeError("Processor is not configured")

        for broker in brokers:
            starter = stream_starters.get(broker)
            if starter is None:
                continue
            self.processor.start_stream_task(broker, starter())

    async def stop_streaming(self, *, brokers: list[str] | None = None) -> None:
        if self.processor is None:
            return

        if brokers is None:
            await self.processor.stop_all_streams()
            return

        for broker in brokers:
            self.processor.stop_stream_task(broker)

    async def handle_streaming_start(
        self,
        payload: dict[str, Any],
        stream_starters: dict[str, Callable[[], Awaitable[None]]],
    ) -> tuple[dict[str, Any], int]:
        brokers = [str(item) for item in payload.get("brokers", [])]
        self.start_streaming(brokers=brokers, stream_starters=stream_starters)
        return {"accepted": True, "brokers": brokers}, 202

    async def handle_streaming_stop(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        brokers = payload.get("brokers")
        if brokers is not None:
            brokers = [str(item) for item in brokers]
        await self.stop_streaming(brokers=brokers)
        return {"accepted": True, "brokers": brokers or []}, 202

    def handle_streaming_status(self) -> tuple[dict[str, Any], int]:
        return self.get_streaming_status(), 200

    async def handle_greeks_snapshots(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        from_value = payload.get("from")
        to_value = payload.get("to")
        from_dt: datetime | None
        to_dt: datetime | None
        if isinstance(from_value, datetime):
            from_dt = from_value
        elif isinstance(from_value, str) and from_value:
            from_dt = datetime.fromisoformat(from_value)
        else:
            from_dt = None

        if isinstance(to_value, datetime):
            to_dt = to_value
        elif isinstance(to_value, str) and to_value:
            to_dt = datetime.fromisoformat(to_value)
        else:
            to_dt = None
        items = await self.query_greek_snapshots(
            broker=payload.get("broker"),
            account_id=payload.get("accountId"),
            contract_key=payload.get("contractKey"),
            from_time=from_dt,
            to_time=to_dt,
            limit=int(payload.get("limit", 1000)),
        )
        return {"items": items}, 200

    async def handle_trades_create(
        self,
        payload: dict[str, Any],
        positions: list[UnifiedPosition] | None = None,
        regime: MarketRegime | None = None,
    ) -> tuple[dict[str, Any], int]:
        """Record a trade.  Issue 1: if *positions* and *regime* are provided the trade is
        pre-validated via :meth:`simulate_trade_impact` and rejected (HTTP 422) when it
        would breach a risk limit.
        """
        # Issue 1: pre-trade simulation guardrail
        if positions is not None and regime is not None:
            proposed = UnifiedPosition(
                symbol=str(payload.get("symbol", "")),
                instrument_type=InstrumentType.OPTION
                if payload.get("contractKey")
                else InstrumentType.EQUITY,
                broker=str(payload.get("broker", "ibkr")),
                quantity=float(payload.get("quantity", 0)),
            )
            sim = self.simulate_trade_impact(positions, proposed, regime)
            if not sim["ok"]:
                return {
                    "error": "Trade would breach risk limits",
                    "violations": sim["violations"],
                    "projected": sim["projected"],
                }, 422
        await self.record_trade(payload)
        return {"created": True}, 201
