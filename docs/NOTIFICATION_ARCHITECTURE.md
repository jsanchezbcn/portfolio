# Notification Architecture

## System Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    PORTFOLIO AGENTS                         │
├─────────────────────────────────────────────────────────────┤
│ • llm_risk_auditor.py                                       │
│ • llm_market_brief.py                                       │
│ • news_sentry.py                                            │
│ • custom alerts (trades, limits breached, etc.)             │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
        ┌────────────────────────────┐
        │   send_alert(             │
        │     title="...",            │
        │     body="...",             │
        │     urgency="red|yellow|green|info"  │
        │     data={suggestions}      │
        │   )                         │
        └────────┬───────────────────┘
                 │
                 ▼
    ┌─────────────────────────────┐
    │  notification_dispatcher.py │
    │  NotificationDispatcher     │
    └──────────┬──────────────────┘
               │
         ┌─────┴──────────────┐
         │                    │
         ▼                    ▼
    ┌──────────────┐   ┌──────────────┐
    │  TELEGRAM    │   │    EMAIL     │
    │  Bot API     │   │  SMTP Server │
    │              │   │              │
    │  Instant     │   │  Backup      │
    │  Free        │   │  Free (Gmail)│
    │  Primary     │   │  Detailed    │
    └──────┬───────┘   └──────┬───────┘
           │                   │
           ▼                   ▼
       ┌────────────┐    ┌──────────────┐
       │ You@Telegram  │    │ you@gmail.com  │
       │ (instant msg)  │    │ (inbox report) │
       └────────────┘    └──────────────┘
```

---

## Alert Flow Diagram

```
Risk Auditor detects breach
    │
    ├─ Urgency: RED (portfolio delta > limit)
    │
    ▼
send_alert(
  title="🔴 Critical: Delta Breach",
  body="Current: 250, Limit: 50\nAction: Rebalance SPY spread",
  urgency="red",
  data={
    "suggestions": [
      "Buy 10 puts to hedge",
      "Close 5 call spreads",
      "Review config/risk_matrix.yaml"
    ]
  }
)
    │
    ▼
NotificationDispatcher.send_alert()
    │
    ├─ Route by NOTIFICATION_METHOD
    │
    ├─ Try PRIMARY: Telegram
    │  ├─ API call to api.telegram.org/bot{TOKEN}/sendMessage
    │  ├─ Format: Markdown with emoji and urgency color
    │  └─ Receive in <1 second
    │
    ├─ Also send BACKUP: Email (if EMAIL_ENABLED=true)
    │  ├─ SMTP connection to smtp.gmail.com:587
    │  ├─ Format: HTML with colored header
    │  ├─ Include suggestions as list
    │  └─ Sent to email_to address
    │
    └─ Return success/failure
```

---

## Message Examples

### 🟢 GREEN (Normal Info)

**Telegram:**

```
🟢 *Market Brief Generated*

VIX: 14.2 | SPX: 6850 | Regime: Risk-on
Opportunity: SPY calls selling into resistance
Risk: FOMC decision Wed

*Suggestions:*
  1. BUY 5 SPY 690C
  2. SET alert at 695
  3. Monitor VIX < 20
```

**Email:**

```
From: portfolio-bot@gmail.com
Subject: Market Brief Generated

┌─────────────────────────────┐
│ 🟢 Market Brief Generated   │
└─────────────────────────────┘

VIX: 14.2 | SPX: 6850 | Regime: Risk-on
Opportunity: SPY calls selling into resistance
Risk: FOMC decision Wed

Suggestions:
1. BUY 5 SPY 690C
2. SET alert at 695
3. Monitor VIX < 20
```

---

### 🟡 YELLOW (Warning)

**Telegram:**

```
🟡 *Warning: Gamma Risk Elevated*

Current gamma P&L: +$850 (high at 15% of NLV)
Recommendation: Consider taking some premium off

*Suggestions:*
  1. Sell 5 SPY call spreads
  2. Take 20% profit on profitable positions
  3. Monitor through earnings
```

**Email:**

```
Subject: [WARNING] Gamma Risk Elevated

┌──────────────────────────────┐
│ 🟡 Warning: Gamma Risk      │
│    Elevated                  │
└──────────────────────────────┘

Current gamma P&L: +$850 (high at 15% of NLV)
Recommendation: Consider taking some premium off

Suggestions:
1. Sell 5 SPY call spreads
2. Take 20% profit on profitable positions
3. Monitor through earnings
```

---

### 🔴 RED (Critical)

**Telegram:**

```
🔴 *CRITICAL: Portfolio Delta > Limit*

