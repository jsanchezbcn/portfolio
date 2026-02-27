"""
agents/proposer_engine.py
──────────────────────────
Core engine for Feature 006: Trade Proposer.

Components:
    RiskRegimeLoader   – loads config/risk_matrix.yaml, applies VIX/TS scalers,
                         returns the active regime name + effective limit dict
    BreachDetector     – compares live Greeks against NLV-scaled limits and
                         returns a list of BreachEvent objects
    CandidateTrade     – value-object representing one proposed option strategy
    CandidateGenerator – builds SPX/SPY/ES option candidates via IBKRAdapter
    ProposerEngine     – orchestrates generation, scoring, ranking, persistence

Design principles:
    - FR-011: Only SPX, SPY, and /ES allowed as benchmarks
    - FR-014: Supersede logic — all Pending rows are voided before new batch
    - Efficiency formula:
        score = weighted_risk_reduction / (max(init_margin_impact, 1.0) + n_legs * arb_fee)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BENCHMARK_ALLOWLIST = frozenset({"SPX", "SPY", "ES"})   # FR-011
_RISK_MATRIX_PATH   = Path(__file__).parent.parent / "config" / "risk_matrix.yaml"


# ===========================================================================
# RiskRegimeLoader (T007)
# ===========================================================================

class RiskRegimeLoader:
    """Load config/risk_matrix.yaml and return regime + effective limits.

    All limits are NLV-scaled:
        effective_limit = ratio * nlv * vix_scaler * ts_scaler

    Usage::

        loader = RiskRegimeLoader()
        regime_name, limits = loader.get_effective_limits(
            vix=18.0, term_structure=1.02, recession_prob=0.3, nlv=100_000
        )
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _RISK_MATRIX_PATH
        self._config: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        with open(self._path) as fh:
            return yaml.safe_load(fh)

    def _vix_scaler(self, vix: float) -> float:
        """Return the VIX scaler for the given VIX level (highest threshold ≤ vix)."""
        scalers = sorted(
            self._config.get("vix_scalers", []),
            key=lambda x: x["vix_threshold"],
            reverse=True,
        )
        for entry in scalers:
            if vix >= entry["vix_threshold"]:
                return float(entry["scale"])
        return 1.0  # below all thresholds → no adjustment

    def _ts_scaler(self, term_structure: float) -> float:
        """Return the term-structure scaler (highest threshold ≤ term_structure)."""
        scalers = sorted(
            self._config.get("term_structure_scalers", []),
            key=lambda x: x["term_structure_threshold"],
            reverse=True,
        )
        for entry in scalers:
            if term_structure >= entry["term_structure_threshold"]:
                return float(entry["scale"])
        return 0.40  # deep backwardation floor

    def detect_regime(
        self,
        vix: float,
        term_structure: float = 1.0,
        recession_prob: float = 0.0,
    ) -> str:
        """Map market conditions to a regime name.

        Priority (highest to lowest):
            crisis_mode       VIX > 35
            high_volatility   VIX > 22 or recession > 0.40
            low_volatility    VIX < 15 and term_structure > 1.10
            neutral_volatility (default)
        """
        if vix > 35:
            return "crisis_mode"
        if vix > 22 or recession_prob > 0.40:
            return "high_volatility"
        if vix < 15 and term_structure > 1.10:
            return "low_volatility"
        return "neutral_volatility"

    def get_effective_limits(
        self,
        vix: float,
        term_structure: float = 1.0,
        recession_prob: float = 0.0,
        nlv: float = 0.0,
    ) -> tuple[str, dict[str, float]]:
        """Return (regime_name, effective_limits_dict) scaled to current market conditions.

        If *nlv* is 0 (unavailable), falls back to legacy absolute values.
        """
        regime_name = self.detect_regime(vix, term_structure, recession_prob)
        regime_cfg  = self._config["regimes"][regime_name]
        raw_limits  = regime_cfg["limits"]

        vs = self._vix_scaler(vix)
        ts = self._ts_scaler(term_structure)

        limits: dict[str, float] = {}

        if nlv > 0:
            # NLV-relative mode (primary)
            limits["min_daily_theta"]    = raw_limits.get("min_daily_theta_pct_nlv",    0.0) * nlv * vs * ts
            limits["max_negative_vega"]  = raw_limits.get("max_negative_vega_pct_nlv",  0.0) * nlv * vs * ts
            limits["max_spx_delta"]      = raw_limits.get("max_spx_delta_pct_nlv",      0.0) * nlv * vs * ts
            limits["max_gamma"]          = raw_limits.get("max_gamma_pct_nlv",          0.0) * nlv * vs * ts
        else:
            # Legacy absolute fallback
            limits["min_daily_theta"]    = raw_limits.get("legacy_min_daily_theta",    0.0) * vs * ts
            limits["max_negative_vega"]  = raw_limits.get("legacy_max_negative_vega",  0.0) * vs * ts
            limits["max_spx_delta"]      = raw_limits.get("legacy_max_beta_delta",     0.0) * vs * ts
            limits["max_gamma"]          = raw_limits.get("legacy_max_gamma",          0.0) * vs * ts

        # Non-Greek limits (not scaled by NLV)
        limits["max_position_contracts"]      = float(raw_limits.get("max_position_contracts", 100))
        limits["max_single_underlying_vega_pct"] = float(raw_limits.get("max_single_underlying_vega_pct", 1.0))
        limits["recession_probability_threshold"] = float(raw_limits.get("recession_probability_threshold", 0.4))

        # Fee constant for efficiency scoring
        limits["arb_fee_per_leg"] = float(self._config.get("arb_fee_per_leg", 0.65))

        return regime_name, limits


