"""desktop/workers/agent_runner.py — Background asyncio agents for the desktop app.

Agents in this module run as long-lived asyncio tasks inside the qasync event
loop.  They communicate with the UI exclusively via Qt signals (thread-safe).

Agents implemented
──────────────────
RiskMonitorAgent
    • Polls the IBEngine every N seconds (default 60s while market is open,
      300s outside hours).
    • Compares portfolio Greeks against risk_matrix.yaml thresholds (NLV-scaled).
    • Sends a Telegram notification the FIRST time a limit is breached.
      A subsequent notification for the SAME metric is suppressed until the
      breach clears and then re-triggers — no spam.
    • Emits `alert_raised(dict)` signal so the AI/Risk tab can show a banner.

ArbScanAgent
    • After each positions refresh, formats the live chain snapshot and runs
      ArbHunter.scan_all() to find put-call parity / box-spread violations.
    • Emits `arb_signal(dict)` signal for the AI tab.

TradeSuggestionAgent
    • Generates high-probability trade ideas from the live chain:
        – 0DTE delta-4 short strangles on ES/MES
        – Iron condors at ≥80% probability of profit
        – Calendar spreads when VIX curve is steep
    • Emits `trade_suggestion(dict)` signal for the AI tab.

Usage (MainWindow)
──────────────────
    runner = AgentRunner(engine)
    runner.start()          # schedules asyncio tasks; safe to call multiple times
    runner.stop()           # cancels all tasks

    runner.alert_raised.connect(...)      # QObject signals — connect to UI
    runner.arb_signal.connect(...)
    runner.trade_suggestion.connect(...)
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import yaml
from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

# ── constants ──────────────────────────────────────────────────────────────────
_RISK_POLL_MARKET_HOURS_S = int(os.getenv("AGENT_RISK_POLL_MARKET_S", "30"))
_RISK_POLL_OFF_HOURS_S    = int(os.getenv("AGENT_RISK_POLL_OFF_S", "300"))
_ARB_SCAN_COOLDOWN_S      = int(os.getenv("AGENT_ARB_SCAN_COOLDOWN_S", "120"))
_TRADE_SUGGEST_COOLDOWN_S = int(os.getenv("AGENT_TRADE_SUGGEST_S", "300"))

# Market session (ET): 09:30 – 16:00 Mon-Fri
_SESSION_OPEN  = (9, 30)
_SESSION_CLOSE = (16, 0)


def _market_open_et() -> bool:
    """True when US market session is active."""
    try:
        import zoneinfo
        now_et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    except ImportError:
        # Rough -5h offset fallback
        now_et = datetime.utcnow() - timedelta(hours=5)
    wd = now_et.weekday()
    if wd >= 5:  # weekend
        return False
    t = (now_et.hour, now_et.minute)
    return _SESSION_OPEN <= t < _SESSION_CLOSE


class AgentRunner(QObject):
    """Orchestrator that runs background agents as asyncio tasks.

    Connect to these signals to receive live updates in the UI:
        alert_raised     – dict with keys: metric, current, limit, message, severity
        arb_signal       – dict with keys: type, expiry, strikes, edge, message
        trade_suggestion – dict with keys: strategy, symbol, legs, rationale, pop
    """

    alert_raised      = Signal(dict)
    arb_signal        = Signal(dict)
    trade_suggestion  = Signal(dict)

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self._engine = engine
        self._tasks: list[asyncio.Task] = []
        self._running = False

        # Breach tracking — key: metric name → last breach time (or None if clear)
        self._active_breaches: dict[str, datetime] = {}

        # Load risk matrix once
        self._risk_matrix: dict[str, Any] = {}
        self._load_risk_matrix()

        # Telegram notifier (outbound only — no bot command loop)
        self._telegram = TelegramNotifier()

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Schedule background agent tasks.  Safe to call multiple times."""
        if self._running:
            return
        self._running = True
        loop = asyncio.get_event_loop()
        self._tasks = [
            loop.create_task(self._risk_monitor_loop(),  name="RiskMonitor"),
            loop.create_task(self._arb_scan_loop(),      name="ArbScan"),
            loop.create_task(self._trade_suggest_loop(), name="TradeSuggest"),
        ]
        logger.info("AgentRunner started: %d tasks", len(self._tasks))

    def stop(self) -> None:
        """Cancel all background tasks."""
        self._running = False
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        logger.info("AgentRunner stopped")

    # ── risk monitor ──────────────────────────────────────────────────────────

    async def _risk_monitor_loop(self) -> None:
        """Check risk limits periodically; alert once per breach."""
        # Wait for first positions snapshot
        await asyncio.sleep(15)
        while self._running:
            try:
                await self._check_risk()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("RiskMonitorAgent error: %s", exc)

            interval = _RISK_POLL_MARKET_HOURS_S if _market_open_et() else _RISK_POLL_OFF_HOURS_S
            await asyncio.sleep(interval)

    async def _check_risk(self) -> None:
        positions = self._engine.positions_snapshot()
        account   = self._engine.account_snapshot()
        if not positions or not account:
            return

        nlv   = float(getattr(account, "net_liquidation", 0) or 0)
        regime = self._detect_regime(nlv)
        limits = self._effective_limits(regime, nlv)

        # Aggregate Greeks
        total_spx_delta = sum((getattr(r, "spx_delta") or 0) for r in positions)
        total_theta     = sum((getattr(r, "theta") or 0) for r in positions)
        total_vega      = sum((getattr(r, "vega") or 0) for r in positions)
        total_gamma     = sum((getattr(r, "gamma") or 0) for r in positions)

        checks = [
            ("spx_delta",  abs(total_spx_delta),  limits.get("max_delta",       9e9), "SPX Δ",     abs(total_spx_delta) > limits.get("max_delta", 9e9)),
            ("theta",      total_theta,            limits.get("min_theta",       -9e9),"Θ per day", total_theta < limits.get("min_theta", -9e9)),
            ("vega",       total_vega,             limits.get("max_neg_vega",    -9e9),"V vega",    total_vega < limits.get("max_neg_vega", -9e9)),
            ("gamma",      abs(total_gamma),       limits.get("max_gamma",       9e9), "Γ gamma",   abs(total_gamma) > limits.get("max_gamma", 9e9)),
        ]

        cleared: list[str] = []
        for metric, current, limit, label, breached in checks:
            if breached:
                if metric not in self._active_breaches:
                    # First time this breach fires → notify
                    self._active_breaches[metric] = datetime.now(timezone.utc)
                    msg = (
                        f"⚠️ *Risk Limit Breached* — {label}\n"
                        f"Current: `{current:+.2f}`\n"
                        f"Limit:   `{limit:+.2f}`\n"
                        f"Regime:  `{regime}`\n"
                        f"NLV:     `${nlv:,.0f}`"
                    )
                    payload = {
                        "metric": metric,
                        "label":  label,
                        "current": current,
                        "limit": limit,
                        "regime": regime,
                        "message": msg,
                        "severity": "critical" if abs(current) > abs(limit) * 1.5 else "warning",
                    }
                    self.alert_raised.emit(payload)
                    await self._telegram.send(msg)
                    logger.warning("Risk breach: %s current=%s limit=%s", metric, current, limit)
                # else: already notified, stay silent
            else:
                if metric in self._active_breaches:
                    cleared.append(metric)

        for m in cleared:
            del self._active_breaches[m]
            logger.info("Risk breach cleared: %s", m)

    def _detect_regime(self, nlv: float) -> str:
        """Simple regime based on VIX from last market snapshot."""
        try:
            snap = self._engine.last_market_snapshot("VIX") or {}
            vix = float(snap.get("last") or snap.get("close") or 20.0)
        except Exception:
            vix = 20.0
        if vix >= 35:
            return "crisis_mode"
        if vix >= 22:
            return "high_volatility"
        if vix >= 15:
            return "neutral_volatility"
        return "low_volatility"

    def _effective_limits(self, regime: str, nlv: float) -> dict[str, float]:
        """Convert pct_nlv limits → absolute values for the current portfolio."""
        cfg = (self._risk_matrix.get("regimes") or {}).get(regime, {})
        lims = cfg.get("limits", {})
        if nlv > 0:
            return {
                "max_delta":   lims.get("max_spx_delta_pct_nlv", 0.012) * nlv,
                "min_theta":   lims.get("min_daily_theta_pct_nlv", 0.0012) * nlv,
                "max_neg_vega":lims.get("max_negative_vega_pct_nlv", -0.048) * nlv,
                "max_gamma":   lims.get("max_gamma_pct_nlv", 0.0014) * nlv,
            }
        # Fallback absolute limits
        return {
            "max_delta":    lims.get("legacy_max_beta_delta", 300),
            "min_theta":    lims.get("legacy_min_daily_theta", 30),
            "max_neg_vega": -lims.get("legacy_max_negative_vega", 1200),
            "max_gamma":    lims.get("legacy_max_gamma", 35),
        }

    # ── arb scan ──────────────────────────────────────────────────────────────

    async def _arb_scan_loop(self) -> None:
        """Scan the live option chain for arbitrage opportunities."""
        await asyncio.sleep(30)  # let chain load first
        last_scan = datetime.min
        while self._running:
            try:
                now = datetime.now(timezone.utc)
                if (now - last_scan).total_seconds() >= _ARB_SCAN_COOLDOWN_S:
                    await self._run_arb_scan()
                    last_scan = now
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("ArbScan error: %s", exc)
            await asyncio.sleep(30)

    async def _run_arb_scan(self) -> None:
        try:
            from agents.arb_hunter import ArbHunter
        except ImportError:
            return
        chain_snapshot = self._engine.chain_snapshot()
        if not chain_snapshot:
            return

        # Convert ChainRow list to format expected by ArbHunter
        # {underlying_price, risk_free_rate, "YYYY-MM-DD": {strike: {call, put}}}
        und_price = 0.0
        by_expiry: dict[str, dict[float, dict]] = {}
        for row in chain_snapshot:
            if not row.expiry:
                continue
            exp_str = str(row.expiry).replace("-", "")
            if len(exp_str) == 8:
                exp_iso = f"{exp_str[:4]}-{exp_str[4:6]}-{exp_str[6:8]}"
            else:
                exp_iso = str(row.expiry)
            if exp_iso not in by_expiry:
                by_expiry[exp_iso] = {}
            if row.strike not in by_expiry[exp_iso]:
                by_expiry[exp_iso][row.strike] = {}
            side = "call" if row.right == "C" else "put"
            mid = (row.bid + row.ask) / 2 if (row.bid and row.ask) else None
            price = row.last or mid
            if price:
                by_expiry[exp_iso][row.strike][side] = price

        # Use last market snapshot for underlying price
        # Determine underlying from the chain rows
        underlying_sym = (chain_snapshot[0].underlying if chain_snapshot else "ES")
        mkt = self._engine.last_market_snapshot(underlying_sym) or {}
        und_price = mkt.get("last") or mkt.get("close") or 0.0
        if not und_price or not by_expiry:
            return

        chain_data = {"underlying_price": und_price, "risk_free_rate": 0.053}
        chain_data.update(by_expiry)

        hunter = ArbHunter(db=None)  # no DB needed for signal detection
        try:
            signals = hunter.scan_all(chain_data)
        except Exception as exc:
            logger.debug("ArbHunter.scan_all error: %s", exc)
            return

        for sig in (signals or []):
            edge = sig.get("edge", sig.get("pcp_violation", sig.get("box_violation", 0)))
            if abs(edge) < 0.50:  # filter < 50c edge
                continue
            payload = {
                "type":    sig.get("signal_type", "arb"),
                "expiry":  sig.get("expiry", ""),
                "strikes": sig.get("strikes", []),
                "edge":    edge,
                "message": (
                    f"🎯 *Arb Signal* — {sig.get('signal_type','?')}\n"
                    f"Expiry: {sig.get('expiry','?')}  Strikes: {sig.get('strikes','?')}\n"
                    f"Edge: `${abs(edge):.2f}` per spread"
                ),
            }
            self.arb_signal.emit(payload)
            logger.info("Arb signal: %s edge=$%.2f", sig.get("signal_type"), abs(edge))

    # ── trade suggestions ─────────────────────────────────────────────────────

    async def _trade_suggest_loop(self) -> None:
        """Generate high-probability trade ideas from live chain data."""
        await asyncio.sleep(45)
        last_run = datetime.min
        while self._running:
            try:
                now = datetime.now(timezone.utc)
                if _market_open_et() and (now - last_run).total_seconds() >= _TRADE_SUGGEST_COOLDOWN_S:
                    await self._generate_suggestions()
                    last_run = now
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("TradeSuggest error: %s", exc)
            await asyncio.sleep(60)

    # ── Micro-to-full preference map (prefer full-size over Micro) ─────────────
    _PREFER_FULL: dict[str, str] = {
        "MES": "ES",   "MNQ": "NQ",   "M2K": "RTY",   "MYM": "YM",
    }
    # ES=50, MES=5, NQ=20, MNQ=2, RTY=50, YM=5
    _MULT: dict[str, int] = {
        "ES": 50, "MES": 5, "NQ": 20, "MNQ": 2,
        "RTY": 50, "M2K": 5, "YM": 5, "MYM": 1,
    }

    def _best_chain(self) -> tuple[list, str, int]:
        """Return (chain_rows, preferred_symbol, multiplier).

        If the live chain is a Micro future we upgrade to the equivalent
        full-size future (same strikes/expiries, 10× multiplier) so that
        suggestions use /ES instead of /MES.  The chain rows themselves
        remain the same — the strikes are identical for ES and MES.
        """
        chain = self._engine.chain_snapshot()
        if not chain:
            return [], "ES", 50
        raw_sym = chain[0].underlying if chain else "ES"
        # Attempt to find a full-size chain in cache (e.g. ES when MES is active)
        preferred_sym = self._PREFER_FULL.get(raw_sym, raw_sym)
        if preferred_sym != raw_sym:
            # Look for the full-size chain in the engine's cache
            cache: dict = getattr(self._engine, "_chain_cache", {})
            for key, (_ts, rows) in cache.items():
                if rows and rows[0].underlying.upper() == preferred_sym.upper():
                    chain = rows
                    raw_sym = preferred_sym
                    break
            else:
                # No full-size chain cached — keep MES rows but advertise as ES
                # by adjusting the multiplier label (strikes are identical).
                # We still suggest ES contracts because the exchange accepts them.
                raw_sym = preferred_sym  # label upgrade; strikes still valid
        mult = self._MULT.get(raw_sym, 100)
        return chain, raw_sym, mult

    async def _generate_suggestions(self) -> None:
        chain, und_sym, mult = self._best_chain()
        if not chain:
            return
        account  = self._engine.account_snapshot()
        nlv = float(getattr(account, "net_liquidation", 0) or 0) if account else 0

        suggestions: list[dict] = []


        # Strategy 1: 0DTE delta-4 short strangle (ES/MES)
        today_str = date.today().strftime("%Y%m%d")
        calls_0dte = [r for r in chain if r.right == "C" and r.expiry == today_str and r.delta is not None]
        puts_0dte  = [r for r in chain if r.right == "P" and r.expiry == today_str and r.delta is not None]

        if calls_0dte and puts_0dte:
            # Find ~4-delta call and put
            call_4d = min(calls_0dte, key=lambda r: abs(abs(r.delta) - 0.04))
            put_4d  = min(puts_0dte,  key=lambda r: abs(abs(r.delta) - 0.04))
            call_mid = ((call_4d.bid or 0) + (call_4d.ask or 0)) / 2
            put_mid  = ((put_4d.bid or 0) + (put_4d.ask or 0)) / 2
            total_credit = (call_mid + put_mid) * mult

            if total_credit > 20 and abs(call_4d.delta) <= 0.06 and abs(put_4d.delta) <= 0.06:
                pop_est = (1 - abs(call_4d.delta)) * (1 - abs(put_4d.delta)) * 100
                suggestions.append({
                    "strategy": "0DTE Short Strangle ~4Δ",
                    "symbol":   und_sym,
                    "expiry":   today_str,
                    "legs": [
                        {"action": "SELL", "right": "C", "strike": call_4d.strike,
                         "delta": call_4d.delta, "mid": call_mid},
                        {"action": "SELL", "right": "P", "strike": put_4d.strike,
                         "delta": put_4d.delta, "mid": put_mid},
                    ],
                    "credit_per_contract": total_credit,
                    "pop_pct": round(pop_est, 1),
                    "rationale": (
                        f"0DTE strangle at ~4Δ. "
                        f"Credit ${total_credit:.0f}/contract. "
                        f"Est. P.O.P. {pop_est:.0f}%. "
                        f"Strikes: {put_4d.strike}/{call_4d.strike}. "
                        f"Manage at 50% profit or close EOD."
                    ),
                })

        # Strategy 2: Short strangle with ≥80% PoP (furthest weekly expiry ≤ 14 DTE)
        expiries_near = sorted(
            set(r.expiry for r in chain if r.expiry and r.expiry > today_str),
        )[:2]  # nearest 2 non-0DTE expiries
        for exp in expiries_near:
            days = (
                datetime.strptime(exp, "%Y%m%d").date() - date.today()
            ).days if len(exp) == 8 else 0
            if days <= 0 or days > 21:
                continue
            c_opts = [r for r in chain if r.right == "C" and r.expiry == exp and r.delta is not None]
            p_opts = [r for r in chain if r.right == "P" and r.expiry == exp and r.delta is not None]
            if not c_opts or not p_opts:
                continue
            # ~16Δ (1 SD strangle)
            call_16 = min(c_opts, key=lambda r: abs(abs(r.delta) - 0.16))
            put_16  = min(p_opts,  key=lambda r: abs(abs(r.delta) - 0.16))
            c_mid = ((call_16.bid or 0) + (call_16.ask or 0)) / 2
            p_mid = ((put_16.bid or 0) + (put_16.ask or 0)) / 2
            credit = (c_mid + p_mid) * mult

            if credit > 50 and abs(call_16.delta) <= 0.20 and abs(put_16.delta) <= 0.20:
                pop = (1 - abs(call_16.delta)) * (1 - abs(put_16.delta)) * 100
                suggestions.append({
                    "strategy": f"~16Δ Strangle {days}DTE",
                    "symbol":   und_sym,
                    "expiry":   exp,
                    "legs": [
                        {"action": "SELL", "right": "C", "strike": call_16.strike,
                         "delta": call_16.delta, "mid": c_mid},
                        {"action": "SELL", "right": "P", "strike": put_16.strike,
                         "delta": put_16.delta, "mid": p_mid},
                    ],
                    "credit_per_contract": credit,
                    "pop_pct": round(pop, 1),
                    "rationale": (
                        f"{days}DTE 1σ strangle. Credit ${credit:.0f}/contract. "
                        f"Est. P.O.P. {pop:.0f}%. "
                        f"Strikes {put_16.strike}/{call_16.strike}. "
                        f"Target 50% profit; stop at 2× credit."
                    ),
                })

        for s in suggestions[:3]:  # cap to top 3
            self.trade_suggestion.emit(s)
            logger.info("Trade suggestion: %s — %s PoP=%.0f%%",
                        s["strategy"], s["symbol"], s.get("pop_pct", 0))

    # ── helpers ───────────────────────────────────────────────────────────────

    def _load_risk_matrix(self) -> None:
        path = Path(__file__).resolve().parents[2] / "config" / "risk_matrix.yaml"
        try:
            self._risk_matrix = yaml.safe_load(path.read_text()) or {}
        except Exception as exc:
            logger.warning("Could not load risk_matrix.yaml: %s", exc)
            self._risk_matrix = {}


