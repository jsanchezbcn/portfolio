#!/usr/bin/env python3
"""Test Finnhub and Alpaca endpoints."""
import requests

env = {}
with open('.env') as f:
    for line in f:
        line = line.strip()
        if line and '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            env[k] = v

print("=" * 70)
print("FINNHUB TESTING")
print("=" * 70)

# Test Finnhub with verbose error info
finnhub_url = 'https://finnhub.io/api/v1/company-news'
finnhub_token = env.get('FINNHUB_API_KEY', '').strip()
params = {'symbol': 'AAPL', 'token': finnhub_token, 'from': '2026-01-01', 'to': '2026-02-18'}

print(f"URL: {finnhub_url}")
print(f"Token: {finnhub_token[:20]}... (hidden)")
print(f"Params: symbol=AAPL, from=2026-01-01, to=2026-02-18")

resp = requests.get(finnhub_url, params=params, timeout=5)
print(f"\nStatus: {resp.status_code}")
print(f"Response: {resp.text[:300]}")

print("\n" + "=" * 70)
print("ALPACA PAPER TRADING API - AVAILABLE ENDPOINTS")
print("=" * 70)

api_key = env.get('NEWS_API_KEY', '').strip()
api_secret = env.get('NEWS_API_SECRET', '').strip()
headers = {
    'APCA-API-KEY-ID': api_key,
    'APCA-API-SECRET-KEY': api_secret,
}

# Try account endpoint to verify authentication works
print("\n1. Testing /account endpoint (verify auth):")
resp = requests.get('https://paper-api.alpaca.markets/v2/account', headers=headers, timeout=5)
print(f"   Status: {resp.status_code}")
if resp.status_code == 200:
    print(f"   ✓ Authentication successful!")
    data = resp.json()
    print(f"   Account: {data.get('account_number', 'N/A')}")
else:
    print(f"   Error: {resp.text[:200]}")

# Try watching lists or positions endpoint
print("\n2. Testing /positions endpoint:")
resp = requests.get('https://paper-api.alpaca.markets/v2/positions', headers=headers, timeout=5)
print(f"   Status: {resp.status_code}")

# Check what news endpoints might exist
print("\n3. Testing potential news endpoints:")
for endpoint in ['news', 'market_data/news', 'watchlist', 'portfolio']:
    url = f'https://paper-api.alpaca.markets/v2/{endpoint}'
    resp = requests.get(url, headers=headers, timeout=5)
    print(f"   /{endpoint}: {resp.status_code}")