# ===========================================================================
# BreachEvent / BreachDetector (T008-T011)
# ===========================================================================

@dataclass
class BreachEvent:
    """Represents a single Greek/risk limit violation."""
    greek: str              # "vega" | "delta" | "theta" | "gamma" | "margin"
    current_value: float    # current portfolio value
    limit: float            # effective (NLV-scaled) limit
    distance_to_target: float  # signed overshoot  (negative = need less exposure)
    regime: str             # active regime name
    account_id: str = ""

    def __str__(self) -> str:
        return (
            f"BreachEvent({self.greek}: current={self.current_value:.2f}, "
            f"limit={self.limit:.2f}, distance={self.distance_to_target:.2f}, "
            f"regime={self.regime})"
        )


class BreachDetector:
    """Compare live portfolio Greeks against regime-adjusted NLV-scaled limits.

    Usage::

        detector = BreachDetector(RiskRegimeLoader())
        events = detector.check(greeks_snapshot, account_nlv=100_000)
    """

    def __init__(self, loader: RiskRegimeLoader) -> None:
        self._loader = loader

    # ── Public API ──────────────────────────────────────────────────────────

    def check(
        self,
        greeks_snapshot: dict[str, float],
        account_nlv: float = 0.0,
        account_id: str = "",
        margin_used: float = 0.0,
        max_margin_pct: float = 0.80,
    ) -> list[BreachEvent]:
        """Compare *greeks_snapshot* against all regime-adjusted limits.

        Args:
            greeks_snapshot: dict with keys like ``total_vega``, ``spx_delta``,
                             ``total_theta``, ``total_gamma``, ``vix``,
                             ``term_structure``, ``recession_prob``
            account_nlv:     Net Liquidating Value in USD (0 → legacy fallbacks)
            account_id:      IBKR account identifier (attached to BreachEvents)
            margin_used:     Current initial margin used (for T011 guard)
            max_margin_pct:  Maximum acceptable margin_used / nlv ratio

        Returns:
            Sorted list of BreachEvent objects (most severe first)
        """
        vix             = float(greeks_snapshot.get("vix",            18.0))
        term_structure  = float(greeks_snapshot.get("term_structure",   1.0))
        recession_prob  = float(greeks_snapshot.get("recession_prob",   0.0))

        regime_name, limits = self._loader.get_effective_limits(
            vix=vix,
            term_structure=term_structure,
            recession_prob=recession_prob,
            nlv=account_nlv,
        )

        events: list[BreachEvent] = []

        # Vega breach: portfolio vega < max_negative_vega (limit is negative)
        total_vega      = float(greeks_snapshot.get("total_vega",  0.0))
        vega_limit      = limits["max_negative_vega"]
        if vega_limit < 0 and total_vega < vega_limit:
            events.append(BreachEvent(
                greek="vega",
                current_value=total_vega,
                limit=vega_limit,
                distance_to_target=self._distance_to_target(total_vega, vega_limit),
                regime=regime_name,
                account_id=account_id,
            ))
        elif vega_limit == 0 and total_vega < 0:
            # crisis_mode: no short vega allowed
            events.append(BreachEvent(
                greek="vega",
                current_value=total_vega,
                limit=vega_limit,
                distance_to_target=-total_vega,  # full amount must be bought back
                regime=regime_name,
                account_id=account_id,
            ))

        # Delta breach: abs(spx_delta) > max_spx_delta
        spx_delta  = float(greeks_snapshot.get("spx_delta", 0.0))
        delta_limit = limits["max_spx_delta"]
        if abs(spx_delta) > abs(delta_limit) and abs(delta_limit) > 0:
            events.append(BreachEvent(
                greek="delta",
                current_value=spx_delta,
                limit=delta_limit,
                distance_to_target=self._distance_to_target(abs(spx_delta), abs(delta_limit)),
                regime=regime_name,
                account_id=account_id,
            ))
        elif delta_limit == 0.0 and spx_delta != 0.0:
            events.append(BreachEvent(
                greek="delta",
                current_value=spx_delta,
                limit=0.0,
                distance_to_target=abs(spx_delta),
                regime=regime_name,
                account_id=account_id,
            ))

        # Theta breach: portfolio theta < min_daily_theta  (must earn at least X/day)
        total_theta  = float(greeks_snapshot.get("total_theta", 0.0))
        theta_limit  = limits["min_daily_theta"]
        if theta_limit > 0 and total_theta < theta_limit:
            events.append(BreachEvent(
                greek="theta",
                current_value=total_theta,
                limit=theta_limit,
                distance_to_target=self._distance_to_target(total_theta, theta_limit),
                regime=regime_name,
                account_id=account_id,
            ))

        # Gamma breach: gamma > max_gamma
        total_gamma = float(greeks_snapshot.get("total_gamma", 0.0))
        gamma_limit = limits["max_gamma"]
        if gamma_limit > 0 and abs(total_gamma) > gamma_limit:
            events.append(BreachEvent(
                greek="gamma",
                current_value=total_gamma,
                limit=gamma_limit,
                distance_to_target=self._distance_to_target(abs(total_gamma), gamma_limit),
                regime=regime_name,
                account_id=account_id,
            ))

        # Margin utilisation guard (T011)
        if account_nlv > 0 and margin_used > 0:
            margin_ratio = margin_used / account_nlv
            if margin_ratio > max_margin_pct:
                events.append(BreachEvent(
                    greek="margin",
                    current_value=margin_ratio,
                    limit=max_margin_pct,
                    distance_to_target=margin_ratio - max_margin_pct,
                    regime=regime_name,
                    account_id=account_id,
                ))

        # Sort by severity (largest distance first)
        events.sort(key=lambda e: abs(e.distance_to_target), reverse=True)
        return events

    # ── Private helpers ─────────────────────────────────────────────────────

    def _detect_regime(
        self,
        vix: float,
        term_structure: float = 1.0,
        recession_prob: float = 0.0,
    ) -> str:
        """Public-ish wrapper around loader.detect_regime for direct testing."""
        return self._loader.detect_regime(vix, term_structure, recession_prob)

    @staticmethod
    def _distance_to_target(current: float, limit: float) -> float:
        """Return signed overshoot: positive means current exceeds limit.

        Examples:
            current=-6000, limit=-4800  →  -1200  (need to reduce short vega by 1200)
            current=50, limit=30        →  +20    (need to reduce delta by 20)
        """
        return current - limit


