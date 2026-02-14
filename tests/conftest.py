from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from risk_engine.regime_detector import RegimeDetector


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def mock_ibkr_positions(fixtures_dir: Path) -> list[dict]:
    with (fixtures_dir / "mock_ibkr_positions.json").open("r", encoding="utf-8") as fixture_file:
        return json.load(fixture_file)


@pytest.fixture
def mock_vix_data(fixtures_dir: Path) -> dict:
    with (fixtures_dir / "mock_vix_data.json").open("r", encoding="utf-8") as fixture_file:
        return json.load(fixture_file)


@pytest.fixture
def mock_ibkr_client() -> Mock:
    client = Mock()
    client.get_positions.return_value = []
    client.get_tastytrade_option_greeks.return_value = {
        "delta": 0.25,
        "gamma": 0.01,
        "theta": -0.12,
        "vega": 0.18,
        "iv": 0.22,
    }
    client.calculate_spx_weighted_delta.return_value = 0.0
    return client


@pytest.fixture
def mock_regime_config(tmp_path: Path) -> Path:
    config_file = tmp_path / "risk_matrix.yaml"
    config_file.write_text(
        """
regimes:
  low_volatility:
    condition: "VIX < 15 and term_structure > 1.10"
    description: "Complacency"
    limits:
      max_beta_delta: 600
      max_negative_vega: -500
      min_daily_theta: 100
      max_gamma: 50
      recession_probability_threshold: 0.4
      allowed_strategies: ["Long Calendars"]
  neutral_volatility:
    condition: "15 <= VIX <= 22"
    description: "Neutral"
    limits:
      max_beta_delta: 300
      max_negative_vega: -1200
      min_daily_theta: 300
      max_gamma: 35
      recession_probability_threshold: 0.4
      allowed_strategies: ["Iron Condors"]
  high_volatility:
    condition: "VIX > 22 or recession_probability > 0.40"
    description: "Fear"
    limits:
      max_beta_delta: 100
      max_negative_vega: -2500
      min_daily_theta: 600
      max_gamma: 20
      recession_probability_threshold: 0.4
      allowed_strategies: ["Ratio Backspreads"]
  crisis_mode:
    condition: "VIX > 35 or vvix > 150"
    description: "Panic"
    limits:
      max_beta_delta: 0
      max_negative_vega: 0
      min_daily_theta: 0
      max_gamma: 10
      recession_probability_threshold: 0.4
      allowed_strategies: ["Cash"]
""".strip(),
        encoding="utf-8",
    )
    return config_file


@pytest.fixture
def regime_detector(mock_regime_config: Path) -> RegimeDetector:
    return RegimeDetector(config_path=mock_regime_config)
