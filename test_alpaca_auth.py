#!/usr/bin/env python3
"""Test Alpaca API with proper authentication headers."""
import requests

# Parse .env
env = {}
with open('.env') as f:
    for line in f:
        line = line.strip()
        if line and '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            env[k] = v

api_key = env.get('NEWS_API_KEY', '').strip()
api_secret = env.get('NEWS_API_SECRET', '').strip()
endpoint = env.get('NEWS_ENDPOINT', 'https://paper-api.alpaca.markets/v2').strip()

print(f"Testing Alpaca with:")
print(f"  Key: {api_key[:10]}... (hidden)")
print(f"  Secret: {api_secret[:10]}... (hidden)")
print(f"  Endpoint: {endpoint}")
print()

# Test 1: Without headers (your current test - will fail with 401)
print("❌ Test 1: WITHOUT headers (current method)")
resp = requests.get(f'{endpoint}/news', params={'limit': 1}, timeout=3)
print(f"   Status: {resp.status_code}")

# Test 2: With proper Alpaca headers
print("\n✓ Test 2: WITH Alpaca headers (correct method)")
headers = {
    'APCA-API-KEY-ID': api_key,
    'APCA-API-SECRET-KEY': api_secret,
}
resp = requests.get(f'{endpoint}/news', headers=headers, params={'limit': 1}, timeout=3)
print(f"   Status: {resp.status_code}")
if resp.status_code == 200:
    print(f"   ✓ SUCCESS - Got news data")
    data = resp.json()
    if 'news' in data:
        print(f"   Articles returned: {len(data['news'])}")
else:
    print(f"   Error: {resp.text[:200]}")
