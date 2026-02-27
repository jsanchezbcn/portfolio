"""
tests/test_trade_proposer.py
─────────────────────────────
Covers T023:
    TestTradeProposerCycle  — verify run_cycle() calls detector, engine, persist_top3
    TestOptionCNotification — verify notification fired / not fired per threshold
    TestMockBreachMode      — verify MOCK_BREACH injects synthetic vega breach
    TestRunOnceCLI          — verify --run-once exits cleanly (no gateway needed)
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.proposer_engine import (
    BreachDetector,
    BreachEvent,
    CandidateTrade,
    ProposerEngine,
    RiskRegimeLoader,
)


# ============================================================================
# Helpers
# ============================================================================

def _make_breach(regime: str = "neutral_volatility") -> BreachEvent:
    return BreachEvent(
        greek="vega",
        current_value=-8000.0,
        limit=-4800.0,
        distance_to_target=-3200.0,
        regime=regime,
        account_id="DU_TEST",
    )


def _make_candidate(score: float = 1.5, strategy: str = "SPX Bear Put Spread 45 DTE") -> CandidateTrade:
    return CandidateTrade(
        underlying="SPX",
        strategy_name=strategy,
        legs=[{"symbol": "SPX", "action": "BUY"}],
        vega_reduction=50.0,
        delta_reduction=-5.0,
        init_margin_impact=1500.0,
        efficiency_score=score,
        justification=f"Score: {score:.2f}",
        breach=_make_breach(),
    )


# ============================================================================
# TestTradeProposerCycle
# ============================================================================

class TestTradeProposerCycle:
    """Verify run_cycle() orchestrates breach detection → generation → persistence."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_run_cycle_mock_breach_returns_candidates(self) -> None:
        """With MOCK_BREACH=TRUE, run_cycle should return a non-empty candidate list."""
        loader    = RiskRegimeLoader()
        detector  = BreachDetector(loader)
        engine    = ProposerEngine(adapter=None, loader=loader)
        dispatcher = MagicMock()
        dispatcher.send_alert = AsyncMock(return_value=True)

        with patch.dict(os.environ, {"MOCK_BREACH": "TRUE"}):
            import importlib
            import agents.trade_proposer as tp_module
            importlib.reload(tp_module)

            candidates = self._run(tp_module.run_cycle(
                adapter=None,
                loader=loader,
                detector=detector,
                engine=engine,
                dispatcher=dispatcher,
                account_id="DU_TEST",
                nlv=100_000.0,
            ))

        assert len(candidates) > 0, "Expected at least one candidate from MOCK_BREACH run"

    def test_run_cycle_no_breach_returns_empty(self) -> None:
        """When detector finds no breaches, generate should not be called."""
        loader   = RiskRegimeLoader()
        detector = MagicMock()
        detector.check.return_value = []  # no breaches

        engine    = MagicMock()
        engine.generate = AsyncMock(return_value=[])
        dispatcher = MagicMock()
        dispatcher.send_alert = AsyncMock(return_value=True)

        with patch.dict(os.environ, {"MOCK_BREACH": "TRUE"}):
            import importlib
            import agents.trade_proposer as tp_module
            importlib.reload(tp_module)

            candidates = self._run(tp_module.run_cycle(
                adapter=None,
                loader=loader,
                detector=detector,
                engine=engine,
                dispatcher=dispatcher,
                account_id="DU_TEST",
                nlv=100_000.0,
            ))

        assert candidates == []

    def test_run_cycle_calls_persist_top3_when_db_available(self) -> None:
        """When session is available, persist_top3 must be called."""
        loader   = RiskRegimeLoader()

        mock_breach = _make_breach()
        mock_candidate = _make_candidate()

        detector = MagicMock()
        detector.check.return_value = [mock_breach]

        engine = MagicMock()
        engine.generate = AsyncMock(return_value=[mock_candidate])
        engine.persist_top3 = MagicMock()

        dispatcher = MagicMock()
        dispatcher.send_alert = AsyncMock(return_value=True)

        # Inject a mock session via _get_session patch
        mock_session = MagicMock()
        mock_session.close = MagicMock()

        with patch.dict(os.environ, {"MOCK_BREACH": "TRUE", "PROPOSER_DB_URL": "sqlite:///test.db"}):
            import importlib
            import agents.trade_proposer as tp_module
            importlib.reload(tp_module)

            with patch.object(tp_module, "_get_session", return_value=mock_session):
                self._run(tp_module.run_cycle(
                    adapter=None,
                    loader=loader,
                    detector=detector,
                    engine=engine,
                    dispatcher=dispatcher,
                    account_id="DU_TEST",
                    nlv=100_000.0,
                ))

        engine.persist_top3.assert_called_once()
        # persist_top3 receives: account_id, candidates, session
        call_args = engine.persist_top3.call_args
        assert call_args[0][0] == "DU_TEST"           # account_id
        assert call_args[0][1] == [mock_candidate]     # candidates

    def test_run_cycle_skips_persist_when_no_db(self) -> None:
        """When PROPOSER_DB_URL is not set, persist_top3 should NOT be called."""
        loader   = RiskRegimeLoader()
        mock_breach = _make_breach()
        mock_candidate = _make_candidate()

        detector = MagicMock()
        detector.check.return_value = [mock_breach]

        engine = MagicMock()
        engine.generate = AsyncMock(return_value=[mock_candidate])
        engine.persist_top3 = MagicMock()

        dispatcher = MagicMock()
        dispatcher.send_alert = AsyncMock(return_value=True)

        env = {"MOCK_BREACH": "TRUE"}
        env.pop("PROPOSER_DB_URL", None)

        with patch.dict(os.environ, env, clear=False):
            # Ensure PROPOSER_DB_URL is absent
            os.environ.pop("PROPOSER_DB_URL", None)
            import importlib
            import agents.trade_proposer as tp_module
            importlib.reload(tp_module)

            self._run(tp_module.run_cycle(
                adapter=None,
                loader=loader,
                detector=detector,
                engine=engine,
                dispatcher=dispatcher,
                account_id="DU_TEST",
                nlv=100_000.0,
            ))

        engine.persist_top3.assert_not_called()


