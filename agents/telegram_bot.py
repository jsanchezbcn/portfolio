#!/usr/bin/env python3
"""
agents/telegram_bot.py — Telegram remote-command centre for the Risk Engine.

Commands exposed:
  /greeks   — SPX delta, gamma, theta, vega + account net-liq
  /regime   — VIX, active risk regime, limit-breach status
  /analyze  — LLM risk assessment via Copilot SDK (gpt-4o-mini)
  /journal  — Append a timestamped note to the trade_journal table
  /help     — Command reference

Background task:
  • Runs every 5 minutes during market hours (Mon-Fri 09:30-16:00 ET).
  • Pushes a Telegram alert if any risk limit is breached.

Security:
  • Refuses ALL commands from users not listed in ALLOWED_TELEGRAM_USERS.

Usage:
  python agents/telegram_bot.py            # foreground (Ctrl-C to stop)
  python agents/telegram_bot.py --dry-run  # test imports, exit immediately
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable

# ── Path / env ─────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s %(levelname)s [telegram_bot] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger("telegram_bot")

# ── Telegram imports ───────────────────────────────────────────────────────────
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_raw_chat_id = os.getenv("TELEGRAM_CHAT_ID", "0")
_OWNER_ID = int(_raw_chat_id) if _raw_chat_id.lstrip("-").isdigit() else 0

# ────────────────────────────────────────────────────────────────────────────────
# ██  SECURITY — edit this list to control who can send commands  ██
# ────────────────────────────────────────────────────────────────────────────────
ALLOWED_TELEGRAM_USERS: list[int] = [_OWNER_ID]   # loaded from TELEGRAM_CHAT_ID

# Alert polling params
_ALERT_INTERVAL_SECONDS = 300   # 5 minutes
_MARKET_OPEN_ET  = (9, 30)      # HH, MM Eastern Time
_MARKET_CLOSE_ET = (16, 0)

# ── Lazy service singletons (initialised once per process) ─────────────────────
_adapter      = None
_portfolio    = None
_market_tools = None
_regime_det   = None


def _services():
    """Return (adapter, portfolio_tools, market_tools, regime_detector) — lazy init."""
    global _adapter, _portfolio, _market_tools, _regime_det
    if _adapter is None:
        from adapters.ibkr_adapter import IBKRAdapter
        from agent_tools.portfolio_tools import PortfolioTools
        from agent_tools.market_data_tools import MarketDataTools
        from risk_engine.regime_detector import RegimeDetector
        _adapter      = IBKRAdapter()
        _portfolio    = PortfolioTools()
        _market_tools = MarketDataTools()
        _regime_det   = RegimeDetector(PROJECT_ROOT / "config" / "risk_matrix.yaml")
    return _adapter, _portfolio, _market_tools, _regime_det


# ═══════════════════════════════════════════════════════════════════════════════
# 1.  SECURITY DECORATOR
# ═══════════════════════════════════════════════════════════════════════════════

def require_auth(handler: Callable) -> Callable:
    """Decorator — silently rejects any update from a non-whitelisted user.

    Usage::

        @require_auth
        async def cmd_greeks(update: Update, ctx: ContextTypes.DEFAULT_TYPE): ...
    """
    @wraps(handler)
    async def _wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or user.id not in ALLOWED_TELEGRAM_USERS:
            uid  = getattr(user, "id", "unknown")
            name = getattr(user, "username", "?")
            logger.warning(
                "SECURITY: rejected command '%s' from unauthorized user %s (%s)",
                update.message.text if update.message else "<no text>",
                uid, name,
            )
            return   # ← silent ignore — do NOT reply to attackers
        return await handler(update, ctx)
    return _wrapper


# ═══════════════════════════════════════════════════════════════════════════════
# 2.  HELPER — fetch Greeks from IBKR (or last cache) + account net-liq
# ═══════════════════════════════════════════════════════════════════════════════

async def _resolve_account_id() -> str:
    """Return account ID from env, or auto-detect the first IBKR account."""
    account_id = os.getenv("IBKR_ACCOUNT_ID", "").strip()
    if account_id:
        return account_id
    # Auto-detect from IBKR
    try:
        adapter, _, _, _ = _services()
        accounts = await asyncio.get_event_loop().run_in_executor(
            None, adapter.client.get_accounts
        )
        if accounts:
            return accounts[0] if isinstance(accounts[0], str) else accounts[0].get("id", "")
    except Exception:
        pass
    return ""


async def _fetch_greeks_summary(account_id: str) -> dict[str, Any]:
    """Return portfolio Greek totals and net liquidation for *account_id*."""
    adapter, portfolio, market_tools, _ = _services()

    # Fetch positions
    try:
        positions = await asyncio.get_event_loop().run_in_executor(
            None, lambda: adapter.fetch_positions(account_id)
        )
    except Exception as exc:
        logger.warning("fetch_positions failed: %s", exc)
        positions = getattr(adapter, "last_positions", None) or []

    # Compute portfolio summary
    summary = portfolio.get_portfolio_summary(positions)

    # Net liquidation from IBKR
    net_liq = None
    try:
        ibkr_summary = await asyncio.get_event_loop().run_in_executor(
            None, lambda: adapter.client.get_account_summary(account_id)
        )
        if isinstance(ibkr_summary, dict):
            raw = ibkr_summary.get("netliquidation")
            net_liq = float(raw.get("amount")) if isinstance(raw, dict) else float(raw)
    except Exception:
        pass

    return {
        "delta":       float(summary.get("total_delta", 0.0)),
        "spx_delta":   float(summary.get("total_spx_delta", 0.0)),
        "gamma":       float(summary.get("total_gamma", 0.0)),
        "theta":       float(summary.get("total_theta", 0.0)),
        "vega":        float(summary.get("total_vega", 0.0)),
        "net_liq":     net_liq,
        "positions":   positions,
        "raw_summary": summary,
    }


async def _fetch_regime():
    """Return (vix_data, regime, violations) tuple."""
    _, portfolio, market_tools, regime_det = _services()
    vix_data = await asyncio.get_event_loop().run_in_executor(
        None, market_tools.get_vix_data
    )
    vix   = float(vix_data.get("vix", 0.0))
    ts    = float(vix_data.get("term_structure", 1.0))
    regime = regime_det.detect_regime(vix=vix, term_structure=ts)

    # Check limits using a dummy summary (greeks not required for regime check)
    violations: list[dict] = []
    try:
        # We fetch actual greeks for the primary account if available
        account_id = await _resolve_account_id()
        if account_id:
            g = await _fetch_greeks_summary(account_id)
            violations = portfolio.check_risk_limits(g["raw_summary"], regime)
    except Exception:
        pass

    return vix_data, regime, violations


# ═══════════════════════════════════════════════════════════════════════════════
# 3.  COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

@require_auth
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📊 *Portfolio Risk Bot*\n\n"
        "/greeks — Greek totals + net-liq\n"
        "/regime — VIX, active regime, limit check\n"
        "/analyze — AI risk assessment (LLM)\n"
        "/journal `<text>` — Append note to trade journal\n"
        "/help — This message",
        parse_mode=ParseMode.MARKDOWN,
    )


@require_auth
async def cmd_greeks(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Return current Greek totals and account net liquidation."""
    await update.message.reply_text("⏳ Fetching Greeks…")

    account_id = await _resolve_account_id()
    if not account_id:
        await update.message.reply_text(
            "⚠️ Cannot resolve IBKR account. Set IBKR_ACCOUNT_ID in .env or ensure the gateway is running."
        )
        return

    try:
        g = await _fetch_greeks_summary(account_id)
    except Exception as exc:
        await update.message.reply_text(f"❌ Error fetching Greeks: {exc}")
        return

    net_liq_str = f"${g['net_liq']:,.2f}" if g["net_liq"] else "_unavailable_"
    msg = (
        f"📐 *Greeks — {account_id}*\n\n"
        f"SPX δ-equiv  `{g['spx_delta']:+.2f}`\n"
        f"Delta        `{g['delta']:+.2f}`\n"
        f"Gamma        `{g['gamma']:+.4f}`\n"
        f"Theta        `{g['theta']:+.2f}` /day\n"
        f"Vega         `{g['vega']:+.2f}`\n"
        f"Net Liq      `{net_liq_str}`\n"
        f"\n_Updated {datetime.now(timezone.utc).strftime('%H:%M UTC')}_"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


@require_auth
async def cmd_regime(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Return VIX, active risk regime, and whether limits are breached."""
    await update.message.reply_text("⏳ Checking regime…")
    try:
        vix_data, regime, violations = await _fetch_regime()
    except Exception as exc:
        await update.message.reply_text(f"❌ Error: {exc}")
        return

    ok = len(violations) == 0
    status = "✅ All limits OK" if ok else f"❌ *{len(violations)} breach{'es' if len(violations)>1 else ''}*"

    breach_lines = ""
    if violations:
        breach_lines = "\n".join(
            f"  • {v.get('metric','?')}: `{v.get('current',0):+.1f}` (limit `{v.get('limit','?')}`)"
            for v in violations
        )
        breach_lines = "\n\n*Breaches:*\n" + breach_lines

    msg = (
        f"🌡️ *Market Regime*\n\n"
        f"VIX            `{vix_data.get('vix', 0):.2f}`\n"
        f"Term structure  `{vix_data.get('term_structure', 1):.3f}`\n"
        f"Regime         `{regime.name}`\n"
        f"Condition      _{regime.condition}_\n\n"
        f"{status}"
        f"{breach_lines}\n"
        f"\n_Updated {datetime.now(timezone.utc).strftime('%H:%M UTC')}_"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


@require_auth
async def cmd_analyze(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Trigger LLM risk assessment via Copilot SDK and return result."""
    await update.message.reply_text("🤖 Asking the AI (gpt-4o-mini)…")

    account_id = await _resolve_account_id()
    try:
        vix_data, regime, violations = await _fetch_regime()
        greeks_ctx = ""
        if account_id:
            g = await _fetch_greeks_summary(account_id)
            greeks_ctx = (
                f"SPX equivalent delta: {g['spx_delta']:+.2f}\n"
                f"Gamma: {g['gamma']:+.4f}\n"
                f"Theta: {g['theta']:+.2f}/day\n"
                f"Vega: {g['vega']:+.2f}\n"
                f"Net liq: {g['net_liq']}\n"
            )

        breach_ctx = (
            "No limit breaches." if not violations
            else "LIMIT BREACHES: " + "; ".join(
                f"{v.get('metric')}: {v.get('current',0):+.1f} (limit {v.get('limit')})"
                for v in violations
            )
        )

        prompt = (
            f"You are a quantitative risk manager reviewing a live options portfolio.\n\n"
            f"Current market data:\n"
            f"  VIX: {vix_data.get('vix', 0):.2f}\n"
            f"  Term structure: {vix_data.get('term_structure', 1):.3f}\n"
            f"  Regime: {regime.name} — {regime.condition}\n\n"
            f"Portfolio Greeks:\n{greeks_ctx}\n"
            f"Risk limits status: {breach_ctx}\n\n"
            f"Provide a concise (3-5 bullet) risk assessment and one actionable recommendation. "
            f"Be direct and quantitative."
        )

        from agents.llm_client import async_llm_chat
        reply = await async_llm_chat(
            prompt,
            model="gpt-4o-mini",
            system="You are a senior quantitative risk manager. Be concise and direct.",
            timeout=45.0,
        )
        await update.message.reply_text(
            f"🤖 *AI Risk Assessment*\n\n{reply}",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ LLM error: {exc}")


@require_auth
async def cmd_journal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Append a timestamped note to the trade_journal SQLite table.

    Usage: /journal <your text here>
    """
    text = " ".join(ctx.args) if ctx.args else ""
    if not text.strip():
        await update.message.reply_text(
            "Usage: /journal <your note text>\n\nExample:\n/journal Closed short ES put — VIX spike expected"
        )
        return

    try:
        from database.local_store import LocalStore
        store = LocalStore()
        await store._ensure_init()

        import aiosqlite
        db_path = store._db_path
        entry_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        account_id = os.getenv("IBKR_ACCOUNT_ID", "telegram_bot")

        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                INSERT INTO trade_journal
                    (id, created_at, broker, account_id, status, user_rationale)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (entry_id, now, "note", account_id, "NOTE", text.strip()),
            )
            await db.commit()

        await update.message.reply_text(
            f"📓 *Journal entry saved*\n\n"
            f"_{now[:19]} UTC_\n"
            f"{text.strip()}",
            parse_mode=ParseMode.MARKDOWN,
        )
        logger.info("Journal note saved: id=%s", entry_id)
    except Exception as exc:
        await update.message.reply_text(f"❌ Journal error: {exc}")
        logger.exception("Failed to save journal note")


# ═══════════════════════════════════════════════════════════════════════════════
# 4.  BACKGROUND ALERT LOOP
# ═══════════════════════════════════════════════════════════════════════════════

def _is_market_hours() -> bool:
    """Return True if current time is Mon-Fri 09:30–16:00 ET (approximate — UTC-5)."""
    now_utc = datetime.now(timezone.utc)
    if now_utc.weekday() >= 5:         # Saturday/Sunday
        return False
    # Approximate ET as UTC-5 (ignores DST — acceptable for alert purposes)
    now_et_hour   = (now_utc.hour * 60 + now_utc.minute - 300) // 60
    now_et_minute = (now_utc.hour * 60 + now_utc.minute - 300) % 60
    open_mins  = _MARKET_OPEN_ET[0]  * 60 + _MARKET_OPEN_ET[1]
    close_mins = _MARKET_CLOSE_ET[0] * 60 + _MARKET_CLOSE_ET[1]
    current_mins = now_et_hour * 60 + now_et_minute
    return open_mins <= current_mins < close_mins


async def _alert_loop(app: Application) -> None:
    """Background coroutine: every 5 min during market hours, push breach alerts."""
    logger.info("Alert loop started (interval=%ds)", _ALERT_INTERVAL_SECONDS)
    while True:
        await asyncio.sleep(_ALERT_INTERVAL_SECONDS)

        if not _is_market_hours():
            continue

        # Only alert if a real account is resolvable
        account_id = await _resolve_account_id()
        if not account_id:
            continue

        try:
            vix_data, regime, violations = await _fetch_regime()
            if not violations:
                continue

            lines = "\n".join(
                f"  • {v.get('metric','?')}: `{v.get('current',0):+.1f}` (limit `{v.get('limit','?')}`)"
                for v in violations
            )
            msg = (
                f"🚨 *RISK ALERT — {account_id}*\n\n"
                f"Regime: `{regime.name}` | VIX: `{vix_data.get('vix', 0):.2f}`\n\n"
                f"*Limit Breaches:*\n{lines}\n\n"
                f"_{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
            )
            for uid in ALLOWED_TELEGRAM_USERS:
                await app.bot.send_message(
                    chat_id=uid,
                    text=msg,
                    parse_mode=ParseMode.MARKDOWN,
                )
            logger.warning("Alert sent: %d breach(es)", len(violations))

        except Exception as exc:
            logger.warning("Alert loop error (non-fatal): %s", exc)


# ═══════════════════════════════════════════════════════════════════════════════
# 5.  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def _build_app() -> Application:
    """Build and return the configured Application object."""
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN env var is not set")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("greeks",  cmd_greeks))
    app.add_handler(CommandHandler("regime",  cmd_regime))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("journal", cmd_journal))

    return app


async def _run(app: Application) -> None:
    """Start polling + alert loop concurrently."""
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    logger.info(
        "Bot running. Allowed users: %s. Commands: /greeks /regime /analyze /journal /help",
        ALLOWED_TELEGRAM_USERS,
    )

    # Start background alert loop as an asyncio task
    alert_task = asyncio.create_task(_alert_loop(app))

    try:
        # Run forever until KeyboardInterrupt
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        alert_task.cancel()
        try:
            await alert_task
        except asyncio.CancelledError:
            pass
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="Portfolio Risk Telegram Bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate imports and config, then exit.",
    )
    args = parser.parse_args()

    if args.dry_run:
        logger.info("Dry-run: bot token present=%s, owner_id=%d", bool(BOT_TOKEN), _OWNER_ID)
        logger.info("ALLOWED_TELEGRAM_USERS=%s", ALLOWED_TELEGRAM_USERS)
        logger.info("Dry-run OK — exiting.")
        return

    app = _build_app()
    asyncio.run(_run(app))


if __name__ == "__main__":
    main()
