#!/usr/bin/env python3
"""Test Telegram notification delivery."""
import os
import requests
from pathlib import Path
from dotenv import load_dotenv

# Load .env
env_file = Path(__file__).parent / ".env"
load_dotenv(env_file)

bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
chat_id = os.getenv("TELEGRAM_CHAT_ID")

if not bot_token or not chat_id:
    print("❌ Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env")
    exit(1)

print(f"✓ Bot token: {bot_token[:20]}...")
print(f"✓ Chat ID: {chat_id}")

# Send test message
url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
payload = {
    "chat_id": chat_id,
    "text": "✅ Portfolio Agent Test Notification - System is operational!"
}

try:
    response = requests.post(url, json=payload, timeout=5)
    if response.status_code == 200:
        print(f"\n✅ SUCCESS: Telegram notification delivered!")
        print(f"   Response: {response.json()}")
    else:
        print(f"\n❌ FAILED: Status {response.status_code}")
        print(f"   Response: {response.text}")
except Exception as e:
    print(f"\n❌ ERROR: {type(e).__name__}: {e}")
