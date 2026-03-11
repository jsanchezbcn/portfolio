from __future__ import annotations

from dataclasses import replace

from desktop.engine.ib_engine import PositionRow
from desktop.models.strategy_reconstructor import StrategyReconstructor
from desktop.models.trade_groups import TradeGroupsModel


def _row(**overrides) -> PositionRow:
    base = PositionRow(
        conid=1,
        symbol="AAPL",
        sec_type="OPT",
        underlying="AAPL",
        strike=200.0,
        right="C",
        expiry="20260417",
        quantity=1.0,
        avg_cost=5.0,
        market_price=5.0,
        market_value=500.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        delta=10.0,
        gamma=1.0,
        theta=-2.0,
        vega=3.0,
        iv=0.25,
        spx_delta=4.0,
    )
    return replace(base, **overrides)


def test_reconstructor_identifies_covered_call():
    reconstructor = StrategyReconstructor(account_id="U1")
    groups = reconstructor.reconstruct([
        _row(
            conid=10,
            symbol="AAPL",
            sec_type="STK",
            underlying="",
            strike=None,
            right=None,
            expiry=None,
            quantity=100.0,
            avg_cost=150.0,
            market_price=190.0,
            market_value=19000.0,
            delta=100.0,
            gamma=0.0,
            theta=0.0,
            vega=0.0,
            iv=None,
            spx_delta=38.0,
        ),
        _row(conid=11, strike=210.0, quantity=-1.0, avg_cost=4.0),
    ])

    assert len(groups) == 1
    assert groups[0].strategy_name == "Covered Call"
    assert len(groups[0].legs) == 2


def test_reconstructor_identifies_bull_call_spread():
    reconstructor = StrategyReconstructor(account_id="U1")
    groups = reconstructor.reconstruct([
        _row(conid=21, strike=200.0, quantity=1.0),
        _row(conid=22, strike=210.0, quantity=-1.0),
    ])

    assert len(groups) == 1
    assert groups[0].strategy_name == "Bull Call Spread"


def test_reconstructor_identifies_calendar_spread():
    reconstructor = StrategyReconstructor(account_id="U1")
    groups = reconstructor.reconstruct([
        _row(conid=31, strike=200.0, expiry="20260417", quantity=-1.0),
        _row(conid=32, strike=200.0, expiry="20260515", quantity=1.0),
    ])

    assert len(groups) == 1
    assert groups[0].strategy_name == "Calendar Spread"


def test_reconstructor_identifies_iron_condor():
    reconstructor = StrategyReconstructor(account_id="U1")
    groups = reconstructor.reconstruct([
        _row(conid=41, right="P", strike=90.0, quantity=1.0),
        _row(conid=42, right="P", strike=95.0, quantity=-1.0),
        _row(conid=43, right="C", strike=105.0, quantity=-1.0),
        _row(conid=44, right="C", strike=110.0, quantity=1.0),
    ])

    assert len(groups) == 1
    assert groups[0].strategy_name == "Iron Condor"
    assert sorted(groups[0].leg_ids) == [41, 42, 43, 44]


def test_reconstructor_does_not_treat_bull_call_spread_as_iron_condor_wing():
    reconstructor = StrategyReconstructor(account_id="U1")
    groups = reconstructor.reconstruct([
        _row(conid=51, right="P", strike=6635.0, quantity=1.0),
        _row(conid=52, right="P", strike=6660.0, quantity=-1.0),
        _row(conid=53, right="C", strike=6815.0, quantity=1.0),
        _row(conid=54, right="C", strike=6910.0, quantity=-1.0),
    ])

    assert {group.strategy_name for group in groups} == {"Bear Put Spread", "Bull Call Spread"}


def test_reconstructor_labels_unmatched_leg_as_single():
    reconstructor = StrategyReconstructor(account_id="U1")
    groups = reconstructor.reconstruct([
        _row(conid=51, right="P", strike=180.0, quantity=1.0),
    ])

    assert len(groups) == 1
    assert groups[0].strategy_name == "Single Leg / Naked"


def test_trade_groups_model_sorts_by_strategy_metric(qapp):
    model = TradeGroupsModel()
    model.set_sorting("theta", descending=True, absolute=True)
    model.set_data([
        _row(conid=61, symbol="AAPL 200C", underlying="AAPL", theta=-2.0, quantity=1.0),
        _row(conid=62, symbol="MSFT 300C", underlying="MSFT", theta=-15.0, quantity=1.0),
    ])

    assert model.rowCount() == 4
    assert model.data(model.index(0, 1)) == "MSFT"
    assert "Single Leg / Naked" in model.data(model.index(0, 0))
