#!/usr/bin/env python3
"""Test all configured news APIs."""
from dotenv import load_dotenv
import os
import requests

load_dotenv()

print("=" * 60)
print("NEWS API CONNECTIVITY TEST")
print("=" * 60)

tests = [
    ('ALPACA', 'https://paper-api.alpaca.markets/v2/news', {'limit': 1}),
    ('FINNHUB', 'https://finnhub.io/api/v1/company-news', {'symbol': 'AAPL', 'token': os.getenv('FINNHUB_API_KEY')}),
    ('NewsAPI', 'https://newsapi.org/v2/top-headlines', {'country': 'us', 'apiKey': os.getenv('NEWSAPI_KEY')}),
    ('GNews', 'https://gnews.io/api/v4/search', {'q': 'stock', 'token': os.getenv('GNEWS_TOKEN')}),
    ('FRED', 'https://api.stlouisfed.org/fred/series', {'series_id': 'TB3MS', 'api_key': os.getenv('FRED_API_KEY')}),
]

for name, url, params in tests:
    try:
        r = requests.get(url, params=params, timeout=5)
        status = "✓ OK" if r.status_code in [200, 401, 403] else f"⚠️  {r.status_code}"
        print(f"{name:15} {status}")
        if r.status_code in [401, 403]:
            print(f"                   → Invalid API key or insufficient permissions")
        elif r.status_code == 429:
            print(f"                   → Rate limited")
    except requests.Timeout:
        print(f"{name:15} ❌ Timeout")
    except requests.ConnectionError:
        print(f"{name:15} ❌ Connection error")
    except Exception as e:
        print(f"{name:15} ❌ {type(e).__name__}")

print("=" * 60)
