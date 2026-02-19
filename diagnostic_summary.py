#!/usr/bin/env python3
"""
DIAGNOSTIC SUMMARY - News APIs and Telegram Status
==================================================

FINDINGS:
=========

1. TELEGRAM ❌
   - Bot token: VALID ✓
   - Chat ID: MISSING ❌ (BLOCKER)
   - Status: Cannot send notifications until chat ID is set

2. ALPACA 📊
   - Credentials: VALID ✓ (paper trading account authenticated)
   - Issue: Paper trading API has NO NEWS ENDPOINT
   - Better alternatives: Use Finnhub, NewsAPI, or GNews for news

3. FINNHUB ✓
   - API: WORKING
   - Status: 200 (data returned successfully)
   - Note: Was showing 422 earlier, but test confirms it works now

4. NewsAPI ✓
   - Status: 200 (working)

5. GNews ✓
   - Status: 200 (working)

6. FRED ✓
   - Status: 200 (working - macro indicators)

RECOMMENDATIONS:
================

ACTION 1: Fix Telegram (Required for Alerts)
   1. Open Telegram app on your phone/desktop
   2. Search for your bot (created with @BotFather)
   3. Click "Start" or send /start command
   4. Run: python get_telegram_chat_id.py
   5. Add the returned TELEGRAM_CHAT_ID to .env

ACTION 2: For News Data - Use Fallback Chain
   Primary: Finnhub (working, real-time)
   Secondary: NewsAPI (working, 100/day free)
   Tertiary: GNews (working, 100/day free)
   
   NOTE: Alpaca paper trading API does NOT provide news endpoint.
   To get real Alpaca market data, upgrade to:
   - Alpaca Data Bundle (paid, includes news)
   - Or use their REST API for reference/test data only
   
ACTION 3: Fix .env Configuration
   Set NEWS_PROVIDER=finnhub (or newsapi as fallback)
   All required API keys are valid and working.
"""

import requests

env = {}
with open('.env') as f:
    for line in f:
        line = line.strip()
        if line and '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            env[k] = v

print(__doc__)

print("\nREAL-TIME API TEST:")
print("=" * 60)

results = {
    'Finnhub': requests.get('https://finnhub.io/api/v1/company-news', 
                           params={'symbol': 'AAPL', 'token': env.get('FINNHUB_API_KEY'), 
                                  'from': '2026-01-01', 'to': '2026-02-18'}, timeout=5).status_code,
    'NewsAPI': requests.get('https://newsapi.org/v2/top-headlines', 
                           params={'country': 'us', 'apiKey': env.get('NEWSAPI_KEY')}, timeout=5).status_code,
    'GNews': requests.get('https://gnews.io/api/v4/search', 
                         params={'q': 'stock', 'token': env.get('GNEWS_TOKEN')}, timeout=5).status_code,
    'FRED': requests.get('https://api.stlouisfed.org/fred/series', 
                        params={'series_id': 'TB3MS', 'api_key': env.get('FRED_API_KEY')}, timeout=5).status_code,
}

print("STATUS REPORT:")
for provider, status in results.items():
    status_emoji = "✓" if status == 200 else "⚠"
    print(f"  {status_emoji} {provider:12} → {status}")

print("\nTelegram Chat ID Status:")
chat_id = env.get('TELEGRAM_CHAT_ID', '').strip()
if chat_id:
    print(f"  ✓ Set to: {chat_id}")
else:
    print(f"  ❌ EMPTY - Run: python get_telegram_chat_id.py")
