#!/usr/bin/env python3
"""Debug specific news APIs."""
import requests
from dotenv import load_dotenv
import os

load_dotenv()

# Test FRED with more detail
print("Testing FRED...")
fred_key = os.getenv('FRED_API_KEY')
print(f"  Key: {fred_key[:20]}..." if fred_key else "  No key configured")
r = requests.get('https://api.stlouisfed.org/fred/series', 
                 params={'series_id': 'TB3MS', 'api_key': fred_key})
print(f"  Status: {r.status_code}")
if r.status_code != 200:
    print(f"  Response: {r.text[:200]}")
else:
    print(f"  ✓ Working!")

# Test GNews with more detail
print("\nTesting GNews...")
gnews_token = os.getenv('GNEWS_TOKEN')
print(f"  Token: {gnews_token[:20]}..." if gnews_token else "  No token configured")
r = requests.get('https://gnews.io/api/v4/search',
                 params={'q': 'stock', 'token': gnews_token})
print(f"  Status: {r.status_code}")
if r.status_code != 200:
    print(f"  Response: {r.text[:200]}")
else:
    print(f"  ✓ Working!")
