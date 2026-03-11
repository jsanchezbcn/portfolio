from __future__ import annotations

from dataclasses import dataclass

import pytest

import desktop_cli


class FakeDatabase:
    cached_positions: list[dict] = []
    cached_account: dict | None = None
    cached_greeks: dict | None = None
    cached_metrics: dict | None = None
    cached_chain: list[dict] = []
    cached_expirations: list[str] | None = None
    connect_calls = 0

    def __init__(self, dsn: str):
        self.dsn = dsn

    async def connect(self) -> None:
        type(self).connect_calls += 1

    async def close(self) -> None:
        return None

    async def get_cached_positions(self, account_id: str, max_age_seconds: int = 60) -> list[dict]:
        return [dict(row) for row in type(self).cached_positions]

    async def get_cached_account_snapshot(self, account_id: str, max_age_seconds: int = 60) -> dict | None:
        return None if type(self).cached_account is None else dict(type(self).cached_account)

    async def get_cached_portfolio_greeks(self, account_id: str, max_age_seconds: int = 60) -> dict | None:
        return None if type(self).cached_greeks is None else dict(type(self).cached_greeks)

    async def get_cached_portfolio_metrics(self, account_id: str, max_age_seconds: int = 60) -> dict | None:
        return None if type(self).cached_metrics is None else dict(type(self).cached_metrics)

    async def get_cached_chain(self, underlying: str, expiry: str, max_age_seconds: int = 60) -> list[dict]:
        return [dict(row) for row in type(self).cached_chain]

    async def get_cached_expirations(self, underlying: str, sec_type: str = "FOP", exchange: str = "CME") -> list[str] | None:
        cached = type(self).cached_expirations
        return None if cached is None else list(cached)


@dataclass
class FakeSummary:
    account_id: str
    net_liquidation: float
    total_cash: float
    buying_power: float
    init_margin: float
    maint_margin: float
    unrealized_pnl: float
    realized_pnl: float


@dataclass
class FakePosition:
    conid: int
    symbol: str
    sec_type: str
    underlying: str
    strike: float | None
    right: str | None
    expiry: str | None
    quantity: float
    avg_cost: float
    market_price: float
    market_value: float
    unrealized_pnl: float
    realized_pnl: float
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    iv: float | None
    spx_delta: float | None
    greeks_source: str | None = None
    underlying_price: float | None = None
    combo_description: str | None = None


class FakeEngine:
    positions_to_return: list[FakePosition] = []
    account_to_return: FakeSummary | None = None
    connect_calls = 0
    refresh_positions_calls = 0
    refresh_account_calls = 0
    disconnect_calls = 0

    def __init__(self, host: str, port: int, client_id: int, db_dsn: str):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.db_dsn = db_dsn
        self._account_id = "U123"

    @property
    def account_id(self) -> str:
        return self._account_id

    async def connect(self) -> None:
        type(self).connect_calls += 1

    async def disconnect(self) -> None:
        type(self).disconnect_calls += 1

    async def refresh_positions(self) -> list[FakePosition]:
        type(self).refresh_positions_calls += 1
        return list(type(self).positions_to_return)

    async def refresh_account(self) -> FakeSummary | None:
        type(self).refresh_account_calls += 1
        return type(self).account_to_return

    async def get_available_expiries(self, underlying: str, sec_type: str = "FOP", exchange: str = "CME") -> list[str]:
        return ["20260320"]

    async def get_chain(self, underlying: str, expiry: str, sec_type: str = "FOP", exchange: str = "CME") -> list[dict]:
        return [{"underlying": underlying, "expiry": expiry, "strike": 5900.0, "option_right": "C"}]

    async def whatif_order(self, legs: list[dict], order_type: str = "LIMIT", limit_price: float | None = None) -> dict:
        return {"status": "success", "init_margin_change": 1000.0}


@pytest.fixture(autouse=True)
def reset_fakes(monkeypatch):
    FakeDatabase.cached_positions = []
    FakeDatabase.cached_account = None
    FakeDatabase.cached_greeks = None
    FakeDatabase.cached_metrics = None
    FakeDatabase.cached_chain = []
    FakeDatabase.cached_expirations = None
    FakeDatabase.connect_calls = 0

    FakeEngine.positions_to_return = []
    FakeEngine.account_to_return = None
    FakeEngine.connect_calls = 0
    FakeEngine.refresh_positions_calls = 0
    FakeEngine.refresh_account_calls = 0
    FakeEngine.disconnect_calls = 0

    monkeypatch.setattr(desktop_cli, "Database", FakeDatabase)
    monkeypatch.setattr(desktop_cli, "IBEngine", FakeEngine)


