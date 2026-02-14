from risk_engine.regime_detector import RegimeDetector


def test_crisis_regime_by_vix(regime_detector: RegimeDetector) -> None:
    regime = regime_detector.detect_regime(vix=36.0, term_structure=1.05)
    assert regime.name == "crisis_mode"


def test_high_regime_by_vix(regime_detector: RegimeDetector) -> None:
    regime = regime_detector.detect_regime(vix=24.0, term_structure=1.02)
    assert regime.name == "high_volatility"


def test_high_regime_by_polymarket_override(regime_detector: RegimeDetector) -> None:
    regime = regime_detector.detect_regime(vix=18.0, term_structure=1.05, recession_probability=0.45)
    assert regime.name == "high_volatility"


def test_low_regime(regime_detector: RegimeDetector) -> None:
    regime = regime_detector.detect_regime(vix=14.0, term_structure=1.15)
    assert regime.name == "low_volatility"


def test_neutral_regime(regime_detector: RegimeDetector) -> None:
    regime = regime_detector.detect_regime(vix=18.0, term_structure=1.08)
    assert regime.name == "neutral_volatility"
