from __future__ import annotations

from core.processor import DataProcessor
from streaming.ibkr_ws import IBKRWebSocketClient


class _FakeDBManager:
    async def connect(self) -> None:
        return None

    async def start_background_flush(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def enqueue_snapshot(self, record) -> None:
        return None


def test_ibkr_subscription_commands_use_smd_conid_format() -> None:
    commands = IBKRWebSocketClient.build_subscription_commands(["853090008", "853090008", "845000792"])

    assert len(commands) == 2
    assert commands[0].startswith("smd+853090008+")
    assert commands[1].startswith("smd+845000792+")


def test_ibkr_backoff_is_bounded() -> None:
    processor = DataProcessor(db_manager=_FakeDBManager())  # type: ignore[arg-type]
    client = IBKRWebSocketClient(
        url="wss://localhost:5000/v1/api/ws",
        account_id="DU123",
        processor=processor,
        reconnect_max_backoff_seconds=8,
    )

    assert client.compute_backoff_seconds(1) == 1
    assert client.compute_backoff_seconds(2) == 2
    assert client.compute_backoff_seconds(3) == 4
    assert client.compute_backoff_seconds(5) == 8


def test_ibkr_account_select_command() -> None:
    assert IBKRWebSocketClient.build_account_select_command("U2052408") == "act+U2052408"
    assert IBKRWebSocketClient.build_account_select_command("") is None
