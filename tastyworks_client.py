import os
import asyncio
import requests
from typing import Optional, Dict
import math
import datetime
import time
import logging
try:
    import yfinance as yf
except Exception:
    yf = None

# configure basic logging for debug during development
logging.basicConfig(level=logging.INFO)

try:
    # session creation helper and models
    from tastyworks.tastyworks_api import tasty_session
    from tastyworks.models import option_chain as tw_option_chain
    from tastyworks.models.underlying import Underlying
    from tastyworks.models.session import TastyAPISession
    from tastyworks.streamer import DataStreamer
    from tastyworks.models.option import Option
    from tastyworks.dxfeed import mapper as dx_mapper
    import logging
except Exception:
    tasty_session = None
    tw_option_chain = None
    Underlying = None
    TastyAPISession = None

# Optionally load .env if python-dotenv is installed
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


class TastyworksClient:
    """Minimal wrapper around the unofficial tastyworks package.

    Reads credentials from env vars: TASTYWORKS_USER, TASTYWORKS_PASS
    Provides synchronous helpers by running the underlying async calls.
    """

    def __init__(self):
        self.user = os.environ.get('TASTYWORKS_USER')
        self.password = os.environ.get('TASTYWORKS_PASS')
        self._session = None

    def _ensure_session(self):
        if tasty_session is None or TastyAPISession is None:
            raise RuntimeError('tastyworks package not available')
        if self._session is None:
            # create a session (synchronous constructor will authenticate)
            try:
                # tasty_session.create_new_session wraps models.session.TastyAPISession
                self._session = tasty_session.create_new_session(self.user, self.password)
            except Exception as e:
                raise RuntimeError(f'Failed to create tastyworks session: {e}')

    def get_option_quote(self, underlying: str, expiry: str, strike: float, right: str) -> Optional[Dict]:
        """Fetch option quote info via tastyworks. Return None on failure.

        expiry: YYYYMMDD or other formats accepted by tastyworks lib
        right: 'C' or 'P'
        """
        # The tastyworks package provides an async option_chain helper; we'll use
        # that to locate the option structure. Retrieving live quote/greeks via
        # dxFeed requires the DataStreamer (websocket) flow and is more involved.
        # For now, this method will attempt to authenticate and locate the matching
        # Option object and return None for the quote (placeholder).
        try:
            self._ensure_session()
        except Exception:
            return None

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def _get_chain():
                und = Underlying(underlying)
                oc = await tw_option_chain.get_option_chain(self._session, und)
                return oc

            oc = loop.run_until_complete(_get_chain())
            # find matching option object
            target_opt = None
            for opt in oc.options:
                try:
                    exp_str = opt.expiry.strftime('%Y%m%d')
                    if exp_str.startswith(str(expiry)) and float(opt.strike) == float(strike) and opt.option_type.value.upper().startswith(right.upper()):
                        target_opt = opt
                        break
                except Exception:
                    continue

            if not target_opt:
                return None

            # Use DataStreamer to subscribe to the option dxFeed symbol and wait for Greeks
            sym = target_opt.get_dxfeed_symbol()

            # helper coroutine to subscribe and wait for Greeks message
            async def _listen_for_greeks(ds: DataStreamer, timeout: float = 5.0):
                # subscribe to Greeks for symbol
                await ds.add_data_sub({'Greeks': [sym]})
                try:
                    # listen yields already-mapped items (see DataStreamer._consumer)
                    async for item in ds.listen():
                        try:
                            # mapped item classes are e.g. Greeks or Quote
                            cls_name = item.__class__.__name__
                            if cls_name == 'Greeks':
                                # item.data should be the underlying dict
                                data = getattr(item, 'data', None)
                                if isinstance(data, dict):
                                    delta = data.get('delta')
                                    theta = data.get('theta')
                                    return {'delta': delta, 'theta': theta}
                        except Exception:
                            continue
                finally:
                    # try to unsubscribe
                    try:
                        await ds.remove_data_sub({'Greeks': [sym]})
                    except Exception:
                        pass
                return None

            # Try to create DataStreamer with a small retry loop
            attempts = 2
            last_err = None
            for attempt in range(attempts):
                try:
                    ds = DataStreamer(self._session)
                    try:
                        # Use asyncio.wait_for to bound overall wait
                        res = loop.run_until_complete(asyncio.wait_for(_listen_for_greeks(ds), timeout=6.0))
                        # close connection cleanly
                        try:
                            loop.run_until_complete(ds._cometd_close())
                        except Exception:
                            pass
                        return res or None
                    finally:
                        # ensure close if cometd_client exists
                        try:
                            if hasattr(ds, 'cometd_client') and ds.cometd_client:
                                loop.run_until_complete(ds._cometd_close())
                        except Exception:
                            pass
                except Exception as e:
                    last_err = e
                    # short backoff
                    time.sleep(0.5)
                    continue
            return None
        except Exception:
            return None


