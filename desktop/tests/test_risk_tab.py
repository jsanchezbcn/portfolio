"""desktop/tests/test_risk_tab.py — Tests for the Risk tab widget."""
from __future__ import annotations

from desktop.ui.risk_tab import RiskTab
from desktop.engine.ib_engine import PortfolioRiskSummary


class TestRiskTabLayout:

    def test_creates_without_crash(self, qtbot, mock_engine):
        tab = RiskTab(mock_engine)
        qtbot.addWidget(tab)

    def test_has_greek_cards(self, qtbot, mock_engine):
        tab = RiskTab(mock_engine)
        qtbot.addWidget(tab)
        assert tab._lbl_spx_delta is not None
        assert tab._lbl_delta is not None
        assert tab._lbl_gamma is not None
        assert tab._lbl_theta is not None
        assert tab._lbl_vega is not None
        assert tab._lbl_tv_ratio is not None

    def test_has_exposure_cards(self, qtbot, mock_engine):
        tab = RiskTab(mock_engine)
        qtbot.addWidget(tab)
        assert tab._lbl_positions is not None
        assert tab._lbl_gross is not None
        assert tab._lbl_net is not None

    def test_has_refresh_button(self, qtbot, mock_engine):
        tab = RiskTab(mock_engine)
        qtbot.addWidget(tab)
        assert "Refresh" in tab._btn_refresh.text()


class TestRiskTabData:

    def test_risk_signal_updates_greeks(self, qtbot, mock_engine):
        tab = RiskTab(mock_engine)
        qtbot.addWidget(tab)

        risk = PortfolioRiskSummary(
            total_positions=10,
            total_value=150000.0,
            total_spx_delta=-25.5,
            total_delta=-30.0,
            total_gamma=0.05,
            total_theta=-45.0,
            total_vega=120.0,
            theta_vega_ratio=-0.375,
            gross_exposure=500000.0,
            net_exposure=150000.0,
            options_count=8,
            stocks_count=2,
        )
        mock_engine.risk_updated.emit(risk)

        assert "-25.50" in tab._lbl_spx_delta.text()
        assert "-30.00" in tab._lbl_delta.text()
        assert "0.0500" in tab._lbl_gamma.text()
        assert "-45.00" in tab._lbl_theta.text()
        assert "120.00" in tab._lbl_vega.text()
        assert "-0.375" in tab._lbl_tv_ratio.text()

    def test_risk_signal_updates_exposure(self, qtbot, mock_engine):
        tab = RiskTab(mock_engine)
        qtbot.addWidget(tab)

        risk = PortfolioRiskSummary(
            total_positions=15,
            total_value=200000.0,
            total_spx_delta=10.0,
            total_delta=10.0,
            total_gamma=0.01,
            total_theta=-20.0,
            total_vega=50.0,
            theta_vega_ratio=-0.4,
            gross_exposure=600000.0,
            net_exposure=200000.0,
            options_count=12,
            stocks_count=3,
        )
        mock_engine.risk_updated.emit(risk)

        assert "15" in tab._lbl_positions.text()
        assert "12" in tab._lbl_options.text()
        assert "3" in tab._lbl_stocks.text()
        assert "600,000" in tab._lbl_gross.text()
        assert "200,000" in tab._lbl_net.text()

    def test_high_delta_shows_red(self, qtbot, mock_engine):
        tab = RiskTab(mock_engine)
        qtbot.addWidget(tab)

        risk = PortfolioRiskSummary(
            total_positions=5, total_value=100000.0,
            total_spx_delta=150.0,  # > 100 → red
            total_delta=150.0, total_gamma=0.01,
            total_theta=-10.0, total_vega=30.0,
            theta_vega_ratio=-0.333,
            gross_exposure=300000.0, net_exposure=100000.0,
            options_count=3, stocks_count=2,
        )
        mock_engine.risk_updated.emit(risk)

        assert "#e74c3c" in tab._lbl_spx_delta.text()  # red for high delta