Current delta: 150 ⚠️
Limit: 50
Breach: +100 (+200%)
Action: IMMEDIATE REBALANCE REQUIRED

*Suggestions:*
  1. ⚡ BUY 20 SPY puts ASAP
  2. ⚡ CLOSE 10 ES call spreads
  3. ⚡ REDUCE position size by 30%
```

**Email:**

```
Subject: [CRITICAL] Portfolio Delta > Limit

┌──────────────────────────────┐
│ 🔴 CRITICAL: Portfolio Delta │
│    EXCEEDS LIMIT             │
└──────────────────────────────┘

⚠️ IMMEDIATE ACTION REQUIRED

Current delta:    150
Limit:            50
Breach amount:    +100 (+200%)

CRITICAL Suggestions:
1. ⚡ BUY 20 SPY puts ASAP
2. ⚡ CLOSE 10 ES call spreads
3. ⚡ REDUCE position size by 30%

Timestamp: 2026-02-18 14:32:05 UTC
```

---

## Configuration Scenarios

### Scenario 1: Telegram Only (Recommended)

```env
NOTIFICATION_METHOD=telegram
TELEGRAM_BOT_TOKEN=123456:ABCdef...
TELEGRAM_CHAT_ID=987654321
EMAIL_ENABLED=false
```

- ✅ Fast alerts during trading
- ✅ Zero cost
- ❌ No audit trail

---

### Scenario 2: Telegram + Email Backup

```env
NOTIFICATION_METHOD=telegram
TELEGRAM_BOT_TOKEN=123456:ABCdef...
TELEGRAM_CHAT_ID=987654321
EMAIL_ENABLED=true
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=<app_password>
EMAIL_TO=you@gmail.com
```

- ✅ Primary: Instant Telegram
- ✅ Backup: Email if Telegram fails
- ✅ Audit trail in email
- Zero cost

---

### Scenario 3: Email Only (if Telegram blocked)

```env
NOTIFICATION_METHOD=email
EMAIL_ENABLED=true
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=<app_password>
EMAIL_TO=you@gmail.com
```

- ✅ Works anywhere
- ✅ Detailed reports
- ⚠️ Slower (5-10s)
- ✅ Zero cost

---

### Scenario 4: Legacy Slack (backward compat)

```env
NOTIFICATION_METHOD=slack
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T...
```

- ⚠️ Still supported but deprecated
- ✅ Works if already using Slack
- ⚠️ Slack changed pricing model in Oct 2023

---

## Urgency & Color Mapping

| Urgency  | Color     | Telegram    | Email         | Use Case                               |
| -------- | --------- | ----------- | ------------- | -------------------------------------- |
| `info`   | ℹ️ Blue   | ℹ️ Neutral  | Blue header   | General info, market briefs            |
| `green`  | 🟢 Green  | 🟢 Good     | Green header  | Success, position closed, profit taken |
| `yellow` | 🟡 Yellow | 🟡 Warning  | Orange header | Risk elevated, review needed           |
| `red`    | 🔴 Red    | 🔴 Critical | Red header    | **IMMEDIATE ACTION** required          |

---

## Error Handling

```python
# If Telegram unavailable but Email enabled:
send_alert(...) ──────────────┐
                              │
        Try Telegram ─────────┤──────► API error?
                              │
                              ├─► Fall back to Email
                              │
                   ┌──────────┘
                   │
        Try Email──┘──────► SMTP error?

                   ├─► Log error
                   └─► Return False

# Result: If any method succeeds, alert sent
# If all fail: Logged, but bot continues (no crash)
```

---

## Performance Targets

| Metric             | Target                            | Typical                       |
| ------------------ | --------------------------------- | ----------------------------- |
| Telegram send time | <1s                               | 0.2-0.8s                      |
| Email send time    | <10s                              | 3-7s                          |
| API failure rate   | <0.1%                             | Gmail: 0.01%, Telegram: 0.02% |
| Retry behavior     | Auto-retry Email if primary fails | Automatic                     |

---

## Security Notes

⚠️ **Keep credentials safe:**

- `.env` is in `.gitignore` ✅
- Never commit tokens to git
- Delete old/compromised tokens from @BotFather
- Gmail app passwords: Can be revoked anytime from myaccount.google.com

✅ **Best practices:**

- Use Gmail app passwords (not main password)
- Rotate Gmail app password every 90 days
- Use unique Telegram bot for this app
- Monitor alerts for suspicious activity