def get_option_greeks_from_tasty(underlying: str, expiry: str, strike: float, right: str, underlying_price: Optional[float] = None, force_yahoo: bool = False) -> Dict:
    """Convenience wrapper: return {'delta':..., 'theta':...} or empty dict."""
    # Prefer a non-streaming fallback: try to compute Greeks using Black–Scholes
    # if we can obtain an underlying price (and optionally an implied vol)
    client = TastyworksClient()
    api = None
    if not force_yahoo:
        try:
            client._ensure_session()
            api = client._session
        except Exception:
            api = None

    def _parse_expiry_to_date(expiry_str: str) -> Optional[datetime.date]:
        # Accept YYYYMMDD or YYYY-MM-DD or YYMMDD-like formats
        try:
            if '-' in expiry_str:
                return datetime.datetime.strptime(expiry_str, '%Y-%m-%d').date()
            if len(expiry_str) == 8:
                return datetime.datetime.strptime(expiry_str, '%Y%m%d').date()
            if len(expiry_str) == 6:  # YYMMDD
                return datetime.datetime.strptime(expiry_str, '%y%m%d').date()
        except Exception:
            return None
        return None

    def _norm_right(r: str) -> str:
        r = (r or '').upper()
        if r.startswith('C'):
            return 'C'
        if r.startswith('P'):
            return 'P'
        return r

    def _bs_greeks(S, K, T, r, sigma, right):
        # T in years
        if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
            return None
        d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        # standard normal cdf
        def N(x):
            return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
        # standard normal pdf
        def n(x):
            return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

        if right == 'C':
            delta = N(d1)
            theta = (-S * n(d1) * sigma / (2 * math.sqrt(T)) - r * K * math.exp(-r * T) * N(d2))
        else:
            # put
            delta = N(d1) - 1
            theta = (-S * n(d1) * sigma / (2 * math.sqrt(T)) + r * K * math.exp(-r * T) * N(-d2))

        # convert theta to per-day for easier reading
        theta_per_day = theta / 365.0
        return {'delta': float(delta), 'theta': float(theta_per_day)}

    # In-memory short-lived cache for Yahoo prices
    # cache: symbol -> (price, timestamp)
    global _yahoo_price_cache
    try:
        _yahoo_price_cache
    except NameError:
        _yahoo_price_cache = {}
    CACHE_TTL = 30.0  # seconds

    # Cache for computed greeks so we don't recompute identical contracts repeatedly
    global _greeks_cache
    if '_greeks_cache' not in globals():
        _greeks_cache = {}
    # greeks cache keys will be like 'SYM|YYYYMMDD|STRIKE|R'

    # Try to find underlying price via REST endpoints exposed by tastyworks (unless forced to Yahoo)
    S = underlying_price
    if not force_yahoo and S is None and api is not None:
        try:
            headers = api.get_request_headers()
            base = api.API_url
            logging.debug("tastyworks API base=%s headers=%s", base, bool(headers))
        except Exception:
            headers = None
            base = None
    else:
        headers = None
        base = None

    candidates = [
        f"{base}/quotes/{underlying}",
        f"{base}/quotes?symbols={underlying}",
        f"{base}/option-chains/{underlying}",
        f"{base}/option-chains/{underlying}/nested",
    ]
    # Use shorter timeouts and fail-fast for tastyworks REST calls
    for url in candidates:
        try:
            if base is None:
                logging.debug("no tastyworks base, skipping REST candidates")
                break
            logging.debug("trying tastyworks REST url: %s", url)
            # small per-request timeout
            r = __import__('requests').get(url, headers=headers, timeout=2)
            logging.debug("url %s status %s", url, getattr(r, 'status_code', None))
            if r.status_code != 200:
                continue
            try:
                jd = r.json()
            except Exception:
                continue
            # Look for common fields
            # case 1: direct quote dict like {'last':..., 'lastPrice':..., 'mid':...}
            if isinstance(jd, dict):
                # nested tastyworks option-chains layout
                if 'data' in jd and isinstance(jd['data'], dict):
                    # try underlying-price
                    items = jd['data'].get('items')
                    if items and isinstance(items, list) and len(items) > 0:
                        item = items[0]
                        up = item.get('underlying-price') or item.get('underlyingLastPrice') or item.get('last')
                        if isinstance(up, (int, float)):
                            S = float(up)
                            break
                # generic quote
                for cand in ('lastPrice', 'last', 'mid', 'price'):
                    v = jd.get(cand)
                    if isinstance(v, (int, float)):
                        S = float(v)
                        break
                if S is not None:
                    break
        except Exception:
            # fail fast on errors (network/DNS) and try next candidate
            continue

    # Try Yahoo Finance as a simple public fallback for underlying price with caching
    def _fetch_price_yahoo(sym: str) -> Optional[float]:
        now = time.time()
        # check cache
        entry = _yahoo_price_cache.get(sym)
        if entry:
            price, ts = entry
            if now - ts < CACHE_TTL:
                return price
        
        # Try yfinance first (more reliable)
        if yf is not None:
            try:
                t = yf.Ticker(sym)
                info = t.history(period='1d')
                if not info.empty:
                    last = info['Close'].iloc[-1]
                    if isinstance(last, (int, float)):
                        _yahoo_price_cache[sym] = (float(last), now)
                        logging.debug("yfinance price for %s = %s", sym, last)
                        return float(last)
            except Exception:
                pass
        
        # fallback to direct Yahoo API
        headers_ua = {'User-Agent': 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)'}
        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={sym}"
        attempts = 2
        for attempt in range(attempts):
            try:
                r = requests.get(url, timeout=6, headers=headers_ua)
                if r.status_code != 200:
                    time.sleep(0.5)
                    continue
                jd = r.json()
                q = jd.get('quoteResponse', {}).get('result', [])
                if not q:
                    time.sleep(0.5)
                    continue
                price = q[0].get('regularMarketPrice') or q[0].get('regularMarketPreviousClose') or q[0].get('postMarketPrice') or q[0].get('preMarketPrice')
                if isinstance(price, (int, float)):
                    _yahoo_price_cache[sym] = (float(price), now)
                    logging.debug("Yahoo API price for %s = %s (attempt %s)", sym, price, attempt+1)
                    return float(price)
            except Exception:
                time.sleep(0.5)
                continue
        return None

    if S is None:
        S = _fetch_price_yahoo(underlying)
    logging.debug("underlying price resolved to %s", S)
    # If underlying price isn't available via tastyworks REST/arg/Yahoo, give up
    if S is None:
        return {}

    # parse expiry and compute T (in years)
    exp_date = _parse_expiry_to_date(expiry)
    if not exp_date:
        return {}
    today = datetime.date.today()
    days = (exp_date - today).days
    if days < 0:
        return {}
    T = max(days / 365.0, 1.0 / 365.0)

    # try to find an implied vol in the option-chains nested structure
    sigma = None
    try:
        if base:
            logging.debug("attempting to fetch option-chains nested for %s", underlying)
            # short timeout here too
            r = __import__('requests').get(f"{base}/option-chains/{underlying}/nested", headers=headers, timeout=2)
            logging.debug("nested chain status %s", getattr(r, 'status_code', None))
            if r.status_code == 200:
                jd = r.json()
                items = jd.get('data', {}).get('items', [])
                if items:
                    for item in items:
                        for exp in item.get('expirations', []):
                            ed = exp.get('expiration-date')
                            if ed and ed.replace('-', '') == expiry.replace('-', '')[:8]:
                                for st in exp.get('strikes', []):
                                    try:
                                        if float(st.get('strike-price')) == float(strike):
                                            side = 'call' if _norm_right(right) == 'C' else 'put'
                                            entry = st.get(side)
                                            # sometimes entry is a dict with implied vol
                                            if isinstance(entry, dict):
                                                iv = entry.get('impliedVol') or entry.get('implied-vol') or entry.get('iv') or entry.get('impliedVolatility')
                                                if isinstance(iv, (int, float)):
                                                    sigma = float(iv)
                                                    break
                                            # if entry is a symbol string, try fetching quotes for that option symbol
                                            if isinstance(entry, str) and base:
                                                try:
                                                    qurl = f"{base}/quotes?symbols={entry}"
                                                    qr = __import__('requests').get(qurl, headers=headers, timeout=1.5)
                                                    if qr.status_code == 200:
                                                        qjd = qr.json()
                                                        # recursive search for iv-like fields
                                                        def _find_iv(obj):
                                                            if isinstance(obj, dict):
                                                                for k, v in obj.items():
                                                                    lk = k.lower()
                                                                    if 'implied' in lk or lk == 'iv' or 'implied_vol' in lk or 'impliedvol' in lk:
                                                                        if isinstance(v, (int, float)):
                                                                            return float(v)
                                                                    res = _find_iv(v)
                                                                    if res is not None:
                                                                        return res
                                                            if isinstance(obj, list):
                                                                for el in obj:
                                                                    res = _find_iv(el)
                                                                    if res is not None:
                                                                        return res
                                                            return None
                                                        qiv = _find_iv(qjd)
                                                        if qiv is not None:
                                                            sigma = float(qiv)
                                                            break
                                                except Exception:
                                                    pass
                                    except Exception:
                                        continue
                            if sigma:
                                break
                        if sigma:
                            break
    except Exception:
        sigma = None

    # fallback sigma
    if sigma is None:
        sigma = 0.25

    # risk-free rate assumption (small constant); could be improved by querying a yield curve
    r_rate = 0.03

    # store/return greeks; use cache key to avoid recomputation
    cache_key = f"{underlying}|{expiry}|{float(strike):.4f}|{_norm_right(right)}"
    try:
        if cache_key in _greeks_cache:
            return _greeks_cache[cache_key]
    except Exception:
        pass

    greeks = _bs_greeks(S, float(strike), T, r_rate, float(sigma), _norm_right(right))
    res = greeks or {}
    try:
        _greeks_cache[cache_key] = res
    except Exception:
        pass
    return res
