from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Literal, cast

from database.db_manager import DBManager, GreekSnapshotRecord
from logging_config import get_stream_logger
from models.unified_position import InstrumentType, UnifiedPosition


LOGGER = get_stream_logger()


@dataclass(slots=True)
class StreamSessionState:
    broker: str
    status: str = "disconnected"
    last_heartbeat_at: datetime | None = None
    last_message_at: datetime | None = None
    reconnect_attempt: int = 0
    subscription_count: int = 0
    last_error: str | None = None


class DataProcessor:
    def __init__(self, db_manager: DBManager) -> None:
        self.db_manager = db_manager
        self.sessions: dict[str, StreamSessionState] = {
            "ibkr": StreamSessionState(broker="ibkr"),
            "tastytrade": StreamSessionState(broker="tastytrade"),
        }
        self._dedupe_keys: deque[str] = deque()
        self._dedupe_set: set[str] = set()
        self._max_dedupe_size: int = 10_000
        self._latencies_ms: deque[float] = deque(maxlen=20_000)
        self._persist_latencies_ms: deque[float] = deque(maxlen=20_000)
        self._lock = asyncio.Lock()
        self._stream_tasks: dict[str, asyncio.Task] = {}
        self._latency_slo_ms = 500.0
        self._latency_alert_every = 50
        self._persist_latency_alert_every = 50

    async def start(self) -> None:
        await self.db_manager.connect()
        await self.db_manager.start_background_flush()

    async def stop(self) -> None:
        await self.db_manager.close()

    async def process_ibkr_message(self, payload: dict[str, Any], account_id: str) -> bool:
        record = self._normalize_ibkr_payload(payload=payload, account_id=account_id)
        if record is None:
            return False
        return await self._persist_record(record)

    async def process_tasty_message(self, payload: dict[str, Any], account_id: str) -> bool:
        record = self._normalize_tasty_payload(payload=payload, account_id=account_id)
        if record is None:
            return False
        return await self._persist_record(record)

    async def _persist_record(self, record: GreekSnapshotRecord) -> bool:
        dedupe_key = self._make_dedupe_key(record)
        async with self._lock:
            if dedupe_key in self._dedupe_set:
                return False
            self._dedupe_set.add(dedupe_key)
            self._dedupe_keys.append(dedupe_key)
            while len(self._dedupe_set) > self._max_dedupe_size:
                oldest = self._dedupe_keys.popleft()
                self._dedupe_set.discard(oldest)

        now = datetime.now(timezone.utc)
        latency_ms = self.compute_latency_ms(record.received_at, now)
        self._latencies_ms.append(latency_ms)
        self._maybe_alert_latency()
        await self.db_manager.enqueue_snapshot(record)
        return True

    @staticmethod
    def compute_latency_ms(start: datetime, end: datetime) -> float:
        return max(0.0, (end - start).total_seconds() * 1000)

    def record_persist_latency(self, *, received_at: datetime, persisted_at: datetime) -> None:
        self._persist_latencies_ms.append(self.compute_latency_ms(received_at, persisted_at))
        self._maybe_alert_persist_latency()

    def _maybe_alert_latency(self) -> None:
        sample_size = len(self._latencies_ms)
        if sample_size < self._latency_alert_every or sample_size % self._latency_alert_every != 0:
            return
        stats = self.get_latency_stats()
        p95 = stats.get("p95_ms", 0.0)
        if p95 > self._latency_slo_ms:
            LOGGER.warning(
                "Ingestion latency SLO breach: p95=%.2fms over %s samples (target < %.2fms)",
                p95,
                int(stats.get("count", 0.0)),
                self._latency_slo_ms,
            )
        else:
            LOGGER.info(
                "Ingestion latency SLO ok: p95=%.2fms over %s samples (target < %.2fms)",
                p95,
                int(stats.get("count", 0.0)),
                self._latency_slo_ms,
            )

    def _maybe_alert_persist_latency(self) -> None:
        sample_size = len(self._persist_latencies_ms)
        if sample_size < self._persist_latency_alert_every or sample_size % self._persist_latency_alert_every != 0:
            return
        stats = self.get_persist_latency_stats()
        p95 = stats.get("p95_ms", 0.0)
        if p95 > self._latency_slo_ms:
            LOGGER.warning(
                "Persist latency SLO breach: p95=%.2fms over %s samples (target < %.2fms)",
                p95,
                int(stats.get("count", 0.0)),
                self._latency_slo_ms,
            )
        else:
            LOGGER.info(
                "Persist latency SLO ok: p95=%.2fms over %s samples (target < %.2fms)",
                p95,
                int(stats.get("count", 0.0)),
                self._latency_slo_ms,
            )

    @staticmethod
    def _make_dedupe_key(record: GreekSnapshotRecord) -> str:
        return (
            f"{record.broker}|{record.contract_key}|{record.event_time.isoformat()}|"
            f"{record.delta}|{record.gamma}|{record.theta}|{record.vega}|{record.rho}"
        )

    def get_latency_stats(self) -> dict[str, float]:
        if not self._latencies_ms:
            return {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
        values = sorted(self._latencies_ms)

        def percentile(p: float) -> float:
            if not values:
                return 0.0
            idx = min(len(values) - 1, int((p / 100.0) * (len(values) - 1)))
            return float(values[idx])

        return {
            "count": float(len(values)),
            "p50_ms": percentile(50),
            "p95_ms": percentile(95),
            "max_ms": float(values[-1]),
        }

    def get_persist_latency_stats(self) -> dict[str, float]:
        if not self._persist_latencies_ms:
            return {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
        values = sorted(self._persist_latencies_ms)

        def percentile(p: float) -> float:
            idx = min(len(values) - 1, int((p / 100.0) * (len(values) - 1)))
            return float(values[idx])

        return {
            "count": float(len(values)),
            "p50_ms": percentile(50),
            "p95_ms": percentile(95),
            "max_ms": float(values[-1]),
        }

    def start_stream_task(self, broker: str, coroutine: Any) -> None:
        self.stop_stream_task(broker)
        task = asyncio.create_task(coroutine, name=f"stream-{broker}")
        self._stream_tasks[broker] = task
        self.set_session_state(broker, status="connecting")

    def stop_stream_task(self, broker: str) -> None:
        task = self._stream_tasks.pop(broker, None)
        if task and not task.done():
            task.cancel()
        self.set_session_state(broker, status="disconnected")

    async def stop_all_streams(self) -> None:
        tasks = [task for task in self._stream_tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._stream_tasks.clear()
        for broker in list(self.sessions.keys()):
            self.set_session_state(broker, status="disconnected")

    def set_session_state(
        self,
        broker: str,
        *,
        status: str | None = None,
        heartbeat: datetime | None = None,
        message_at: datetime | None = None,
        reconnect_attempt: int | None = None,
        subscription_count: int | None = None,
        last_error: str | None = None,
    ) -> None:
        state = self.sessions.setdefault(broker, StreamSessionState(broker=broker))
        if status is not None:
            state.status = status
        if heartbeat is not None:
            state.last_heartbeat_at = heartbeat
        if message_at is not None:
            state.last_message_at = message_at
        if reconnect_attempt is not None:
            state.reconnect_attempt = reconnect_attempt
        if subscription_count is not None:
            state.subscription_count = subscription_count
        if last_error is not None:
            state.last_error = last_error

    def get_stream_sessions(self) -> list[dict[str, Any]]:
        return [
            {
                "broker": item.broker,
                "status": item.status,
                "lastHeartbeatAt": item.last_heartbeat_at.isoformat() if item.last_heartbeat_at else None,
                "lastMessageAt": item.last_message_at.isoformat() if item.last_message_at else None,
                "reconnectAttempt": item.reconnect_attempt,
                "subscriptionCount": item.subscription_count,
                "lastError": item.last_error,
            }
            for item in self.sessions.values()
        ]

    def _normalize_ibkr_payload(self, *, payload: dict[str, Any], account_id: str) -> GreekSnapshotRecord | None:
        topic = str(payload.get("topic") or "")
        topic_conid: str | None = None
        if topic.startswith("smd+"):
            topic_parts = topic.split("+", 2)
            if len(topic_parts) >= 2 and topic_parts[1].strip().isdigit():
                topic_conid = topic_parts[1].strip()

        contract_key = payload.get("contract_key") or payload.get("symbol") or payload.get("conid") or topic_conid
        if not contract_key:
            return None

        event_time = self._parse_event_time(payload.get("event_time") or payload.get("_updated"))
        received_at = datetime.now(timezone.utc)
        underlying = str(payload.get("underlying") or self._guess_underlying(contract_key)).upper()

        return GreekSnapshotRecord(
            event_time=event_time,
            received_at=received_at,
            broker="ibkr",
            account_id=account_id,
            underlying=underlying,
            contract_key=contract_key,
            expiration=self._parse_expiration(payload.get("expiration")),
            strike=self._safe_float(payload.get("strike")),
            option_type=self._normalize_option_type(payload.get("option_type")),
            quantity=self._safe_float(payload.get("quantity")),
            delta=self._extract_ibkr_greek(payload, "delta"),
            gamma=self._extract_ibkr_greek(payload, "gamma"),
            theta=self._extract_ibkr_greek(payload, "theta"),
            vega=self._extract_ibkr_greek(payload, "vega"),
            rho=self._safe_float(payload.get("rho")),
            implied_volatility=self._safe_float(payload.get("implied_volatility") or payload.get("iv")),
            underlying_price=self._safe_float(payload.get("underlying_price")),
            source_payload=payload,
        )

    @classmethod
    def _extract_ibkr_greek(cls, payload: dict[str, Any], greek_name: str) -> float | None:
        direct_candidates = {
            "delta": ["delta", "optDelta", "modelDelta", "7308"],
            "gamma": ["gamma", "optGamma", "modelGamma", "7309"],
            "theta": ["theta", "optTheta", "modelTheta", "7310"],
            "vega": ["vega", "optVega", "modelVega", "7311"],
        }
        for key in direct_candidates.get(greek_name, []):
            value = cls._safe_float(payload.get(key))
            if value is not None:
                return value
        nested = payload.get("greeks")
        if isinstance(nested, dict):
            value = cls._safe_float(nested.get(greek_name))
            if value is not None:
                return value
        return None

    def _normalize_tasty_payload(self, *, payload: dict[str, Any], account_id: str) -> GreekSnapshotRecord | None:
        contract_key = payload.get("contract_key") or payload.get("eventSymbol") or payload.get("symbol")
        if not contract_key:
            return None

        event_time = self._parse_event_time(payload.get("event_time") or payload.get("eventTime") or payload.get("time"))
        received_at = datetime.now(timezone.utc)
        underlying = str(payload.get("underlying") or self._guess_underlying(contract_key)).upper().lstrip("/")

        return GreekSnapshotRecord(
            event_time=event_time,
            received_at=received_at,
            broker="tastytrade",
            account_id=account_id,
            underlying=underlying,
            contract_key=contract_key,
            expiration=self._parse_expiration(payload.get("expiration")),
            strike=self._safe_float(payload.get("strike")),
            option_type=self._normalize_option_type(payload.get("option_type")),
            quantity=self._safe_float(payload.get("quantity")),
            delta=self._safe_float(payload.get("delta")),
            gamma=self._safe_float(payload.get("gamma")),
            theta=self._safe_float(payload.get("theta")),
            vega=self._safe_float(payload.get("vega")),
            rho=self._safe_float(payload.get("rho")),
            implied_volatility=self._safe_float(payload.get("implied_volatility") or payload.get("volatility")),
            underlying_price=self._safe_float(payload.get("underlying_price") or payload.get("price")),
            source_payload=payload,
        )

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, str) and value.strip().lower() in {"", "nan", "none"}:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_expiration(value: Any) -> date | None:
        if value is None:
            return None
        if isinstance(value, date):
            return value
        text = str(value)
        try:
            if "-" in text:
                return datetime.strptime(text, "%Y-%m-%d").date()
            if len(text) == 8:
                return datetime.strptime(text, "%Y%m%d").date()
        except ValueError:
            return None
        return None

    @staticmethod
    def _parse_event_time(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if value is None:
            return datetime.now(timezone.utc)

        if isinstance(value, (int, float)):
            timestamp = float(value)
            if timestamp > 1e12:
                timestamp /= 1000.0
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)

        text = str(value)
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            pass
        try:
            parsed = float(text)
            if parsed > 1e12:
                parsed /= 1000.0
            return datetime.fromtimestamp(parsed, tz=timezone.utc)
        except ValueError:
            return datetime.now(timezone.utc)

    @staticmethod
    def _normalize_option_type(value: Any) -> str | None:
        if value is None:
            return None
        v = str(value).lower()
        if v in {"c", "call"}:
            return "call"
        if v in {"p", "put"}:
            return "put"
        return None

    @staticmethod
    def _as_literal_option_type(value: str | None) -> Literal["call", "put"] | None:
        if value in {"call", "put"}:
            return cast(Literal["call", "put"], value)
        return None

    @staticmethod
    def _guess_underlying(contract_key: str) -> str:
        # Best-effort extraction for symbols like .HPQ260227C22 or HPQ_20260227_22_call
        cleaned = contract_key.replace(".", "").lstrip("/")
        for sep in ("_", ":"):
            if sep in cleaned:
                return cleaned.split(sep, 1)[0]
        letters = []
        for ch in cleaned:
            if ch.isalpha():
                letters.append(ch)
            else:
                break
        return "".join(letters) or cleaned

    @staticmethod
    def to_unified_position(record: GreekSnapshotRecord) -> UnifiedPosition:
        instrument_type = InstrumentType.OPTION if record.option_type else InstrumentType.EQUITY
        expiration = record.expiration
        if instrument_type == InstrumentType.OPTION and expiration is None:
            expiration = date.today()

        return UnifiedPosition(
            symbol=record.contract_key,
            instrument_type=instrument_type,
            broker=record.broker,
            quantity=record.quantity or 0.0,
            avg_price=record.underlying_price or 0.0,
            market_value=0.0,
            unrealized_pnl=0.0,
            delta=record.delta or 0.0,
            gamma=record.gamma or 0.0,
            theta=record.theta or 0.0,
            vega=record.vega or 0.0,
            spx_delta=0.0,
            underlying=record.underlying,
            strike=record.strike,
            expiration=expiration,
            option_type=DataProcessor._as_literal_option_type(record.option_type),
            iv=record.implied_volatility,
            greeks_source=record.broker,
            timestamp=record.event_time,
        )
