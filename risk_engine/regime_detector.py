from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

LOGGER = logging.getLogger(__name__)


@dataclass
class RegimeLimits:
    """Risk limits applied when a regime is active.

    Limits can be expressed two ways (the system picks whichever is available):

    NLV-relative ratios (preferred — scale with portfolio size):
        min_daily_theta_pct_nlv    – minimum theta as fraction of NLV
        max_negative_vega_pct_nlv  – maximum short-vega as fraction of NLV (negative)
        max_spx_delta_pct_nlv      – maximum |SPX-delta| as fraction of NLV
        max_gamma_pct_nlv          – maximum |gamma| as fraction of NLV

    Legacy absolute fallbacks (used when NLV is unavailable):
        max_beta_delta, max_negative_vega, min_daily_theta, max_gamma
    """

    # ── NLV-relative ratios ────────────────────────────────────────────────
    min_daily_theta_pct_nlv: float = 0.0
    max_negative_vega_pct_nlv: float = 0.0
    max_spx_delta_pct_nlv: float = 0.0
    max_gamma_pct_nlv: float = 0.0

    # ── Legacy absolute fallbacks ──────────────────────────────────────────
    max_beta_delta: float = 300.0
    max_negative_vega: float = -1200.0
    min_daily_theta: float = 30.0
    max_gamma: float = 35.0

    # ── Non-Greek limits ───────────────────────────────────────────────────
    allowed_strategies: list[str] = field(default_factory=list)
    recession_probability_threshold: float = 0.4
    max_single_underlying_vega_pct: float = 0.60
    max_position_contracts: int = 50

    def resolve(
        self,
        nlv: float | None = None,
        vix_scaler: float = 1.0,
        ts_scaler: float = 1.0,
    ) -> "ResolvedLimits":
        """Return effective absolute limits after applying NLV scaling.

        Priority:
        1. If NLV is provided and NLV-relative ratios are defined, compute
           effective limits = ratio × NLV × vix_scaler × ts_scaler.
        2. Otherwise fall back to legacy absolute values (also scaled by
           vix_scaler and ts_scaler for market-condition sensitivity).

        Args:
            nlv: Current net liquidation value of the account (dollars).
            vix_scaler: Multiplier from VIX level (< 1 tightens limits).
            ts_scaler: Multiplier from VIX term-structure (< 1 tightens).
        """
        combined = vix_scaler * ts_scaler

        if nlv is not None and nlv > 0 and (
            self.min_daily_theta_pct_nlv != 0
            or self.max_negative_vega_pct_nlv != 0
            or self.max_spx_delta_pct_nlv != 0
            or self.max_gamma_pct_nlv != 0
        ):
            return ResolvedLimits(
                min_daily_theta=self.min_daily_theta_pct_nlv * nlv * combined,
                max_negative_vega=self.max_negative_vega_pct_nlv * nlv * combined,
                max_beta_delta=self.max_spx_delta_pct_nlv * nlv * combined,
                max_gamma=self.max_gamma_pct_nlv * nlv * combined,
                # Non-Greek limits are unchanged
                max_single_underlying_vega_pct=self.max_single_underlying_vega_pct,
                max_position_contracts=self.max_position_contracts,
                allowed_strategies=self.allowed_strategies,
                # Surface the scaling context for display
                nlv_used=nlv,
                vix_scaler=vix_scaler,
                ts_scaler=ts_scaler,
                is_nlv_scaled=True,
            )

        # Fallback: legacy absolute values, still adjusted by market scalers
        return ResolvedLimits(
            min_daily_theta=self.min_daily_theta * combined,
            max_negative_vega=self.max_negative_vega * combined,
            max_beta_delta=self.max_beta_delta * combined,
            max_gamma=self.max_gamma * combined,
            max_single_underlying_vega_pct=self.max_single_underlying_vega_pct,
            max_position_contracts=self.max_position_contracts,
            allowed_strategies=self.allowed_strategies,
            nlv_used=nlv,
            vix_scaler=vix_scaler,
            ts_scaler=ts_scaler,
            is_nlv_scaled=False,
        )


