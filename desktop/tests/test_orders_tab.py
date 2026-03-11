"""desktop/tests/test_orders_tab.py — Tests for the Orders tab widget."""
from __future__ import annotations

from desktop.ui.orders_tab import OrdersTab, OpenOrdersTableModel
from desktop.engine.ib_engine import OpenOrder


def _sample_orders() -> list[OpenOrder]:
    return [
        OpenOrder(
            order_id=101, perm_id=9001, symbol="ES FOP 20260320 5500 C",
            action="BUY", quantity=1.0, order_type="LMT",
            limit_price=10.50, status="PreSubmitted",
            filled=0.0, remaining=1.0, avg_fill_price=0.0,
        ),
        OpenOrder(
            order_id=102, perm_id=9002, symbol="SPY",
            action="SELL", quantity=100.0, order_type="MKT",
            limit_price=None, status="Submitted",
            filled=0.0, remaining=100.0, avg_fill_price=0.0,
        ),
    ]


class TestOrdersTabLayout:

    def test_creates_without_crash(self, qtbot, mock_engine):
        tab = OrdersTab(mock_engine)
        qtbot.addWidget(tab)

    def test_has_refresh_button(self, qtbot, mock_engine):
        tab = OrdersTab(mock_engine)
        qtbot.addWidget(tab)
        assert "Refresh" in tab._btn_refresh.text()

    def test_has_cancel_buttons(self, qtbot, mock_engine):
        tab = OrdersTab(mock_engine)
        qtbot.addWidget(tab)
        assert "Cancel Selected" in tab._btn_cancel_selected.text()
        assert "Cancel All" in tab._btn_cancel_all.text()

    def test_cancel_selected_starts_disabled(self, qtbot, mock_engine):
        tab = OrdersTab(mock_engine)
        qtbot.addWidget(tab)
        assert not tab._btn_cancel_selected.isEnabled()

    def test_table_starts_empty(self, qtbot, mock_engine):
        tab = OrdersTab(mock_engine)
        qtbot.addWidget(tab)
        assert tab._model.rowCount() == 0


class TestOrdersTabData:

    def test_orders_signal_populates_table(self, qtbot, mock_engine):
        tab = OrdersTab(mock_engine)
        qtbot.addWidget(tab)

        orders = _sample_orders()
        mock_engine.orders_updated.emit(orders)

        assert tab._model.rowCount() == 2

    def test_orders_table_shows_symbol(self, qtbot, mock_engine):
        tab = OrdersTab(mock_engine)
        qtbot.addWidget(tab)

        orders = _sample_orders()
        mock_engine.orders_updated.emit(orders)

        idx = tab._model.index(0, 1)  # Symbol column
        assert "ES" in tab._model.data(idx)

    def test_orders_table_shows_status(self, qtbot, mock_engine):
        tab = OrdersTab(mock_engine)
        qtbot.addWidget(tab)

        orders = _sample_orders()
        mock_engine.orders_updated.emit(orders)

        idx = tab._model.index(0, 6)  # Status column
        assert "PreSubmitted" in tab._model.data(idx)

    def test_market_order_shows_mkt(self, qtbot, mock_engine):
        tab = OrdersTab(mock_engine)
        qtbot.addWidget(tab)

        mock_engine.orders_updated.emit(_sample_orders())

        idx = tab._model.index(1, 5)  # Limit column
        assert "MKT" in tab._model.data(idx)

    def test_status_label_updates(self, qtbot, mock_engine):
        tab = OrdersTab(mock_engine)
        qtbot.addWidget(tab)

        mock_engine.orders_updated.emit(_sample_orders())

        assert "2 open orders" in tab._lbl_status.text()


class TestOpenOrdersTableModel:

    def test_empty_model(self, qapp):
        m = OpenOrdersTableModel()
        assert m.rowCount() == 0

    def test_column_count(self, qapp):
        m = OpenOrdersTableModel()
        assert m.columnCount() == 10

    def test_get_order_at(self, qapp):
        m = OpenOrdersTableModel()
        orders = _sample_orders()
        m.set_data(orders)
        assert m.get_order_at(0).order_id == 101
        assert m.get_order_at(1).order_id == 102
        assert m.get_order_at(5) is None
