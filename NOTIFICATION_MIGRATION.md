# Notification System Migration: Slack → Telegram/Email

## ✅ What's Been Done

### 1. Updated `.env` Configuration

- **Removed** deprecated `SLACK_WEBHOOK_URL`
- **Added** Telegram configuration:
  - `NOTIFICATION_METHOD=telegram` (primary)
  - `TELEGRAM_BOT_TOKEN` (get from @BotFather)
  - `TELEGRAM_CHAT_ID` (your Telegram user ID)
- **Added** Email (Gmail SMTP) configuration:
  - `EMAIL_ENABLED` (flag to enable/disable)
  - `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`
  - `EMAIL_TO`, `EMAIL_FROM`

### 2. Created `agent_tools/notification_dispatcher.py`

A unified notification system that supports:

- **Telegram Bot API** (instant, free) — PRIMARY
- **SMTP Email** (Gmail or SendGrid) — BACKUP
- **Slack webhooks** (legacy support for backward compatibility)

**Key Features:**

- Single `send_alert()` function for all methods
- Automatic fallback: Try primary → try backup email
- Rich formatting (Markdown for Telegram, HTML for email)
- Urgency levels (green/yellow/red/info) with emoji
- Suggestions/trades displayed in collapsible sections

### 3. Created `test_notifications.py`

Quick validation script:

```bash
python test_notifications.py
```

- Checks Telegram token validity
- Checks Gmail SMTP credentials
- Sends test alert to verify setup

### 4. Created `docs/NOTIFICATION_SETUP.md`

Complete step-by-step guide:

- Telegram Bot creation (5 mins)
- Getting Telegram Chat ID
- Gmail app password setup
- SendGrid alternative
- Troubleshooting guide

---

## 📋 Quick Setup Checklist

### Telegram (Recommended — Free, Instant)

```bash
# 1. Message @BotFather on Telegram
# 2. /newbot → Create bot → Get token

# 3. Start your bot & send /start

# 4. Get chat ID (use helper script below)
python3 << 'PY'
import os, requests
from dotenv import load_dotenv
load_dotenv()
token = os.getenv("TELEGRAM_BOT_TOKEN")
url = f"https://api.telegram.org/bot{token}/getUpdates"
updates = requests.get(url).json()
if updates['ok'] and updates['result']:
    chat_id = updates['result'][-1]['message']['chat']['id']
    print(f"TELEGRAM_CHAT_ID={chat_id}")
PY

# 5. Fill .env
# TELEGRAM_BOT_TOKEN=...
# TELEGRAM_CHAT_ID=...
```

### Email (Optional Backup — Free)

```bash
# 1. Enable 2FA on Gmail: https://myaccount.google.com/security
# 2. Generate app password: https://myaccount.google.com/apppasswords
# 3. Fill .env:
# EMAIL_ENABLED=true
# SMTP_USERNAME=you@gmail.com
# SMTP_PASSWORD=<16-char-app-password>
# EMAIL_TO=recipient@example.com
# EMAIL_FROM=you@gmail.com
```

---

## 🔌 Integration with Agents

### Example: Update `agents/llm_risk_auditor.py`

```python
# At top of file
from agent_tools.notification_dispatcher import send_alert

# In audit_now() method, after generating audit result:
async def audit_now(self, **kwargs) -> dict:
    # ... existing audit logic ...
    result = {
        "headline": "Portfolio Delta Breach",
        "body": f"Current delta: {current_delta}, Limit: {limit}",
        "urgency": "red",  # red|yellow|green
        "suggestions": [
            "Buy 10 puts to hedge",
            "Close 5 call spreads",
            "Review risk matrix config"
        ]
    }

    # SEND ALERT (NEW)
    await send_alert(
        title=f"🔴 {result['headline']}",
        body=result["body"],
        urgency=result["urgency"],
        data={"suggestions": result["suggestions"]}
    )

    # Persist to DB
    self._persist(result)

    return result
```

### Example: Update `agents/llm_market_brief.py`

