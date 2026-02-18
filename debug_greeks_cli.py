#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import statistics
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

import pandas as pd
import websockets

from adapters.ibkr_adapter import IBKRAdapter
from agent_config import load_streaming_environment
from agent_tools.portfolio_tools import PortfolioTools
from core.processor import DataProcessor
from database.db_manager import DBManager
from models.unified_position import InstrumentType
from streaming.ibkr_ws import IBKRWebSocketClient
from streaming.tasty_dxlink import TastyDXLinkStreamerClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CLI diagnostics for IBKR/Tasty option Greeks")
    parser.add_argument("--account", required=True, help="IBKR account id (e.g. U2052408)")
    parser.add_argument(
        "--stream-only",
        choices=["ibkr", "tastytrade"],
        default=None,
        help="Run dedicated realtime ingestion path only (no summary report)",
    )
    parser.add_argument(
        "--contracts",
        default="",
        help="Comma-separated IBKR contract keys for --stream-only ibkr",
    )
    parser.add_argument(
        "--stream-symbols",
        default="",
        help="Comma-separated Tastytrade streamer symbols for --stream-only tastytrade",
    )
    parser.add_argument(
        "--disable-cache",
        action="store_true",
        help="Disable Tastytrade cache reads and force live fetch attempts per contract",
    )
    parser.add_argument(
        "--force-refresh-on-miss",
        action="store_true",
        default=False,
        help="When cache is enabled, force a live fetch if cache miss occurs",
    )
    parser.add_argument(
        "--output-prefix",
        default=None,
        help="Output prefix for JSON/CSV (default: .missing_greeks_<account>)",
    )
    parser.add_argument(
        "--max-options",
        type=int,
        default=0,
        help="Limit number of option positions processed (0 = all)",
    )
    parser.add_argument(
        "--ibkr-stream-benchmark",
        action="store_true",
        help="Benchmark IBKR websocket Greek retrieval latency and field coverage",
    )
    parser.add_argument(
        "--stream-timeout-seconds",
        type=int,
        default=25,
        help="Max seconds to listen for IBKR stream events in benchmark mode",
    )
    parser.add_argument(
        "--ibkr-ws-url",
        default=None,
        help="Override IBKR websocket URL for benchmark mode",
    )
    return parser.parse_args()


def _is_us_market_hours() -> bool:
    """Issue 16: Return True when current UTC time is within approximate US equity trading hours.

    Covers 9:30 AM – 4:00 PM US/Eastern (approx 13:30–21:00 UTC).  Does NOT account for
    irregular early closes or individual exchange holidays.
    """
    now_utc = datetime.now(timezone.utc)
    if now_utc.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    hour_min = now_utc.hour * 60 + now_utc.minute
    return 810 <= hour_min <= 1260  # 13:30–21:00 UTC


def _safe_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in {"", "nan", "none", "n/a"}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_contract_key(payload: dict[str, Any]) -> str:
    topic = payload.get("topic")
    if isinstance(topic, str) and topic.startswith("smd+"):
        parts = topic.split("+", 2)
        if len(parts) >= 2 and parts[1].strip():
            return parts[1].strip()

    for key in ("contract_key", "contractKey", "eventSymbol", "symbol", "conid"):
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _extract_greek(payload: dict[str, Any], name: str) -> float | None:
    nested = payload.get("greeks")
    if isinstance(nested, dict):
        value = _safe_float_or_none(nested.get(name))
        if value is not None:
            return value

    for key in (name, name.capitalize(), f"model{name.capitalize()}", f"position{name.capitalize()}"):
        value = _safe_float_or_none(payload.get(key))
        if value is not None:
            return value
    return None


