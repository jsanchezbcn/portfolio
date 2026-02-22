"""
tests/test_portfolio_cli_greeks.py
==================================
Unit + integration tests for the `greeks` sub-command of portfolio_cli.py.

Coverage
--------
* _fmt / _hr            — pure helper functions
* _build_greeks_diag    — diagnostic JSON builder (mocked adapter + positions)
* _print_greeks_table   — stdout table rendering
* _run_pipeline         — async pipeline with mocked IBKRAdapter
* cmd_greeks (--json)   — end-to-end with patched pipeline; verifies JSON output
* cmd_greeks (table)    — end-to-end with patched pipeline; verifies stdout
* cmd_greeks (empty)    — graceful handling of zero positions
* import coexistence    — IBKRAdapter + SocketBridge importable together

All tests run fully offline; no IBKR Portal or network access required.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import types
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── make sure the repo root is on sys.path ─────────────────────────────────────
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from models.unified_position import InstrumentType, UnifiedPosition

# Import CLI module-level helpers under test
import scripts.portfolio_cli as cli


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

def _make_equity(symbol: str = "SPY", qty: float = 10.0, spx_delta: float = 1.0) -> UnifiedPosition:
    return UnifiedPosition(
        symbol=symbol,
        instrument_type=InstrumentType.EQUITY,
        broker="IBKR",
        quantity=qty,
        avg_price=500.0,
        market_value=5000.0,
        unrealized_pnl=100.0,
        delta=qty,
        spx_delta=spx_delta,
        broker_id="756733",
        greeks_source="ibkr_native",
    )


def _make_option(
    symbol: str = "ES MAR2026 6000 P",
    qty: float = -1.0,
    delta: float = -15.0,
    theta: float = 80.0,
    vega: float = -200.0,
    gamma: float = -0.05,
    spx_delta: float = -11.88,
    source: str = "ibkr_snapshot",
    dte: int = 30,
) -> UnifiedPosition:
    exp = date.today() + timedelta(days=dte)
    return UnifiedPosition(
        symbol=symbol,
        instrument_type=InstrumentType.OPTION,
        broker="IBKR",
        quantity=qty,
        avg_price=10.0,
        market_value=-500.0,
        unrealized_pnl=50.0,
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        spx_delta=spx_delta,
        underlying="ES",
        strike=6000.0,
        expiration=exp,
        option_type="put",
        iv=0.20,
        contract_multiplier=50.0,
        broker_id="999000111",
        greeks_source=source,
    )


def _make_adapter(last_greeks_status: dict | None = None) -> MagicMock:
    """Return a mock IBKRAdapter with sensible last_greeks_status."""
    adapter = MagicMock()
    adapter.last_greeks_status = last_greeks_status or {
        "spx_price": 5900.0,
        "spx_price_source": "ibkr_snapshot",
        "ibkr_snapshot_total": 1,
        "ibkr_snapshot_hits": 1,
        "ibkr_snapshot_errors": [],
        "missing_greeks_details": [],
        "prefetch_targets": {},
        "prefetch_results": {},
        "cache_miss_count": 0,
        "disable_tasty_cache": True,
        "force_refresh_on_miss": False,
        "last_session_error": None,
    }
    return adapter


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Pure helper functions
# ═══════════════════════════════════════════════════════════════════════════════

class TestHelpers:
    def test_fmt_formats_float(self):
        result = cli._fmt(3.14159, width=9, prec=2)
        assert "3.14" in result
        assert len(result) == 9

    def test_fmt_none_returns_na(self):
        result = cli._fmt(None, width=9)
        assert "N/A" in result

    def test_fmt_negative(self):
        result = cli._fmt(-42.5, width=10, prec=1)
        assert "-42.5" in result

    def test_hr_returns_correct_length(self):
        assert len(cli._hr(90)) == 90
        assert len(cli._hr(40)) == 40

    def test_hr_default_length(self):
        assert len(cli._hr()) == 90

    def test_now_utc_contains_utc(self):
        ts = cli._now_utc()
        assert "UTC" in ts


# ═══════════════════════════════════════════════════════════════════════════════
# 2. _build_greeks_diag
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildGreeksDiag:
    """_build_greeks_diag converts positions + adapter → diagnostic dict."""

    def _run(self, positions, adapter=None):
        if adapter is None:
            adapter = _make_adapter()
        return cli._build_greeks_diag("U9999", positions, adapter)

    def test_returns_required_top_level_keys(self):
        diag = self._run([_make_equity(), _make_option()])
        for key in (
            "account", "positions_total", "options_total", "portfolio_totals",
            "greeks_source_breakdown", "ibkr_snapshot", "tastytrade",
            "missing_greeks", "per_position", "generated_at",
        ):
            assert key in diag, f"Missing key: {key}"

    def test_account_matches(self):
        diag = self._run([_make_equity()])
        assert diag["account"] == "U9999"

    def test_positions_total_counts_all(self):
        positions = [_make_equity(), _make_equity("VOO"), _make_option()]
        diag = self._run(positions)
        assert diag["positions_total"] == 3

    def test_options_total_excludes_zero_qty(self):
        active_opt = _make_option(qty=-1.0)
        zero_opt = _make_option(symbol="ES MAR2026 7000 C", qty=0.0, delta=0.0,
                                theta=0.0, vega=0.0, gamma=0.0, spx_delta=0.0)
        diag = self._run([active_opt, zero_opt])
        assert diag["options_total"] == 1
        assert diag["options_zero_qty_total"] == 1

    def test_greeks_source_breakdown_counts_correctly(self):
        positions = [
            _make_option(source="ibkr_snapshot"),
            _make_option(symbol="ES MAR2026 6500 P", source="ibkr_snapshot"),
            _make_option(symbol="ES APR2026 5500 P", source="tastytrade"),
        ]
        diag = self._run(positions)
        assert diag["greeks_source_breakdown"]["ibkr_snapshot"] == 2
        assert diag["greeks_source_breakdown"]["tastytrade"] == 1

    def test_portfolio_totals_has_all_greek_keys(self):
        diag = self._run([_make_option(delta=-5.0, theta=30.0, vega=-100.0, gamma=-0.02)])
        totals = diag["portfolio_totals"]
        for k in ("spx_delta", "delta", "gamma", "theta", "vega", "theta_vega_ratio"):
            assert k in totals

    def test_portfolio_totals_are_floats(self):
        diag = self._run([_make_equity(spx_delta=2.5)])
        totals = diag["portfolio_totals"]
        for v in totals.values():
            assert isinstance(v, float)

    def test_per_position_contains_all_positions(self):
        positions = [_make_equity(), _make_option()]
        diag = self._run(positions)
        assert len(diag["per_position"]) == 2

    def test_per_position_entry_has_required_fields(self):
        diag = self._run([_make_option()])
        p = diag["per_position"][0]
        for field in ("symbol", "broker_id", "asset_type", "delta", "theta",
                      "vega", "gamma", "spx_delta", "greeks_source"):
            assert field in p, f"per_position missing field: {field}"

    def test_ibkr_snapshot_section_from_adapter_status(self):
        adapter = _make_adapter({
            "spx_price": 5800.0,
            "spx_price_source": "test",
            "ibkr_snapshot_total": 5,
            "ibkr_snapshot_hits": 4,
            "ibkr_snapshot_errors": ["conid_X"],
            "missing_greeks_details": [],
        })
        diag = self._run([_make_equity()], adapter)
        assert diag["ibkr_snapshot"]["candidates"] == 5
        assert diag["ibkr_snapshot"]["hits"] == 4
        assert diag["ibkr_snapshot"]["no_data"] == ["conid_X"]

    def test_missing_greeks_forwarded_from_status(self):
        adapter = _make_adapter({
            "spx_price": 5900.0,
            "missing_greeks_details": [
                {"symbol": "MISSING_OPT", "reason": "no_data", "underlying": "ES"}
            ],
        })
        diag = self._run([_make_option()], adapter)
        assert len(diag["missing_greeks"]) == 1
        assert diag["missing_greeks"][0]["symbol"] == "MISSING_OPT"

    def test_empty_positions_returns_zero_counts(self):
        diag = self._run([])
        assert diag["positions_total"] == 0
        assert diag["options_total"] == 0
        assert diag["greeks_source_breakdown"] == {}


# ═══════════════════════════════════════════════════════════════════════════════
# 3. _print_greeks_table
# ═══════════════════════════════════════════════════════════════════════════════

class TestPrintGreeksTable:
    """_print_greeks_table renders options / futures / equity sections to stdout."""

    def _capture(self, positions):
        buf = StringIO()
        with patch("sys.stdout", buf):
            cli._print_greeks_table(positions)
        return buf.getvalue()

    def test_option_symbol_appears_in_output(self):
        output = self._capture([_make_option("ES MAR2026 6000 P")])
        assert "ES MAR2026 6000 P" in output

    def test_equity_symbol_appears_in_output(self):
        output = self._capture([_make_equity("SPY")])
        assert "SPY" in output

    def test_options_section_header_printed(self):
        output = self._capture([_make_option()])
        assert "OPTION" in output

    def test_equity_section_header_printed(self):
        output = self._capture([_make_equity()])
        assert "EQUITY" in output

    def test_empty_positions_produces_no_output(self):
        output = self._capture([])
        assert output.strip() == ""

    def test_mixed_positions_all_sections_present(self):
        positions = [_make_equity("SPY"), _make_option("ES MAR2026 6000 P")]
        output = self._capture(positions)
        assert "SPY" in output
        assert "ES MAR2026 6000 P" in output

    def test_greeks_source_in_option_row(self):
        output = self._capture([_make_option(source="ibkr_snapshot")])
        assert "ibkr_snapshot" in output


# ═══════════════════════════════════════════════════════════════════════════════
# 4. _run_pipeline  (async, mocked IBKRAdapter)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRunPipeline:
    """_run_pipeline calls fetch_positions then fetch_greeks on a real adapter instance."""

    @pytest.mark.asyncio
    async def test_returns_positions_and_adapter(self):
        equity = _make_equity()
        mock_adapter = _make_adapter()
        mock_adapter.fetch_positions = AsyncMock(return_value=[equity])
        mock_adapter.fetch_greeks = AsyncMock(side_effect=lambda positions: positions)

        with patch("scripts.portfolio_cli._make_adapter", return_value=mock_adapter):
            positions, adapter = await cli._run_pipeline("U9999")

        assert positions == [equity]
        assert adapter is mock_adapter

    @pytest.mark.asyncio
    async def test_fetch_greeks_called_after_positions(self):
        opt = _make_option()
        mock_adapter = _make_adapter()
        mock_adapter.fetch_positions = AsyncMock(return_value=[opt])
        mock_adapter.fetch_greeks = AsyncMock(side_effect=lambda p: p)

        with patch("scripts.portfolio_cli._make_adapter", return_value=mock_adapter):
            await cli._run_pipeline("U9999")

        mock_adapter.fetch_positions.assert_awaited_once_with("U9999")
        mock_adapter.fetch_greeks.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_max_options_limits_option_count(self):
        """When max_options=1, only 1 of 3 options should pass through."""
        positions = [
            _make_option("ES MAR2026 6000 P"),
            _make_option("ES MAR2026 5500 P"),
            _make_option("ES APR2026 5000 P"),
            _make_equity("SPY"),
        ]
        mock_adapter = _make_adapter()
        mock_adapter.fetch_positions = AsyncMock(return_value=positions)
        mock_adapter.fetch_greeks = AsyncMock(side_effect=lambda p: p)

        with patch("scripts.portfolio_cli._make_adapter", return_value=mock_adapter):
            result, _ = await cli._run_pipeline("U9999", max_options=1)

        opts = [p for p in result if p.instrument_type == InstrumentType.OPTION]
        assert len(opts) == 1

    @pytest.mark.asyncio
    async def test_empty_account_returns_empty_list(self):
        mock_adapter = _make_adapter()
        mock_adapter.fetch_positions = AsyncMock(return_value=[])
        mock_adapter.fetch_greeks = AsyncMock(side_effect=lambda p: p)

        with patch("scripts.portfolio_cli._make_adapter", return_value=mock_adapter):
            positions, _ = await cli._run_pipeline("U9999")

        assert positions == []


# ═══════════════════════════════════════════════════════════════════════════════
# 5. cmd_greeks  (JSON mode + table mode)
# ═══════════════════════════════════════════════════════════════════════════════

def _make_args(
    account: str = "U9999",
    as_json: bool = True,
    ibkr_only: bool = True,
    disable_cache: bool = False,
    max_options: int = 0,
    output: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        account=account,
        as_json=as_json,
        ibkr_only=ibkr_only,
        disable_cache=disable_cache,
        max_options=max_options,
        output=output,
    )


class TestCmdGreeks:
    """cmd_greeks end-to-end: patches _run_pipeline, runs via asyncio.run."""

    @pytest.fixture(autouse=True)
    def _restore_event_loop(self):
        """cmd_greeks calls asyncio.run() which tears down the current event
        loop.  Restore a fresh loop after each test so that subsequent test
        files (e.g. test_trade_journal.py) that rely on asyncio.get_event_loop()
        don't see a RuntimeError."""
        yield
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

    def _patch_pipeline(self, positions, adapter=None):
        """Context manager that patches _run_pipeline to return test positions."""
        if adapter is None:
            adapter = _make_adapter()

        async def _fake_pipeline(*args, **kwargs):
            return positions, adapter

        return patch("scripts.portfolio_cli._run_pipeline", side_effect=_fake_pipeline)

    # ── JSON mode ─────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_json(stdout_str: str) -> dict:
        """cmd_greeks prints a timestamp header line before the JSON block."""
        idx = stdout_str.find("{")
        assert idx != -1, f"No JSON block found in output:\n{stdout_str!r}"
        return json.loads(stdout_str[idx:])

    def test_json_output_is_valid_json(self, tmp_path):
        opt = _make_option()
        args = _make_args(output=str(tmp_path / "diag.json"))

        buf = StringIO()
        with self._patch_pipeline([opt]):
            with patch("sys.stdout", buf):
                cli.cmd_greeks(args)

        parsed = self._extract_json(buf.getvalue())
        assert isinstance(parsed, dict)

    def test_json_output_contains_per_position(self, tmp_path):
        opt = _make_option()
        args = _make_args(output=str(tmp_path / "diag.json"))

        buf = StringIO()
        with self._patch_pipeline([opt]):
            with patch("sys.stdout", buf):
                cli.cmd_greeks(args)

        parsed = self._extract_json(buf.getvalue())
        assert "per_position" in parsed
        assert len(parsed["per_position"]) == 1

    def test_json_output_account_matches_args(self, tmp_path):
        args = _make_args(account="U12345", output=str(tmp_path / "diag.json"))

        buf = StringIO()
        with self._patch_pipeline([_make_equity()]):
            with patch("sys.stdout", buf):
                cli.cmd_greeks(args)

        parsed = self._extract_json(buf.getvalue())
        assert parsed["account"] == "U12345"

    def test_json_output_portfolio_totals_present(self, tmp_path):
        args = _make_args(output=str(tmp_path / "diag.json"))

        buf = StringIO()
        with self._patch_pipeline([_make_option(delta=-5.0, theta=30.0, vega=-100.0)]):
            with patch("sys.stdout", buf):
                cli.cmd_greeks(args)

        parsed = self._extract_json(buf.getvalue())
        totals = parsed["portfolio_totals"]
        assert "delta" in totals and "theta" in totals and "vega" in totals

    def test_diagnostic_json_file_written(self, tmp_path):
        out_file = tmp_path / "my_diag.json"
        args = _make_args(output=str(out_file))

        with self._patch_pipeline([_make_equity()]):
            with patch("sys.stdout", StringIO()):
                cli.cmd_greeks(args)

        assert out_file.exists()
        content = json.loads(out_file.read_text())
        assert "per_position" in content

    # ── Table mode ────────────────────────────────────────────────────────────

    def test_table_mode_prints_symbol(self, tmp_path):
        args = _make_args(as_json=False, output=str(tmp_path / "diag.json"))

        buf = StringIO()
        with self._patch_pipeline([_make_option("ES MAR2026 6000 P")]):
            with patch("sys.stdout", buf):
                cli.cmd_greeks(args)

        assert "ES MAR2026 6000 P" in buf.getvalue()

    def test_table_mode_portfolio_totals_printed(self, tmp_path):
        args = _make_args(as_json=False, output=str(tmp_path / "diag.json"))

        buf = StringIO()
        with self._patch_pipeline([_make_option()]):
            with patch("sys.stdout", buf):
                cli.cmd_greeks(args)

        output = buf.getvalue()
        assert "PORTFOLIO TOTALS" in output

    def test_table_mode_diagnostics_section_printed(self, tmp_path):
        args = _make_args(as_json=False, output=str(tmp_path / "diag.json"))

        buf = StringIO()
        with self._patch_pipeline([_make_option()]):
            with patch("sys.stdout", buf):
                cli.cmd_greeks(args)

        assert "GREEKS DIAGNOSTICS" in buf.getvalue()

    # ── Empty positions ───────────────────────────────────────────────────────

    def test_empty_positions_json_still_valid(self, tmp_path):
        args = _make_args(output=str(tmp_path / "diag.json"))

        buf = StringIO()
        with self._patch_pipeline([]):
            with patch("sys.stdout", buf):
                cli.cmd_greeks(args)

        parsed = self._extract_json(buf.getvalue())
        assert parsed["positions_total"] == 0
        assert parsed["per_position"] == []

    def test_empty_positions_writes_diag_file(self, tmp_path):
        out_file = tmp_path / "empty_diag.json"
        args = _make_args(output=str(out_file))

        with self._patch_pipeline([]):
            with patch("sys.stdout", StringIO()):
                cli.cmd_greeks(args)

        assert out_file.exists()

    # ── Missing greeks section ────────────────────────────────────────────────

    def test_table_mode_shows_missing_greeks_list(self, tmp_path):
        args = _make_args(as_json=False, output=str(tmp_path / "diag.json"))
        adapter = _make_adapter({
            "spx_price": 5900.0,
            "missing_greeks_details": [
                {"symbol": "GHOST_OPT", "reason": "no_data", "underlying": "ES",
                 "expiry": "2026-03-20", "strike": 7500}
            ],
        })

        buf = StringIO()
        with self._patch_pipeline([_make_option()], adapter=adapter):
            with patch("sys.stdout", buf):
                cli.cmd_greeks(args)

        assert "GHOST_OPT" in buf.getvalue()


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Import coexistence: IBKRAdapter (PORTAL) + SocketBridge (ib_async)
# ═══════════════════════════════════════════════════════════════════════════════

class TestImportCoexistence:
    def test_ibkr_adapter_and_socket_bridge_no_conflict(self):
        from adapters.ibkr_adapter import IBKRAdapter
        from bridge.ib_bridge import SocketBridge
        assert IBKRAdapter is not None
        assert SocketBridge is not None

    def test_socket_bridge_not_leaked_into_cli_module(self):
        """ib_async must NOT be imported anywhere in portfolio_cli.py."""
        assert not hasattr(cli, "SocketBridge"), (
            "SocketBridge leaked into portfolio_cli — keep concerns separated"
        )