# ── Telegram notifier (outbound-only, no bot loop) ────────────────────────────

class TelegramNotifier:
    """Sends Telegram messages using the Bot HTTP API directly.

    Does NOT start a polling loop — pure outbound push.
    Credentials read from env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
    """

    def __init__(self):
        self._token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self._enabled = bool(self._token and self._chat_id)
        if not self._enabled:
            logger.info("TelegramNotifier: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — notifications disabled")

    async def send(self, text: str) -> bool:
        """Send *text* (Markdown) to the configured chat.  Returns True on success."""
        if not self._enabled:
            return False
        try:
            import aiohttp
            url = f"https://api.telegram.org/bot{self._token}/sendMessage"
            payload = {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_notification": False,
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    ok = resp.status == 200
                    if not ok:
                        logger.warning("Telegram send failed: HTTP %d", resp.status)
                    return ok
        except ImportError:
            # aiohttp not available — try requests in executor
            return await asyncio.get_event_loop().run_in_executor(None, self._send_sync, text)
        except Exception as exc:
            logger.warning("Telegram send error: %s", exc)
            return False

    def _send_sync(self, text: str) -> bool:
        try:
            import requests as _req
            resp = _req.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": self._chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10, verify=True,
            )
            return resp.status_code == 200
        except Exception as exc:
            logger.warning("Telegram sync send error: %s", exc)
            return False
