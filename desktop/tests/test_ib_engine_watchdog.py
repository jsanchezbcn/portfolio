from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from desktop.engine.ib_engine import IBEngine


@pytest.mark.asyncio
async def test_attempt_reconnect_emits_connected_after_success():
    engine = IBEngine()
    engine._ib = MagicMock()
    engine._ib.connectAsync = AsyncMock(side_effect=[RuntimeError("down"), None])
    engine._ib.managedAccounts.return_value = ["U777"]
    engine._active_chain_request = None

    states: list[tuple[str, str]] = []
    connected = []
    engine.connection_state.connect(lambda state, detail: states.append((state, detail)))
    engine.connected.connect(lambda: connected.append(True))

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("desktop.engine.ib_engine.asyncio.sleep", fake_sleep)
        await engine._attempt_reconnect()

    assert connected == [True]
    assert engine.account_id == "U777"
    assert any(state == "reconnecting" for state, _ in states)
    assert any(state == "reconnected" for state, _ in states)
    assert sleep_calls[:2] == [1, 2]


@pytest.mark.asyncio
async def test_attempt_reconnect_emits_failed_after_max_attempts():
    engine = IBEngine()
    engine._watchdog_max_attempts = 2
    engine._ib = MagicMock()
    engine._ib.connectAsync = AsyncMock(side_effect=RuntimeError("still down"))

    states: list[tuple[str, str]] = []
    errors: list[str] = []
    engine.connection_state.connect(lambda state, detail: states.append((state, detail)))
    engine.error_occurred.connect(lambda msg: errors.append(msg))

    async def fake_sleep(_delay: float) -> None:
        return None

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("desktop.engine.ib_engine.asyncio.sleep", fake_sleep)
        await engine._attempt_reconnect()

    assert states[-1][0] == "failed"
    assert errors and "Unable to reconnect" in errors[-1]