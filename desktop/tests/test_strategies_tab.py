from __future__ import annotations

from datetime import date, timedelta

from desktop.engine.ib_engine import PositionRow
from desktop.ui.strategies_tab import (
    StrategiesTab,
    calculate_taleb_gamma_warning,
    calculate_theta_vega_state,
)


def _opt_row(*, expiry: str, gamma: float, theta: float, vega: float) -> PositionRow:
    return PositionRow(
        conid=100,
        symbol="ES",
        sec_type="FOP",
        underlying="ES",
        strike=5500.0,
        right="C",
        expiry=expiry,
        quantity=1.0,
        avg_cost=10.0,
        market_price=10.0,
        market_value=1000.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        delta=0.2,
        gamma=gamma,
        theta=theta,
        vega=vega,
        iv=0.2,
        spx_delta=10.0,
    )


def test_calculate_taleb_gamma_warning_breaches_on_0_7d_exposure():
    today = date(2026, 3, 6)
    near = (today + timedelta(days=2)).strftime("%Y%m%d")
    far = (today + timedelta(days=14)).strftime("%Y%m%d")
    rows = [
        _opt_row(expiry=near, gamma=12.0, theta=-4.0, vega=10.0),
        _opt_row(expiry=near, gamma=-8.0, theta=-3.0, vega=8.0),
        _opt_row(expiry=far, gamma=100.0, theta=-1.0, vega=2.0),
    ]

    state = calculate_taleb_gamma_warning(rows, gamma_threshold=15.0, as_of=today)

    assert state.breached is True
    assert state.gamma_0_7d_abs == 20.0


def test_calculate_taleb_gamma_warning_ignores_non_option_and_invalid_expiry():
    today = date(2026, 3, 6)
    valid = (today + timedelta(days=1)).strftime("%Y%m%d")
    opt = _opt_row(expiry=valid, gamma=2.0, theta=-1.0, vega=5.0)
    stock = PositionRow(
        conid=1,
        symbol="SPY",
        sec_type="STK",
        underlying="",
        strike=None,
        right=None,
        expiry=None,
        quantity=100.0,
        avg_cost=500.0,
        market_price=500.0,
        market_value=50000.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        delta=100.0,
        gamma=999.0,
        theta=0.0,
        vega=0.0,
        iv=None,
        spx_delta=100.0,
    )
    bad_exp = _opt_row(expiry="BAD", gamma=999.0, theta=-5.0, vega=5.0)

    state = calculate_taleb_gamma_warning([opt, stock, bad_exp], gamma_threshold=5.0, as_of=today)

    assert state.breached is False
    assert state.gamma_0_7d_abs == 2.0


def test_calculate_theta_vega_state_marks_inside_target_band():
    expiry = (date.today() + timedelta(days=3)).strftime("%Y%m%d")
    rows = [
        _opt_row(expiry=expiry, gamma=1.0, theta=-12.0, vega=40.0),
        _opt_row(expiry=expiry, gamma=1.0, theta=-8.0, vega=60.0),
    ]

    state = calculate_theta_vega_state(rows, lower_band=-0.30, upper_band=-0.15)

    assert state.ratio == -0.2
    assert state.zone == "inside"


def test_calculate_theta_vega_state_marks_outside_target_band():
    expiry = (date.today() + timedelta(days=3)).strftime("%Y%m%d")
    rows = [_opt_row(expiry=expiry, gamma=1.0, theta=-2.0, vega=40.0)]

    state = calculate_theta_vega_state(rows, lower_band=-0.30, upper_band=-0.15)

    assert state.zone == "outside"


def test_strategies_tab_has_required_controls(qtbot, mock_engine):
    tab = StrategiesTab(mock_engine)
    qtbot.addWidget(tab)

    assert tab._spn_stop_loss.value() > 0
    assert tab._spn_take_profit.value() > 0
    assert tab._btn_start.text().startswith("▶")


def test_strategies_tab_start_sets_monitoring_state(qtbot, mock_engine):
    tab = StrategiesTab(mock_engine)
    qtbot.addWidget(tab)

    tab._on_start()

    assert "Monitoring" in tab._lbl_run_state.text()
    assert tab._btn_stop.isEnabled() is True
    assert tab._btn_start.isEnabled() is False
