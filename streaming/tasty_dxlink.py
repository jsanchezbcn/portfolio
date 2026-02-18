from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable

from core.processor import DataProcessor
from logging_config import get_stream_logger


LOGGER = get_stream_logger()

try:
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Greeks
except Exception:  # pragma: no cover - import tested indirectly
    DXLinkStreamer = None  # type: ignore[assignment]
    Greeks = None  # type: ignore[assignment]


class TastyDXLinkStreamerClient:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        account_id: str,
        processor: DataProcessor,
        reconnect_max_backoff_seconds: int = 30,
    ) -> None:
        self.session_factory = session_factory
        self.account_id = account_id
        self.processor = processor
        self.reconnect_max_backoff_seconds = reconnect_max_backoff_seconds
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    @staticmethod
    def build_streamer_symbols_from_positions(positions: list[dict[str, Any]]) -> list[str]:
        symbols: list[str] = []
        for position in positions:
            candidate = (
                position.get("streamer_symbol")
                or position.get("symbol")
                or position.get("eventSymbol")
                or position.get("contract_key")
            )
            if candidate:
                symbols.append(str(candidate))
        # maintain order while deduping
        return list(dict.fromkeys(symbols))

    def compute_backoff_seconds(self, attempt: int) -> int:
        return int(min(max(1, 2 ** max(0, attempt - 1)), self.reconnect_max_backoff_seconds))

    async def run(self, streamer_symbols: list[str]) -> None:
        if DXLinkStreamer is None or Greeks is None:
            raise RuntimeError("tastytrade SDK is required for DXLink streaming")

        attempt = 0
        self.processor.set_session_state("tastytrade", status="connecting")

        while not self._stop_event.is_set():
            try:
                await self._connect_and_stream(streamer_symbols)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt += 1
                self.processor.set_session_state(
                    "tastytrade",
                    status="degraded",
                    reconnect_attempt=attempt,
                    last_error=str(exc),
                )
                backoff = self.compute_backoff_seconds(attempt)
                LOGGER.warning("Tastytrade DXLink stream error: %s (retry in %ss)", exc, backoff)
                await asyncio.sleep(backoff)

        self.processor.set_session_state("tastytrade", status="disconnected")

    async def _connect_and_stream(self, streamer_symbols: list[str]) -> None:
        session = self.session_factory()
        if DXLinkStreamer is None or Greeks is None:
            raise RuntimeError("tastytrade SDK is required for DXLink streaming")
        greeks_type = Greeks
        async with DXLinkStreamer(session) as streamer:  # type: ignore[misc]
            if streamer_symbols:
                await streamer.subscribe(greeks_type, streamer_symbols)
            self.processor.set_session_state(
                "tastytrade",
                status="connected",
                reconnect_attempt=0,
                subscription_count=len(streamer_symbols),
            )

            while not self._stop_event.is_set():
                message = await streamer.get_event(greeks_type)
                events = message if isinstance(message, list) else [message]
                now = datetime.now(timezone.utc)
                for event in events:
                    payload = self._event_to_payload(event)
                    await self.processor.process_tasty_message(payload, account_id=self.account_id)
                self.processor.set_session_state("tastytrade", message_at=now, heartbeat=now)

    @staticmethod
    def _event_to_payload(event: Any) -> dict[str, Any]:
        payload = {
            "eventSymbol": getattr(event, "event_symbol", None),
            "event_time": getattr(event, "event_time", None),
            "delta": getattr(event, "delta", None),
            "gamma": getattr(event, "gamma", None),
            "theta": getattr(event, "theta", None),
            "vega": getattr(event, "vega", None),
            "rho": getattr(event, "rho", None),
            "volatility": getattr(event, "volatility", None),
            "price": getattr(event, "price", None),
        }
        return payload
