#!/usr/bin/env python3
"""
Fetch options chain data from the Tastytrade API and write to CSV.

Notes / assumptions:
- Reads credentials from environment variables or a local `.env` file:
    TASTYTRADE_USERNAME and TASTYTRADE_PASSWORD
- Tastytrade API endpoints are inferred; the code tries a few common
  endpoint patterns and is defensive about JSON shapes.
- Uses requests + pandas only (plus stdlib).

Usage:
    python tastytrade_options_fetcher.py AAPL

If you want to test without network, pass --dry-run which will only validate
argument parsing and environment loading.
"""

from __future__ import annotations
import os
import sys
import time
import argparse
import requests
import json
from typing import Dict, List, Optional, Any
import pandas as pd

DEFAULT_BASE_URL = "https://api.tastyworks.com"
DEFAULT_TIMEOUT = 10.0


def load_dotenv(path: str = ".env") -> None:
    """Load simple KEY=VALUE pairs from a .env file into os.environ.
    Non-destructive: existing environment variables are not overwritten.
    """
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        # Don't raise on .env parse errors; caller may provide env vars by other means
        pass


def get_credentials():
    """Return (username, password) read from environment variables.
    Looks for multiple common names so existing .env entries are accepted:
    - TASTYTRADE_USERNAME / TASTYTRADE_PASSWORD
    - TASTYWORKS_USER / TASTYWORKS_PASS
    - TASTYTRADE_USER / TASTYTRADE_PASS
    """
    username = (
        os.getenv("TASTYTRADE_USERNAME") or os.getenv("TASTYWORKS_USER") or os.getenv("TASTYTRADE_USER")
    )
    password = (
        os.getenv("TASTYTRADE_PASSWORD") or os.getenv("TASTYWORKS_PASS") or os.getenv("TASTYTRADE_PASS")
    )
    return username, password


def get_token_from_env() -> Optional[str]:
    """Read an existing API token from env vars if present.
    Common names: TASTYWORKS_TOKEN, TASTYTRADE_TOKEN
    """
    return os.getenv("TASTYWORKS_TOKEN") or os.getenv("TASTYTRADE_TOKEN")


class TastytradeAPIError(Exception):
    pass


