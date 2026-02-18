from __future__ import annotations

import asyncio
import json
import os
import ssl
from urllib.parse import urlparse
from datetime import datetime, timezone
from typing import Any, Iterable

import requests
import websockets

from core.processor import DataProcessor
from logging_config import get_stream_logger


LOGGER = get_stream_logger()


class IBKRWebSocketClient:
    def __init__(
        self,
        *,
        url: str,
        account_id: str,
        processor: DataProcessor,
        heartbeat_interval_seconds: int = 60,
        reconnect_max_backoff_seconds: int = 30,
        verify_tls: bool = False,
    ) -> None:
        self.url = url
        self.account_id = account_id
        self.processor = processor
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.reconnect_max_backoff_seconds = reconnect_max_backoff_seconds
        self.verify_tls = verify_tls
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    @staticmethod
    def build_account_select_command(account_id: str) -> str | None:
        value = str(account_id or "").strip()
        if not value:
            return None
        return f"act+{value}"

    @staticmethod
    def _extract_conid(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            return text
        if text.lower().startswith("smd+"):
            parts = text.split("+", 2)
            if len(parts) >= 2 and parts[1].isdigit():
                return parts[1]
        return None

    @staticmethod
    def _market_data_fields() -> list[int | str]:
        raw = str(os.getenv("IBKR_WS_MARKETDATA_FIELDS", "31,83,84,86")).strip()
        fields: list[int | str] = []
        for token in raw.split(","):
            value = token.strip()
            if not value:
                continue
            if value.isdigit():
                fields.append(int(value))
            else:
                fields.append(value)
        return fields

    @staticmethod
    def _use_snapshot_mode() -> bool:
        raw = str(os.getenv("IBKR_WS_SNAPSHOT", "1")).strip().lower()
        return raw in {"1", "true", "yes", "on"}

    @classmethod
    def build_subscription_commands(cls, contract_keys: Iterable[str]) -> list[str]:
        fields = cls._market_data_fields()
        snapshot = cls._use_snapshot_mode()
        conids: list[str] = []
        for contract_key in contract_keys:
            conid = cls._extract_conid(contract_key)
            if conid:
                conids.append(conid)

        conids = list(dict.fromkeys(conids))
        if not conids:
            return []

        fields_payload = json.dumps({"fields": fields, "snapshot": snapshot}, separators=(",", ":"))
        return [f"smd+{conid}+{fields_payload}" for conid in conids]

    @staticmethod
    def build_subscription_payload(contract_keys: Iterable[str]) -> dict[str, Any]:
        channels = [
            {"name": "greeks", "contractKey": key}
            for key in contract_keys
            if key
        ]
        return {"op": "subscribe", "channels": channels}

    def _get_ssl_context(self) -> ssl.SSLContext | None:
        if not self.url.startswith("wss://"):
            return None
        context = ssl.create_default_context()
        if not self.verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def _rest_base_url(self) -> str | None:
        parsed = urlparse(self.url)
        if not parsed.scheme or not parsed.netloc:
            return None
        http_scheme = "https" if parsed.scheme == "wss" else "http"
        return f"{http_scheme}://{parsed.netloc}"

    async def _ensure_account_selected(self) -> None:
        base_url = self._rest_base_url()
        if not base_url or not self.account_id:
            return

        endpoint = f"{base_url}/v1/api/iserver/account"

        def _post() -> requests.Response:
            return requests.post(
                endpoint,
                json={"acctId": self.account_id},
                timeout=5,
                verify=self.verify_tls,
            )

        try:
            response = await asyncio.to_thread(_post)
            if response.status_code != 200:
                LOGGER.debug("IBKR account selection request returned %s: %s", response.status_code, response.text)
        except Exception as exc:
            LOGGER.debug("IBKR account selection request failed: %s", exc)

    async def _prime_market_data(self, contract_keys: list[str]) -> None:
        base_url = self._rest_base_url()
        if not base_url:
            return

        conids: list[str] = []
        for contract_key in contract_keys:
            conid = self._extract_conid(contract_key)
            if conid:
                conids.append(conid)

        conids = list(dict.fromkeys(conids))
        if not conids:
            return

        fields = self._market_data_fields()
        field_tokens = [str(field) for field in fields]

        endpoint = f"{base_url}/v1/api/iserver/marketdata/snapshot"
        params = {
            "conids": ",".join(conids),
            "fields": ",".join(field_tokens),
        }

        def _get() -> requests.Response:
            return requests.get(endpoint, params=params, timeout=8, verify=self.verify_tls)

        try:
            response = await asyncio.to_thread(_get)
            if response.status_code != 200:
                LOGGER.debug("IBKR marketdata prime request returned %s: %s", response.status_code, response.text)
        except Exception as exc:
            LOGGER.debug("IBKR marketdata prime request failed: %s", exc)

    def compute_backoff_seconds(self, attempt: int) -> int:
        base = min(max(1, 2 ** max(0, attempt - 1)), self.reconnect_max_backoff_seconds)
        return int(base)

    async def run(self, contract_keys: list[str]) -> None:
        attempt = 0
        self.processor.set_session_state("ibkr", status="connecting")

        while not self._stop_event.is_set():
            try:
                await self._connect_and_stream(contract_keys)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt += 1
                self.processor.set_session_state(
                    "ibkr",
                    status="degraded",
                    reconnect_attempt=attempt,
                    last_error=str(exc),
                )
                sleep_seconds = self.compute_backoff_seconds(attempt)
                LOGGER.warning("IBKR websocket stream error: %s (retry in %ss)", exc, sleep_seconds)
                await asyncio.sleep(sleep_seconds)

        self.processor.set_session_state("ibkr", status="disconnected")

    async def _connect_and_stream(self, contract_keys: list[str]) -> None:
        await self._ensure_account_selected()
        await self._prime_market_data(contract_keys)
        ssl_context = self._get_ssl_context()
        async with websockets.connect(self.url, ssl=ssl_context, ping_interval=None) as websocket:
            self.processor.set_session_state("ibkr", status="connected", reconnect_attempt=0)

            account_command = self.build_account_select_command(self.account_id)
            if account_command:
                await websocket.send(account_command)

            commands = self.build_subscription_commands(contract_keys)
            if commands:
                for command in commands:
                    await websocket.send(command)
                self.processor.set_session_state("ibkr", subscription_count=len(commands))
            else:
                payload = self.build_subscription_payload(contract_keys)
                await websocket.send(json.dumps(payload))
                self.processor.set_session_state("ibkr", subscription_count=len(payload["channels"]))

            heartbeat_task = asyncio.create_task(self._heartbeat_loop(websocket), name="ibkr-heartbeat")
            try:
                async for raw_message in websocket:
                    if self._stop_event.is_set():
                        break
                    await self._handle_message(raw_message)
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

    async def _heartbeat_loop(self, websocket: Any) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(self.heartbeat_interval_seconds)
            await websocket.send("ech+hb")
            now = datetime.now(timezone.utc)
            self.processor.set_session_state("ibkr", heartbeat=now)

    async def _handle_message(self, raw_message: str | bytes) -> None:
        if isinstance(raw_message, bytes):
            text = raw_message.decode("utf-8", errors="ignore")
        else:
            text = raw_message

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            LOGGER.debug("Skipping non-JSON IBKR message: %s", text)
            return

        await self.processor.process_ibkr_message(payload, account_id=self.account_id)
        self.processor.set_session_state("ibkr", message_at=datetime.now(timezone.utc))