# ===========================================================================
# CandidateTrade (T013 prerequisite)
# ===========================================================================

@dataclass
class CandidateTrade:
    """Value object representing one candidate option strategy to propose."""
    underlying: str
    strategy_name: str
    legs: list[dict[str, Any]] = field(default_factory=list)  # leg dicts for legs_json

    # Greeks improvement projections
    delta_reduction: float = 0.0
    vega_reduction: float = 0.0

    # Margin data (from simulate_margin_impact)
    init_margin_impact: float = 0.0
    maint_margin_impact: float = 0.0

    # P&L
    net_premium: float = 0.0

    # Computed scoring
    efficiency_score: float = 0.0
    justification: str = ""

    # Which breach this candidate addresses
    breach: Optional[BreachEvent] = None


# ===========================================================================
# CandidateGenerator (T013)
# ===========================================================================

class CandidateGenerator:
    """Generate simple hedging candidate structures for benchmarks (SPX/SPY/ES).

    In production the candidates are built from real option chain data.  In test
    mode (MOCK_BREACH=TRUE) synthetic candidates are returned so the engine can
    run without live market data.
    """

    # Simple static strike offsets for candidate generation (% from ATM)
    _PUT_SPREAD_WING  = 0.02  # 2% OTM for short put
    _PUT_SPREAD_FLOOR = 0.04  # 4% OTM for long put (wider spread)

    def __init__(self, adapter: Any = None) -> None:
        """
        Args:
            adapter: IBKRAdapter instance.  If None, only mock candidates are
                     returned (useful for tests).
        """
        self._adapter = adapter

    def fetch_benchmark_options(
        self,
        underlying: str,
        dte_min: int = 30,
        dte_max: int = 60,
        atm_price: float = 0.0,
        breach: Optional[BreachEvent] = None,
    ) -> list[CandidateTrade]:
        """Build candidate trades for *underlying*.

        FR-011: only SPX, SPY, or ES allowed.

        Returns synthetic candidates (no live API call) so the engine can score
        them even when market data is unavailable.  The SimulateMarginImpact call
        in ProposerEngine will populate the real margin figures.
        """
        if underlying not in BENCHMARK_ALLOWLIST:
            logger.warning("fetch_benchmark_options: %s not in allowlist, skipping", underlying)
            return []

        if not atm_price:
            # Use rough defaults so candidates can be built without live prices
            atm_price = {"SPX": 5500.0, "SPY": 550.0, "ES": 5500.0}.get(underlying, 5500.0)

        strike_short = round(atm_price * (1 - self._PUT_SPREAD_WING / 100), -1)
        strike_long  = round(atm_price * (1 - self._PUT_SPREAD_FLOOR / 100), -1)

        # Midpoint DTE
        dte_target = (dte_min + dte_max) // 2

        candidates: list[CandidateTrade] = []

        if breach is None or breach.greek in ("vega", "delta"):
            # Bear put spread: buy lower put, sell higher put (reduces short delta + adds long vega)
            legs = [
                {
                    "symbol":   underlying,
                    "action":   "BUY",
                    "quantity": 1,
                    "right":    "P",
                    "strike":   strike_long,
                    "dte":      dte_target,
                    "conId":    0,   # will be resolved via IBKR API in production
                },
                {
                    "symbol":   underlying,
                    "action":   "SELL",
                    "quantity": 1,
                    "right":    "P",
                    "strike":   strike_short,
                    "dte":      dte_target,
                    "conId":    0,
                },
            ]
            candidates.append(CandidateTrade(
                underlying=underlying,
                strategy_name=f"{underlying} Bear Put Spread {dte_target} DTE",
                legs=legs,
                vega_reduction=50.0 * (1 if underlying == "SPX" else 0.1),
                delta_reduction=-5.0 * (1 if underlying == "SPX" else 0.1),
                breach=breach,
            ))

        if breach is None or breach.greek in ("vega",):
            # Calendar spread: buy far-dated put, sell near-dated put (long vega harvesting)
            legs_cal = [
                {
                    "symbol":   underlying,
                    "action":   "BUY",
                    "quantity": 1,
                    "right":    "P",
                    "strike":   strike_short,
                    "dte":      dte_max,
                    "conId":    0,
                },
                {
                    "symbol":   underlying,
                    "action":   "SELL",
                    "quantity": 1,
                    "right":    "P",
                    "strike":   strike_short,
                    "dte":      dte_min,
                    "conId":    0,
                },
            ]
            candidates.append(CandidateTrade(
                underlying=underlying,
                strategy_name=f"{underlying} Put Calendar {dte_min}/{dte_max} DTE",
                legs=legs_cal,
                vega_reduction=80.0 * (1 if underlying == "SPX" else 0.1),
                delta_reduction=0.0,
                breach=breach,
            ))

        return candidates

    async def get_candidates(
        self,
        underlying: str,
        breach: Optional[BreachEvent] = None,
        dte_min: int = 30,
        dte_max: int = 60,
        atm_price: float = 0.0,
    ) -> list[CandidateTrade]:
        """Return candidates using real TWS chain data when available; otherwise
        fall back to :meth:`fetch_benchmark_options` (synthetic strikes).

        When a live ``adapter.fetch_options_chain_tws`` call succeeds the legs
        in every returned :class:`CandidateTrade` carry real *conId*, *bid*,
        *ask*, and *mid* fields so that the downstream margin simulation and
        dashboard display both use real market prices.
        """
        if self._adapter is not None and callable(
            getattr(self._adapter, "fetch_options_chain_tws", None)
        ):
            try:
                chain = await self._adapter.fetch_options_chain_tws(
                    underlying=underlying,
                    dte_min=dte_min,
                    dte_max=dte_max,
                    atm_price=atm_price,
                    right="P",
                    n_strikes=4,
                )
                if chain:
                    real_candidates = self._build_from_real_chain(
                        chain, underlying, atm_price, breach
                    )
                    if real_candidates:
                        return real_candidates
            except Exception as exc:
                logger.warning(
                    "get_candidates: real chain failed for %s (%s) – using synthetic",
                    underlying, exc,
                )

        # Fallback: synthetic candidates (suitable for MOCK_BREACH / test mode)
        return self.fetch_benchmark_options(
            underlying=underlying,
            dte_min=dte_min,
            dte_max=dte_max,
            atm_price=atm_price,
            breach=breach,
        )

    def _build_from_real_chain(
        self,
        chain: list[dict],
        underlying: str,
        atm_price: float,
        breach: Optional[BreachEvent],
    ) -> list[CandidateTrade]:
        """Convert raw TWS chain dicts into :class:`CandidateTrade` objects.

        Bear Put Spread:
            - Short leg: nearest OTM put below ATM (highest strike < ATM)
            - Long leg:  farther OTM put (2nd strike below ATM)
            Net premium = (short_mid − long_mid) × multiplier  (debit for hedge)

        Put Calendar (synthetic fallback for vega breach):
            Requires two expirations; uses :meth:`fetch_benchmark_options` for
            the calendar leg since we only have one expiry from the chain call.
        """
        candidates: list[CandidateTrade] = []

        if not atm_price:
            atm_price = {"SPX": 5500.0, "SPY": 550.0, "ES": 5500.0}.get(underlying, 5500.0)

        # Puts strictly below ATM, sorted nearest-ATM first
        otm_puts = sorted(
            [c for c in chain if c["strike"] < atm_price],
            key=lambda c: c["strike"],
            reverse=True,
        )

        if (breach is None or breach.greek in ("vega", "delta")) and len(otm_puts) >= 2:
            short_data = otm_puts[0]
            long_data  = otm_puts[min(2, len(otm_puts) - 1)]
            mult = short_data.get("multiplier", 100)

            short_leg: dict = {
                "symbol":  underlying,
                "action":  "SELL",
                "quantity": 1,
                "right":   "P",
                "strike":  short_data["strike"],
                "dte":     short_data["dte"],
                "conId":   short_data["conId"],
                "expiry":  short_data["expiry"],
                "bid":     short_data["bid"],
                "ask":     short_data["ask"],
                "mid":     short_data["mid"],
            }
            long_leg: dict = {
                "symbol":  underlying,
                "action":  "BUY",
                "quantity": 1,
                "right":   "P",
                "strike":  long_data["strike"],
                "dte":     long_data["dte"],
                "conId":   long_data["conId"],
                "expiry":  long_data["expiry"],
                "bid":     long_data["bid"],
                "ask":     long_data["ask"],
                "mid":     long_data["mid"],
            }

            # Debit = cost of the spread (positive = credit received / negative = debit paid)
            net_prem = round((short_data["mid"] - long_data["mid"]) * mult, 2)

            candidates.append(CandidateTrade(
                underlying=underlying,
                strategy_name=(
                    f"{underlying} Bear Put Spread {short_data['dte']} DTE "
                    f"({short_data['strike']:.0f}/{long_data['strike']:.0f})"
                ),
                legs=[short_leg, long_leg],
                vega_reduction=50.0 * (1 if underlying == "SPX" else 0.1),
                delta_reduction=-5.0 * (1 if underlying == "SPX" else 0.1),
                net_premium=net_prem,
                breach=breach,
            ))

        # Put Calendar requires two expiries — add as synthetic complement for vega breaches
        if breach is None or breach.greek in ("vega",):
            for synth in self.fetch_benchmark_options(underlying=underlying, breach=breach):
                if "Calendar" in synth.strategy_name:
                    candidates.append(synth)

        return candidates


