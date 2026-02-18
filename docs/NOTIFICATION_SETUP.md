# Notification Setup Guide — Telegram + Email

## Quick Setup (5 minutes)

### 1. Telegram Setup (PRIMARY — Instant Alerts)

**Step 1: Create a Telegram Bot**

1. Open Telegram and search for `@BotFather`
2. Send `/newbot`
3. Follow prompts:
   - Bot name: `PortfolioAlerts` (or any name)
   - Username: `portfolio_alerts_bot` (or `your_name_alerts_bot`)
4. **Copy the bot token** (looks like: `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`)

**Step 2: Get Your Chat ID**

1. Open your new bot and send `/start`
2. Copy **chat ID** from the bot's response (or use this Python helper):

```bash
# Get chat ID
python3 << 'PYTHON'
import os
from dotenv import load_dotenv
import requests

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# After sending /start to your bot, check Telegram updates
url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
response = requests.get(url).json()
if response['ok']:
    for message in response['result']:
        if 'message' in message:
            chat_id = message['message']['chat']['id']
            print(f"TELEGRAM_CHAT_ID={chat_id}")
PYTHON
```

**Step 3: Fill .env**

```bash
NOTIFICATION_METHOD=telegram
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
TELEGRAM_CHAT_ID=987654321
```

**Test Telegram:**

```python
import asyncio
from agent_tools.notification_dispatcher import send_alert

asyncio.run(send_alert(
    title="Test Alert",
    body="If you see this, Telegram is working! 🎉",
    urgency="green"
))
```

---

### 2. Gmail SMTP Setup (BACKUP — Detailed Reports)

**Step 1: Enable 2FA on Gmail**

1. Go to https://myaccount.google.com/security
2. Enable "2-Step Verification"

**Step 2: Generate App Password**

1. Go to https://myaccount.google.com/apppasswords
2. Select "Mail" → "Windows/Mac/Linux"
3. **Copy the 16-character password** (looks like: `abcd efgh ijkl mnop`)

**Step 3: Fill .env**

```bash
EMAIL_ENABLED=true
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your_email@gmail.com
SMTP_PASSWORD=abcd efgh ijkl mnop
EMAIL_FROM=your_email@gmail.com
EMAIL_TO=recipient@gmail.com  # Can be same or different
```

**Test Email (Optional):**

```python
from agent_tools.notification_dispatcher import NotificationDispatcher

dispatcher = NotificationDispatcher()
success = dispatcher._send_email(
    title="Test Email",
    body="If you see this, Gmail is working! 📧",
    urgency="info",
    data=None
)
print("Email sent:", success)
```

---

### 3. Alternative: SendGrid (if Gmail doesn't work)

**Step 1: Create SendGrid Account**

- Signup: https://sendgrid.com/ (free 100 emails/day)
- Generate API key in Settings → API Keys

**Step 2: Fill .env**

```bash
EMAIL_ENABLED=true
SMTP_SERVER=smtp.sendgrid.net
SMTP_PORT=587
SMTP_USERNAME=apikey
SMTP_PASSWORD=SG.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
EMAIL_FROM=no-reply@yourdomain.com
EMAIL_TO=your_email@gmail.com
```

---

## How It Works

### Notification Flow

```
Alert triggered in agent
    ↓
send_alert(title, body, urgency, data)
    ├─ Check NOTIFICATION_METHOD
    ├─ Send via PRIMARY (Telegram/Email/Slack)
    └─ If EMAIL_ENABLED, also send detailed backup email
```

### Example: Risk Audit Alert

```python
from agent_tools.notification_dispatcher import send_alert

await send_alert(
    title="🔴 Critical: Portfolio Delta Way Too High",
    body="Current delta: 250 (limit: 50)\nInstrument: SPY call spread",
    urgency="red",
    data={
        "suggestions": [
            "Buy 10 puts to delta hedge",
            "Close 5 call spreads (20 delta each)",
            "Review VIX-scaled limits in config/risk_matrix.yaml"
        ]
    }
)
```

**Telegram Output:**