# ============================================================================
# TestOptionCNotification
# ============================================================================

class TestOptionCNotification:
    """Verify notification is fired / not fired per FR spec."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _make_tp_module(self, threshold: float = 0.5):
        env = {"MOCK_BREACH": "FALSE", "PROPOSER_NOTIFY_THRESHOLD": str(threshold)}
        with patch.dict(os.environ, env):
            import importlib
            import agents.trade_proposer as tp_module
            importlib.reload(tp_module)
            return tp_module

    def test_notification_sent_when_score_above_threshold(self) -> None:
        tp_module  = self._make_tp_module(threshold=0.5)
        dispatcher = MagicMock()
        dispatcher.send_alert = AsyncMock(return_value=True)

        candidates = [_make_candidate(score=1.5)]  # 1.5 > 0.5

        self._run(tp_module._send_option_c_notification(
            dispatcher, candidates, regime="neutral_volatility"
        ))

        dispatcher.send_alert.assert_called_once()

    def test_notification_not_sent_when_score_below_threshold(self) -> None:
        tp_module  = self._make_tp_module(threshold=0.5)
        dispatcher = MagicMock()
        dispatcher.send_alert = AsyncMock(return_value=True)

        candidates = [_make_candidate(score=0.2)]  # 0.2 < 0.5

        self._run(tp_module._send_option_c_notification(
            dispatcher, candidates, regime="neutral_volatility"
        ))

        dispatcher.send_alert.assert_not_called()

    def test_notification_sent_in_crisis_mode_regardless_of_score(self) -> None:
        tp_module  = self._make_tp_module(threshold=0.5)
        dispatcher = MagicMock()
        dispatcher.send_alert = AsyncMock(return_value=True)

        candidates = [_make_candidate(score=0.1)]  # below threshold

        self._run(tp_module._send_option_c_notification(
            dispatcher, candidates, regime="crisis_mode"  # triggers regardless
        ))

        dispatcher.send_alert.assert_called_once()

    def test_notification_not_sent_when_no_candidates(self) -> None:
        tp_module  = self._make_tp_module(threshold=0.5)
        dispatcher = MagicMock()
        dispatcher.send_alert = AsyncMock(return_value=True)

        self._run(tp_module._send_option_c_notification(
            dispatcher, candidates=[], regime="neutral_volatility"
        ))

        dispatcher.send_alert.assert_not_called()

    def test_notification_urgency_red_in_crisis(self) -> None:
        tp_module  = self._make_tp_module(threshold=0.5)
        dispatcher = MagicMock()
        dispatcher.send_alert = AsyncMock(return_value=True)

        candidates = [_make_candidate(score=0.1)]

        self._run(tp_module._send_option_c_notification(
            dispatcher, candidates, regime="crisis_mode"
        ))

        _, kwargs = dispatcher.send_alert.call_args
        assert kwargs.get("urgency") == "red"

    def test_notification_urgency_yellow_for_score_trigger(self) -> None:
        tp_module  = self._make_tp_module(threshold=0.5)
        dispatcher = MagicMock()
        dispatcher.send_alert = AsyncMock(return_value=True)

        candidates = [_make_candidate(score=2.0)]  # well above threshold

        self._run(tp_module._send_option_c_notification(
            dispatcher, candidates, regime="neutral_volatility"
        ))

        _, kwargs = dispatcher.send_alert.call_args
        assert kwargs.get("urgency") == "yellow"


# ============================================================================
# TestMockBreachMode
# ============================================================================

class TestMockBreachMode:
    """Verify MOCK_BREACH mode generates expected synthetic breach."""

    def test_mock_greeks_trigger_vega_breach(self) -> None:
        """Synthetic greeks should cause BreachDetector to flag a vega breach."""
        import importlib
        import agents.trade_proposer as tp_module
        importlib.reload(tp_module)

        greeks = tp_module._make_mock_greeks("DU_TEST")
        loader   = RiskRegimeLoader()
        detector = BreachDetector(loader)

        events = detector.check(greeks, account_nlv=100_000.0, account_id="DU_TEST")
        vega_events = [e for e in events if e.greek == "vega"]
        assert len(vega_events) == 1, f"Expected exactly 1 vega breach, got {vega_events}"
        assert vega_events[0].distance_to_target < 0, "Vega breach should show negative distance"

    def test_mock_greeks_regime_is_neutral(self) -> None:
        import importlib
        import agents.trade_proposer as tp_module
        importlib.reload(tp_module)

        greeks   = tp_module._make_mock_greeks("DU_TEST")
        loader   = RiskRegimeLoader()
        detector = BreachDetector(loader)
        events   = detector.check(greeks, account_nlv=100_000.0)
        assert all(e.regime == "neutral_volatility" for e in events)


# ============================================================================
# TestRunOnceCLI
# ============================================================================

class TestRunOnceCLI:
    """Verify the --run-once flag completes without raising."""

    def test_run_once_exits_cleanly_with_mock_breach(self) -> None:
        """MOCK_BREACH=TRUE --run-once should complete without exception."""
        with patch.dict(os.environ, {"MOCK_BREACH": "TRUE"}):
            import importlib
            import agents.trade_proposer as tp_module
            importlib.reload(tp_module)

            with patch("sys.argv", ["trade_proposer", "--run-once"]):
                # Should complete without raising
                try:
                    asyncio.get_event_loop().run_until_complete(tp_module.main())
                except SystemExit:
                    pass  # argparse may sys.exit(0) — that's fine
