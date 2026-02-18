#!/usr/bin/env python3
"""Get your Telegram chat ID from bot updates."""

import os
import requests
from dotenv import load_dotenv

load_dotenv()
token = os.getenv("TELEGRAM_BOT_TOKEN")

if not token:
    print("❌ No TELEGRAM_BOT_TOKEN found")
else:
    print(f"✓ Bot token found: {token[:20]}...")
    
    # Get recent updates to find your chat ID
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        response = requests.get(url, timeout=5)
        data = response.json()
        
        if data.get('ok'):
            updates = data.get('result', [])
            if updates:
                # Get the latest message
                latest = updates[-1]
                if 'message' in latest:
                    chat_id = latest['message']['chat']['id']
                    print(f"\n✓ Your Chat ID: {chat_id}")
                    print("\nAdd to .env:")
                    print(f"TELEGRAM_CHAT_ID={chat_id}")
                else:
                    print("\n⚠️  No messages yet. Please:")
                    print("  1. Open Telegram")
                    print("  2. Find your bot (search by name or @username)")
                    print("  3. Send /start to it")
                    print("  4. Then run this script again")
            else:
                print("\n⚠️  No updates found. Please:")
                print("  1. Open Telegram")
                print("  2. Search for your bot")
                print("  3. Send /start")
                print("  4. Run this script again")
        else:
            print(f"❌ API error: {data.get('description')}")
    except Exception as e:
        print(f"❌ Error: {e}")
