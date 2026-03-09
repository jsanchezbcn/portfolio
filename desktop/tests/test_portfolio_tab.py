"""desktop/tests/test_portfolio_tab.py — Tests for the Portfolio tab widget.

Verifies:
  - Account summary cards update from signal
  - Positions table populates from signal
  - Refresh button triggers engine method
"""
from __future__ import annotations

import csv
import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTableView

from desktop.ui.portfolio_tab import PortfolioTab
from desktop.engine.ib_engine import AccountSummary
from desktop.ui.widgets.position_menu import PositionContextMenu


class TestPortfolioTabLayout:
    """Verify Portfolio tab structure."""

    def test_creates_without_crash(self, qtbot, mock_engine):
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)

    def test_has_account_summary_labels(self, qtbot, mock_engine):
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)
        assert tab._lbl_nlv is not None
        assert tab._lbl_cash is not None
        assert tab._lbl_bp is not None
        assert tab._lbl_margin is not None
        assert tab._lbl_upnl is not None
        assert tab._lbl_rpnl is not None

    def test_has_positions_table(self, qtbot, mock_engine):
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)
        assert isinstance(tab._table, QTableView)
        assert tab._table.model() is not None

    def test_has_refresh_button(self, qtbot, mock_engine):
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)
        assert tab._btn_refresh is not None
        assert "Refresh" in tab._btn_refresh.text()

    def test_has_export_buttons(self, qtbot, mock_engine):
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)

        assert "Export CSV" in tab._btn_export_csv.text()
        assert "Export JSON" in tab._btn_export_json.text()

    def test_table_starts_empty(self, qtbot, mock_engine):
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)
        assert tab._table.model().rowCount() == 0


class TestPortfolioTabData:
    """Test that signals populate widgets correctly."""

    def test_positions_signal_populates_table(self, qtbot, mock_engine, sample_positions):
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)

        mock_engine.positions_updated.emit(sample_positions)

        # sample_positions has 2 FOPs (same expiry) + 1 STK
        # = 1 group-header + 2 FOP rows + 1 STK row = 4 display rows
        assert tab._table.model().rowCount() == 4

    def test_positions_table_shows_symbol(self, qtbot, mock_engine, sample_positions):
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)

        mock_engine.positions_updated.emit(sample_positions)

        # Row 0 is the expiry group header; row 1 is the first FOP position.
        model = tab._table.model()
        idx = model.index(1, 0)
        assert model.data(idx) == "ES"

    def test_positions_table_shows_quantity(self, qtbot, mock_engine, sample_positions):
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)

        mock_engine.positions_updated.emit(sample_positions)

        # Row 0 is the group header; row 1 is ES FOP with qty=-1.
        model = tab._table.model()
        idx = model.index(1, 3)
        assert "-1" in str(model.data(idx))

    def test_account_summary_signal_updates_nlv(self, qtbot, mock_engine, sample_account_summary):
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)

        mock_engine.account_updated.emit(sample_account_summary)

        assert "250,000" in tab._lbl_nlv.text()

    def test_account_summary_signal_updates_cash(self, qtbot, mock_engine, sample_account_summary):
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)

        mock_engine.account_updated.emit(sample_account_summary)

        assert "50,000" in tab._lbl_cash.text()

    def test_account_summary_signal_updates_buying_power(self, qtbot, mock_engine, sample_account_summary):
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)

        mock_engine.account_updated.emit(sample_account_summary)

        assert "500,000" in tab._lbl_bp.text()

    def test_positive_pnl_shows_green(self, qtbot, mock_engine, sample_account_summary):
        """Positive unrealized PnL should show green color in label."""
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)

        # Ensure positive PnL
        summary = AccountSummary(
            account_id="U12345678",
            net_liquidation=250000.0,
            total_cash=50000.0,
            buying_power=100000.0,
            init_margin=30000.0,
            maint_margin=25000.0,
            unrealized_pnl=5000.0,
            realized_pnl=1000.0,
        )
        mock_engine.account_updated.emit(summary)

        assert "#27ae60" in tab._lbl_upnl.text()  # green

    def test_negative_pnl_shows_red(self, qtbot, mock_engine):
        """Negative unrealized PnL should show red color in label."""
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)

        summary = AccountSummary(
            account_id="U12345678",
            net_liquidation=250000.0,
            total_cash=50000.0,
            buying_power=100000.0,
            init_margin=30000.0,
            maint_margin=25000.0,
            unrealized_pnl=-3000.0,
            realized_pnl=0.0,
        )
        mock_engine.account_updated.emit(summary)

        assert "#e74c3c" in tab._lbl_upnl.text()  # red

    def test_status_label_updates_after_positions(self, qtbot, mock_engine, sample_positions):
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)

        mock_engine.positions_updated.emit(sample_positions)

        assert "3 positions" in tab._lbl_status.text()

    def test_empty_positions_clears_table(self, qtbot, mock_engine, sample_positions):
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)

        # Load then clear
        mock_engine.positions_updated.emit(sample_positions)
        assert tab._table.model().rowCount() == 4  # 1 group header + 2 FOPs + 1 STK

        mock_engine.positions_updated.emit([])
        assert tab._table.model().rowCount() == 0

    def test_can_sort_positions_by_abs_vega_desc(self, qtbot, mock_engine, sample_positions):
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)

        metric_idx = tab._cmb_sort_metric.findData("vega")
        order_idx = tab._cmb_sort_order.findData(True)
        tab._cmb_sort_metric.setCurrentIndex(metric_idx)
        tab._cmb_sort_order.setCurrentIndex(order_idx)
        tab._chk_sort_abs.setChecked(True)

        mock_engine.positions_updated.emit(sample_positions)

        model = tab._table.model()
        first_symbol = model.data(model.index(0, 0))
        assert first_symbol == "ES"


