from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

LOGGER = logging.getLogger(__name__)


@dataclass
class RegimeLimits:
    """Risk limits applied when a regime is active."""

    max_beta_delta: float
    max_negative_vega: float
    min_daily_theta: float
    max_gamma: float
    allowed_strategies: list[str]
    recession_probability_threshold: float = 0.4
    # Issue 5: Per-underlying vega concentration cap (fraction of total vega)
    max_single_underlying_vega_pct: float = 0.60
    # Issue 6: Maximum contracts per single position
    max_position_contracts: int = 50


@dataclass
class MarketRegime:
    """Detected market regime with descriptive metadata and limits."""

    name: str
    condition: str
    description: str
    limits: RegimeLimits


class RegimeDetector:
    """Detect market regime from volatility and macro indicators."""

    def __init__(self, config_path: str | Path = "config/risk_matrix.yaml") -> None:
        """Load regime definitions from YAML configuration."""

        self.config_path = Path(config_path)
        self._regimes = self._load_config()
        # Issue 8: Track last detected regime so changes trigger callbacks
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

    def _build_regime(self, regime_key: str) -> MarketRegime:
        # Issue 17: Schema validation — fail loudly on missing / mistyped fields
        regimes_map = self._regimes.get("regimes", {})
        if regime_key not in regimes_map:
            raise ValueError(
                f"Regime '{regime_key}' not found in {self.config_path}. "
                f"Available: {sorted(regimes_map.keys())}"
            )
        regime_payload = regimes_map[regime_key]
        limits_payload = regime_payload.get("limits", {})
        required_fields = {"max_beta_delta", "max_negative_vega", "min_daily_theta", "max_gamma"}
        missing = required_fields - set(limits_payload.keys())
        if missing:
            raise ValueError(
                f"Regime '{regime_key}' in {self.config_path} is missing required limit "
                f"fields: {sorted(missing)}"
            )
        for field_name in required_fields:
            try:
                float(limits_payload[field_name])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Regime '{regime_key}' field '{field_name}' must be numeric, "
                    f"got {limits_payload[field_name]!r}"
                ) from exc

        limits = RegimeLimits(
            max_beta_delta=float(limits_payload["max_beta_delta"]),
            max_negative_vega=float(limits_payload["max_negative_vega"]),
            min_daily_theta=float(limits_payload["min_daily_theta"]),
            max_gamma=float(limits_payload["max_gamma"]),
            allowed_strategies=list(limits_payload.get("allowed_strategies", [])),
            recession_probability_threshold=float(limits_payload.get("recession_probability_threshold", 0.4)),
            max_single_underlying_vega_pct=float(limits_payload.get("max_single_underlying_vega_pct", 0.60)),
            max_position_contracts=int(limits_payload.get("max_position_contracts", 50)),
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
        """Return the active regime using configured thresholds and priority ordering."""

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

        # Issue 8: Track regime changes and invoke callback on transitions
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
