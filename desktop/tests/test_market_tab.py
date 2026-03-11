"""desktop/tests/test_market_tab.py — Tests for the Market Data tab widget."""
from __future__ import annotations

from desktop.models.favorites import FavoriteSymbol, FavoritesStore
from desktop.ui.market_tab import MarketTab, WatchlistModel
from desktop.engine.ib_engine import MarketSnapshot


def _snap(symbol: str = "SPY", last: float = 555.0) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol, last=last, bid=554.5, ask=555.5,
        high=560.0, low=550.0, close=552.0,
        volume=50000000, timestamp="2025-01-15T14:30:00+00:00",
    )


class TestMarketTabLayout:

    def test_creates_without_crash(self, qtbot, mock_engine):
        tab = MarketTab(mock_engine)
        qtbot.addWidget(tab)

    def test_has_symbol_field(self, qtbot, mock_engine):
        tab = MarketTab(mock_engine)
        qtbot.addWidget(tab)
        assert tab._txt_symbol is not None

    def test_has_quote_button(self, qtbot, mock_engine):
        tab = MarketTab(mock_engine)
        qtbot.addWidget(tab)
        assert "Quote" in tab._btn_quote.text()

    def test_has_load_defaults_button(self, qtbot, mock_engine):
        tab = MarketTab(mock_engine)
        qtbot.addWidget(tab)
        assert "Default" in tab._btn_add_default.text()

    def test_table_starts_empty(self, qtbot, mock_engine):
        tab = MarketTab(mock_engine)
        qtbot.addWidget(tab)
        assert tab._model.rowCount() == 0

    def test_favorites_refresh_timer_is_one_second(self, qtbot, mock_engine):
        tab = MarketTab(mock_engine)
        qtbot.addWidget(tab)

        assert tab._refresh_timer.interval() == 1000


class TestMarketTabData:

    def test_market_snapshot_adds_to_watchlist(self, qtbot, mock_engine):
        tab = MarketTab(mock_engine)
        qtbot.addWidget(tab)

        mock_engine.market_snapshot.emit(_snap("SPY", 555.0))

        assert tab._model.rowCount() == 1

    def test_multiple_snapshots_accumulate(self, qtbot, mock_engine):
        tab = MarketTab(mock_engine)
        qtbot.addWidget(tab)

        mock_engine.market_snapshot.emit(_snap("SPY", 555.0))
        mock_engine.market_snapshot.emit(_snap("QQQ", 480.0))

        assert tab._model.rowCount() == 2

    def test_duplicate_symbol_updates_in_place(self, qtbot, mock_engine):
        tab = MarketTab(mock_engine)
        qtbot.addWidget(tab)

        mock_engine.market_snapshot.emit(_snap("SPY", 555.0))
        mock_engine.market_snapshot.emit(_snap("SPY", 560.0))

        assert tab._model.rowCount() == 1  # Still 1, not 2

    def test_persisted_favorites_load_on_startup(self, qtbot, mock_engine, tmp_path):
        store = FavoritesStore(tmp_path / "favorites.json")
        store.save([
            FavoriteSymbol(symbol="SPY", sec_type="STK", exchange="SMART")
        ])

        tab = MarketTab(mock_engine, favorites_store=store)
        qtbot.addWidget(tab)

        assert tab._lst_favorites.count() == 1
        assert "SPY" in tab._lst_favorites.item(0).text()

    def test_add_favorite_persists_to_store(self, qtbot, mock_engine, tmp_path):
        store = FavoritesStore(tmp_path / "favorites.json")
        tab = MarketTab(mock_engine, favorites_store=store)
        qtbot.addWidget(tab)

        added = tab._add_favorite("MSFT", "STK", "SMART")

        assert added is True
        assert "MSFT" in store.path.read_text(encoding="utf-8")


class TestWatchlistModel:

    def test_empty(self, qapp):
        m = WatchlistModel()
        assert m.rowCount() == 0
        assert m.columnCount() == 9

    def test_upsert_new(self, qapp):
        m = WatchlistModel()
        m.upsert(_snap("SPY"))
        assert m.rowCount() == 1

    def test_upsert_update(self, qapp):
        m = WatchlistModel()
        m.upsert(_snap("SPY", 550.0))
        m.upsert(_snap("SPY", 560.0))
        assert m.rowCount() == 1
        # Check updated value
        idx = m.index(0, 1)  # Last column
        assert "560.00" in str(m.data(idx))

    def test_data_format(self, qapp):
        m = WatchlistModel()
        m.upsert(_snap("SPY", 555.0))
        assert m.data(m.index(0, 0)) == "SPY"
        assert "$555.00" in str(m.data(m.index(0, 1)))
        assert "50,000,000" in str(m.data(m.index(0, 7)))