```
🔴 *Critical: Portfolio Delta Way Too High*

Current delta: 250 (limit: 50)
Instrument: SPY call spread

*Suggestions:*
  1. Buy 10 puts to delta hedge
  2. Close 5 call spreads (20 delta each)
  3. Review VIX-scaled limits in config/risk_matrix.yaml
```

**Email Output:**

- Red header with title
- Body text
- Numbered suggestions list
- HTML formatted with colors

---

## Integration with Existing Agents

### Update agents to use notifications:

```python
# In agents/llm_risk_auditor.py
from agent_tools.notification_dispatcher import send_alert

async def audit_now(self, **kwargs):
    # ... audit logic ...
    result = {
        "headline": "Delta breach detected",
        "body": "Current: 150, Limit: 50",
        "urgency": "red",
        "suggestions": ["...", "..."]
    }

    # Send via configured method
    await send_alert(
        title=result["headline"],
        body=result["body"],
        urgency=result["urgency"],
        data={"suggestions": result["suggestions"]}
    )

    return result
```

---

## Feature Comparison

| Feature                 | Telegram             | Email              | Slack          |
| ----------------------- | -------------------- | ------------------ | -------------- |
| **Cost**                | FREE                 | FREE (Gmail)       | FREE (webhook) |
| **Speed**               | <1sec                | 5-10sec            | <1sec          |
| **Setup Time**          | 5 min                | 5 min              | 5 min          |
| **Rich Format**         | ✅ Markdown, buttons | ✅ HTML            | ✅ Blocks      |
| **Multiple Recipients** | ✅ Group chat        | ✅ Multiple emails | ✅ Channel     |
| **Suggestions Display** | ✅ Collapsible list  | ✅ Formatted       | ✅ Code block  |
| **Archive/Audit Trail** | ❌ Chat history      | ✅ Searchable      | ✅ Log history |
| **Recommended**         | ⭐⭐⭐⭐⭐           | ⭐⭐⭐⭐           | Legacy         |

---

## Troubleshooting

### Telegram Not Sending

```bash
# Check:
1. Token: TELEGRAM_BOT_TOKEN=... (not empty)
2. Chat ID: TELEGRAM_CHAT_ID=... (numeric, not empty)
3. Network: Can reach api.telegram.org
4. Bot started: Send /start to bot first

# Debug:
python3 -c "
import os, requests
from dotenv import load_dotenv
load_dotenv()
token = os.getenv('TELEGRAM_BOT_TOKEN')
chat_id = os.getenv('TELEGRAM_CHAT_ID')
url = f'https://api.telegram.org/bot{token}/getMe'
print('Token valid:', requests.get(url).json()['ok'])
"
```

### Email Not Sending

```bash
# Check:
1. 2FA enabled on Gmail account
2. App password used (not mail password)
3. SMTP credentials correct: SMTP_USERNAME=..., SMTP_PASSWORD=...
4. EMAIL_TO and EMAIL_FROM set
5. EMAIL_ENABLED=true

# Debug:
python3 << 'PYTHON'
import smtplib
import os
from dotenv import load_dotenv
load_dotenv()

try:
    server = smtplib.SMTP("smtp.gmail.com", 587)
    server.starttls()
    server.login(os.getenv("SMTP_USERNAME"), os.getenv("SMTP_PASSWORD"))
    print("Gmail login: OK")
    server.quit()
except Exception as e:
    print(f"Gmail login error: {e}")
PYTHON
```

### Both Methods Fail

- If `NOTIFICATION_METHOD=telegram` but no `TELEGRAM_BOT_TOKEN`: falls back silently
- Check logs: `grep "notification" logs/*.log`
- Set alerts to ALSO send Email: `EMAIL_ENABLED=true` ensures backup

---

## What's Next?

1. **Fill .env** with Telegram credentials
2. **Test Telegram**: Run test script above
3. **Optional: Add Email** for audit trail
4. **Update agents** to call `send_alert()` after actions
5. **Monitor**: Check Telegram/Email for live alerts during trading

---

## Questions?

- **Telegram Bot API Docs**: https://core.telegram.org/bots/api
- **Gmail SMTP Docs**: https://developers.google.com/gmail/imap
- **SendGrid Docs**: https://sendgrid.com/solutions/email-api/
