#!/usr/bin/env python3
"""Test all news providers."""
import requests

# Parse .env manually
env = {}
with open('.env') as f:
    for line in f:
        line = line.strip()
        if line and '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            env[k] = v

print("=" * 60)
print("NEWS PROVIDERS STATUS")
print("=" * 60)
print(f"ALPACA:    {requests.get('https://paper-api.alpaca.markets/v2/news', params={'limit': 1}, timeout=3).status_code}")
print(f"FINNHUB:   {requests.get('https://finnhub.io/api/v1/company-news', params={'symbol': 'AAPL', 'token': env.get('FINNHUB_API_KEY')}, timeout=3).status_code}")
print(f"NewsAPI:   {requests.get('https://newsapi.org/v2/top-headlines', params={'country': 'us', 'apiKey': env.get('NEWSAPI_KEY')}, timeout=3).status_code}")
print(f"GNews:     {requests.get('https://gnews.io/api/v4/search', params={'q': 'stock', 'token': env.get('GNEWS_TOKEN')}, timeout=3).status_code}")
print(f"FRED:      {requests.get('https://api.stlouisfed.org/fred/series', params={'series_id': 'TB3MS', 'api_key': env.get('FRED_API_KEY')}, timeout=3).status_code}")
print("=" * 60)
print("\nNOTE: 401/403 = Auth issue, 200 = OK, 400 = Bad request")