@dataclass
class ResolvedLimits:
    """Effective absolute limits after NLV / VIX / term-structure scaling.

    These are the values that ``check_risk_limits`` actually compares
    portfolio Greeks against.
    """

    min_daily_theta: float
    max_negative_vega: float
    max_beta_delta: float
    max_gamma: float
    max_single_underlying_vega_pct: float
    max_position_contracts: int
    allowed_strategies: list[str]
    # Metadata — what was used to compute these limits
    nlv_used: float | None = None
    vix_scaler: float = 1.0
    ts_scaler: float = 1.0
    is_nlv_scaled: bool = False


@dataclass
class MarketRegime:
    """Detected market regime with descriptive metadata and limits."""

    name: str
    condition: str
    description: str
    limits: RegimeLimits
    # VIX and term-structure scalers computed at detection time
    vix_scaler: float = 1.0
    ts_scaler: float = 1.0

    def resolve_limits(self, nlv: float | None = None) -> ResolvedLimits:
        """Convenience proxy → resolve limits with stored market scalers."""
        return self.limits.resolve(
            nlv=nlv,
            vix_scaler=self.vix_scaler,
            ts_scaler=self.ts_scaler,
        )


class RegimeDetector:
    """Detect market regime from volatility and macro indicators."""

    def __init__(self, config_path: str | Path = "config/risk_matrix.yaml") -> None:
        """Load regime definitions from YAML configuration."""

        self.config_path = Path(config_path)
        self._regimes = self._load_config()
        # Track last detected regime so changes trigger callbacks
        self._last_regime: str | None = None
        self._on_regime_change: Callable[[str, str], None] | None = None

    def set_regime_change_callback(self, callback: Callable[[str, str], None]) -> None:
        """Register *callback(old_name, new_name)* called on every regime transition."""
        self._on_regime_change = callback

    def _load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            raise FileNotFoundError(f"Regime config not found: {self.config_path}")

        with self.config_path.open("r", encoding="utf-8") as config_file:
            return yaml.safe_load(config_file) or {}

    def _compute_vix_scaler(self, vix: float) -> float:
        """Return the VIX-derived limit scaler from config (< 1.0 tightens limits)."""
        scalers = sorted(
            self._regimes.get("vix_scalers", []),
            key=lambda e: e["vix_threshold"],
        )
        result = 1.0
        for entry in scalers:
            if vix >= entry["vix_threshold"]:
                result = float(entry["scale"])
        return result

    def _compute_ts_scaler(self, term_structure: float) -> float:
        """Return the term-structure-derived limit scaler from config."""
        # Entries sorted descending by threshold; first threshold that the
        # actual term_structure value is >= determines the scale.
        scalers = sorted(
            self._regimes.get("term_structure_scalers", []),
            key=lambda e: e["term_structure_threshold"],
            reverse=True,
        )
        for entry in scalers:
            if term_structure >= entry["term_structure_threshold"]:
                return float(entry["scale"])
        return 1.0

    def _build_regime(self, regime_key: str) -> MarketRegime:
        """Build a MarketRegime object from the YAML config — no scaling applied here."""
        regimes_map = self._regimes.get("regimes", {})
        if regime_key not in regimes_map:
            raise ValueError(
                f"Regime '{regime_key}' not found in {self.config_path}. "
                f"Available: {sorted(regimes_map.keys())}"
            )
        regime_payload = regimes_map[regime_key]
        lp = regime_payload.get("limits", {})

        # Accept either new NLV-relative keys or legacy absolute keys (or both).
        # We require at least the legacy keys so old configs still work.
        has_new = any(k in lp for k in (
            "min_daily_theta_pct_nlv",
            "max_negative_vega_pct_nlv",
            "max_spx_delta_pct_nlv",
            "max_gamma_pct_nlv",
        ))
        has_legacy = any(k in lp for k in (
            "legacy_min_daily_theta",
            "max_beta_delta",
            "min_daily_theta",
        ))
        if not has_new and not has_legacy:
            raise ValueError(
                f"Regime '{regime_key}' has no recognized limit fields. "
                "Provide NLV-relative ratios (min_daily_theta_pct_nlv, …) "
                "or legacy absolute values."
            )

        limits = RegimeLimits(
            # NLV-relative ratios (new)
            min_daily_theta_pct_nlv=float(lp.get("min_daily_theta_pct_nlv", 0.0)),
            max_negative_vega_pct_nlv=float(lp.get("max_negative_vega_pct_nlv", 0.0)),
            max_spx_delta_pct_nlv=float(lp.get("max_spx_delta_pct_nlv", 0.0)),
            max_gamma_pct_nlv=float(lp.get("max_gamma_pct_nlv", 0.0)),
            # Legacy absolute fallbacks
            max_beta_delta=float(
                lp.get("legacy_max_beta_delta", lp.get("max_beta_delta", 300.0))
            ),
            max_negative_vega=float(
                lp.get("legacy_max_negative_vega", lp.get("max_negative_vega", -1200.0))
            ),
            min_daily_theta=float(
                lp.get("legacy_min_daily_theta", lp.get("min_daily_theta", 30.0))
            ),
            max_gamma=float(
                lp.get("legacy_max_gamma", lp.get("max_gamma", 35.0))
            ),
            # Non-Greek
            allowed_strategies=list(lp.get("allowed_strategies", [])),
            recession_probability_threshold=float(lp.get("recession_probability_threshold", 0.4)),
            max_single_underlying_vega_pct=float(lp.get("max_single_underlying_vega_pct", 0.60)),
            max_position_contracts=int(lp.get("max_position_contracts", 50)),
        )

        return MarketRegime(
            name=regime_key,
            condition=str(regime_payload.get("condition", "")),
            description=str(regime_payload.get("description", "")),
            limits=limits,
        )

    def detect_regime(
        self,
        vix: float,
        term_structure: float,
        recession_probability: float | None = None,
        vvix: float | None = None,
    ) -> MarketRegime:
        """Return the active regime using configured thresholds and priority ordering.

        The returned ``MarketRegime`` carries ``vix_scaler`` and ``ts_scaler``
        attributes which are used by ``resolve_limits()`` to produce effective
        absolute limits adjusted for current market conditions.
        """

        crisis = self._build_regime("crisis_mode")
        high = self._build_regime("high_volatility")
        low = self._build_regime("low_volatility")
        neutral = self._build_regime("neutral_volatility")

        if vix > 35 or (vvix is not None and vvix > 150):
            result = crisis
        elif vix > 22:
            result = high
        elif (
            recession_probability is not None
            and recession_probability > high.limits.recession_probability_threshold
        ):
            result = high
        elif vix < 15 and term_structure > 1.10:
            result = low
        else:
            result = neutral

        # Attach market-condition scalers so callers can resolve dynamic limits
        result.vix_scaler = self._compute_vix_scaler(vix)
        result.ts_scaler = self._compute_ts_scaler(term_structure)

        LOGGER.debug(
            "Regime=%s  VIX=%.2f  TS=%.3f  vix_scaler=%.2f  ts_scaler=%.2f",
            result.name, vix, term_structure, result.vix_scaler, result.ts_scaler,
        )

        # Track regime changes and invoke callback on transitions
        if self._last_regime is not None and result.name != self._last_regime:
            LOGGER.warning(
                "Regime transition: %s -> %s (VIX=%.2f)",
                self._last_regime,
                result.name,
                vix,
            )
            if self._on_regime_change is not None:
                try:
                    self._on_regime_change(self._last_regime, result.name)
                except Exception as exc:  # pragma: no cover
                    LOGGER.error("regime_change callback error: %s", exc)
        self._last_regime = result.name
        return result