def _normalize_expiration_for_output(value: date | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


async def run_ibkr_stream_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    # Issue 16: warn when markets are closed so coverage=0 is self-explanatory
    if not _is_us_market_hours():
        _ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M %Z")
        print(
            f"WARNING: US markets appear to be closed (UTC: {_ts}). "
            "IBKR stream will likely return no Greek data."
        )

    config = load_streaming_environment()
    ws_url = args.ibkr_ws_url or config.ibkr_ws_url
    adapter = IBKRAdapter()

    fetch_start = perf_counter()
    positions = await adapter.fetch_positions(args.account)
    fetch_positions_ms = (perf_counter() - fetch_start) * 1000.0

    option_positions = [position for position in positions if position.instrument_type == InstrumentType.OPTION]
    if args.max_options and args.max_options > 0:
        option_positions = option_positions[: args.max_options]

    # Issue 10: reuse the cached raw positions from fetch_positions instead of a second REST call
    raw_positions = getattr(adapter, "_last_raw_positions", [])
    raw_option_positions = [
        position
        for position in raw_positions
        if adapter.client.is_option_contract(position)
    ]
    raw_by_contract_desc = {
        str(position.get("contractDesc") or position.get("ticker") or ""): position
        for position in raw_option_positions
    }

    contract_keys = [position.symbol for position in option_positions if position.symbol]
    if args.contracts:
        contract_keys = [item.strip() for item in args.contracts.split(",") if item.strip()]
    contract_keys = list(dict.fromkeys(contract_keys))

    conid_to_contract_key: dict[str, str] = {}
    for contract_key in contract_keys:
        raw = raw_by_contract_desc.get(contract_key)
        if not isinstance(raw, dict):
            continue
        conid = raw.get("conid")
        if conid is None:
            continue
        conid_text = str(conid).strip()
        if conid_text.isdigit():
            conid_to_contract_key[conid_text] = contract_key

    if args.contracts:
        for token in args.contracts.split(","):
            token_text = token.strip()
            if token_text.isdigit() and token_text not in conid_to_contract_key:
                conid_to_contract_key[token_text] = token_text

    if not contract_keys:
        return {
            "account": args.account,
            "ibkr_ws_url": ws_url,
            "positions_total": len(positions),
            "options_total": len(option_positions),
            "subscribed_contracts": 0,
            "timings_ms": {
                "fetch_positions": round(fetch_positions_ms, 2),
                "connect_ws": None,
                "first_stream_message": None,
            },
            "stream_messages": {
                "raw_message_count": 0,
                "unknown_contract_messages": 0,
            },
            "coverage": {"delta": 0, "gamma": 0, "theta": 0, "vega": 0},
            "first_greek_latency_ms": {"count": 0, "p50": None, "max": None},
            "contract_stream_data": {},
            "positions": rows,
        }

    rows: list[dict[str, Any]] = []
    for position in option_positions:
        rows.append(
            {
                "symbol": position.symbol,
                "underlying": position.underlying,
                "expiration": _normalize_expiration_for_output(position.expiration),
                "strike": position.strike,
                "option_type": position.option_type,
                "quantity": position.quantity,
                "native_delta": position.delta,
                "native_gamma": position.gamma,
                "native_theta": position.theta,
                "native_vega": position.vega,
                "native_source": position.greeks_source,
            }
        )

    stream_data: dict[str, dict[str, Any]] = {
        key: {
            "messages": 0,
            "first_greek_latency_ms": None,
            "delta": None,
            "gamma": None,
            "theta": None,
            "vega": None,
        }
        for key in contract_keys
    }
    unknown_contract_messages = 0
    unknown_contract_keys: dict[str, int] = {}
    raw_message_count = 0
    message_type_counts: dict[str, int] = {}
    sample_payloads: list[dict[str, Any]] = []
    first_message_latency_ms: float | None = None
    connect_latency_ms: float | None = None
    connect_error: str | None = None

    candidate_urls: list[str] = [ws_url]
    if "localhost:5000" in ws_url:
        candidate_urls.append(ws_url.replace("localhost:5000", "localhost:5001"))
    if "127.0.0.1:5000" in ws_url:
        candidate_urls.append(ws_url.replace("127.0.0.1:5000", "127.0.0.1:5001"))
    candidate_urls = list(dict.fromkeys(candidate_urls))

    connected_url = ws_url
    try:
        response = await asyncio.to_thread(
            adapter.client.session.post,
            f"{adapter.client.base_url}/v1/api/iserver/account",
            json={"acctId": args.account},
            timeout=5,
        )
        if response.status_code != 200:
            connect_error = f"account-select status={response.status_code}"
    except Exception as exc:
        connect_error = f"account-select error: {exc}"

    try:
        fields = [str(value) for value in IBKRWebSocketClient._market_data_fields()]
        response = await asyncio.to_thread(
            adapter.client.session.get,
            f"{adapter.client.base_url}/v1/api/iserver/marketdata/snapshot",
            params={
                "conids": ",".join(conid_to_contract_key.keys()),
                "fields": ",".join(fields),
            },
            timeout=8,
        )
        if response.status_code != 200 and not connect_error:
            connect_error = f"marketdata-prime status={response.status_code}"
    except Exception as exc:
        if not connect_error:
            connect_error = f"marketdata-prime error: {exc}"

    for candidate_url in candidate_urls:
        ssl_context = None
        if str(candidate_url).startswith("wss://"):
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        try:
            connect_start = perf_counter()
            async with websockets.connect(
                candidate_url,
                ssl=ssl_context,
                ping_interval=None,
                open_timeout=8,
            ) as websocket:
                connect_latency_ms = (perf_counter() - connect_start) * 1000.0
                connected_url = candidate_url
                account_command = IBKRWebSocketClient.build_account_select_command(args.account)
                if account_command:
                    await websocket.send(account_command)
                subscription_commands = IBKRWebSocketClient.build_subscription_commands(list(conid_to_contract_key.keys()))
                if not subscription_commands:
                    subscription_payload = IBKRWebSocketClient.build_subscription_payload(contract_keys)
                    await websocket.send(json.dumps(subscription_payload))
                else:
                    for command in subscription_commands:
                        await websocket.send(command)
                subscribed_at = perf_counter()
                deadline = subscribed_at + float(max(1, args.stream_timeout_seconds))

                while perf_counter() < deadline:
                    time_left = max(0.1, deadline - perf_counter())
                    try:
                        raw_message = await asyncio.wait_for(websocket.recv(), timeout=min(1.0, time_left))
                    except asyncio.TimeoutError:
                        continue

                    raw_message_count += 1
                    if first_message_latency_ms is None:
                        first_message_latency_ms = (perf_counter() - subscribed_at) * 1000.0

                    if isinstance(raw_message, bytes):
                        message_text = raw_message.decode("utf-8", errors="ignore")
                    else:
                        message_text = raw_message

                    try:
                        payload = json.loads(message_text)
                    except json.JSONDecodeError:
                        continue

                    payloads: list[dict[str, Any]] = []
                    if isinstance(payload, dict):
                        msg_type = str(payload.get("type") or "<none>")
                        message_type_counts[msg_type] = message_type_counts.get(msg_type, 0) + 1
                        if len(sample_payloads) < 5:
                            sample_payloads.append(payload)
                        payloads.append(payload)
                        nested_data = payload.get("data")
                        if isinstance(nested_data, list):
                            payloads.extend([item for item in nested_data if isinstance(item, dict)])
                    elif isinstance(payload, list):
                        payloads.extend([item for item in payload if isinstance(item, dict)])

                    for item in payloads:
                        contract_key = _extract_contract_key(item)
                        if contract_key in conid_to_contract_key:
                            contract_key = conid_to_contract_key[contract_key]
                        if contract_key not in stream_data:
                            unknown_contract_messages += 1
                            if contract_key:
                                unknown_contract_keys[contract_key] = unknown_contract_keys.get(contract_key, 0) + 1
                            continue

                        entry = stream_data[contract_key]
                        entry["messages"] += 1
                        if entry["first_greek_latency_ms"] is None:
                            entry["first_greek_latency_ms"] = (perf_counter() - subscribed_at) * 1000.0

                        for greek in ("delta", "gamma", "theta", "vega"):
                            if entry[greek] is None:
                                entry[greek] = _extract_greek(item, greek)

                        if all(
                            stream_data[key]["delta"] is not None
                            and stream_data[key]["gamma"] is not None
                            and stream_data[key]["theta"] is not None
                            and stream_data[key]["vega"] is not None
                            for key in stream_data
                        ):
                            break
                break
        except Exception as exc:
            connect_error = str(exc)
            continue

    first_latencies = [
        float(entry["first_greek_latency_ms"])
        for entry in stream_data.values()
        if entry["first_greek_latency_ms"] is not None
    ]
    greek_coverage = {
        greek: sum(1 for entry in stream_data.values() if entry[greek] is not None)
        for greek in ("delta", "gamma", "theta", "vega")
    }

    stream_lookup = stream_data
    for row in rows:
        stream_entry = stream_lookup.get(row["symbol"], {})
        row["stream_messages"] = stream_entry.get("messages", 0)
        row["stream_first_greek_latency_ms"] = stream_entry.get("first_greek_latency_ms")
        row["stream_delta"] = stream_entry.get("delta")
        row["stream_gamma"] = stream_entry.get("gamma")
        row["stream_theta"] = stream_entry.get("theta")
        row["stream_vega"] = stream_entry.get("vega")

    report = {
        "account": args.account,
        "ibkr_ws_url": connected_url,
        "requested_ws_url": ws_url,
        "positions_total": len(positions),
        "options_total": len(option_positions),
        "subscribed_contracts": len(contract_keys),
        "timings_ms": {
            "fetch_positions": round(fetch_positions_ms, 2),
            "connect_ws": round(connect_latency_ms, 2) if connect_latency_ms is not None else None,
            "first_stream_message": round(first_message_latency_ms, 2) if first_message_latency_ms is not None else None,
        },
        "connect_error": connect_error,
        "stream_messages": {
            "raw_message_count": raw_message_count,
            "unknown_contract_messages": unknown_contract_messages,
            "unknown_contract_keys": unknown_contract_keys,
            "message_type_counts": message_type_counts,
            "sample_payloads": sample_payloads,
        },
        "coverage": {
            "delta": greek_coverage["delta"],
            "gamma": greek_coverage["gamma"],
            "theta": greek_coverage["theta"],
            "vega": greek_coverage["vega"],
        },
        "first_greek_latency_ms": {
            "count": len(first_latencies),
            "p50": round(statistics.median(first_latencies), 2) if first_latencies else None,
            "max": round(max(first_latencies), 2) if first_latencies else None,
        },
        "contract_stream_data": stream_data,
        "positions": rows,
    }
    return report


async def run_debug(account: str) -> dict:
    adapter = IBKRAdapter()
    portfolio_tools = PortfolioTools()

    positions = await adapter.fetch_positions(account)

    max_options_raw = os.getenv("GREEKS_DEBUG_MAX_OPTIONS", "").strip()
    max_options = int(max_options_raw) if max_options_raw.isdigit() else None
    if max_options is not None and max_options > 0:
        option_positions_all = [p for p in positions if p.instrument_type == InstrumentType.OPTION]
        non_option_positions = [p for p in positions if p.instrument_type != InstrumentType.OPTION]
        positions = non_option_positions + option_positions_all[:max_options]

    positions = await adapter.fetch_greeks(positions)

    status = getattr(adapter, "last_greeks_status", {})
    missing_details = status.get("missing_greeks_details", []) or []

    summary = portfolio_tools.get_portfolio_summary(positions)

    option_positions = [p for p in positions if p.instrument_type == InstrumentType.OPTION]
    source_counts = Counter(getattr(p, "greeks_source", "none") for p in option_positions)
    reason_counts = Counter(item.get("reason", "") for item in missing_details)

    return {
        "positions_total": len(positions),
        "options_total": len(option_positions),
        "summary": {
            "spx_delta": float(summary.get("total_spx_delta", 0.0)),
            "theta_dollars": float(summary.get("total_theta", 0.0)),
            "vega_dollars": float(summary.get("total_vega", 0.0)),
            "gamma_dollars": float(summary.get("total_gamma", 0.0)),
        },
        "greeks_source_counts": dict(source_counts),
        "missing_reason_counts": dict(reason_counts),
        "status": status,
        "missing_details": missing_details,
    }


async def run_stream_only(args: argparse.Namespace) -> None:
    config = load_streaming_environment()
    db_manager = await DBManager.get_instance()
    db_manager.flush_interval_seconds = config.stream_flush_interval_seconds
    db_manager.flush_batch_size = config.stream_flush_batch_size

    processor = DataProcessor(db_manager)
    await processor.start()

    try:
        if args.stream_only == "ibkr":
            contract_keys = [item.strip() for item in args.contracts.split(",") if item.strip()]
            client = IBKRWebSocketClient(
                url=config.ibkr_ws_url,
                account_id=args.account,
                processor=processor,
                heartbeat_interval_seconds=config.ibkr_heartbeat_seconds,
                reconnect_max_backoff_seconds=config.ibkr_reconnect_max_backoff_seconds,
                verify_tls=False,
            )
            await client.run(contract_keys)
            return

        if args.stream_only == "tastytrade":
            from tastytrade import Session

            username = os.getenv("TASTYTRADE_USERNAME") or os.getenv("TASTYWORKS_USER")
            password = os.getenv("TASTYTRADE_PASSWORD") or os.getenv("TASTYWORKS_PASS")
            if not username or not password:
                raise RuntimeError("Missing Tastytrade credentials for stream-only mode")

            streamer_symbols = [item.strip() for item in args.stream_symbols.split(",") if item.strip()]
            streamer = TastyDXLinkStreamerClient(
                session_factory=lambda: Session(username, password),
                account_id=args.account,
                processor=processor,
                reconnect_max_backoff_seconds=config.ibkr_reconnect_max_backoff_seconds,
            )
            await streamer.run(streamer_symbols)
    finally:
        await processor.stop()


def main() -> None:
    args = parse_args()

    if args.ibkr_stream_benchmark:
        report = asyncio.run(run_ibkr_stream_benchmark(args))
        output_prefix = args.output_prefix or f".ibkr_stream_benchmark_{args.account}"
        output_json = Path(f"{output_prefix}.json")
        output_csv = Path(f"{output_prefix}.csv")
        output_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        pd.DataFrame(report.get("positions", [])).to_csv(output_csv, index=False)

        print("=== IBKR Stream Benchmark ===")
        print(f"account: {report['account']}")
        print(f"ibkr_ws_url: {report['ibkr_ws_url']}")
        print(f"positions_total: {report['positions_total']}")
        print(f"options_total: {report['options_total']}")
        print(f"subscribed_contracts: {report['subscribed_contracts']}")
        print(f"fetch_positions_ms: {report['timings_ms']['fetch_positions']}")
        print(f"connect_ws_ms: {report['timings_ms']['connect_ws']}")
        print(f"first_stream_message_ms: {report['timings_ms']['first_stream_message']}")
        print(f"coverage: {report['coverage']}")
        print(f"first_greek_latency_ms: {report['first_greek_latency_ms']}")
        print(f"json: {output_json}")
        print(f"csv: {output_csv}")
        return

    if args.stream_only:
        asyncio.run(run_stream_only(args))
        return

    if args.disable_cache:
        os.environ["GREEKS_DISABLE_CACHE"] = "1"
    if args.force_refresh_on_miss:
        os.environ["GREEKS_FORCE_REFRESH_ON_MISS"] = "1"
    if args.max_options and args.max_options > 0:
        os.environ["GREEKS_DEBUG_MAX_OPTIONS"] = str(args.max_options)

    report = asyncio.run(run_debug(args.account))

    output_prefix = args.output_prefix or f".missing_greeks_{args.account}"
    output_json = Path(f"{output_prefix}.json")
    output_csv = Path(f"{output_prefix}.csv")

    missing_details = report.get("missing_details", [])
    output_json.write_text(json.dumps(missing_details, indent=2), encoding="utf-8")

    missing_df = pd.DataFrame(missing_details)
    missing_df.to_csv(output_csv, index=False)

    print("=== Greeks Debug Report ===")
    print(f"account: {args.account}")
    print(f"disable_cache: {bool(args.disable_cache)}")
    print(f"force_refresh_on_miss: {bool(args.force_refresh_on_miss)}")
    print(f"positions_total: {report['positions_total']}")
    print(f"options_total: {report['options_total']}")
    print(f"spx_delta: {report['summary']['spx_delta']:.2f}")
    print(f"theta_dollars: {report['summary']['theta_dollars']:.2f}")
    print(f"vega_dollars: {report['summary']['vega_dollars']:.2f}")
    print(f"gamma_dollars: {report['summary']['gamma_dollars']:.2f}")
    print(f"greeks_source_counts: {report['greeks_source_counts']}")
    print(f"missing_reason_counts: {report['missing_reason_counts']}")
    print(f"json: {output_json}")
    print(f"csv: {output_csv}")


if __name__ == "__main__":
    main()
