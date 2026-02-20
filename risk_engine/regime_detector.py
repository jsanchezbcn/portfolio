from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class RegimeLimits:
    """Risk limits applied when a regime is active."""

    max_beta_delta: float
    max_negative_vega: float
    min_daily_theta: float
    max_gamma: float
    allowed_strategies: list[str]
    recession_probability_threshold: float = 0.4


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

    def _load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            raise FileNotFoundError(f"Regime config not found: {self.config_path}")

        with self.config_path.open("r", encoding="utf-8") as config_file:
            return yaml.safe_load(config_file) or {}

    def _build_regime(self, regime_key: str) -> MarketRegime:
        regimes_payload = self._regimes.get("regimes", {})
        regime_payload_raw = regimes_payload.get(regime_key, {}) if isinstance(regimes_payload, dict) else {}
        regime_payload: dict[str, Any] = regime_payload_raw if isinstance(regime_payload_raw, dict) else {}
        limits_raw = regime_payload.get("limits", {})
        limits_payload: dict[str, Any] = limits_raw if isinstance(limits_raw, dict) else {}

        def _first_numeric(payload: dict[str, Any], keys: Any, default: float) -> float:
            key_list: list[str]
            if isinstance(keys, str):
                key_list = [keys]
            elif isinstance(keys, (list, tuple, set)):
                key_list = [str(key) for key in keys]
            else:
                key_list = []

            for key in key_list:
                if key not in payload:
                    continue
                try:
                    return float(payload[key])
                except (TypeError, ValueError):
                    continue
            return float(default)

        allowed_strategies_raw = limits_payload.get("allowed_strategies", [])
        if isinstance(allowed_strategies_raw, list):
            allowed_strategies = [str(item) for item in allowed_strategies_raw]
        elif isinstance(allowed_strategies_raw, str):
            allowed_strategies = [allowed_strategies_raw]
        else:
            allowed_strategies = []

        limits = RegimeLimits(
            max_beta_delta=_first_numeric(
                limits_payload,
                ["max_beta_delta", "legacy_max_beta_delta", "max_spx_delta", "max_portfolio_delta"],
                0.0,
            ),
            max_negative_vega=_first_numeric(
                limits_payload,
                ["max_negative_vega", "legacy_max_negative_vega"],
                0.0,
            ),
            min_daily_theta=_first_numeric(
                limits_payload,
                ["min_daily_theta", "legacy_min_daily_theta"],
                0.0,
            ),
            max_gamma=_first_numeric(
                limits_payload,
                ["max_gamma", "legacy_max_gamma"],
                0.0,
            ),
            allowed_strategies=allowed_strategies,
            recession_probability_threshold=_first_numeric(
                limits_payload,
                ["recession_probability_threshold"],
                0.4,
            ),
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
            return crisis

        if vix > 22:
            return high

        recession_threshold = high.limits.recession_probability_threshold
        if recession_probability is not None and recession_probability > recession_threshold:
            return high

        if vix < 15 and term_structure > 1.10:
            return low

        return neutral
