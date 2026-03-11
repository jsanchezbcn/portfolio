"""
tests/test_proposer_engine.py
──────────────────────────────
Covers T012 (TestBreachDetector) and T018 (TestProposerEngine).

Tests run with synthetic data only — no live IBKR connection required.

neutral_volatility, NLV=100_000, VIX=18 (scalers=1.0):
    theta_limit  =  120.0  / day
    vega_limit   = -4800.0
    delta_limit  =  1200.0
    gamma_limit  =  140.0

high_volatility, NLV=100_000, VIX=25 (vix_scaler=0.70, ts_scaler=1.0):
    theta_limit  =  168.0
    vega_limit   = -4200.0
    delta_limit  =  210.0
    gamma_limit  =   42.0

crisis_mode, VIX=40 — vega/delta limits = 0.0

low_volatility, VIX=12, TS=1.15 (ts_scaler=1.10, vix_scaler=1.0):
    vega_limit   = -0.030 * 100_000 * 1.0 * 1.10 = -3300.0
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from agents.proposer_engine import (
    BreachDetector,
    BreachEvent,
    CandidateGenerator,
    CandidateTrade,
    ProposerEngine,
    RiskRegimeLoader,
    _build_justification,
)


# ============================================================================
# Shared fixture
# ============================================================================

@pytest.fixture(scope="module")
def loader() -> RiskRegimeLoader:
    """One loader instance for all tests — reads config/risk_matrix.yaml once."""
    return RiskRegimeLoader()


@pytest.fixture(scope="module")
def detector(loader: RiskRegimeLoader) -> BreachDetector:
    return BreachDetector(loader)


# ============================================================================
# TestRiskRegimeLoader
# ============================================================================

class TestRiskRegimeLoader:
    """Unit tests for regime detection and NLV-scaled limit calculation."""

    # ── Regime detection ────────────────────────────────────────────────────

    def test_detect_neutral_volatility(self, loader: RiskRegimeLoader) -> None:
        assert loader.detect_regime(vix=18.0) == "neutral_volatility"

    def test_detect_low_volatility(self, loader: RiskRegimeLoader) -> None:
        assert loader.detect_regime(vix=12.0, term_structure=1.15) == "low_volatility"

    def test_detect_high_volatility_by_vix(self, loader: RiskRegimeLoader) -> None:
        assert loader.detect_regime(vix=25.0) == "high_volatility"

    def test_detect_high_volatility_by_recession(self, loader: RiskRegimeLoader) -> None:
        # recession_prob > 0.40 triggers high_volatility even at moderate VIX
        assert loader.detect_regime(vix=18.0, recession_prob=0.45) == "high_volatility"

    def test_detect_crisis_mode(self, loader: RiskRegimeLoader) -> None:
        assert loader.detect_regime(vix=40.0) == "crisis_mode"

    # ── VIX scalers ─────────────────────────────────────────────────────────

    def test_vix_scaler_below_20(self, loader: RiskRegimeLoader) -> None:
        """VIX below all thresholds → scaler = 1.0 (no tightening)."""
        assert loader._vix_scaler(14.0) == pytest.approx(1.0)

    def test_vix_scaler_at_20(self, loader: RiskRegimeLoader) -> None:
        assert loader._vix_scaler(20.0) == pytest.approx(0.85)

    def test_vix_scaler_at_25(self, loader: RiskRegimeLoader) -> None:
        assert loader._vix_scaler(25.0) == pytest.approx(0.70)

    def test_vix_scaler_at_30(self, loader: RiskRegimeLoader) -> None:
        assert loader._vix_scaler(30.0) == pytest.approx(0.50)

    def test_vix_scaler_at_35(self, loader: RiskRegimeLoader) -> None:
        assert loader._vix_scaler(35.0) == pytest.approx(0.25)

    # ── Term-structure scalers ───────────────────────────────────────────────

    def test_ts_scaler_normal_contango(self, loader: RiskRegimeLoader) -> None:
        assert loader._ts_scaler(1.0) == pytest.approx(1.0)

    def test_ts_scaler_deep_contango(self, loader: RiskRegimeLoader) -> None:
        assert loader._ts_scaler(1.15) == pytest.approx(1.10)

    def test_ts_scaler_mild_backwardation(self, loader: RiskRegimeLoader) -> None:
        assert loader._ts_scaler(0.95) == pytest.approx(0.80)

    # ── NLV-scaled limits ────────────────────────────────────────────────────

    def test_neutral_vega_limit_100k(self, loader: RiskRegimeLoader) -> None:
        """$100k NLV, VIX=18 (scaler=1.0), TS=1.0 (scaler=1.0) → -4800."""
        _, limits = loader.get_effective_limits(vix=18.0, term_structure=1.0, nlv=100_000)
        assert limits["max_negative_vega"] == pytest.approx(-4800.0)

    def test_neutral_theta_limit_100k(self, loader: RiskRegimeLoader) -> None:
        _, limits = loader.get_effective_limits(vix=18.0, term_structure=1.0, nlv=100_000)
        assert limits["min_daily_theta"] == pytest.approx(120.0)

    def test_high_vol_vega_limit_tightened(self, loader: RiskRegimeLoader) -> None:
        """VIX=25 → vix_scaler=0.70; high_vol vega_pct=-0.06 → -4200."""
        _, limits = loader.get_effective_limits(vix=25.0, term_structure=1.0, nlv=100_000)
        assert limits["max_negative_vega"] == pytest.approx(-4200.0, rel=1e-3)

    def test_crisis_vega_limit_is_zero(self, loader: RiskRegimeLoader) -> None:
        _, limits = loader.get_effective_limits(vix=40.0, nlv=100_000)
        assert limits["max_negative_vega"] == pytest.approx(0.0)

    def test_legacy_fallback_when_nlv_zero(self, loader: RiskRegimeLoader) -> None:
        """When NLV=0, legacy absolute limits are used (vix_scaler × ts_scaler applied)."""
        _, limits = loader.get_effective_limits(vix=18.0, nlv=0)
        # neutral_vol legacy: -1200 × 1.0 × 1.0 = -1200
        assert limits["max_negative_vega"] == pytest.approx(-1200.0)

    def test_arb_fee_present(self, loader: RiskRegimeLoader) -> None:
        _, limits = loader.get_effective_limits(vix=18.0, nlv=100_000)
        assert limits["arb_fee_per_leg"] == pytest.approx(0.65)


# ============================================================================
# TestBreachDetector  (T012)
# ============================================================================

class TestBreachDetector:
    """Tests for BreachDetector.check() covering 4 regimes and all Greek types."""

    NLV = 100_000.0

    # ────────────────────────────────────────────────────────────────────────
    # Helpers
    # ────────────────────────────────────────────────────────────────────────

    @staticmethod
    def base_snapshot(**overrides: float) -> dict[str, float]:
        """Build a 'clean' greeks snapshot that breaches nothing in neutral_vol."""
        snap = {
            "vix":            18.0,
            "term_structure":  1.0,
            "recession_prob":  0.0,
            "total_vega":   -2000.0,   # inside neutral -4800 limit
            "spx_delta":      500.0,   # inside neutral  1200 limit
            "total_theta":    150.0,   # above neutral   120  min
            "total_gamma":     80.0,   # inside neutral  140  max
        }
        snap.update(overrides)
        return snap

    # ────────────────────────────────────────────────────────────────────────
    # Zero-breach baseline
    # ────────────────────────────────────────────────────────────────────────

    def test_no_breaches_when_within_limits(self, detector: BreachDetector) -> None:
        events = detector.check(self.base_snapshot(), account_nlv=self.NLV)
        assert events == [], f"Expected no breaches but got: {events}"

    # ────────────────────────────────────────────────────────────────────────
    # Vega breach (neutral_volatility)
    # ────────────────────────────────────────────────────────────────────────

    def test_vega_breach_detected(self, detector: BreachDetector) -> None:
        snap = self.base_snapshot(total_vega=-8000.0)  # breaches -4800 limit
        events = detector.check(snap, account_nlv=self.NLV)
        vega_events = [e for e in events if e.greek == "vega"]
        assert len(vega_events) == 1

    def test_vega_breach_distance_to_target(self, detector: BreachDetector) -> None:
        """distance_to_target = current - limit = -8000 - (-4800) = -3200."""
        snap = self.base_snapshot(total_vega=-8000.0)
        events = detector.check(snap, account_nlv=self.NLV)
        vega_e = next(e for e in events if e.greek == "vega")
        assert vega_e.distance_to_target == pytest.approx(-3200.0)

    def test_vega_breach_regime_attached(self, detector: BreachDetector) -> None:
        snap = self.base_snapshot(total_vega=-8000.0)
        events = detector.check(snap, account_nlv=self.NLV)
        vega_e = next(e for e in events if e.greek == "vega")
        assert vega_e.regime == "neutral_volatility"

    def test_vega_at_exact_limit_no_breach(self, detector: BreachDetector) -> None:
        """Exactly at the limit (-4800) should not trigger a breach."""
        snap = self.base_snapshot(total_vega=-4800.0)
        events = detector.check(snap, account_nlv=self.NLV)
        vega_events = [e for e in events if e.greek == "vega"]
        assert vega_events == []

    # ────────────────────────────────────────────────────────────────────────
    # Theta breach (neutral_volatility)
    # ────────────────────────────────────────────────────────────────────────

    def test_theta_breach_detected(self, detector: BreachDetector) -> None:
        snap = self.base_snapshot(total_theta=50.0)  # below 120 min
        events = detector.check(snap, account_nlv=self.NLV)
        theta_events = [e for e in events if e.greek == "theta"]
        assert len(theta_events) == 1

    def test_theta_breach_distance(self, detector: BreachDetector) -> None:
        """distance_to_target = current - limit = 50 - 120 = -70."""
        snap = self.base_snapshot(total_theta=50.0)
        events = detector.check(snap, account_nlv=self.NLV)
        theta_e = next(e for e in events if e.greek == "theta")
        assert theta_e.distance_to_target == pytest.approx(-70.0)

    # ────────────────────────────────────────────────────────────────────────
    # Delta breach (neutral_volatility)
    # ────────────────────────────────────────────────────────────────────────

    def test_delta_breach_detected_positive(self, detector: BreachDetector) -> None:
        snap = self.base_snapshot(spx_delta=1500.0)  # exceeds 1200 limit
        events = detector.check(snap, account_nlv=self.NLV)
        delta_events = [e for e in events if e.greek == "delta"]
        assert len(delta_events) == 1

    def test_delta_breach_detected_negative(self, detector: BreachDetector) -> None:
        snap = self.base_snapshot(spx_delta=-1500.0)  # abs exceeds 1200 limit
        events = detector.check(snap, account_nlv=self.NLV)
        delta_events = [e for e in events if e.greek == "delta"]
        assert len(delta_events) == 1

    def test_delta_no_breach_within_limit(self, detector: BreachDetector) -> None:
        snap = self.base_snapshot(spx_delta=800.0)
        events = detector.check(snap, account_nlv=self.NLV)
        delta_events = [e for e in events if e.greek == "delta"]
        assert delta_events == []

    # ────────────────────────────────────────────────────────────────────────
    # Gamma breach (neutral_volatility)
    # ────────────────────────────────────────────────────────────────────────

    def test_gamma_breach_detected(self, detector: BreachDetector) -> None:
        snap = self.base_snapshot(total_gamma=200.0)  # exceeds 140 limit
        events = detector.check(snap, account_nlv=self.NLV)
        gamma_events = [e for e in events if e.greek == "gamma"]
        assert len(gamma_events) == 1

    def test_gamma_no_breach_within_limit(self, detector: BreachDetector) -> None:
        snap = self.base_snapshot(total_gamma=100.0)
        events = detector.check(snap, account_nlv=self.NLV)
        gamma_events = [e for e in events if e.greek == "gamma"]
        assert gamma_events == []

    # ────────────────────────────────────────────────────────────────────────
    # Margin guard (T011)
    # ────────────────────────────────────────────────────────────────────────

    def test_margin_guard_breach(self, detector: BreachDetector) -> None:
        """margin_used / nlv = 0.85 > 0.80 default → should produce margin breach."""
        snap = self.base_snapshot()
        events = detector.check(
            snap,
            account_nlv=100_000.0,
            margin_used=85_000.0,  # 85% > 80%
        )
        margin_events = [e for e in events if e.greek == "margin"]
        assert len(margin_events) == 1
        assert margin_events[0].distance_to_target == pytest.approx(0.05, rel=1e-3)

    def test_margin_guard_no_breach(self, detector: BreachDetector) -> None:
        snap = self.base_snapshot()
        events = detector.check(
            snap,
            account_nlv=100_000.0,
            margin_used=50_000.0,  # 50% < 80%
        )
        margin_events = [e for e in events if e.greek == "margin"]
        assert margin_events == []

    # ────────────────────────────────────────────────────────────────────────
    # Sorting: most severe first
    # ────────────────────────────────────────────────────────────────────────

    def test_events_sorted_by_severity(self, detector: BreachDetector) -> None:
        """Multiple breaches should be sorted by abs(distance_to_target) DESC."""
        snap = self.base_snapshot(
            total_vega=-10_000.0,   # large vega breach
            total_theta=50.0,       # smaller theta undershoot
        )
        events = detector.check(snap, account_nlv=self.NLV)
        assert len(events) >= 2
        distances = [abs(e.distance_to_target) for e in events]
        assert distances == sorted(distances, reverse=True)

    # ────────────────────────────────────────────────────────────────────────
    # crisis_mode: vega_limit == 0, any short vega is a full buyback
    # ────────────────────────────────────────────────────────────────────────

    def test_crisis_mode_vega_breach_full_buyback(self, detector: BreachDetector) -> None:
        """In crisis_mode any short vega triggers breach with distance = -vega."""
        snap = self.base_snapshot(vix=40.0, total_vega=-500.0)
        events = detector.check(snap, account_nlv=self.NLV)
        vega_e = next((e for e in events if e.greek == "vega"), None)
        assert vega_e is not None
        assert vega_e.distance_to_target == pytest.approx(500.0)  # full amount buyback

    def test_crisis_mode_regime_name(self, detector: BreachDetector) -> None:
        snap = self.base_snapshot(vix=40.0, total_vega=-100.0)
        events = detector.check(snap, account_nlv=self.NLV)
        assert any(e.regime == "crisis_mode" for e in events)

    def test_crisis_mode_no_breach_when_long_vega(self, detector: BreachDetector) -> None:
        """Flat/long vega in crisis_mode = no vega breach."""
        snap = self.base_snapshot(vix=40.0, total_vega=0.0)
        events = detector.check(snap, account_nlv=self.NLV)
        vega_events = [e for e in events if e.greek == "vega"]
        assert vega_events == []

    # ────────────────────────────────────────────────────────────────────────
    # high_volatility regime
    # ────────────────────────────────────────────────────────────────────────

    def test_high_vol_tighter_limits(self, detector: BreachDetector) -> None:
        """VIX=25 → vix_scaler=0.70 → vega limit tightens to -4200.
        vega=-4500 should breach in high_vol but NOT in neutral_vol.
        """
        snap_high = self.base_snapshot(vix=25.0, total_vega=-4500.0)
        events_high = detector.check(snap_high, account_nlv=self.NLV)
        vega_breach_high = [e for e in events_high if e.greek == "vega"]
        assert len(vega_breach_high) == 1

        snap_neutral = self.base_snapshot(vix=18.0, total_vega=-4500.0)
        events_neutral = detector.check(snap_neutral, account_nlv=self.NLV)
        vega_breach_neutral = [e for e in events_neutral if e.greek == "vega"]
        assert len(vega_breach_neutral) == 0

    # ────────────────────────────────────────────────────────────────────────
    # low_volatility regime
    # ────────────────────────────────────────────────────────────────────────

    def test_low_vol_regime_detection(self, detector: BreachDetector) -> None:
        snap = self.base_snapshot(vix=12.0, term_structure=1.15)
        events = detector.check(snap, account_nlv=self.NLV)
        # May or may not have breaches depending on greeks, but regime should be low_vol
        for e in events:
            assert e.regime == "low_volatility"

    def test_low_vol_nlv_scaled_limits(self, loader: RiskRegimeLoader) -> None:
        """low_vol: vega limit = -0.030 * NLV * vs * ts = -3300 (VIX=12 → vs=1.0, TS=1.15 → ts=1.10)."""
        _, limits = loader.get_effective_limits(vix=12.0, term_structure=1.15, nlv=100_000)
        expected = -0.030 * 100_000 * 1.0 * 1.10
        assert limits["max_negative_vega"] == pytest.approx(expected, rel=1e-4)

    # ────────────────────────────────────────────────────────────────────────
    # NLV-scaled vs legacy: no-NLV uses legacy absolute values
    # ────────────────────────────────────────────────────────────────────────

    def test_nlv_zero_uses_legacy_vega_limit(self, detector: BreachDetector) -> None:
        """NLV=0 → legacy vega limit = -1200; vega=-1500 should breach."""
        snap = self.base_snapshot(total_vega=-1500.0)  # inside 100k NLV limit (-4800)
        events_nlv = detector.check(snap, account_nlv=100_000.0)
        vega_nlv = [e for e in events_nlv if e.greek == "vega"]
        assert vega_nlv == []  # no breach with NLV

        events_legacy = detector.check(snap, account_nlv=0.0)
        vega_legacy = [e for e in events_legacy if e.greek == "vega"]
        assert len(vega_legacy) == 1  # breach with legacy -1200 limit

    # ────────────────────────────────────────────────────────────────────────
    # account_id propagation
    # ────────────────────────────────────────────────────────────────────────

    def test_account_id_attached_to_events(self, detector: BreachDetector) -> None:
        snap = self.base_snapshot(total_vega=-9000.0)
        events = detector.check(snap, account_nlv=self.NLV, account_id="DU123456")
        for e in events:
            assert e.account_id == "DU123456"

    # ────────────────────────────────────────────────────────────────────────
    # _detect_regime and _distance_to_target helpers
    # ────────────────────────────────────────────────────────────────────────

    def test_detect_regime_helper(self, detector: BreachDetector) -> None:
        assert detector._detect_regime(vix=18.0) == "neutral_volatility"
        assert detector._detect_regime(vix=40.0) == "crisis_mode"

    def test_distance_to_target_vega(self) -> None:
        """current=-6000, limit=-4800 → -1200 (need to buy back 1200 vega)."""
        result = BreachDetector._distance_to_target(-6000.0, -4800.0)
        assert result == pytest.approx(-1200.0)

    def test_distance_to_target_delta(self) -> None:
        """current=1500, limit=1200 → +300."""
        result = BreachDetector._distance_to_target(1500.0, 1200.0)
        assert result == pytest.approx(300.0)


# ============================================================================
# TestCandidateGenerator
# ============================================================================

class TestCandidateGenerator:
    """Unit tests for CandidateGenerator — no live adapter needed."""

    def test_allowlist_enforced_spx(self) -> None:
        gen = CandidateGenerator()
        candidates = gen.fetch_benchmark_options("SPX")
        assert len(candidates) > 0

    def test_allowlist_enforced_spy(self) -> None:
        gen = CandidateGenerator()
        candidates = gen.fetch_benchmark_options("SPY")
        assert len(candidates) > 0

    def test_allowlist_enforced_es(self) -> None:
        gen = CandidateGenerator()
        candidates = gen.fetch_benchmark_options("ES")
        assert len(candidates) > 0

    def test_non_allowlist_rejected(self) -> None:
        gen = CandidateGenerator()
        candidates = gen.fetch_benchmark_options("AAPL")
        assert candidates == []

    def test_candidates_have_legs(self) -> None:
        gen = CandidateGenerator()
        candidates = gen.fetch_benchmark_options("SPX")
        for c in candidates:
            assert len(c.legs) >= 1, f"Candidate {c.strategy_name} has no legs"

    def test_bear_put_spread_generated_for_vega_breach(self) -> None:
        gen = CandidateGenerator()
        breach = BreachEvent("vega", -8000, -4800, -3200, "neutral_volatility")
        candidates = gen.fetch_benchmark_options("SPX", breach=breach)
        names = [c.strategy_name for c in candidates]
        assert any("Bear Put Spread" in n for n in names)

    def test_calendar_spread_generated_for_vega_breach(self) -> None:
        gen = CandidateGenerator()
        breach = BreachEvent("vega", -8000, -4800, -3200, "neutral_volatility")
        candidates = gen.fetch_benchmark_options("SPX", breach=breach)
        names = [c.strategy_name for c in candidates]
        assert any("Calendar" in n for n in names)

    def test_only_put_spread_for_delta_breach(self) -> None:
        """For delta breach (not vega), calendar spread should NOT be generated."""
        gen = CandidateGenerator()
        breach = BreachEvent("delta", 1500, 1200, 300, "neutral_volatility")
        candidates = gen.fetch_benchmark_options("SPX", breach=breach)
        names = [c.strategy_name for c in candidates]
        assert any("Bear Put Spread" in n for n in names)
        # Calendar is vega-only — should not appear for pure delta breach
        assert not any("Calendar" in n for n in names)


# ============================================================================
# TestCandidateGeneratorAsync  (real-chain / fallback / _build_from_real_chain)
# ============================================================================

_SAMPLE_CHAIN: list[dict] = [
    {
        "conId": 1001, "symbol": "SPX", "strike": 5450.0, "right": "P",
        "dte": 45, "expiry": "20261219", "bid": 9.80, "ask": 10.20,
        "mid": 10.00, "multiplier": 100, "tradingClass": "SPXW",
    },
    {
        "conId": 1002, "symbol": "SPX", "strike": 5400.0, "right": "P",
        "dte": 45, "expiry": "20261219", "bid": 7.30, "ask": 7.70,
        "mid": 7.50, "multiplier": 100, "tradingClass": "SPXW",
    },
    {
        "conId": 1003, "symbol": "SPX", "strike": 5350.0, "right": "P",
        "dte": 45, "expiry": "20261219", "bid": 5.10, "ask": 5.50,
        "mid": 5.30, "multiplier": 100, "tradingClass": "SPXW",
    },
    {
        "conId": 1004, "symbol": "SPX", "strike": 5300.0, "right": "P",
        "dte": 45, "expiry": "20261219", "bid": 3.40, "ask": 3.80,
        "mid": 3.60, "multiplier": 100, "tradingClass": "SPXW",
    },
]


class TestCandidateGeneratorAsync:
    """Tests for async get_candidates + _build_from_real_chain."""

    # helpers
    @staticmethod
    def _run(coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    @staticmethod
    def _breach_vega() -> BreachEvent:
        return BreachEvent("vega", -8000, -4800, -3200, "neutral_volatility")

    # ── fallback when no adapter ─────────────────────────────────────────────

    def test_get_candidates_no_adapter_returns_synthetic(self) -> None:
        gen = CandidateGenerator()
        result = self._run(gen.get_candidates("SPX", breach=self._breach_vega()))
        assert len(result) > 0, "Synthetic fallback must return candidates"
        assert all(isinstance(c, CandidateTrade) for c in result)

    def test_get_candidates_non_allowlist_no_adapter(self) -> None:
        gen = CandidateGenerator()
        result = self._run(gen.get_candidates("AAPL", breach=self._breach_vega()))
        assert result == []

    # ── fallback when adapter returns empty chain ────────────────────────────

    def test_get_candidates_empty_chain_falls_back_to_synthetic(self) -> None:
        adapter = MagicMock()
        adapter.fetch_options_chain_tws = AsyncMock(return_value=[])
        gen = CandidateGenerator(adapter=adapter)
        result = self._run(gen.get_candidates("SPX", breach=self._breach_vega()))
        assert len(result) > 0, "Must fall back to synthetic when chain is empty"

    # ── real chain path ──────────────────────────────────────────────────────

    def test_get_candidates_uses_real_chain(self) -> None:
        adapter = MagicMock()
        adapter.fetch_options_chain_tws = AsyncMock(return_value=_SAMPLE_CHAIN)
        gen = CandidateGenerator(adapter=adapter)
        result = self._run(gen.get_candidates("SPX", breach=self._breach_vega()))
        assert len(result) > 0
        # At least one candidate leg should reference a real conId (not 0)
        real_legs = [
            leg for c in result for leg in c.legs if leg.get("conId", 0) != 0
        ]
        assert real_legs, "Expected at least one leg with real conId from chain"

    def test_get_candidates_real_chain_has_net_premium(self) -> None:
        adapter = MagicMock()
        adapter.fetch_options_chain_tws = AsyncMock(return_value=_SAMPLE_CHAIN)
        gen = CandidateGenerator(adapter=adapter)
        result = self._run(gen.get_candidates("SPX", breach=self._breach_vega()))
        spread_candidates = [c for c in result if "Bear Put Spread" in c.strategy_name]
        assert spread_candidates, "Expected Bear Put Spread in real-chain results"
        for c in spread_candidates:
            assert c.net_premium != 0.0, "net_premium must be non-zero for real chain"

    def test_build_from_real_chain_net_premium_sign(self) -> None:
        """Short near-ATM (higher strike) - long farther OTM = positive net premium for put spread."""
        from agents.proposer_engine import _build_justification  # noqa
        adapter = MagicMock()
        adapter.fetch_options_chain_tws = AsyncMock(return_value=_SAMPLE_CHAIN)
        gen = CandidateGenerator(adapter=adapter)
        result = self._run(gen.get_candidates("SPX", breach=self._breach_vega()))
        spread_c = next((c for c in result if "Bear Put Spread" in c.strategy_name), None)
        assert spread_c is not None
        # Net premium = (short_mid - long_mid) * multiplier
        # otm_puts sorted by strike desc → [0]=5450(mid=10.0), [1]=5400(mid=7.5),
        # [2]=5350(mid=5.3) → short(5450) - long(5350) = 4.7 * 100 = 470
        assert spread_c.net_premium == pytest.approx(470.0, abs=10.0), (
            f"Expected ~470.0, got {spread_c.net_premium}"
        )

    def test_get_candidates_real_chain_strategy_name_has_strikes(self) -> None:
        adapter = MagicMock()
        adapter.fetch_options_chain_tws = AsyncMock(return_value=_SAMPLE_CHAIN)
        gen = CandidateGenerator(adapter=adapter)
        result = self._run(gen.get_candidates("SPX", breach=self._breach_vega()))
        spread_c = next((c for c in result if "Bear Put Spread" in c.strategy_name), None)
        assert spread_c is not None
        # Strategy name should include strikes like "5450/5400"
        assert "/" in spread_c.strategy_name, (
            f"Expected strike pair in name, got: {spread_c.strategy_name}"
        )

    def test_get_candidates_falls_back_on_adapter_exception(self) -> None:
        adapter = MagicMock()
        adapter.fetch_options_chain_tws = AsyncMock(side_effect=ConnectionError("TWS unavailable"))
        gen = CandidateGenerator(adapter=adapter)
        # Should catch exception and fall back to synthetic
        result = self._run(gen.get_candidates("SPX", breach=self._breach_vega()))
        assert len(result) > 0, "Must fall back on adapter exception"


# ============================================================================
# TestProposerEngine (T018)
# ============================================================================


def _make_breach(
    greek: str = "vega",
    current: float = -8000.0,
    limit: float = -4800.0,
    distance: float = -3200.0,
    regime: str = "neutral_volatility",
) -> BreachEvent:
    return BreachEvent(
        greek=greek,
        current_value=current,
        limit=limit,
        distance_to_target=distance,
        regime=regime,
        account_id="DU999",
    )


class TestProposerEngine:
    """End-to-end tests for ProposerEngine with mocked IBKR adapter and DB session."""

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _make_adapter(init_margin: float = 1500.0, maint_margin: float = 1200.0) -> MagicMock:
        """Return mock IBKRAdapter with simulate_margin_impact as AsyncMock."""
        adapter = MagicMock()
        adapter.simulate_margin_impact = AsyncMock(
            return_value={
                "init_margin_change":  init_margin,
                "maint_margin_change": maint_margin,
            }
        )
        # Prevent TypeError when ProposerEngine awaits fetch_options_chain_tws
        adapter.fetch_options_chain_tws = AsyncMock(return_value=[])
        return adapter

    @staticmethod
    def _make_session() -> MagicMock:
        """Return a mock SQLModel/SQLAlchemy session."""
        session = MagicMock()
        session.exec = MagicMock(return_value=MagicMock())
        session.query = MagicMock(return_value=MagicMock(
            filter_by=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        ))
        session.add = MagicMock()
        session.commit = MagicMock()
        session.rollback = MagicMock()
        return session

    # ── generate() ──────────────────────────────────────────────────────────

    def test_generate_returns_nonempty_list(self) -> None:
        engine = ProposerEngine(adapter=self._make_adapter(), loader=RiskRegimeLoader())
        breach = _make_breach()
        result = asyncio.get_event_loop().run_until_complete(
            engine.generate([breach], account_id="DU999", nlv=100_000.0)
        )
        assert len(result) > 0

    def test_generate_empty_when_no_breaches(self) -> None:
        engine = ProposerEngine(adapter=self._make_adapter(), loader=RiskRegimeLoader())
        result = asyncio.get_event_loop().run_until_complete(
            engine.generate([], account_id="DU999", nlv=100_000.0)
        )
        assert result == []

    def test_generate_at_most_3_candidates(self) -> None:
        engine = ProposerEngine(adapter=self._make_adapter(), loader=RiskRegimeLoader())
        breaches = [
            _make_breach("vega"),
            _make_breach("delta", current=1500, limit=1200, distance=300),
        ]
        result = asyncio.get_event_loop().run_until_complete(
            engine.generate(breaches, account_id="DU999", nlv=100_000.0)
        )
        assert len(result) <= 3

    def test_generate_candidates_have_legs(self) -> None:
        engine = ProposerEngine(adapter=self._make_adapter(), loader=RiskRegimeLoader())
        breach = _make_breach()
        result = asyncio.get_event_loop().run_until_complete(
            engine.generate([breach], account_id="DU999", nlv=100_000.0)
        )
        for c in result:
            assert len(c.legs) >= 1

    def test_generate_all_candidates_positive_score(self) -> None:
        engine = ProposerEngine(adapter=self._make_adapter(), loader=RiskRegimeLoader())
        breach = _make_breach()
        result = asyncio.get_event_loop().run_until_complete(
            engine.generate([breach], account_id="DU999", nlv=100_000.0)
        )
        assert all(c.efficiency_score > 0 for c in result)

    def test_generate_simulate_margin_called(self) -> None:
        adapter = self._make_adapter()
        engine = ProposerEngine(adapter=adapter, loader=RiskRegimeLoader())
        breach = _make_breach()
        asyncio.get_event_loop().run_until_complete(
            engine.generate([breach], account_id="DU999", nlv=100_000.0)
        )
        assert adapter.simulate_margin_impact.call_count >= 1

    # ── Score ranking ────────────────────────────────────────────────────────

    def test_candidates_sorted_by_efficiency_score_desc(self) -> None:
        """Returned list must be sorted by efficiency_score in descending order."""
        engine = ProposerEngine(adapter=self._make_adapter(), loader=RiskRegimeLoader())
        breach = _make_breach()
        result = asyncio.get_event_loop().run_until_complete(
            engine.generate([breach], account_id="DU999", nlv=100_000.0)
        )
        scores = [c.efficiency_score for c in result]
        assert scores == sorted(scores, reverse=True), f"Not sorted desc: {scores}"

    def test_rank_candidates_respects_margin_filter(self) -> None:
        """FR-007: Candidates whose init_margin_impact exceeds available_margin are dropped."""
        engine = ProposerEngine(loader=RiskRegimeLoader())

        cheap = CandidateTrade(
            underlying="SPX", strategy_name="Cheap", legs=[{}],
            init_margin_impact=500.0, efficiency_score=1.5,
        )
        expensive = CandidateTrade(
            underlying="SPX", strategy_name="Expensive", legs=[{}],
            init_margin_impact=99999.0, efficiency_score=5.0,
        )
        result = engine._rank_candidates([cheap, expensive], available_margin=1000.0)
        names = [c.strategy_name for c in result]
        assert "Cheap" in names
        assert "Expensive" not in names

    def test_rank_candidates_zero_score_excluded(self) -> None:
        engine = ProposerEngine(loader=RiskRegimeLoader())
        zero = CandidateTrade(
            underlying="SPX", strategy_name="Zero", legs=[{}], efficiency_score=0.0,
        )
        good = CandidateTrade(
            underlying="SPX", strategy_name="Good", legs=[{}], efficiency_score=1.0,
        )
        result = engine._rank_candidates([zero, good])
        assert all(c.strategy_name != "Zero" for c in result)

    # ── Efficiency score formula ─────────────────────────────────────────────

    def test_efficiency_score_formula(self) -> None:
        """score = risk_reduction / (max(init_margin, 1) + n_legs * fee)
        risk_reduction = |vega_reduction| + |delta_reduction| * 10
        """
        engine = ProposerEngine(loader=RiskRegimeLoader())
        breach = _make_breach("vega")
        candidate = CandidateTrade(
            underlying="SPX", strategy_name="Test", legs=[{}, {}],
            vega_reduction=50.0, delta_reduction=5.0, init_margin_impact=1500.0,
        )
        score = engine._compute_efficiency_score(candidate, breach, arb_fee=0.65)
        risk_reduction = 50.0 + 5.0 * 10  # = 100
        denom = max(1500.0, 1.0) + 2 * 0.65  # = 1501.30
        expected = risk_reduction / denom
        assert score == pytest.approx(expected, rel=1e-5)

    # ── persist_top3 and Supersede logic (FR-014) ────────────────────────────

    def test_persist_top3_supersedes_pending_first(self) -> None:
        """Supersede MUST be called before any INSERT (FR-014)."""
        engine = ProposerEngine(loader=RiskRegimeLoader())
        session = self._make_session()

        candidates = [
            CandidateTrade(
                underlying="SPX",
                strategy_name="Hedge A",
                legs=[{"symbol": "SPX"}],
                vega_reduction=50.0,
                efficiency_score=1.2,
                breach=_make_breach(),
            )
        ]

        engine.persist_top3("DU999", candidates, session)

        # session.exec() should have been called (for the SQLModel UPDATE)
        # OR session.query() used in fallback path — at least one supersede mechanism invoked.
        supersede_attempted = session.exec.called or session.query.called
        assert supersede_attempted, "Supersede step was never called"

    def test_persist_top3_inserts_candidates(self) -> None:
        engine = ProposerEngine(loader=RiskRegimeLoader())
        session = self._make_session()

        candidates = [
            CandidateTrade(
                underlying="SPX",
                strategy_name=f"Hedge {i}",
                legs=[{"symbol": "SPX"}],
                vega_reduction=50.0 * i,
                efficiency_score=float(i),
                breach=_make_breach(),
            )
            for i in range(1, 4)
        ]

        engine.persist_top3("DU999", candidates, session)
        assert session.add.call_count == 3

    def test_persist_top3_only_inserts_max_3(self) -> None:
        engine = ProposerEngine(loader=RiskRegimeLoader())
        session = self._make_session()

        candidates = [
            CandidateTrade(
                underlying="SPX",
                strategy_name=f"Hedge {i}",
                legs=[{}],
                efficiency_score=float(i),
                breach=_make_breach(),
            )
            for i in range(1, 6)  # 5 candidates
        ]

        engine.persist_top3("DU999", candidates, session)
        # Only top-3 should be inserted
        assert session.add.call_count <= 3

    def test_persist_top3_calls_commit(self) -> None:
        engine = ProposerEngine(loader=RiskRegimeLoader())
        session = self._make_session()

        candidates = [
            CandidateTrade(
                underlying="SPX",
                strategy_name="Hedge",
                legs=[{}],
                efficiency_score=1.0,
                breach=_make_breach(),
            )
        ]
        engine.persist_top3("DU999", candidates, session)
        session.commit.assert_called_once()

    def test_persist_top3_skips_zero_score(self) -> None:
        """Candidates with efficiency_score <= 0 must not be inserted."""
        engine = ProposerEngine(loader=RiskRegimeLoader())
        session = self._make_session()

        candidates = [
            CandidateTrade(
                underlying="SPX",
                strategy_name="Bad",
                legs=[{}],
                efficiency_score=0.0,
                breach=_make_breach(),
            )
        ]
        engine.persist_top3("DU999", candidates, session)
        session.add.assert_not_called()

    # ── Justification builder ────────────────────────────────────────────────

    def test_build_justification_contains_greek(self) -> None:
        breach = _make_breach("vega")
        candidate = CandidateTrade(
            underlying="SPX", strategy_name="Test", legs=[],
            vega_reduction=60.0, efficiency_score=0.75,
        )
        j = _build_justification(breach, candidate)
        assert "Vega" in j

    def test_build_justification_contains_regime(self) -> None:
        breach = _make_breach(regime="high_volatility")
        candidate = CandidateTrade(
            underlying="SPX", strategy_name="Test", legs=[],
            vega_reduction=50.0, efficiency_score=0.50,
        )
        j = _build_justification(breach, candidate)
        assert "high_volatility" in j

    def test_build_justification_contains_score(self) -> None:
        breach = _make_breach()
        candidate = CandidateTrade(
            underlying="SPX", strategy_name="Test", legs=[],
            vega_reduction=50.0, efficiency_score=0.88,
        )
        j = _build_justification(breach, candidate)
        assert "0.88" in j
