from __future__ import annotations

from core.processor import DataProcessor
from streaming.tasty_dxlink import TastyDXLinkStreamerClient


class _FakeDBManager:
    async def connect(self) -> None:
        return None

    async def start_background_flush(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def enqueue_snapshot(self, record) -> None:
        return None


def _fake_session() -> object:
    return object()


def test_tasty_streamer_symbol_mapping_dedupes_ordered() -> None:
    positions = [
        {"streamer_symbol": ".AAPL260220C200"},
        {"streamer_symbol": ".AAPL260220C200"},
        {"symbol": ".MSFT260220P450"},
    ]

    symbols = TastyDXLinkStreamerClient.build_streamer_symbols_from_positions(positions)

    assert symbols == [".AAPL260220C200", ".MSFT260220P450"]


def test_tasty_backoff_is_bounded() -> None:
    processor = DataProcessor(db_manager=_FakeDBManager())  # type: ignore[arg-type]
    client = TastyDXLinkStreamerClient(
        session_factory=_fake_session,
        account_id="ACC-1",
        processor=processor,
        reconnect_max_backoff_seconds=16,
    )

    assert client.compute_backoff_seconds(1) == 1
    assert client.compute_backoff_seconds(2) == 2
    assert client.compute_backoff_seconds(3) == 4
    assert client.compute_backoff_seconds(10) == 16