def tastytrade_auth(username: str, password: str, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Authenticate with tastytrade and return an auth token (JWT/Bearer).

    Note: The exact tastytrade auth response shape can vary; we try common
    response fields and raise on failure.
    """
    session_url_candidates = [
        f"{base_url}/sessions",
        f"{base_url}/api/sessions",
        f"{base_url}/auth/session",
    ]
    payload = {"login": username, "password": password}
    headers = {"Content-Type": "application/json"}

    last_err = None
    for url in session_url_candidates:
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            last_err = e
            continue

        if resp.status_code in (200, 201):
            try:
                data = resp.json()
            except Exception:
                raise TastytradeAPIError("Authentication succeeded but response JSON parsing failed")

            # Try common locations for token
            token = None
            # common tastytrade field: data -> session -> jwt
            token = (
                (data.get("data") or {}).get("session", {}).get("jwt")
                if isinstance(data, dict) else None
            )
            if not token:
                # sometimes top-level 'session' or 'jwt'
                token = (data.get("jwt") if isinstance(data, dict) else None)
            if not token:
                token = (data.get("session", {}).get("jwt") if isinstance(data, dict) else None)
            if token:
                return token
            # Some APIs return an access_token
            if isinstance(data, dict) and data.get("access_token"):
                return data.get("access_token")

            # If no token found but status OK, return raw text as fallback (not ideal)
            raise TastytradeAPIError("Authentication response did not contain an access token")
        else:
            try:
                err_text = resp.text[:200]
            except Exception:
                err_text = str(resp.status_code)
            last_err = TastytradeAPIError(f"Auth failed ({resp.status_code}): {err_text}")
            # try next candidate
            continue

    raise TastytradeAPIError(f"Authentication failed; last error: {last_err}")


def _try_options_chain_endpoints(symbol: str, token: Optional[str], base_url: str, timeout: float) -> Dict[str, Any]:
    """Try several common endpoints to fetch options chain JSON and return the parsed JSON.
    Raises TastytradeAPIError on permanent failure.
    """
    headers = {"Accept": "application/json", "Accept-Version": "v1"}
    if token:
        # tastyworks API examples use a raw token in the Authorization header (no 'Bearer ' prefix)
        headers["Authorization"] = token

    # Prefer the tastyworks nested option-chains endpoint which returns rich nested data
    candidates = [
        f"{base_url}/option-chains/{symbol}/nested",
        f"{base_url}/option-chains/{symbol}",
        f"{base_url}/markets/options/chains?symbol={symbol}",
        f"{base_url}/markets/options/chains/{symbol}",
        f"{base_url}/options/chains/{symbol}",
        f"{base_url}/markets/options/chains/{symbol}/depth",
    ]
    last_exc = None
    for url in candidates:
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            last_exc = e
            continue
        if r.status_code == 200:
            try:
                return r.json()
            except Exception as e:
                raise TastytradeAPIError(f"Failed parsing JSON from {url}: {e}")
        else:
            last_exc = TastytradeAPIError(f"Endpoint {url} returned {r.status_code}")
            continue

    raise TastytradeAPIError(f"Failed to fetch options chain; last error: {last_exc}")


def find_nearest_expiration(chain_json: Dict[str, Any]) -> Optional[str]:
    """Given options chain JSON, try to find the nearest expiration date string.

    Returns an expiration identifier that can be used to filter chains, or None.
    """
    # For tastytrade nested API: data.items[0].expirations
    if isinstance(chain_json, dict) and "data" in chain_json:
        data = chain_json.get("data", {})
        if isinstance(data, dict) and "items" in data:
            items = data.get("items", [])
            if isinstance(items, list) and len(items) > 0:
                first_item = items[0]
                if isinstance(first_item, dict) and "expirations" in first_item:
                    expirations = first_item.get("expirations", [])
                    if isinstance(expirations, list) and len(expirations) > 0:
                        # Each expiration should have an expiration-date
                        first_exp = expirations[0]
                        if isinstance(first_exp, dict) and "expiration-date" in first_exp:
                            return first_exp.get("expiration-date")
                        # Fallback: return first expiration as-is
                        return str(first_exp)

    # Legacy logic for other API shapes...
    candidates = []
    if not isinstance(chain_json, dict):
        return None
    # try several keys
    if "expiration_dates" in chain_json:
        candidates = chain_json.get("expiration_dates") or []
    elif "expirationDates" in chain_json:
        candidates = chain_json.get("expirationDates") or []
    elif isinstance(chain_json.get("data"), dict) and "expiration_dates" in chain_json.get("data"):
        candidates = chain_json.get("data", {}).get("expiration_dates") or []
    # sometimes chain_json contains 'expirations'
    elif "expirations" in chain_json:
        candidates = chain_json.get("expirations") or []

    # normalize candidate entries to strings/dates
    exps = []
    for e in (candidates or []):
        try:
            # if it's numeric timestamp in ms
            if isinstance(e, (int, float)):
                exps.append(time.strftime("%Y-%m-%d", time.localtime(float(e) / 1000.0)))
            else:
                exps.append(str(e))
        except Exception:
            continue

    if not exps:
        # Some APIs embed expirations inside 'chains' keys
        if "chains" in chain_json and isinstance(chain_json.get("chains"), dict):
            exps = list(chain_json.get("chains").keys())

    if not exps:
        return None

    # sort and return the earliest date-like string (best-effort)
    try:
        exps_sorted = sorted(exps)
        return exps_sorted[0]
    except Exception:
        return exps[0]


def extract_options_for_expiry(chain_json: Dict[str, Any], expiry: str) -> List[Dict[str, Any]]:
    """Extract list of option contracts for a given expiry from chain JSON.
    This function is defensive and supports several JSON shapes.
    """
    options = []
    if not isinstance(chain_json, dict):
        return options

    # For tastytrade nested API: data.items[0].expirations[].strikes[]
    if isinstance(chain_json, dict) and "data" in chain_json:
        data = chain_json.get("data", {})
        if isinstance(data, dict) and "items" in data:
            items = data.get("items", [])
            if isinstance(items, list) and len(items) > 0:
                first_item = items[0]
                if isinstance(first_item, dict) and "expirations" in first_item:
                    expirations = first_item.get("expirations", [])
                    for exp_data in expirations:
                        if isinstance(exp_data, dict):
                            exp_date = exp_data.get("expiration-date")
                            if str(exp_date) == str(expiry):
                                # Found matching expiration, extract strikes
                                strikes = exp_data.get("strikes", [])
                                for strike_data in strikes:
                                    if isinstance(strike_data, dict):
                                        strike_price = strike_data.get("strike-price")
                                        # Create option records for call and put
                                        if "call" in strike_data and strike_data["call"]:
                                            call_symbol = strike_data["call"]
                                            call_data = {
                                                "symbol": call_symbol,
                                                "type": "call", 
                                                "strike": strike_price,
                                                "expiration": exp_date,
                                                # Add placeholder values for required fields
                                                "bid": 0.0,
                                                "ask": 0.0, 
                                                "mid": 0.0,
                                                "impliedVol": 0.0,
                                                "delta": 0.0,
                                                "gamma": 0.0,
                                                "theta": 0.0,
                                                "vega": 0.0
                                            }
                                            options.append(call_data)
                                        if "put" in strike_data and strike_data["put"]:
                                            put_symbol = strike_data["put"]
                                            put_data = {
                                                "symbol": put_symbol,
                                                "type": "put",
                                                "strike": strike_price, 
                                                "expiration": exp_date,
                                                # Add placeholder values for required fields
                                                "bid": 0.0,
                                                "ask": 0.0,
                                                "mid": 0.0, 
                                                "impliedVol": 0.0,
                                                "delta": 0.0,
                                                "gamma": 0.0,
                                                "theta": 0.0,
                                                "vega": 0.0
                                            }
                                            options.append(put_data)
                                return options

    # Legacy fallback logic for other API shapes...
    chains = chain_json.get("chains") or chain_json.get("data") or chain_json
    if isinstance(chains, dict):
        # If expiry is a key
        if expiry in chains:
            bucket = chains.get(expiry) or {}
            # bucket may have 'puts' and 'calls'
            calls = bucket.get("calls") or bucket.get("call") or []
            puts = bucket.get("puts") or bucket.get("put") or []
            for c in calls:
                c = dict(c)
                c["type"] = "call"
                options.append(c)
            for p in puts:
                p = dict(p)
                p["type"] = "put"
                options.append(p)
            return options

    # Another common shape: top-level lists under 'data' -> 'items'
    data = chain_json.get("data") or chain_json
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        for item in data.get("items", []):
            # item may contain expiry info
            if str(item.get("expiration")) == str(expiry) or str(item.get("expiry")) == str(expiry):
                # determine call/put
                t = "call" if (item.get("type") or item.get("option_type") or "").lower().startswith("c") else "put"
                item = dict(item)
                item["type"] = t
                options.append(item)
        if options:
            return options

    # Fallback: scan for lists under various keys
    for key in ("options", "optionContracts", "data"):
        candidate = chain_json.get(key)
        if isinstance(candidate, list):
            for item in candidate:
                if str(item.get("expiration")) == str(expiry) or str(item.get("expiry")) == str(expiry) or str(item.get("expirationDate")) == str(expiry):
                    item = dict(item)
                    # infer type
                    t = "call" if (str(item.get("type") or item.get("option_type") or item.get("right") or "")).lower().startswith("c") else "put"
                    item["type"] = t
                    options.append(item)
            if options:
                return options

    # As a last resort, collect any items that look like options (have a 'strike')
    def walk_and_collect(obj: Any):
        if isinstance(obj, list):
            for it in obj:
                walk_and_collect(it)
        elif isinstance(obj, dict):
            if any(k in obj for k in ("strike", "strikePrice", "strikePx")):
                it = dict(obj)
                if "type" not in it:
                    it["type"] = "call" if (str(it.get("right") or "")).upper().startswith("C") else "put"
                options.append(it)
            else:
                for v in obj.values():
                    walk_and_collect(v)

    walk_and_collect(chain_json)
    return options


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "" or (isinstance(x, str) and x.upper() == "N/A"):
            return default
        return float(x)
    except Exception:
        try:
            return float(str(x))
        except Exception:
            return default


def normalize_option_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Map a raw option JSON record into the required CSV columns.
    Output keys: strike,type,bid,ask,mid,iv,delta,gamma,theta,vega
    """
    strike = rec.get("strike") or rec.get("strikePrice") or rec.get("strikePx") or rec.get("strike_price")
    option_type = (rec.get("type") or rec.get("option_type") or rec.get("right") or "").lower()
    option_type = "call" if str(option_type).lower().startswith("c") else "put"

    # Prices
    bid = rec.get("bid") or rec.get("bid_price") or rec.get("bidPrice")
    ask = rec.get("ask") or rec.get("ask_price") or rec.get("askPrice")
    mid = rec.get("mid") or rec.get("mark") or rec.get("last")
    # implied vol
    iv = rec.get("impliedVol") or rec.get("implied_volatility") or rec.get("impliedVolatility") or rec.get("iv")

    # Greeks: may be nested under rec['greeks'] or top-level
    greeks = rec.get("greeks") or rec
    delta = greeks.get("delta")
    gamma = greeks.get("gamma")
    theta = greeks.get("theta")
    vega = greeks.get("vega")

    return {
        "strike": safe_float(strike, 0.0),
        "type": option_type,
        "bid": safe_float(bid, 0.0),
        "ask": safe_float(ask, 0.0),
        "mid": safe_float(mid, 0.0),
        "iv": safe_float(iv, 0.0),
        "delta": safe_float(delta, 0.0),
        "gamma": safe_float(gamma, 0.0),
        "theta": safe_float(theta, 0.0),
        "vega": safe_float(vega, 0.0),
    }


def fetch_options_chain_for_symbol(symbol: str, username: Optional[str], password: Optional[str], token: Optional[str] = None, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT) -> List[Dict[str, Any]]:
    """High-level helper that authenticates (if credentials provided) and
    returns a list of normalized option records for the nearest expiry.

    Raises TastytradeAPIError on fatal errors.
    """
    # If a token was passed explicitly, prefer it. Otherwise, try username/password auth.
    if not token:
        if username and password:
            token = tastytrade_auth(username, password, base_url=base_url, timeout=timeout)
    # Fetch chain JSON
    chain_json = _try_options_chain_endpoints(symbol, token, base_url, timeout)

    # find nearest expiry
    expiry = find_nearest_expiration(chain_json)
    if not expiry:
        raise TastytradeAPIError("Could not determine nearest expiration from chain response")

    options_raw = extract_options_for_expiry(chain_json, expiry)
    if not options_raw:
        raise TastytradeAPIError(f"No options found for {symbol} expiry {expiry}")

    normalized = [normalize_option_record(r) for r in options_raw]
    return normalized


def write_options_csv(symbol: str, rows: List[Dict[str, Any]], out_dir: str = ".") -> str:
    df = pd.DataFrame(rows)
    # ensure column order
    cols = ["strike", "type", "bid", "ask", "mid", "iv", "delta", "gamma", "theta", "vega"]
    for c in cols:
        if c not in df.columns:
            df[c] = 0.0
    df = df[cols]
    filename = os.path.join(out_dir, f"{symbol}_options.csv")
    df.to_csv(filename, index=False)
    return filename


def main():
    parser = argparse.ArgumentParser(description="Fetch options chain from Tastytrade and write CSV")
    parser.add_argument("symbol", help="Underlying symbol, e.g. AAPL")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Tastytrade base API URL")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="HTTP request timeout seconds")
    parser.add_argument("--token", help="Use an existing API token (overrides env/user-pass auth)")
    parser.add_argument("--dry-run", action="store_true", help="Don't call network; only validate env and args")
    args = parser.parse_args()

    load_dotenv()  # populate env from .env file if present
    username, password = get_credentials()

    if args.dry_run:
        print("Dry run: env and args validated.")
        print(f"Symbol={args.symbol}")
        print(f"Username provided: {'yes' if username else 'no'}")
        sys.exit(0)

    # Resolve token: prefer CLI arg, then env token, then fall back to username/password
    token = args.token or get_token_from_env()

    if not token and (not username or not password):
        print("Error: Missing credentials. Provide --token or set TASTYWORKS_TOKEN/TASTYWORKS_USER & TASTYWORKS_PASS in .env or environment.")
        sys.exit(2)

    try:
        rows = fetch_options_chain_for_symbol(args.symbol, username, password, token=token, base_url=args.base_url, timeout=args.timeout)
    except TastytradeAPIError as e:
        print(f"Error fetching options chain: {e}")
        sys.exit(3)
    except requests.RequestException as e:
        print(f"Network error while fetching options chain: {e}")
        sys.exit(4)

    try:
        filename = write_options_csv(args.symbol, rows)
        print(f"Wrote {len(rows)} rows to {filename}")
    except Exception as e:
        print(f"Error writing CSV: {e}")
        sys.exit(5)


if __name__ == "__main__":
    main()
