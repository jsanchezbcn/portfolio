#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from agents.llm_client import async_list_models, async_llm_chat
from desktop.db.database import Database
from desktop.engine.ib_engine import IBEngine, PortfolioRiskSummary, PositionRow


def _build_db_dsn() -> str:
    explicit = os.environ.get("PORTFOLIO_DB_URL")
    if explicit:
        return explicit.replace("postgresql+psycopg2://", "postgresql://")

    host = os.environ.get("DB_HOST", "localhost").strip()
    port = os.environ.get("DB_PORT", "5432").strip()
    name = os.environ.get("DB_NAME", "portfolio_engine").strip()
    user = os.environ.get("DB_USER", "portfolio").strip()
    password = os.environ.get("DB_PASS", "yazooo").strip()
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


def _default_account_id() -> str:
    explicit = (os.environ.get("IBKR_ACCOUNT_ID") or "").strip()
    if explicit:
        return explicit
    accounts = [value.strip() for value in os.environ.get("IB_ACCOUNTS", "").split(",") if value.strip()]
    return accounts[0] if accounts else ""


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _print_payload(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=_json_default))
        return
    if isinstance(payload, str):
        print(payload)
        return
    print(json.dumps(payload, indent=2, default=_json_default))


def _compute_portfolio_greeks(positions: list[dict[str, Any]]) -> dict[str, Any]:
    total_delta = sum(float(item.get("delta") or 0.0) for item in positions)
    total_gamma = sum(float(item.get("gamma") or 0.0) for item in positions)
    total_theta = sum(float(item.get("theta") or 0.0) for item in positions)
    total_vega = sum(float(item.get("vega") or 0.0) for item in positions)
    total_spx_delta = sum(float(item.get("spx_delta") or 0.0) for item in positions)
    underlying_prices = [float(item.get("underlying_price")) for item in positions if item.get("underlying_price") is not None]
    return {
        "total_delta": total_delta,
        "total_gamma": total_gamma,
        "total_theta": total_theta,
        "total_vega": total_vega,
        "total_spx_delta": total_spx_delta,
        "underlying_price": underlying_prices[0] if underlying_prices else None,
    }


def _compute_portfolio_metrics(positions: list[dict[str, Any]], account: dict[str, Any] | None = None) -> dict[str, Any]:
    greeks = _compute_portfolio_greeks(positions)
    total_value = sum(float(item.get("market_value") or 0.0) for item in positions)
    gross_exposure = sum(abs(float(item.get("market_value") or 0.0)) for item in positions)
    total_vega = float(greeks.get("total_vega") or 0.0)
    total_theta = float(greeks.get("total_theta") or 0.0)
    return {
        "total_positions": len(positions),
        "total_value": total_value,
        "total_spx_delta": greeks.get("total_spx_delta"),
        "total_delta": greeks.get("total_delta"),
        "total_gamma": greeks.get("total_gamma"),
        "total_theta": greeks.get("total_theta"),
        "total_vega": greeks.get("total_vega"),
        "theta_vega_ratio": (total_theta / total_vega) if total_vega else 0.0,
        "gross_exposure": gross_exposure,
        "net_exposure": total_value,
        "options_count": sum(1 for item in positions if item.get("sec_type") in {"OPT", "FOP"}),
        "stocks_count": sum(1 for item in positions if item.get("sec_type") == "STK"),
        "nlv": None if not account else account.get("net_liquidation"),
        "buying_power": None if not account else account.get("buying_power"),
        "init_margin": None if not account else account.get("init_margin"),
        "maint_margin": None if not account else account.get("maint_margin"),
    }


