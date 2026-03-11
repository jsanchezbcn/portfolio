"""database/business_store.py — shared Postgres-backed business-data store.

This module provides a common persistence surface for business data used across
both the desktop app and the dashboard. It deliberately wraps the existing
PostgreSQL runtime wrapper so business-data features can converge on one API
without duplicating LocalStore semantics.
"""
from __future__ import annotations

import os
from typing import Any

from desktop.db.database import Database


def _build_dsn() -> str:
    explicit = os.environ.get("PORTFOLIO_DB_URL")
    if explicit:
        return explicit.replace("postgresql+psycopg2://", "postgresql://")
    host = os.environ.get("DB_HOST", "localhost").strip()
    port = os.environ.get("DB_PORT", "5432").strip()
    name = os.environ.get("DB_NAME", "portfolio_engine").strip()
    user = os.environ.get("DB_USER", "portfolio").strip()
    pw = os.environ.get("DB_PASS", "yazooo").strip()
    return f"postgresql://{user}:{pw}@{host}:{port}/{name}"


class PostgresBusinessStore:
    """Shared Postgres business-data store used by dashboard and desktop features."""

    def __init__(self, dsn: str | None = None, database: Database | None = None) -> None:
        self._db = database or Database(dsn or _build_dsn())

    async def connect(self) -> None:
        await self._db.connect()

    async def close(self) -> None:
        await self._db.close()

    async def upsert_market_intel(self, **kwargs: Any) -> str:
        await self.connect()
        return await self._db.upsert_market_intel(**kwargs)

    async def get_recent_market_intel(self, *, limit: int = 20) -> list[dict[str, Any]]:
        await self.connect()
        return await self._db.get_recent_market_intel(limit=limit)

    async def get_market_intel_by_source(
        self,
        source: str,
        *,
        symbol: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        await self.connect()
        return await self._db.get_market_intel_by_source(source, symbol=symbol, limit=limit)

    async def create_journal_note(self, **kwargs: Any):
        await self.connect()
        return await self._db.create_journal_note(**kwargs)

    async def list_journal_notes(self, **kwargs: Any):
        await self.connect()
        return await self._db.list_journal_notes(**kwargs)

    async def record_fill(self, entry: Any) -> str:
        """Map a legacy trade-journal fill entry into a Postgres journal note."""
        await self.connect()
        underlying = getattr(entry, "underlying", "") or "Trade"
        strategy_tag = getattr(entry, "strategy_tag", None)
        status = getattr(entry, "status", None) or "FILLED"
        title = f"{underlying} {strategy_tag or 'trade'} {status}".strip()
        body_parts = [
            f"Broker order id: {getattr(entry, 'broker_order_id', None) or '—'}",
            f"Status: {status}",
            f"Rationale: {getattr(entry, 'user_rationale', None) or getattr(entry, 'ai_rationale', None) or '—'}",
        ]
        regime = getattr(entry, "regime", None)
        if regime:
            body_parts.append(f"Regime: {regime}")
        vix_at_fill = getattr(entry, "vix_at_fill", None)
        if vix_at_fill is not None:
            body_parts.append(f"VIX at fill: {vix_at_fill}")
        tags = [tag for tag in [strategy_tag, status.lower() if isinstance(status, str) else None, underlying.lower() if underlying else None] if tag]
        note_id = await self._db.create_journal_note(
            account_id=getattr(entry, "account_id", None),
            title=title,
            body="\n".join(body_parts),
            tags=tags,
        )
        return str(note_id)

    async def capture_snapshot(self, snapshot: Any) -> None:
        await self.connect()
        payload = snapshot if isinstance(snapshot, dict) else {
            "account_id": getattr(snapshot, "account_id", ""),
            "net_liquidation": getattr(snapshot, "net_liquidation", None),
            "cash_balance": getattr(snapshot, "cash_balance", None),
            "buying_power": getattr(snapshot, "buying_power", None),
            "init_margin": getattr(snapshot, "init_margin", None),
            "maint_margin": getattr(snapshot, "maint_margin", None),
            "unrealized_pnl": getattr(snapshot, "unrealized_pnl", None),
            "realized_pnl": getattr(snapshot, "realized_pnl", None),
            "spx_delta": getattr(snapshot, "spx_delta", None),
            "gamma": getattr(snapshot, "gamma", None),
            "theta": getattr(snapshot, "theta", None),
            "vega": getattr(snapshot, "vega", None),
            "vix": getattr(snapshot, "vix", None),
            "regime": getattr(snapshot, "regime", None),
            "margin_used_pct": getattr(snapshot, "margin_used_pct", None),
        }
        await self._db.capture_portfolio_snapshot(payload)

    async def query_snapshots(self, **kwargs: Any) -> list[dict[str, Any]]:
        await self.connect()
        return await self._db.query_snapshots(**kwargs)