class TestPortfolioTabPositionActions:
    def test_option_payload_exposes_roll_action(self, qtbot, mock_engine, sample_positions):
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)

        payload = {"legs": [sample_positions[0]]}

        assert PositionContextMenu.action_names_for_payload(payload) == ["Buy", "Sell", "Roll"]

    def test_stock_payload_excludes_roll_action(self, qtbot, mock_engine, sample_positions):
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)

        payload = {"legs": [sample_positions[1]]}

        assert PositionContextMenu.action_names_for_payload(payload) == ["Buy", "Sell"]

    def test_emit_position_action_includes_requested_action(self, qtbot, mock_engine, sample_positions):
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)

        payload = {"kind": "position", "description": "ES", "legs": [sample_positions[0]]}

        with qtbot.waitSignal(tab.position_action_requested, timeout=1000) as blocker:
            tab._emit_position_action(payload, "sell")

        assert blocker.args[0]["action"] == "SELL"


class TestPortfolioTabExport:
    def test_serialize_positions_for_export_includes_greeks_and_bid_ask(self, qtbot, mock_engine, sample_positions):
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)
        tab._raw_positions = sample_positions

        mock_engine.chain_snapshot = lambda: [
            type("Row", (), {"underlying": "ES", "expiry": "20260320", "strike": 5500.0, "right": "C", "bid": 10.0, "ask": 11.0})(),
            type("Row", (), {"underlying": "MES", "expiry": "20260320", "strike": 5600.0, "right": "P", "bid": 5.0, "ask": 6.0})(),
        ]
        mock_engine.last_market_snapshot = lambda sym: {"bid": 554.5, "ask": 555.5} if sym == "SPY" else None

        rows = tab._serialize_positions_for_export(sample_positions)

        assert len(rows) == 3
        es = next(row for row in rows if row["symbol"] == "ES")
        spy = next(row for row in rows if row["symbol"] == "SPY")

        assert es["delta"] == sample_positions[0].delta
        assert es["gamma"] == sample_positions[0].gamma
        assert es["bid"] == 10.0
        assert es["ask"] == 11.0
        assert es["bid_ask_source"] == "chain_snapshot"

        assert spy["bid"] == 554.5
        assert spy["ask"] == 555.5
        assert spy["bid_ask_source"] == "market_snapshot"

    def test_write_json_export_file(self, qtbot, mock_engine, tmp_path):
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)
        path = tmp_path / "portfolio.json"

        tab._write_json(
            str(path),
            {"positions": [{"symbol": "SPY", "delta": 10.0, "bid": 554.5, "ask": 555.5}]},
        )

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["positions"][0]["symbol"] == "SPY"
        assert payload["positions"][0]["bid"] == 554.5

    def test_write_csv_export_file(self, qtbot, mock_engine, tmp_path):
        tab = PortfolioTab(mock_engine)
        qtbot.addWidget(tab)
        path = tmp_path / "portfolio.csv"

        tab._write_csv(
            str(path),
            [{"symbol": "ES", "delta": -17.5, "bid": 10.0, "ask": 11.0}],
        )

        with path.open("r", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert rows[0]["symbol"] == "ES"
        assert rows[0]["bid"] == "10.0"