@pytest.mark.asyncio
async def test_positions_command_uses_cache_without_live_fallback(capsys):
    FakeDatabase.cached_positions = [{
        "conid": 1,
        "symbol": "AAPL",
        "sec_type": "STK",
        "quantity": 100.0,
        "delta": 100.0,
    }]

    exit_code = await desktop_cli.async_main(["--account", "U123", "--json", "positions"])
    captured = capsys.readouterr().out

    assert exit_code == 0
    assert '"source": "cache"' in captured
    assert '"symbol": "AAPL"' in captured
    assert FakeEngine.connect_calls == 0
    assert FakeEngine.refresh_positions_calls == 0


@pytest.mark.asyncio
async def test_positions_command_falls_back_to_live_when_cache_empty(capsys):
    FakeEngine.positions_to_return = [
        FakePosition(
            conid=2,
            symbol="SPY",
            sec_type="OPT",
            underlying="SPY",
            strike=600.0,
            right="C",
            expiry="20260320",
            quantity=1.0,
            avg_cost=10.0,
            market_price=12.0,
            market_value=1200.0,
            unrealized_pnl=200.0,
            realized_pnl=0.0,
            delta=30.0,
            gamma=2.0,
            theta=-100.0,
            vega=500.0,
            iv=0.20,
            spx_delta=25.0,
            greeks_source="live",
            underlying_price=600.0,
        )
    ]

    exit_code = await desktop_cli.async_main(["--account", "U123", "--json", "positions"])
    captured = capsys.readouterr().out

    assert exit_code == 0
    assert '"source": "live"' in captured
    assert '"greeks_source": "live"' in captured
    assert FakeEngine.connect_calls == 1
    assert FakeEngine.refresh_positions_calls == 1
    assert FakeEngine.disconnect_calls == 1


@pytest.mark.asyncio
async def test_chat_command_builds_prompt_from_cached_portfolio(monkeypatch, capsys):
    FakeDatabase.cached_positions = [{
        "conid": 10,
        "symbol": "SPY",
        "sec_type": "OPT",
        "quantity": 1.0,
        "delta": 25.0,
        "gamma": 1.0,
        "theta": -50.0,
        "vega": 400.0,
        "iv": 0.18,
        "spx_delta": 20.0,
        "underlying_price": 590.0,
    }]
    FakeDatabase.cached_account = {
        "account_id": "U123",
        "net_liquidation": 250000.0,
        "buying_power": 200000.0,
        "init_margin": 30000.0,
        "maint_margin": 25000.0,
        "total_cash": 40000.0,
        "unrealized_pnl": 5000.0,
        "realized_pnl": 1000.0,
    }
    FakeDatabase.cached_greeks = {
        "total_delta": 25.0,
        "total_gamma": 1.0,
        "total_theta": -50.0,
        "total_vega": 400.0,
        "total_spx_delta": 20.0,
    }
    FakeDatabase.cached_metrics = {
        "total_positions": 1,
        "total_value": 1200.0,
        "total_spx_delta": 20.0,
    }

    recorded: dict[str, str] = {}

    async def fake_chat(prompt: str, *, model: str = "gpt-5-mini", system: str = "", timeout: float = 90.0) -> str:
        recorded["prompt"] = prompt
        recorded["model"] = model
        recorded["system"] = system
        return "risk answer"

    monkeypatch.setattr(desktop_cli, "async_llm_chat", fake_chat)

    exit_code = await desktop_cli.async_main([
        "--account", "U123", "--json", "chat", "--prompt", "Summarize my risk", "--model", "gpt-5-mini"
    ])
    captured = capsys.readouterr().out

    assert exit_code == 0
    assert '"reply": "risk answer"' in captured
    assert "Portfolio context:" in recorded["prompt"]
    assert '"symbol": "SPY"' in recorded["prompt"]
    assert "Summarize my risk" in recorded["prompt"]
    assert recorded["model"] == "gpt-5-mini"
    assert FakeEngine.connect_calls == 0