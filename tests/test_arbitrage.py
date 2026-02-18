"""
Tests for agents/arb_hunter.py — User Story 3: Passive arbitrage detection.

TDD: written BEFORE implementation (T027–T030).

Test IDs:
- T027: Put-Call Parity violation → signal written with correct legs
- T028: Box Spread detection → signal written with signal_type="BOX_SPREAD"
- T029: No opportunity → nothing inserted into signals
- T030: Expiry — ACTIVE signal turns EXPIRED when conditions no longer hold
"""
from __future__ import annotations

import math
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.arb_hunter import ArbHunter


# ---------------------------------------------------------------------------
# Minimal option chain structure
# chain[symbol][expiration][strike] = {"call": price, "put": price, "iv": iv}
# plus top-level "underlying_price" and "risk_free_rate"
# ---------------------------------------------------------------------------


def make_chain(
    *,
    strikes: list[float],
    expiration: str = "2025-12-19",
    call_prices: list[float],
    put_prices: list[float],
    underlying_price: float = 5000.0,
    risk_free_rate: float = 0.05,
) -> dict[str, Any]:
    chain: dict[str, Any] = {
        "underlying_price": underlying_price,
        "risk_free_rate": risk_free_rate,
        expiration: {},
    }
    for i, strike in enumerate(strikes):
        chain[expiration][strike] = {
            "call": call_prices[i],
            "put": put_prices[i],
        }
    return chain


# ---------------------------------------------------------------------------
# T027 — Put-Call Parity violation detected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_call_parity_violation_writes_signal() -> None:
    """T027: PCP violation → insert_signal() called with correct legs_json."""
    mock_db = MagicMock()
    mock_db.insert_signal = AsyncMock(return_value="uuid-signal-001")
    mock_db.expire_stale_signals = AsyncMock()

    hunter = ArbHunter(db=mock_db, fee_per_leg=0.65)

    # PCP: C - P should equal F - K*exp(-rT)
    # With S=5000, K=5000, r=0.05, T=1, F ≈ 5000 - 5000*exp(-0.05) ≈ 244
    # We inject a clear violation: C - P = 400 but forward ≈ 244
    chain = make_chain(
        strikes=[5000.0],
        call_prices=[500.0],   # C - P = 200 (forward parity violation)
        put_prices=[300.0],
        underlying_price=5000.0,
        risk_free_rate=0.05,
    )

    await hunter.scan(chain)

    # At least one signal inserted
    assert mock_db.insert_signal.await_count >= 1
    call_kwargs = mock_db.insert_signal.call_args_list[0].kwargs
    assert "PUT_CALL_PARITY" in call_kwargs["signal_type"]
    legs = call_kwargs["legs_json"]
    assert "strike" in legs
    assert legs["strike"] == 5000.0


@pytest.mark.asyncio
async def test_put_call_parity_no_violation_no_signal() -> None:
    """T027: If PCP holds (within fees), no signal should be written."""
    import math
    import datetime

    mock_db = MagicMock()
    mock_db.insert_signal = AsyncMock(return_value="uuid-signal-001")
    mock_db.expire_stale_signals = AsyncMock()

    hunter = ArbHunter(db=mock_db, fee_per_leg=0.65)

    # Compute actual forward parity so C-P is within fee band (±1.30).
    # S=5000, K=5000, r=0.05, use expiration 365 days from today.
    S = 5000.0
    K = 5000.0
    r = 0.05
    T = 1.0  # exactly 1 year
    exp_date = datetime.date.today() + datetime.timedelta(days=365)
    forward_parity = S - K * math.exp(-r * T)  # ≈ 243.8

    # Price C and P so that C - P = forward_parity exactly (no arb)
    call_p = forward_parity + 200.0
    put_p = 200.0

    expiration = exp_date.isoformat()
    chain: dict[str, Any] = {
        "underlying_price": S,
        "risk_free_rate": r,
        expiration: {K: {"call": call_p, "put": put_p}},
    }

    await hunter.scan(chain)

    pcp_calls = [
        c for c in mock_db.insert_signal.call_args_list
        if "PUT_CALL_PARITY" in c.kwargs.get("signal_type", "")
    ]
    assert len(pcp_calls) == 0


