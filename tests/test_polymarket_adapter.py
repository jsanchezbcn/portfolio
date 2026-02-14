from __future__ import annotations

import pytest

from adapters.polymarket_adapter import PolymarketAdapter


@pytest.mark.asyncio
async def test_get_recession_probability_parses_yes_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = PolymarketAdapter()

    async def fake_fetch() -> list[dict]:
        return [
            {
                "question": "Will the US enter a recession in 2026?",
                "outcomes": ["Yes", "No"],
                "outcomePrices": ["0.42", "0.58"],
            }
        ]

    monkeypatch.setattr(adapter, "_fetch_recession_markets", fake_fetch)

    result = await adapter.get_recession_probability()

    assert result["source"] == "polymarket"
    assert result["recession_probability"] == 0.42


@pytest.mark.asyncio
async def test_get_recession_probability_handles_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = PolymarketAdapter()

    async def fake_fetch_fail() -> list[dict]:
        raise RuntimeError("network down")

    monkeypatch.setattr(adapter, "_fetch_recession_markets", fake_fetch_fail)

    result = await adapter.get_recession_probability()

    assert result["source"] == "unavailable"
    assert result["recession_probability"] is None
    assert "error" in result


def test_extract_recession_probability_uses_max_candidate() -> None:
    adapter = PolymarketAdapter()
    markets = [
        {"outcomes": ["Yes", "No"], "outcomePrices": ["0.31", "0.69"]},
        {"outcomes": ["Yes", "No"], "outcomePrices": ["0.47", "0.53"]},
    ]

    value = adapter._extract_recession_probability(markets)

    assert value == 0.47
