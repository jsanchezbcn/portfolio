#!/usr/bin/env python3
"""
Quick test script for Telegram and Email notifications.
Run: python test_notifications.py
"""

import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment
load_dotenv()

def test_telegram():
    """Test Telegram connectivity."""
    print("\n" + "="*60)
    print("TESTING TELEGRAM")
    print("="*60)
    
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    
    if not token:
        print("❌ TELEGRAM_BOT_TOKEN not set in .env")
        print("   Follow: https://core.telegram.org/bots#botfather")
        return False
    
    if not chat_id:
        print("❌ TELEGRAM_CHAT_ID not set in .env")
        print("   Run this bot, send /start, then extract chat ID")
        return False
    
    print(f"✓ Token configured (length: {len(token)})")
    print(f"✓ Chat ID configured: {chat_id}")
    
    # Try simple HTTP request
    try:
        import httpx
        url = f"https://api.telegram.org/bot{token}/getMe"
        response = httpx.get(url, timeout=5)
        data = response.json()
        
        if data.get('ok'):
            bot_name = data['result']['first_name']
            print(f"✓ API connectivity: OK (Bot: {bot_name})")
            return True
        else:
            print(f"❌ API error: {data.get('description', 'Unknown error')}")
            return False
            
    except Exception as e:
        print(f"❌ Connection error: {e}")
        return False


def test_email():
    """Test Gmail SMTP connectivity."""
    print("\n" + "="*60)
    print("TESTING EMAIL (SMTP)")
    print("="*60)
    
    enabled = os.getenv("EMAIL_ENABLED", "false").lower() in ("true", "1", "yes")
    
    if not enabled:
        print("⚠️  EMAIL_ENABLED=false (skipped)")
        return None
    
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    server = os.getenv("SMTP_SERVER", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    email_to = os.getenv("EMAIL_TO", "").strip()
    
    if not all([username, password, email_to]):
        print("❌ Missing SMTP credentials:")
        print(f"   - SMTP_USERNAME: {'✓' if username else '✗'}")
        print(f"   - SMTP_PASSWORD: {'✓' if password else '✗'}")
        print(f"   - EMAIL_TO: {'✓' if email_to else '✗'}")
        return False
    
    print(f"✓ Username: {username}")
    print(f"✓ Server: {server}:{port}")
    print(f"✓ Recipient: {email_to}")
    
    try:
        import smtplib
        server_obj = smtplib.SMTP(server, port, timeout=5)
        server_obj.starttls()
        server_obj.login(username, password)
        server_obj.quit()
        print("✓ SMTP login: OK")
        return True
        
    except smtplib.SMTPAuthenticationError:
        print("❌ SMTP auth failed (check password/app password)")
        print("   For Gmail: Use app-specific password from myaccount.google.com/apppasswords")
        return False
    except smtplib.SMTPException as e:
        print(f"❌ SMTP error: {e}")
        return False
    except Exception as e:
        print(f"❌ Connection error: {e}")
        return False


async def test_send():
    """Test actual send."""
    print("\n" + "="*60)
    print("SENDING TEST ALERT")
    print("="*60)
    
    try:
        from agent_tools.notification_dispatcher import send_alert
        
        success = await send_alert(
            title="🧪 Test Alert from Portfolio Bot",
            body="If you see this message, notifications are working correctly! 🎉",
            urgency="green",
            data={
                "suggestions": [
                    "Sample suggestion 1",
                    "Sample suggestion 2",
                    "Sample suggestion 3"
                ]
            }
        )
        
        if success:
            print("✓ Alert sent successfully!")
            print("  Check your Telegram (if enabled)")
            print("  or Email inbox (if enabled)")
        else:
            print("❌ Alert send failed (check logs)")
        
        return success
        
    except Exception as e:
        print(f"❌ Error during send: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """Run all tests."""
    print("\n🔍 NOTIFICATION SYSTEM TEST")
    print("="*60)
    
    tg_ok = test_telegram()
    email_ok = test_email()
    
    # Only test send if at least one method is configured
    if tg_ok or email_ok:
        send_ok = await test_send()
    else:
        print("\n⚠️  No notification methods configured")
        print("   Configure TELEGRAM or EMAIL in .env")
        send_ok = False
    
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    print(f"Telegram:  {'✓ Ready' if tg_ok else '❌ Not configured' if not os.getenv('TELEGRAM_BOT_TOKEN') else '❌ Failed'}")
    print(f"Email:     {'✓ Ready' if email_ok else '⚠️  Disabled' if email_ok is None else '❌ Failed'}")
    print(f"Send Test: {'✓ Success' if send_ok else '❌ Failed'}")
    print("="*60 + "\n")
    
    return tg_ok or email_ok


if __name__ == "__main__":
    success = asyncio.run(main())
    exit(0 if success else 1)
