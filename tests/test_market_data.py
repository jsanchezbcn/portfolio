"""tests/test_market_data.py — Unit tests for core.market_data.

Tests cover:
  - Quote and OptionQuote dataclass helpers (mid, spread, is_valid)
  - MarketDataService.resolve_conid()
  - MarketDataService.get_quote() — stock/ETF path
  - MarketDataService.get_futures_quote() — futures path
  - MarketDataService.get_options_chain() — Tastytrade path
  - Graceful handling of HTTP errors and missing data
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.market_data import MarketDataService, OptionQuote, Quote


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_ibkr():
    """Returns a minimal mock ibkr_portfolio_client instance."""
    client = MagicMock()
    client.base_url = "https://localhost:5001"
    client.session = MagicMock()
    return client


@pytest.fixture
def svc(mock_ibkr):
    """MarketDataService with no Tastytrade fetcher."""
    return MarketDataService(ibkr_client=mock_ibkr)


@pytest.fixture
def svc_with_tt(mock_ibkr):
    """MarketDataService with a mocked Tastytrade fetcher."""
    tt_fetcher = MagicMock()
    return MarketDataService(ibkr_client=mock_ibkr, tastytrade_fetcher=tt_fetcher)


# ---------------------------------------------------------------------------
# Quote dataclass
# ---------------------------------------------------------------------------


class TestQuote:
    def test_mid_returns_average_of_bid_ask(self):
        q = Quote(symbol="SPY", conid=1, bid=100.0, ask=101.0, last=100.5, fetched_at=datetime.utcnow())
        assert q.mid == pytest.approx(100.5)

    def test_mid_returns_none_when_bid_missing(self):
        q = Quote(symbol="SPY", conid=1, bid=None, ask=101.0, last=None, fetched_at=datetime.utcnow())
        assert q.mid is None

    def test_spread_is_ask_minus_bid(self):
        q = Quote(symbol="SPY", conid=1, bid=100.0, ask=101.0, last=None, fetched_at=datetime.utcnow())
        assert q.spread == pytest.approx(1.0)

    def test_spread_returns_none_when_prices_missing(self):
        q = Quote(symbol="SPY", conid=None, bid=None, ask=None, last=None, fetched_at=datetime.utcnow())
        assert q.spread is None

    def test_is_valid_requires_at_least_one_price(self):
        # is_valid() returns True if ANY of bid/ask/last is non-None
        q_full = Quote(symbol="SPY", conid=1, bid=100.0, ask=101.0, last=100.5, fetched_at=datetime.utcnow())
        q_ask_only = Quote(symbol="SPY", conid=1, bid=None, ask=101.0, last=None, fetched_at=datetime.utcnow())
        q_last_only = Quote(symbol="SPY", conid=1, bid=None, ask=None, last=100.9, fetched_at=datetime.utcnow())
        q_empty = Quote(symbol="SPY", conid=1, bid=None, ask=None, last=None, fetched_at=datetime.utcnow())
        assert q_full.is_valid() is True
        assert q_ask_only.is_valid() is True
        assert q_last_only.is_valid() is True
        assert q_empty.is_valid() is False


# ---------------------------------------------------------------------------
# OptionQuote dataclass
# ---------------------------------------------------------------------------


class TestOptionQuote:
    def _make(self, bid=1.0, ask=1.5) -> OptionQuote:
        return OptionQuote(
            symbol="SPX  250120C05000000",
            underlying="SPX",
            expiry="2025-01-20",
            strike=5000.0,
            option_type="CALL",
            bid=bid,
            ask=ask,
            last=1.2,
            delta=0.45,
            iv=0.18,
            gamma=0.002,
            theta=-0.05,
            vega=0.3,
            conid=None,
            fetched_at=datetime.utcnow(),
        )

    def test_mid(self):
        q = self._make(bid=1.0, ask=1.5)
        assert q.mid == pytest.approx(1.25)

    def test_spread(self):
        q = self._make(bid=1.0, ask=1.5)
        assert q.spread == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# resolve_conid
# ---------------------------------------------------------------------------


class TestResolveConid:
    def test_returns_first_conid_on_success(self, svc, mock_ibkr):
        mock_ibkr.session.get.return_value = MagicMock(
            status_code=200,
            json=lambda: [{"conid": 12345}],
        )
        result = svc.resolve_conid("SPY", sec_type="STK")
        assert result == 12345

    def test_returns_none_on_empty_list(self, svc, mock_ibkr):
        mock_ibkr.session.get.return_value = MagicMock(
            status_code=200,
            json=lambda: [],
        )
        result = svc.resolve_conid("DOESNOTEXIST")
        assert result is None

    def test_returns_none_on_http_error(self, svc, mock_ibkr):
        mock_ibkr.session.get.return_value = MagicMock(status_code=500)
        result = svc.resolve_conid("SPY")
        assert result is None

    def test_returns_none_on_network_exception(self, svc, mock_ibkr):
        import requests
        mock_ibkr.session.get.side_effect = requests.RequestException("timeout")
        result = svc.resolve_conid("SPY")
        assert result is None


# ---------------------------------------------------------------------------
# get_quote (stock / ETF)
# ---------------------------------------------------------------------------


class TestGetQuote:
    def _setup_snapshot(self, mock_ibkr, conid=123, bid=100.0, ask=101.0, last=100.5):
        """Configure mock ibkr to return a valid snapshot response.

        get_quote() calls:
          1. session.get for secdef/search  → conid
          2. ibkr.get_market_snapshot()    → {conid: {field: value}}
        """
        mock_ibkr.session.get.return_value = MagicMock(
            status_code=200,
            json=lambda: [{"conid": conid}],
        )
        mock_ibkr.get_market_snapshot.return_value = {
            conid: {
                "84": str(bid),   # bid
                "86": str(ask),   # ask
                "31": str(last),  # last
            }
        }

    def test_returns_quote_with_prices(self, svc, mock_ibkr):
        self._setup_snapshot(mock_ibkr)
        quote = svc.get_quote("SPY")
        assert quote is not None
        assert quote.bid == pytest.approx(100.0)
        assert quote.ask == pytest.approx(101.0)
        assert quote.last == pytest.approx(100.5)
        assert quote.symbol == "SPY"

    def test_returns_none_when_conid_not_found(self, svc, mock_ibkr):
        mock_ibkr.session.get.return_value = MagicMock(status_code=200, json=lambda: [])
        quote = svc.get_quote("GHOST")
        assert quote is None

    def test_handles_missing_price_fields_gracefully(self, svc, mock_ibkr):
        conid = 99
        mock_ibkr.session.get.return_value = MagicMock(
            status_code=200, json=lambda: [{"conid": conid}]
        )
        # Snapshot returns entry but no price fields
        mock_ibkr.get_market_snapshot.return_value = {conid: {}}
        quote = svc.get_quote("NOPRICE")
        # Should return a Quote with None prices rather than exploding
        assert quote is not None
        assert quote.bid is None
        assert quote.ask is None


# ---------------------------------------------------------------------------
# get_futures_quote
# ---------------------------------------------------------------------------


class TestGetFuturesQuote:
    def test_returns_quote_for_es(self, svc, mock_ibkr):
        """Happy path: ES front-month futures quote.

        get_futures_quote() calls:
          1. session.get secdef/search FUT  → [{conid, sections}]
          2. session.get secdef/info        → [{conid}]  (a list)
          3. ibkr.get_market_snapshot()     → {conid: {fields}}
        """
        front_conid = 495512558
        parent_conid = 11004968
        mock_ibkr.session.get.side_effect = [
            # secdef/search FUT → parent_conid with FUT section
            MagicMock(
                status_code=200,
                json=lambda: [{
                    "conid": parent_conid,
                    "sections": [{"secType": "FUT", "exchange": "CME"}],
                }],
            ),
            # secdef/info → list of front-month contracts
            MagicMock(
                status_code=200,
                json=lambda: [{"conid": front_conid}],
            ),
        ]
        mock_ibkr.get_market_snapshot.return_value = {
            front_conid: {
                "84": "5250.00",
                "86": "5250.25",
                "31": "5250.00",
            }
        }
        quote = svc.get_futures_quote("ES")
        assert quote is not None
        assert quote.bid == pytest.approx(5250.0)
        assert quote.is_valid() is True

    def test_returns_none_when_secdef_empty(self, svc, mock_ibkr):
        mock_ibkr.session.get.return_value = MagicMock(status_code=200, json=lambda: [])
        quote = svc.get_futures_quote("ES")
        assert quote is None


# ---------------------------------------------------------------------------
# get_options_chain (Tastytrade path)
# ---------------------------------------------------------------------------


class TestGetOptionsChain:
    def _make_option_data(self, strike=5000.0, opt_type="Call", expiry="2025-01-17"):
        od = MagicMock()
        od.symbol = "SPX"
        od.underlying = "SPX"
        od.strike = strike
        od.option_type = opt_type
        od.expiration = expiry
        od.bid = 10.0
        od.ask = 11.0
        od.mid = 10.5
        od.iv = 0.18
        od.delta = 0.45
        od.gamma = 0.001
        od.theta = -0.10
        od.vega = 0.50
        return od

    def test_returns_option_quotes_from_tastytrade(self, svc_with_tt):
        # Tastytrade fetcher is stored as _tt
        tt = svc_with_tt._tt
        tt.simulate_prefetch.return_value = {
            "SPX_C_5000": self._make_option_data(5000, "Call"),
            "SPX_P_5000": self._make_option_data(5000, "Put"),
        }
        chain = svc_with_tt.get_options_chain("SPX")
        assert len(chain) == 2
        assert all(isinstance(q, OptionQuote) for q in chain)
        symbols = {q.option_type.upper() for q in chain}
        assert "CALL" in symbols
        assert "PUT" in symbols

    def test_returns_empty_list_without_tastytrade(self, svc):
        """When no Tastytrade fetcher is configured, chain is empty."""
        chain = svc.get_options_chain("SPX")
        assert chain == []

    def test_filters_by_expiry_when_provided(self, svc_with_tt):
        tt = svc_with_tt._tt
        tt.simulate_prefetch.return_value = {
            "a": self._make_option_data(5000, "Call", "2025-01-17"),
            "b": self._make_option_data(5000, "Call", "2025-02-21"),
        }
        chain = svc_with_tt.get_options_chain("SPX", expiry="2025-01-17")
        assert all(q.expiry == "2025-01-17" for q in chain)
        assert len(chain) == 1

    def test_returns_empty_on_tastytrade_exception(self, svc_with_tt):
        tt = svc_with_tt._tt
        tt.simulate_prefetch.side_effect = RuntimeError("TT unavailable")
        chain = svc_with_tt.get_options_chain("SPX")
        assert chain == []