```python
from agent_tools.notification_dispatcher import send_alert

async def brief_now(self) -> dict:
    # ... existing brief logic ...
    result = {
        "headline": "Market Brief - High Conviction Setup",
        "regime_read": "Risk-on, VIX < 15",
        "opportunity": "SPY calls, fade resistance at 600",
        "risk": "Fed speakers this week",
        "action": "BUY 5 SPY 600C",
        "confidence": "high"
    }

    # SEND ALERT (NEW)
    await send_alert(
        title=f"📰 {result['headline']}",
        body=f"Regime: {result['regime_read']}\n\n{result['opportunity']}\n\nRisk: {result['risk']}",
        urgency="info",
        data={"suggestions": [result["action"]]}
    )

    self._persist(result)

    return result
```

### Example: Update `agents/news_sentry.py`

```python
from agent_tools.notification_dispatcher import send_alert

async def check_news(self, symbol: str):
    # ... existing news fetch logic ...

    # If high sentiment or critical news
    if sentiment_score < -0.7:  # Very negative
        await send_alert(
            title=f"🚨 Critical News: {symbol}",
            body=f"{headline}\n\n{summary}",
            urgency="red",
            data={"suggestions": [
                f"Check {symbol} position size",
                "Consider stop-loss review",
                f"Set alert for {symbol}"
            ]}
        )
```

---

## 🧪 Test Before Integration

```bash
# 1. Test connectivity
python test_notifications.py

# 2. Test Telegram manually
python3 << 'PY'
import asyncio
from agent_tools.notification_dispatcher import send_alert

asyncio.run(send_alert(
    title="🧪 Test",
    body="If you see this, Telegram works!",
    urgency="green"
))
PY

# 3. Test Email manually
python3 << 'PY'
from agent_tools.notification_dispatcher import NotificationDispatcher
dispatcher = NotificationDispatcher()
dispatcher._send_email(
    "🧪 Test",
    "If you see this, Email works!",
    "info",
    None
)
PY
```

---

## 📊 Comparison: Slack → Telegram/Email

| Feature                     | Slack               | Telegram             | Email               |
| --------------------------- | ------------------- | -------------------- | ------------------- |
| **Cost**                    | FREE                | FREE ⭐              | FREE (Gmail)        |
| **Setup**                   | 5 min               | 5 min ⭐             | 5 min               |
| **Speed**                   | <1s                 | <1s ⭐               | 5-10s               |
| **No Business Terms**       | ❌ Changed Oct 2023 | ✅ Unchanged ⭐      | ✅ Personal use     |
| **Rich Format**             | Blocks              | Markdown ⭐          | HTML ⭐             |
| **Mobile**                  | ✅ App              | ✅ App ⭐            | ✅ Native           |
| **Audit Trail**             | ❌ Limited          | ⚠️ Chat history      | ✅ Email archive ⭐ |
| **Backup if Primary Fails** | ❌ No               | ✅ Email fallback ⭐ | ✅ Guaranteed       |

---

## 🎯 Recommended Strategy

### Production Setup:

```bash
# Primary: Telegram (instant, free, no terms changes)
NOTIFICATION_METHOD=telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# Backup: Email for critical alerts
EMAIL_ENABLED=true
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=... (app password)
EMAIL_TO=you@gmail.com
```

### Why This?

- **Telegram** = Instant alerts during market hours
- **Email** = Audit trail for compliance/review
- **No Slack** = Avoid business model/ToS changes
- **Multiple channels** = Redundancy (if Telegram API down, email still works)

---

## 🚀 Next Steps

1. **Setup Telegram**: Follow `docs/NOTIFICATION_SETUP.md`
2. **Test connectivity**: `python test_notifications.py`
3. **Update agents**: Add `send_alert()` calls in risk_auditor/brief/sentry
4. **Verify alerts**: Run agents and check Telegram/Email
5. **Set EMAIL_ENABLED=true** for critical trades (optional backup)

---

## Questions?

- **Setup help**: See `docs/NOTIFICATION_SETUP.md`
- **Integration help**: Check examples above
- **Issues**: Run `test_notifications.py` for diagnostics