# ---------------------------------------------------------------------------
# T028 — Box Spread detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_box_spread_positive_ev_writes_signal() -> None:
    """T028: 4-leg box with net credit > fee_per_leg*4 → signal with type BOX_SPREAD."""
    mock_db = MagicMock()
    mock_db.insert_signal = AsyncMock(return_value="uuid-box-001")
    mock_db.expire_stale_signals = AsyncMock()

    hunter = ArbHunter(db=mock_db, fee_per_leg=0.65)

    # Box spread: (K2 - K1) = theoretical value = 1000 - 900 = 100
    # Box credit = (C_K1 - C_K2) + (P_K2 - P_K1)
    # Inject prices so net credit = 105 > 100 + 4*0.65 fees = 102.60 → arb exists
    expiration = "2025-12-19"
    chain: dict[str, Any] = {
        "underlying_price": 5000.0,
        "risk_free_rate": 0.05,
        expiration: {
            900.0: {"call": 4110.0, "put": 5.0},   # deep ITM call, OTM put
            1000.0: {"call": 4010.0, "put": 15.0},  # deep ITM call, OTM put
        },
    }
    # Box credit = (4110 - 4010) + (15 - 5) = 100 + 10 = 110 > 100 + 2.60 = fair

    await hunter.scan(chain)

    box_calls = [
        c for c in mock_db.insert_signal.call_args_list
        if c.kwargs.get("signal_type") == "BOX_SPREAD"
    ]
    assert len(box_calls) >= 1
    legs = box_calls[0].kwargs["legs_json"]
    assert "lower_strike" in legs
    assert "upper_strike" in legs


# ---------------------------------------------------------------------------
# T029 — No opportunity → nothing inserted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_opportunity_no_signal_written() -> None:
    """T029: All spreads yield negative EV → no rows inserted in signals."""
    mock_db = MagicMock()
    mock_db.insert_signal = AsyncMock(return_value="uuid-no-arb")
    mock_db.expire_stale_signals = AsyncMock()

    hunter = ArbHunter(db=mock_db, fee_per_leg=0.65)

    # Perfectly fair box spread: credit = K2 - K1 = 100, no excess
    expiration = "2025-12-19"
    chain: dict[str, Any] = {
        "underlying_price": 5000.0,
        "risk_free_rate": 0.05,
        expiration: {
            900.0: {"call": 4105.0, "put": 5.0},
            1000.0: {"call": 4005.0, "put": 5.0},
        },
    }
    # Box credit = (4105-4005) + (5-5) = 100 = K2-K1  → fair, no arb after fees

    await hunter.scan(chain)

    assert mock_db.insert_signal.await_count == 0


# ---------------------------------------------------------------------------
# T030 — Expiry: ACTIVE signal becomes EXPIRED when conditions no longer hold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expire_stale_signals_called_with_active_ids() -> None:
    """T030: After scan(), expire_stale_signals() is called so stale signals expire."""
    mock_db = MagicMock()
    mock_db.insert_signal = AsyncMock(return_value="uuid-new-001")
    mock_db.expire_stale_signals = AsyncMock()

    hunter = ArbHunter(db=mock_db, fee_per_leg=0.65)

    # Inject data that produces at least one new signal
    expiration = "2025-12-19"
    chain: dict[str, Any] = {
        "underlying_price": 5000.0,
        "risk_free_rate": 0.05,
        expiration: {
            900.0: {"call": 4110.0, "put": 5.0},
            1000.0: {"call": 4010.0, "put": 15.0},
        },
    }

    await hunter.scan(chain)

    # expire_stale_signals must be called exactly once per scan()
    mock_db.expire_stale_signals.assert_awaited_once()
    # active_ids kwarg must be a list
    call_kwargs = mock_db.expire_stale_signals.call_args.kwargs
    assert "active_ids" in call_kwargs
    assert isinstance(call_kwargs["active_ids"], list)