# ===========================================================================
# ProposerEngine (T014-T017)
# ===========================================================================

class ProposerEngine:
    """Orchestrate candidate generation → scoring → ranking → persistence.

    Usage::

        engine = ProposerEngine(adapter, loader)
        candidates = await engine.generate(breaches, account_id, nlv)
        engine.persist_top3(account_id, candidates, session)
    """

    def __init__(
        self,
        adapter: Any = None,
        loader: Optional[RiskRegimeLoader] = None,
    ) -> None:
        self._adapter   = adapter
        self._loader    = loader or RiskRegimeLoader()
        self._generator = CandidateGenerator(adapter)

    async def generate(
        self,
        breaches: list[BreachEvent],
        account_id: str,
        nlv: float = 0.0,
        atm_price: float = 0.0,
    ) -> list[CandidateTrade]:
        """For each breach, generate candidates, simulate margin, score, and rank.

        Args:
            breaches:   BreachEvent list from BreachDetector.check()
            account_id: IBKR account identifier
            nlv:        Net Liquidating Value (for available-margin filter, FR-007)
            atm_price:  SPX/SPY price for strike generation (0 = use defaults)

        Returns:
            Top-ranked candidates (up to 3, sorted by efficiency_score DESC)
        """
        if not breaches:
            return []

        _, limits = self._loader.get_effective_limits(
            vix=breaches[0].__dict__.get("_vix", 18.0),
            nlv=nlv,
        )
        arb_fee = limits.get("arb_fee_per_leg", 0.65)

        all_candidates: list[CandidateTrade] = []

        for breach in breaches[:3]:  # process top-3 breaches max
            for underlying in ("SPX", "ES"):  # primary benchmarks
                raw = await self._generator.get_candidates(
                    underlying=underlying,
                    breach=breach,
                    atm_price=atm_price,
                )
                for candidate in raw:
                    # Simulate margin impact via IBKR What-If API
                    if self._adapter is not None:
                        try:
                            margin_data = await self._adapter.simulate_margin_impact(
                                account_id, candidate.legs
                            )
                            candidate.init_margin_impact  = abs(margin_data.get("init_margin_change",  0.0))
                            candidate.maint_margin_impact = abs(margin_data.get("maint_margin_change", 0.0))
                        except Exception as exc:
                            logger.warning("simulate_margin_impact failed for %s: %s", candidate.strategy_name, exc)

                    # Compute efficiency score
                    candidate.efficiency_score = self._compute_efficiency_score(
                        candidate, breach, arb_fee
                    )
                    candidate.justification = _build_justification(breach, candidate)
                    all_candidates.append(candidate)

        return self._rank_candidates(all_candidates, available_margin=nlv * 0.20)

    def _compute_efficiency_score(
        self,
        candidate: CandidateTrade,
        breach: BreachEvent,
        arb_fee: float,
    ) -> float:
        """Efficiency = weighted_risk_reduction / (max(init_margin, 1) + n_legs * fee)."""
        risk_reduction = abs(candidate.vega_reduction) + abs(candidate.delta_reduction) * 10
        n_legs = len(candidate.legs)
        denominator = max(candidate.init_margin_impact, 1.0) + n_legs * arb_fee
        return risk_reduction / denominator

    def _rank_candidates(
        self,
        candidates: list[CandidateTrade],
        available_margin: float = 0.0,
    ) -> list[CandidateTrade]:
        """Sort by efficiency_score DESC; apply FR-007 margin filter; return top-3."""
        # FR-007: reject if margin exceeds available margin (when known)
        if available_margin > 0:
            candidates = [
                c for c in candidates
                if c.init_margin_impact <= available_margin or c.init_margin_impact == 0.0
            ]

        # Only keep candidates with positive score
        candidates = [c for c in candidates if c.efficiency_score > 0]

        candidates.sort(key=lambda c: c.efficiency_score, reverse=True)
        return candidates[:3]

    def persist_top3(
        self,
        account_id: str,
        candidates: list[CandidateTrade],
        session: Any,
    ) -> None:
        """Supersede all Pending rows then INSERT up to 3 new ProposedTrade rows.

        FR-014: Before inserting, all Pending rows for this account are set to
        Superseded so the dashboard always shows the freshest proposals.

        Args:
            account_id: IBKR account identifier.
            candidates: Ranked list from :meth:`generate` (max 3 used).
            session:    SQLModel/SQLAlchemy Session (sync or duck-typed mock).
        """
        from models.proposed_trade import ProposedTrade  # avoid circular at module level

        # Supersede existing Pending rows (FR-014)
        try:
            from sqlmodel import select, update
            stmt = (
                update(ProposedTrade)
                .where(ProposedTrade.account_id == account_id)
                .where(ProposedTrade.status == "Pending")
                .values(status="Superseded")
            )
            session.exec(stmt)
        except Exception as exc:
            # Fallback for plain SQLAlchemy sessions or mocks
            logger.debug("Using fallback supersede (not sqlmodel session): %s", exc)
            existing = session.query(ProposedTrade).filter_by(
                account_id=account_id, status="Pending"
            ).all()
            for row in existing:
                row.status = "Superseded"

        # Insert new candidates
        top3 = candidates[:3]
        for c in top3:
            if c.efficiency_score <= 0:
                continue
            trade = ProposedTrade(
                account_id=account_id,
                strategy_name=c.strategy_name,
                legs_json=c.legs,
                net_premium=c.net_premium,
                init_margin_impact=c.init_margin_impact,
                maint_margin_impact=c.maint_margin_impact,
                margin_impact=c.init_margin_impact,
                efficiency_score=c.efficiency_score,
                delta_reduction=c.delta_reduction,
                vega_reduction=c.vega_reduction,
                status="Pending",
                justification=c.justification,
                created_at=datetime.now(timezone.utc),
            )
            session.add(trade)

        try:
            session.commit()
            logger.info(
                "persist_top3: superseded existing Pending, inserted %d new ProposedTrade rows for %s",
                len(top3), account_id,
            )
        except Exception:
            session.rollback()
            raise


# ===========================================================================
# Helpers (T017)
# ===========================================================================

def _build_justification(breach: BreachEvent, candidate: CandidateTrade) -> str:
    """Build a human-readable justification string for a proposed trade."""
    greek_label = breach.greek.capitalize()
    score_str   = f"{candidate.efficiency_score:.2f}" if candidate.efficiency_score else "N/A"
    reduction   = abs(candidate.vega_reduction) if breach.greek == "vega" else abs(candidate.delta_reduction)

    return (
        f"Corrects {greek_label} breach "
        f"({breach.current_value:+.0f} vs {breach.limit:+.0f} limit) "
        f"in {breach.regime} regime. "
        f"Reduces {breach.greek} by ~{reduction:.0f}. "
        f"Score: {score_str}"
    )