def _serialize_rows(rows: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        if is_dataclass(row):
            result.append(asdict(row))
        elif isinstance(row, dict):
            result.append(dict(row))
        else:
            result.append(dict(vars(row)))
    return result


class DesktopCLIService:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.account_id = args.account or _default_account_id()
        self.db = Database(args.db)
        self.db_connected = False
        self.engine: IBEngine | None = None
        self._live_positions: list[dict[str, Any]] | None = None
        self._live_account: dict[str, Any] | None = None

    async def connect_db(self) -> bool:
        if self.db_connected:
            return True
        try:
            await self.db.connect()
            self.db_connected = True
            return True
        except Exception:
            return False

    async def ensure_engine(self) -> IBEngine:
        if self.engine is not None:
            return self.engine

        engine = IBEngine(
            host=self.args.host,
            port=self.args.port,
            client_id=self.args.client_id,
            db_dsn=self.args.db,
        )
        if self.account_id:
            engine._account_id = self.account_id
        await engine.connect()
        if engine.account_id:
            self.account_id = engine.account_id
        self.engine = engine
        return engine

    async def close(self) -> None:
        if self.engine is not None:
            await self.engine.disconnect()
            self.engine = None
        elif self.db_connected:
            await self.db.close()
        self.db_connected = False

    async def get_positions(self, *, force_live: bool = False) -> tuple[list[dict[str, Any]], str]:
        if not force_live and self.account_id and await self.connect_db():
            cached = await self.db.get_cached_positions(self.account_id, max_age_seconds=self.args.cache_ttl)
            if cached:
                return cached, "cache"

        engine = await self.ensure_engine()
        rows = await engine.refresh_positions()
        payload = _serialize_rows(rows)
        self._live_positions = payload
        return payload, "live"

    async def get_account(self, *, force_live: bool = False) -> tuple[dict[str, Any] | None, str]:
        if not force_live and self.account_id and await self.connect_db():
            cached = await self.db.get_cached_account_snapshot(self.account_id, max_age_seconds=self.args.cache_ttl)
            if cached:
                return cached, "cache"

        engine = await self.ensure_engine()
        summary = await engine.refresh_account()
        if summary is None:
            return None, "live"
        payload = asdict(summary)
        self._live_account = payload
        return payload, "live"

    async def get_portfolio_greeks(self, *, force_live: bool = False) -> tuple[dict[str, Any] | None, str]:
        if not force_live and self.account_id and await self.connect_db():
            cached = await self.db.get_cached_portfolio_greeks(self.account_id, max_age_seconds=self.args.cache_ttl)
            if cached:
                return cached, "cache"

        positions, _ = await self.get_positions(force_live=True)
        return _compute_portfolio_greeks(positions), "live"

    async def get_portfolio_metrics(self, *, force_live: bool = False) -> tuple[dict[str, Any] | None, str]:
        if not force_live and self.account_id and await self.connect_db():
            cached = await self.db.get_cached_portfolio_metrics(self.account_id, max_age_seconds=self.args.cache_ttl)
            if cached:
                return cached, "cache"

        positions, _ = await self.get_positions(force_live=True)
        account, _ = await self.get_account(force_live=True)
        return _compute_portfolio_metrics(positions, account), "live"

    async def get_summary(self, *, force_live: bool = False) -> tuple[dict[str, Any], str]:
        positions, positions_source = await self.get_positions(force_live=force_live)
        account, account_source = await self.get_account(force_live=force_live)
        greeks, greeks_source = await self.get_portfolio_greeks(force_live=force_live)
        metrics, metrics_source = await self.get_portfolio_metrics(force_live=force_live)
        payload = {
            "account_id": self.account_id,
            "sources": {
                "positions": positions_source,
                "account": account_source,
                "greeks": greeks_source,
                "metrics": metrics_source,
            },
            "account": account,
            "greeks": greeks,
            "metrics": metrics,
            "positions_count": len(positions),
        }
        return payload, "mixed"

    async def get_expiries(self, underlying: str, sec_type: str, exchange: str, *, force_live: bool = False) -> tuple[list[str], str]:
        normalized_underlying = underlying.upper()
        if not force_live and await self.connect_db():
            cached = await self.db.get_cached_expirations(normalized_underlying, sec_type=sec_type, exchange=exchange)
            if cached:
                return cached, "cache"

        engine = await self.ensure_engine()
        expiries = await engine.get_available_expiries(normalized_underlying, sec_type=sec_type, exchange=exchange)
        return expiries, "live"

    async def get_chain(self, underlying: str, expiry: str, sec_type: str, exchange: str, *, force_live: bool = False) -> tuple[list[dict[str, Any]], str]:
        normalized_underlying = underlying.upper()
        if not force_live and await self.connect_db():
            cached = await self.db.get_cached_chain(normalized_underlying, expiry, max_age_seconds=self.args.cache_ttl)
            if cached:
                return cached, "cache"

        engine = await self.ensure_engine()
        rows = await engine.get_chain(normalized_underlying, expiry, sec_type=sec_type, exchange=exchange)
        return _serialize_rows(rows), "live"

    async def run_whatif(self, legs: list[dict[str, Any]], order_type: str, limit_price: float | None) -> dict[str, Any]:
        engine = await self.ensure_engine()
        return await engine.whatif_order(legs, order_type=order_type, limit_price=limit_price)

    async def build_chat_context(self, *, force_live: bool = False) -> dict[str, Any]:
        positions, positions_source = await self.get_positions(force_live=force_live)
        account, account_source = await self.get_account(force_live=force_live)
        greeks, greeks_source = await self.get_portfolio_greeks(force_live=force_live)
        metrics, metrics_source = await self.get_portfolio_metrics(force_live=force_live)
        clipped_positions = positions[: self.args.chat_positions_limit]
        return {
            "account_id": self.account_id,
            "sources": {
                "positions": positions_source,
                "account": account_source,
                "greeks": greeks_source,
                "metrics": metrics_source,
            },
            "account": account,
            "greeks": greeks,
            "metrics": metrics,
            "positions": clipped_positions,
            "positions_omitted": max(0, len(positions) - len(clipped_positions)),
        }


def _parse_legs(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.legs_json:
        return json.loads(args.legs_json)
    if args.legs_file:
        return json.loads(Path(args.legs_file).read_text())
    raise ValueError("Provide --legs-json or --legs-file")


async def _cmd_positions(service: DesktopCLIService, args: argparse.Namespace) -> dict[str, Any]:
    positions, source = await service.get_positions(force_live=args.force_live)
    return {"account_id": service.account_id, "source": source, "positions": positions}


async def _cmd_account(service: DesktopCLIService, args: argparse.Namespace) -> dict[str, Any]:
    account, source = await service.get_account(force_live=args.force_live)
    return {"account_id": service.account_id, "source": source, "account": account}


async def _cmd_greeks(service: DesktopCLIService, args: argparse.Namespace) -> dict[str, Any]:
    greeks, source = await service.get_portfolio_greeks(force_live=args.force_live)
    return {"account_id": service.account_id, "source": source, "greeks": greeks}


async def _cmd_metrics(service: DesktopCLIService, args: argparse.Namespace) -> dict[str, Any]:
    metrics, source = await service.get_portfolio_metrics(force_live=args.force_live)
    return {"account_id": service.account_id, "source": source, "metrics": metrics}


async def _cmd_summary(service: DesktopCLIService, args: argparse.Namespace) -> dict[str, Any]:
    payload, _ = await service.get_summary(force_live=args.force_live)
    return payload


async def _cmd_expiries(service: DesktopCLIService, args: argparse.Namespace) -> dict[str, Any]:
    expiries, source = await service.get_expiries(args.underlying, args.sec_type, args.exchange, force_live=args.force_live)
    return {
        "underlying": args.underlying.upper(),
        "sec_type": args.sec_type,
        "exchange": args.exchange,
        "source": source,
        "expiries": expiries,
    }


async def _cmd_chain(service: DesktopCLIService, args: argparse.Namespace) -> dict[str, Any]:
    rows, source = await service.get_chain(args.underlying, args.expiry, args.sec_type, args.exchange, force_live=args.force_live)
    return {
        "underlying": args.underlying.upper(),
        "expiry": args.expiry,
        "sec_type": args.sec_type,
        "exchange": args.exchange,
        "source": source,
        "rows": rows,
    }


async def _cmd_models(_: DesktopCLIService, __: argparse.Namespace) -> dict[str, Any]:
    return {"models": await async_list_models()}


async def _cmd_whatif(service: DesktopCLIService, args: argparse.Namespace) -> dict[str, Any]:
    legs = _parse_legs(args)
    payload = await service.run_whatif(legs, order_type=args.order_type, limit_price=args.limit_price)
    payload["legs"] = legs
    return payload


async def _run_chat_once(service: DesktopCLIService, args: argparse.Namespace, prompt: str) -> str:
    context = await service.build_chat_context(force_live=args.force_live)
    user_prompt = (
        "Portfolio context:\n"
        f"{json.dumps(context, indent=2, default=_json_default)}\n\n"
        "User question:\n"
        f"{prompt.strip()}"
    )
    system = (
        "You are a concise portfolio risk assistant. Use the provided portfolio context first, "
        "and say clearly when information is missing or stale."
    )
    return await async_llm_chat(user_prompt, model=args.model, system=system, timeout=args.timeout)


async def _cmd_chat(service: DesktopCLIService, args: argparse.Namespace) -> dict[str, Any] | None:
    if args.interactive:
        while True:
            try:
                prompt = input("chat> ").strip()
            except EOFError:
                break
            if not prompt or prompt.lower() in {"exit", "quit"}:
                break
            reply = await _run_chat_once(service, args, prompt)
            print(reply)
        return None

    if not args.prompt:
        raise ValueError("Provide --prompt or use --interactive")

    reply = await _run_chat_once(service, args, args.prompt)
    return {"model": args.model, "reply": reply}


COMMANDS: dict[str, Any] = {
    "positions": _cmd_positions,
    "account": _cmd_account,
    "greeks": _cmd_greeks,
    "metrics": _cmd_metrics,
    "summary": _cmd_summary,
    "expiries": _cmd_expiries,
    "chain": _cmd_chain,
    "models": _cmd_models,
    "whatif": _cmd_whatif,
    "chat": _cmd_chat,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Desktop cache-first portfolio CLI")
    parser.add_argument("--account", default=_default_account_id(), help="IBKR account ID")
    parser.add_argument("--host", default=os.environ.get("IB_SOCKET_HOST", "127.0.0.1"), help="IB Gateway host")
    parser.add_argument("--port", type=int, default=int(os.environ.get("IB_SOCKET_PORT", "4001")), help="IB Gateway port")
    parser.add_argument("--client-id", type=int, default=31, help="IBKR client ID for live fallbacks")
    parser.add_argument("--db", default=_build_db_dsn(), help="PostgreSQL DSN")
    parser.add_argument("--cache-ttl", type=int, default=60, help="Cache TTL in seconds")
    parser.add_argument("--force-live", action="store_true", help="Bypass DB cache and refresh from IBKR")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("positions", help="Show current positions")
    subparsers.add_parser("account", help="Show account summary")
    subparsers.add_parser("greeks", help="Show aggregated portfolio greeks")
    subparsers.add_parser("metrics", help="Show aggregated portfolio metrics")
    subparsers.add_parser("summary", help="Show account, greeks, and metrics together")

    expiries = subparsers.add_parser("expiries", help="Show cached/live expiries")
    expiries.add_argument("underlying")
    expiries.add_argument("--sec-type", default="FOP")
    expiries.add_argument("--exchange", default="CME")

    chain = subparsers.add_parser("chain", help="Show cached/live option chain rows")
    chain.add_argument("underlying")
    chain.add_argument("expiry")
    chain.add_argument("--sec-type", default="FOP")
    chain.add_argument("--exchange", default="CME")

    subparsers.add_parser("models", help="List available LLM models")

    whatif = subparsers.add_parser("whatif", help="Run an IBKR WhatIf simulation")
    whatif.add_argument("--legs-json", help="JSON array describing legs")
    whatif.add_argument("--legs-file", help="Path to JSON file containing legs")
    whatif.add_argument("--order-type", default="LIMIT", choices=["LIMIT", "MARKET"])
    whatif.add_argument("--limit-price", type=float)

    chat = subparsers.add_parser("chat", help="Chat against cached/live portfolio context")
    chat.add_argument("--prompt", help="Single prompt to send")
    chat.add_argument("--interactive", action="store_true", help="Start a REPL chat session")
    chat.add_argument("--model", default=os.environ.get("LLM_FAST_MODEL", "gpt-5-mini"))
    chat.add_argument("--timeout", type=float, default=90.0)
    chat.add_argument("--chat-positions-limit", type=int, default=25)

    return parser


async def async_main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    service = DesktopCLIService(args)
    try:
        payload = await COMMANDS[args.command](service, args)
        if payload is not None:
            _print_payload(payload, as_json=args.json)
        return 0
    finally:
        await service.close()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())