#!/usr/bin/env python3
"""
Python script to interact with the IBKR Client Portal Web API.

This script:
1. Checks if the gateway is running by calling GET https://localhost:5001/v1/tickle
2. If not running, starts the gateway automatically
3. Handles authentication via the web interface
4. Fetches portfolio accounts and positions
5. Prints position details for each account
6. Integrates Tastytrade options data for enhanced Greeks calculation with caching
"""

import requests
import time
import subprocess
import os
import sys
import json
import re
from typing import Dict, List, Optional, Tuple, Any, Set
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import asyncio
from datetime import datetime, timedelta
import pickle
from dataclasses import dataclass
import yfinance as yf

# Disable SSL warnings for localhost
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Set up logging to reduce debug noise
logging.basicConfig(level=logging.INFO)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# Optional tastyworks integration
try:
    from tastyworks_client import get_option_greeks_from_tasty
    TASTYWORKS_AVAILABLE = True
except Exception:
    TASTYWORKS_AVAILABLE = False

# Tastytrade SDK integration
try:
    from tastytrade import Session, OAuthSession
    from tastytrade.instruments import get_option_chain, NestedFutureOptionChain
    from tastytrade.dxfeed import Quote, Greeks
    from tastytrade import DXLinkStreamer
    TASTYTRADE_SDK_AVAILABLE = True
except ImportError:
    TASTYTRADE_SDK_AVAILABLE = False


@dataclass
class OptionData:
    """Data class to hold option market data and Greeks."""
    symbol: str
    underlying: str
    strike: float
    option_type: str  # 'call' or 'put'
    expiration: str
    bid: float = 0.0
    ask: float = 0.0
    mid: float = 0.0
    iv: float = 0.0
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    timestamp: Optional[datetime] = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


@dataclass
class CacheEntry:
    """Cache entry for options data."""
    data: Dict[str, OptionData]  # key: option_key, value: OptionData
    timestamp: datetime
    expiry_minutes: int = 5
    
    def is_valid(self) -> bool:
        """Check if cache entry is still valid."""
        return datetime.now() < self.timestamp + timedelta(minutes=self.expiry_minutes)


class BetaConfig:
    """Manager for beta coefficients and multipliers for SPX-weighted delta calculation."""
    
    def __init__(self, config_file: str = 'beta_config.json'):
        self.config_file = config_file
        self.config = {}
        self.betas = {}
        self.multipliers = {}
        self.default_beta = 1.0
        self.default_stock_multiplier = 1.0
        self.default_option_multiplier = 100.0
        self._load_config()
    
    def _load_config(self):
        """Load beta configuration from JSON file."""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    self.config = json.load(f)
                    self.betas = self.config.get('betas', {})
                    self.multipliers = self.config.get('multipliers', {})
                    self.default_beta = self.config.get('default_beta', 1.0)
                    self.default_stock_multiplier = self.multipliers.get('default_stock_multiplier', 1.0)
                    self.default_option_multiplier = self.multipliers.get('default_option_multiplier', 100.0)
                    print(f"Loaded beta config with {len(self.betas)} betas and {len(self.multipliers)} multipliers")
            else:
                print(f"Beta config file {self.config_file} not found, using defaults")
        except Exception as e:
            print(f"Error loading beta config: {e}, using defaults")
    
    def get_beta(self, symbol: str) -> float:
        """Get beta coefficient for a symbol."""
        return self.betas.get(symbol.upper(), self.default_beta)


class TastytradeOptionsCache:
    """Cache manager for Tastytrade options data."""
    
    def __init__(self, cache_file: str = '.tastytrade_cache.pkl', default_expiry_minutes: int = 5):
        self.cache_file = cache_file
        self.default_expiry_minutes = default_expiry_minutes
        self.cache: Dict[str, CacheEntry] = {}
        self.session: Optional[Session] = None
        self.last_session_error: Optional[str] = None
        self.last_session_attempt_at: Optional[datetime] = None
        # Cache for symbols that don't have options (to avoid repeated failed lookups)
        self.no_options_cache: set = set()
        # Known futures roots that have option chains in Tastytrade (non-exhaustive)
        self.futures_roots: Set[str] = {
            'ES', 'MES', 'NQ', 'MNQ', 'YM', 'MYM', 'RTY', 'M2K', 'CL', 'NG', 'GC', 'SI', 'ZB', 'ZN', 'ZF'
        }
        self._load_cache()
    
    def _load_cache(self):
        """Load cache from disk."""
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'rb') as f:
                    # First attempt: normal unpickle
                    try:
                        cache_data = pickle.load(f)
                    except Exception:
                        # Legacy compatibility: cache may have been pickled when this module
                        # ran as __main__, so classes were recorded under '__main__'.
                        try:
                            f.seek(0)
                            class _RemapUnpickler(pickle.Unpickler):
                                def find_class(self, module, name):
                                    if module == '__main__' and name in ('CacheEntry', 'OptionData'):
                                        # Remap to current module definitions
                                        from ibkr_portfolio_client import CacheEntry as _CE, OptionData as _OD
                                        return {'CacheEntry': _CE, 'OptionData': _OD}[name]
                                    return super().find_class(module, name)
                            cache_data = _RemapUnpickler(f).load()
                        except Exception as e2:
                            print(f"Warning: Could not load cache: {e2}")
                            self.cache = {}
                            self.no_options_cache = set()
                            return
                    # Handle both old and new cache formats
                    if isinstance(cache_data, dict) and 'cache' in cache_data:
                        self.cache = cache_data['cache']
                        self.no_options_cache = cache_data.get('no_options_cache', set())
                    else:
                        self.cache = cache_data
                        self.no_options_cache = set()
                # Normalize and clean no-options cache: remove futures roots and normalize symbols
                if self.no_options_cache:
                    normalized = {str(s).upper().lstrip('/') for s in self.no_options_cache}
                    self.no_options_cache = {s for s in normalized if s not in self.futures_roots}
                # Clean expired entries
                self._cleanup_expired()
            except Exception as e:
                print(f"Warning: Could not load cache: {e}")
                self.cache = {}
                self.no_options_cache = set()
    
    def _save_cache(self):
        """Save cache to disk."""
        try:
            cache_data = {
                'cache': self.cache,
                'no_options_cache': self.no_options_cache
            }
            with open(self.cache_file, 'wb') as f:
                pickle.dump(cache_data, f)
        except Exception as e:
            print(f"Warning: Could not save cache: {e}")
    
    def _cleanup_expired(self):
        """Remove expired cache entries."""
        expired_keys = [key for key, entry in self.cache.items() if not entry.is_valid()]
        for key in expired_keys:
            del self.cache[key]
        if expired_keys:
            self._save_cache()
    
    def _get_credentials(self) -> Tuple[Optional[str], Optional[str]]:
        """Get Tastytrade credentials from environment."""
        username_keys = ['TASTYTRADE_USERNAME', 'TASTYWORKS_USER', 'TASTYTRADE_USER']
        password_keys = ['TASTYTRADE_PASSWORD', 'TASTYWORKS_PASS']
        
        username = None
        for key in username_keys:
            username = os.environ.get(key)
            if username:
                break
        
        password = None
        for key in password_keys:
            password = os.environ.get(key)
            if password:
                break
        
        return username, password
    
    def _get_session(self) -> Optional[Session]:
        """Get or create Tastytrade session."""
        if self.session is not None:
            return self.session

        if self.last_session_error and self.last_session_attempt_at is not None:
            if (datetime.now() - self.last_session_attempt_at).total_seconds() < 30:
                return None

        username, password = self._get_credentials()
        if not username:
            self.last_session_error = "Missing tastytrade username in environment"
            return None

        oauth_refresh_token = os.environ.get('TASTYTRADE_REFRESH_TOKEN') or os.environ.get('TASTYWORKS_REFRESH_TOKEN')
        oauth_client_secret = (
            os.environ.get('TASTYTRADE_CLIENT_SECRET')
            or os.environ.get('TASTYWORKS_CLIENT_SECRET')
            or os.environ.get('SECRET')
        )
        remember_token = os.environ.get('TASTYTRADE_REMEMBER_TOKEN') or os.environ.get('TASTYWORKS_REMEMBER_TOKEN')
        two_factor_code = os.environ.get('TASTYTRADE_2FA_CODE') or os.environ.get('TASTYTRADE_TWO_FACTOR_CODE')
        auth_errors: List[str] = []

        if oauth_refresh_token and oauth_client_secret:
            try:
                self.last_session_attempt_at = datetime.now()
                self.session = OAuthSession(
                    provider_secret=oauth_client_secret,
                    refresh_token=oauth_refresh_token,
                    is_test=False,
                )
                self.last_session_error = None
                return self.session
            except Exception as e:
                auth_errors.append(f"oauth refresh auth failed: {e}")

        if remember_token:
            try:
                self.last_session_attempt_at = datetime.now()
                self.session = Session(username, password=None, remember_token=remember_token)
                self.last_session_error = None
                return self.session
            except Exception as e:
                auth_errors.append(f"remember-token auth failed: {e}")

        if not password:
            self.last_session_error = "Missing tastytrade password and remember token"
            return None

        try:
            self.last_session_attempt_at = datetime.now()
            if two_factor_code:
                self.session = Session(username, password, two_factor_authentication=two_factor_code)
            else:
                self.session = Session(username, password)
            self.last_session_error = None
            return self.session
        except Exception as e:
            auth_errors.append(f"password auth failed: {e}")
            self.last_session_error = " | ".join(auth_errors)
            print(f"Warning: Could not create Tastytrade session: {self.last_session_error}")
            return None
    
    def _make_cache_key(self, underlying: str, expiry_minutes: Optional[int] = None) -> str:
        """Create cache key for an underlying symbol."""
        expiry = expiry_minutes or self.default_expiry_minutes
        base = underlying.upper().lstrip('/')
        return f"{base}_{expiry}min"

    def _normalize_underlying_key(self, underlying: str) -> str:
        """Canonical key used in cache and option keys (no leading slash, uppercased)."""
        return underlying.upper().lstrip('/')

    def _to_tasty_underlying(self, underlying: str) -> str:
        """Convert an underlying to Tastytrade futures format if needed (prepend slash)."""
        u = underlying.upper()
        if u.startswith('/'):
            return u
        if u in self.futures_roots:
            return f"/{u}"
        return u
    
    def _normalize_expiry(self, expiry_val: Any) -> str:
        """Normalize expiry to canonical YYYYMMDD string."""
        # Accept date/datetime, ms timestamp, 'YYYY-MM-DD' or 'YYYYMMDD'
        try:
            from datetime import date, datetime as dt
            if isinstance(expiry_val, (date, dt)):
                d = expiry_val if isinstance(expiry_val, date) else expiry_val.date()
                return d.strftime("%Y%m%d")
        except Exception:
            pass
        s = str(expiry_val).strip()
        if not s:
            return ""
        # Convert ms epoch
        try:
            if s.isdigit() and len(s) > 8:
                import time as _t
                d = datetime.fromtimestamp(int(s) / 1000)
                return d.strftime("%Y%m%d")
        except Exception:
            pass
        # Remove dashes if present
        s = s.replace("-", "")
        # Pad to 8 if needed (best-effort)
        return s[:8]

    def _make_option_key(self, underlying: str, expiry: Any, strike: float, option_type: str) -> str:
        """Create unique, normalized key for an option."""
        exp_norm = self._normalize_expiry(expiry)
        opt_norm = (option_type or "").strip().lower()
        return f"{underlying.upper()}_{exp_norm}_{float(strike):.2f}_{opt_norm}"

    def get_cached_option(self, underlying: str, expiry: str, strike: float, option_type: str) -> Optional[OptionData]:
        """Return an OptionData from the cache without attempting external network calls.

        This is useful for dry-run/testing where the cache may have been populated
        by a simulated prefetch.
        """
        self._cleanup_expired()
        option_key = self._make_option_key(underlying, expiry, strike, option_type)
        for entry in self.cache.values():
            if option_key in entry.data:
                return entry.data[option_key]
        return None

    def simulate_prefetch(self, underlying: str, only_options: Optional[Set[Tuple[str, float, str]]] = None, expiry_minutes: Optional[int] = None) -> Dict[str, OptionData]:
        """Create fake OptionData records for testing/dry-run and store them in the cache.

        The fake data will include reasonable placeholder Greeks so the display logic
        can exercise formatting and aggregation without contacting tastytrade.
        """
        cache_key = self._make_cache_key(underlying, expiry_minutes)
        fake_data: Dict[str, OptionData] = {}

        if not only_options:
            # Nothing requested — return empty
            return {}

        for (exp_str, strike_f, opt_type) in only_options:
            symbol = f"SIM:{underlying}:{exp_str}:{strike_f:.2f}:{opt_type}"
            
            # Create more realistic delta based on strike and underlying
            # Simulate different moneyness scenarios
            import random
            random.seed(hash(f"{underlying}_{strike_f}_{opt_type}"))  # Consistent fake data
            
            if opt_type == 'call':
                # Calls: ITM = higher delta, OTM = lower delta
                base_delta = random.uniform(0.15, 0.85)
            else:  # put
                # Puts: ITM = lower (more negative) delta, OTM = higher (less negative) delta
                base_delta = random.uniform(-0.85, -0.15)
            
            # Add some variation based on expiry (longer = slightly different delta)
            exp_factor = random.uniform(0.9, 1.1)
            delta = round(base_delta * exp_factor, 3)
            
            # More realistic theta (time decay)
            theta = round(random.uniform(-0.05, -0.001), 4)
            
            od = OptionData(
                symbol=symbol,
                underlying=underlying,
                strike=float(strike_f),
                option_type=opt_type,
                expiration=self._normalize_expiry(str(exp_str)),
                bid=1.0,
                ask=1.2,
                mid=1.1,
                iv=random.uniform(15.0, 35.0),
                delta=delta,
                gamma=round(random.uniform(0.005, 0.025), 4),
                theta=theta,
                vega=round(random.uniform(0.05, 0.15), 3),
                timestamp=datetime.now()
            )
            key = self._make_option_key(underlying, str(exp_str), float(strike_f), opt_type)
            fake_data[key] = od

        # Merge into cache
        existing_entry = self.cache.get(cache_key)
        existing = existing_entry.data if existing_entry else {}
        merged = dict(existing)
        merged.update(fake_data)
        self.cache[cache_key] = CacheEntry(data=merged, timestamp=datetime.now(), expiry_minutes=expiry_minutes or self.default_expiry_minutes)
        self._save_cache()
        print(f"Simulated prefetch: created {len(fake_data)} fake option(s) for {underlying}")
        return fake_data
    
    async def _has_options_chain(self, session: Session, underlying: str, retry_auth: bool = True) -> bool:
        """Quick check if an underlying symbol has options available."""
        try:
            # Prefer futures check if symbol is a known futures root
            is_future = underlying.startswith('/') or underlying.upper() in self.futures_roots
            
            if is_future:
                tasty_sym = underlying if underlying.startswith('/') else self._to_tasty_underlying(underlying)
                nested_chain = NestedFutureOptionChain.get(session, tasty_sym)
                return bool(nested_chain and nested_chain.option_chains and 
                           len(nested_chain.option_chains) > 0 and
                           len(nested_chain.option_chains[0].expirations) > 0)
            else:
                chain = get_option_chain(session, underlying)
                if chain and len(chain) > 0:
                    return True
                # If equity failed but this is a known futures root, try futures path
                u = underlying.upper()
                if u in self.futures_roots:
                    tasty_sym = self._to_tasty_underlying(underlying)
                    nested_chain = NestedFutureOptionChain.get(session, tasty_sym)
                    return bool(nested_chain and nested_chain.option_chains and 
                                len(nested_chain.option_chains) > 0 and
                                len(nested_chain.option_chains[0].expirations) > 0)
                return False
                
        except Exception as e:
            if retry_auth and ("unauthorized" in str(e).lower() or "401" in str(e)):
                # Session token can expire mid-run; refresh once and retry.
                self.session = None
                fresh_session = self._get_session()
                if fresh_session:
                    return await self._has_options_chain(fresh_session, underlying, retry_auth=False)
            print(f"Error checking options availability for {underlying}: {e}")
            return False

    async def _fetch_options_data(
        self,
        session: Session,
        underlying: str,
        only_options: Optional[Set[Tuple[str, float, str]]] = None,
        retry_auth: bool = True,
    ) -> Dict[str, OptionData]:
        """Fetch options data from Tastytrade API.

        If `only_options` is provided it should be a set of tuples:
            (expiry_str, strike_float, option_type_str ('call'|'put'))

        When provided, the function will only create OptionData records for
        matching expiry/strike/type combinations.
        """
        
        # Determine if this is a futures symbol (supports roots like MES without slash)
        is_future = underlying.startswith('/') or underlying.upper() in self.futures_roots

        # Check if we already know this symbol has no options; don't skip futures based on this cache
        if not is_future and underlying.upper() in self.no_options_cache:
            print(f"Skipping {underlying} - known to have no options")
            return {}

        print(f"Fetching fresh options data for {underlying} from Tastytrade...")

        # First check if this symbol has options at all
        if not await self._has_options_chain(session, underlying, retry_auth=retry_auth):
            if not is_future:
                print(f"No options chain available for {underlying}, adding to no-options cache...")
                self.no_options_cache.add(underlying.upper())
                self._save_cache()
            else:
                print(f"No futures options chain visible for {underlying} right now; will not cache as no-options")
            return {}
        
        try:
            if is_future:
                tasty_sym = underlying if underlying.startswith('/') else self._to_tasty_underlying(underlying)
                nested_chain = NestedFutureOptionChain.get(session, tasty_sym)
                if not nested_chain or not nested_chain.option_chains:
                    return {}
                
                chain_data = nested_chain.option_chains[0]
                if not chain_data.expirations:
                    return {}
                # Futures weekly/root coverage logging for observability
                try:
                    exp_dates = [str(getattr(exp, 'expiration_date', exp)) for exp in chain_data.expirations]
                    if exp_dates:
                        first_exp = exp_dates[0]
                        last_exp = exp_dates[-1]
                        print(f"Futures chain loaded for {tasty_sym}: {len(exp_dates)} expirations (range {first_exp} .. {last_exp})")
                    else:
                        print(f"Futures chain loaded for {tasty_sym}: 0 expirations")
                except Exception:
                    pass
                
                # Convert to a format similar to equity options
                chain = {}
                for exp in chain_data.expirations:
                    chain[exp.expiration_date] = exp.strikes
            else:
                chain = get_option_chain(session, underlying)
            
            if not chain:
                return {}
            
            # Determine which expirations to process. If only_options is
            # provided, restrict to expirations present in that set.
            available_expirations = sorted(chain.keys())
            if not available_expirations:
                return {}

            # Build normalization maps for expirations
            # Map normalized YYYYMMDD -> actual chain key
            norm_to_key: Dict[str, Any] = {}
            for k in available_expirations:
                # chain keys may be date/datetime/str; normalize
                norm = self._normalize_expiry(k)
                norm_to_key[norm] = k

            expirations_to_process = available_expirations
            only_options_norm: Optional[Set[Tuple[str, float, str]]] = None
            if only_options:
                # Normalize only_options to (YYYYMMDD, float(strike), 'call'/'put')
                only_options_norm = set()
                for e, k, t in only_options:
                    only_options_norm.add((self._normalize_expiry(e), float(k), (t or "").lower()))

                requested_exps = {e for (e, _, _) in only_options_norm}
                expirations_to_process = [norm_to_key[e] for e in requested_exps if e in norm_to_key]
                if not expirations_to_process:
                    # No matching expirations in the chain
                    return {}
            
            # Create option data records
            option_data = {}
            streamer_symbols = []
            symbol_to_option_key = {}
            key_underlying = self._normalize_underlying_key(underlying)
            
            # Iterate expirations and build records only for requested strikes
            for exp in expirations_to_process:
                options_list = chain[exp]
                exp_str = str(exp)
                exp_norm = self._normalize_expiry(exp)
                for option in options_list:
                    if is_future:
                        call_streamer_symbol = getattr(option, 'call_streamer_symbol', None)
                        put_streamer_symbol = getattr(option, 'put_streamer_symbol', None)
                        strike_price = getattr(option, 'strike_price', 0)
                        # Only include if not filtering, or if this strike/type is requested
                        if only_options:
                            # Use normalized set
                            want_call = (exp_norm, float(strike_price), 'call') in (only_options_norm or set())
                            want_put = (exp_norm, float(strike_price), 'put') in (only_options_norm or set())
                        else:
                            want_call = bool(call_streamer_symbol)
                            want_put = bool(put_streamer_symbol)

                        if call_streamer_symbol and want_call:
                            key = self._make_option_key(key_underlying, exp_norm, float(strike_price), 'call')
                            option_data[key] = OptionData(
                                symbol=call_streamer_symbol,
                                underlying=key_underlying,
                                strike=float(strike_price),
                                option_type='call',
                                expiration=exp_norm
                            )
                            streamer_symbols.append(call_streamer_symbol)
                            symbol_to_option_key[call_streamer_symbol] = key

                        if put_streamer_symbol and want_put:
                            key = self._make_option_key(key_underlying, exp_norm, float(strike_price), 'put')
                            option_data[key] = OptionData(
                                symbol=put_streamer_symbol,
                                underlying=key_underlying,
                                strike=float(strike_price),
                                option_type='put',
                                expiration=exp_norm
                            )
                            streamer_symbols.append(put_streamer_symbol)
                            symbol_to_option_key[put_streamer_symbol] = key
                    else:
                        strike_price = float(option.strike_price)
                        opt_type = 'call' if getattr(option.option_type, 'value', getattr(option, 'option_type', 'C')) == 'C' else 'put'
                        if only_options:
                            want = (exp_norm, strike_price, opt_type) in (only_options_norm or set())
                        else:
                            want = True

                        if want:
                            key = self._make_option_key(key_underlying, exp_norm, strike_price, opt_type)
                            option_data[key] = OptionData(
                                symbol=option.streamer_symbol,
                                underlying=key_underlying,
                                strike=strike_price,
                                option_type=opt_type,
                                expiration=exp_norm
                            )
                            streamer_symbols.append(option.streamer_symbol)
                            symbol_to_option_key[option.streamer_symbol] = key
            
            if not streamer_symbols:
                # Nothing to subscribe to (filtered out everything)
                return option_data

            # Fetch real-time market data
            try:
                async with DXLinkStreamer(session) as streamer:
                    await streamer.subscribe(Quote, streamer_symbols)
                    await streamer.subscribe(Greeks, streamer_symbols)

                    # Slightly increase wait window for day-of-expiry symbols to capture late ticks
                    try:
                        today_norm = datetime.now().strftime('%Y%m%d')
                        day_of_expiry = any(od.expiration == today_norm for od in option_data.values())
                    except Exception:
                        day_of_expiry = False

                    initial_wait = 5 if day_of_expiry else 3
                    data_timeout = 20 if day_of_expiry else 15
                    if day_of_expiry:
                        print(f"Extended wait window for {underlying} options expiring today: initial {initial_wait}s, timeout {data_timeout}s")

                    await asyncio.sleep(initial_wait)
                    
                    start_time = asyncio.get_event_loop().time()
                    
                    while (asyncio.get_event_loop().time() - start_time) < data_timeout:
                        try:
                            # Get quotes
                            quote_events = await asyncio.wait_for(streamer.get_event(Quote), timeout=1.0)
                            if not isinstance(quote_events, list):
                                quote_events = [quote_events]
                            
                            for quote in quote_events:
                                event_symbol = getattr(quote, 'event_symbol', None)
                                if event_symbol in symbol_to_option_key:
                                    option_key = symbol_to_option_key[event_symbol]
                                    opt_data = option_data[option_key]
                                    
                                    bid_price = getattr(quote, 'bid_price', None)
                                    ask_price = getattr(quote, 'ask_price', None)
                                    if bid_price is not None:
                                        opt_data.bid = float(bid_price) if bid_price else 0.0
                                    if ask_price is not None:
                                        opt_data.ask = float(ask_price) if ask_price else 0.0
                                        
                                    if opt_data.bid > 0 and opt_data.ask > 0:
                                        opt_data.mid = (opt_data.bid + opt_data.ask) / 2
                        
                        except asyncio.TimeoutError:
                            pass
                        
                        try:
                            # Get greeks
                            greeks_events = await asyncio.wait_for(streamer.get_event(Greeks), timeout=1.0)
                            if not isinstance(greeks_events, list):
                                greeks_events = [greeks_events]
                            
                            for greeks in greeks_events:
                                event_symbol = getattr(greeks, 'event_symbol', None)
                                if event_symbol in symbol_to_option_key:
                                    option_key = symbol_to_option_key[event_symbol]
                                    opt_data = option_data[option_key]
                                    
                                    # Update greeks
                                    volatility = getattr(greeks, 'volatility', None)
                                    if volatility is not None:
                                        opt_data.iv = float(volatility) if volatility else 0.0
                                    
                                    delta = getattr(greeks, 'delta', None)
                                    if delta is not None:
                                        opt_data.delta = float(delta) if delta else 0.0
                                    
                                    gamma = getattr(greeks, 'gamma', None)
                                    if gamma is not None:
                                        opt_data.gamma = float(gamma) if gamma else 0.0
                                    
                                    theta = getattr(greeks, 'theta', None)
                                    if theta is not None:
                                        opt_data.theta = float(theta) if theta else 0.0
                                    
                                    vega = getattr(greeks, 'vega', None)
                                    if vega is not None:
                                        opt_data.vega = float(vega) if vega else 0.0
                        
                        except asyncio.TimeoutError:
                            pass
            
            except Exception as e:
                print(f"Warning: Could not fetch real-time market data for {underlying}: {e}")
            
            return option_data
            
        except Exception as e:
            if retry_auth and ("unauthorized" in str(e).lower() or "401" in str(e)):
                # Token/session may have expired between checks and fetch.
                self.session = None
                fresh_session = self._get_session()
                if fresh_session:
                    return await self._fetch_options_data(
                        fresh_session,
                        underlying,
                        only_options=only_options,
                        retry_auth=False,
                    )
            print(f"Error fetching options data for {underlying}: {e}")
            return {}
    
    async def get_option_data(self, underlying: str, expiry: str, strike: float,
                              option_type: str, expiry_minutes: Optional[int] = None) -> Optional[OptionData]:
        """Get option data with caching."""

        # Normalize underlying for cache key/lookup (no leading slash)
        norm_underlying = self._normalize_underlying_key(underlying)

        # Quick check if we know this symbol has no options (ignore for futures roots)
        if norm_underlying not in self.futures_roots and norm_underlying in self.no_options_cache:
            return None

        cache_key = self._make_cache_key(norm_underlying, expiry_minutes)
        option_key = self._make_option_key(norm_underlying, expiry, strike, option_type)

        # Clean up expired entries first
        self._cleanup_expired()

        # Check cache
        if cache_key in self.cache and self.cache[cache_key].is_valid():
            cache_entry = self.cache[cache_key]
            if option_key in cache_entry.data:
                return cache_entry.data[option_key]

        # Need to fetch fresh data
        if not TASTYTRADE_SDK_AVAILABLE:
            return None

        session = self._get_session()
        if not session:
            return None

        # Fetch all options for this underlying to populate cache efficiently
        options_data = await self._fetch_options_data(session, underlying)

        if options_data:
            # Update cache
            self.cache[cache_key] = CacheEntry(
                data=options_data,
                timestamp=datetime.now(),
                expiry_minutes=expiry_minutes or self.default_expiry_minutes
            )
            self._save_cache()

            # Return requested option if available
            if option_key in options_data:
                return options_data[option_key]

        return None
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        self._cleanup_expired()
        total_entries = len(self.cache)
        total_options = sum(len(entry.data) for entry in self.cache.values())
        
        return {
            'total_cache_entries': total_entries,
            'total_options_cached': total_options,
            'no_options_symbols': len(self.no_options_cache),
            'no_options_list': sorted(list(self.no_options_cache)),
            'cache_file': self.cache_file,
            'default_expiry_minutes': self.default_expiry_minutes
        }

    async def fetch_and_cache_options_for_underlying(self, underlying: str,
                                                     only_options: Optional[Set[Tuple[str, float, str]]] = None,
                                                     expiry_minutes: Optional[int] = None,
                                                     force_refresh: bool = False) -> Dict[str, OptionData]:
        """Public helper to fetch (and cache) options for a single underlying.

        `only_options` is a set of tuples (expiry_str, strike_float, option_type)
        - If provided, only those entries will be fetched/created.
        - If force_refresh is True, bypass existing cache TTL.
        """
        # Use normalized underlying for cache key (no leading slash)
        norm_underlying = self._normalize_underlying_key(underlying)
        cache_key = self._make_cache_key(norm_underlying, expiry_minutes)

        # Force-refresh should bypass stale "no options" suppression.
        if force_refresh and norm_underlying in self.no_options_cache:
            self.no_options_cache.discard(norm_underlying)
            self._save_cache()

        # If symbol known to have no options, short-circuit (but never for futures roots)
        if norm_underlying in self.no_options_cache and norm_underlying not in self.futures_roots:
            return {}

        # Use cached entry if present and not expired unless force_refresh
        if not force_refresh and cache_key in self.cache and self.cache[cache_key].is_valid():
            # If only_options is supplied, filter the cached data
            if only_options:
                # Normalize only_options for comparison against cached OptionData.expiration (stored normalized)
                only_norm = {(self._normalize_expiry(e), float(s), (t or '').lower()) for (e, s, t) in only_options}
                filtered = {k: v for k, v in self.cache[cache_key].data.items() if (v.expiration, v.strike, v.option_type) in only_norm}
                return filtered
            return self.cache[cache_key].data

        # Create a session and fetch only requested options
        session = self._get_session()
        if not session:
            return {}

        options_data = await self._fetch_options_data(session, underlying, only_options=only_options)

        if options_data:
            # Update cache (merge with existing cached data to avoid losing unrelated entries)
            existing_entry = self.cache.get(cache_key)
            existing = existing_entry.data if existing_entry else {}
            merged = dict(existing)
            merged.update(options_data)
            self.cache[cache_key] = CacheEntry(data=merged, timestamp=datetime.now(), expiry_minutes=expiry_minutes or self.default_expiry_minutes)
            self._save_cache()

        return options_data

# Control whether external data sources (tastyworks / Yahoo) are allowed.
# Default: False -> only use IBKR/local placeholders.
USE_EXTERNAL = False
STREAM_DB_MANAGER = None
STREAM_PROCESSOR = None


def load_dotenv(env_file: str = '.env') -> None:
    """Load environment variables from .env file if it exists."""
    if not os.path.exists(env_file):
        return
    
    with open(env_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                # Strip inline comments: value = value before any ' #' or '\t#'
                value = value.strip().strip('"\'')
                if '  #' in value:
                    value = value[:value.index('  #')].strip()
                elif '\t#' in value:
                    value = value[:value.index('\t#')].strip()
                os.environ[key] = value


async def initialize_streaming_runtime() -> tuple[Any, Any]:
    """Initialize async DB manager and stream processor used by realtime ingestion."""

    global STREAM_DB_MANAGER
    global STREAM_PROCESSOR

    if STREAM_DB_MANAGER is not None and STREAM_PROCESSOR is not None:
        return STREAM_DB_MANAGER, STREAM_PROCESSOR

    from agent_config import load_streaming_environment
    from core.processor import DataProcessor
    from database.db_manager import DBManager

    config = load_streaming_environment()
    os.environ.setdefault("DB_HOST", config.db_host)
    os.environ.setdefault("DB_PORT", str(config.db_port))
    os.environ.setdefault("DB_NAME", config.db_name)
    os.environ.setdefault("DB_USER", config.db_user)
    os.environ.setdefault("DB_PASS", config.db_pass)
    os.environ.setdefault("DB_POOL_MIN", str(config.db_pool_min))
    os.environ.setdefault("DB_POOL_MAX", str(config.db_pool_max))
    os.environ.setdefault("DB_COMMAND_TIMEOUT", str(config.db_command_timeout))

    STREAM_DB_MANAGER = await DBManager.get_instance()
    STREAM_DB_MANAGER.flush_interval_seconds = config.stream_flush_interval_seconds
    STREAM_DB_MANAGER.flush_batch_size = config.stream_flush_batch_size
    STREAM_PROCESSOR = DataProcessor(STREAM_DB_MANAGER)
    await STREAM_PROCESSOR.start()
    return STREAM_DB_MANAGER, STREAM_PROCESSOR

class IBKRClient:
    def __init__(self, base_url: str = "https://localhost:5001", cache_expiry_minutes: int = 5):
        self.base_url = base_url
        self.session = requests.Session()
        # Disable SSL verification for localhost
        self.session.verify = False
        
        # Initialize Tastytrade options cache
        self.options_cache = TastytradeOptionsCache(
            cache_file='.tastytrade_cache.pkl',
            default_expiry_minutes=cache_expiry_minutes
        )
        
        # Initialize beta configuration for SPX-weighted delta calculation
        self.beta_config = BetaConfig('beta_config.json')
        
        # Cache for SPX price (updated periodically)
        self.spx_price = None
        self.spx_price_timestamp = None
        self.spx_price_cache_minutes = 5  # Cache SPX price for 5 minutes
        
        # Simulation mode flag
        self._simulation_mode = False
        
    def _normalize_symbol(self, sym: str) -> str:
        """Normalize a symbol/underlying for grouping (uppercased, strip leading '/')."""
        if not sym:
            return 'N/A'
        return str(sym).strip().split()[0].upper().lstrip('/')

    def check_gateway_status(self) -> bool:
        """Check if the gateway is running and responsive."""
        try:
            response = self.session.get(f"{self.base_url}/v1/api/tickle", timeout=5)
            return response.status_code == 200
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            return False
    
    def start_gateway(self) -> bool:
        """Start the IBKR Client Portal Gateway."""
        print("Starting IBKR Client Portal Gateway...")
        
        # Change to the clientportal directory
        gateway_dir = os.path.join(os.getcwd(), "clientportal")
        if not os.path.exists(gateway_dir):
            print(f"Error: Gateway directory not found at {gateway_dir}")
            return False
        
        try:
            # Start the gateway using the run.sh script
            subprocess.Popen(
                ["./bin/run.sh", "root/conf.yaml"],
                cwd=gateway_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            
            # Wait for gateway to start
            print("Waiting for gateway to start...")
            for i in range(30):  # Wait up to 30 seconds
                if self.check_gateway_status():
                    print("Gateway started successfully!")
                    return True
                time.sleep(1)
                print(f"  Waiting... ({i+1}/30)")
            
            print("Gateway failed to start within 30 seconds")
            return False
            
        except Exception as e:
            print(f"Error starting gateway: {e}")
            return False
    
    def stop_gateway(self) -> bool:
        """Stop the running IBKR Client Portal Gateway process."""
        try:
            result = subprocess.run(
                ["pkill", "-f", "clientportal.*run.sh"],
                capture_output=True,
                timeout=10,
            )
            # Also kill any lingering Java process spawned by run.sh
            subprocess.run(
                ["pkill", "-f", "root/conf.yaml"],
                capture_output=True,
                timeout=10,
            )
            # Give the process a moment to die
            time.sleep(2)
            return True
        except Exception as e:
            print(f"Error stopping gateway: {e}")
            return False

    def restart_gateway(self) -> bool:
        """Stop the current gateway instance and start a fresh one."""
        self.stop_gateway()
        return self.start_gateway()

    def check_auth_status(self) -> Dict:
        """Check authentication status."""
        try:
            response = self.session.get(f"{self.base_url}/v1/api/iserver/auth/status")
            if response.status_code == 200:
                return response.json()
            else:
                return {"authenticated": False, "error": f"Status code: {response.status_code}"}
        except Exception as e:
            return {"authenticated": False, "error": str(e)}
    
    def initiate_sso_login(self) -> bool:
        """Initiate SSO login process."""
        try:
            response = self.session.post(f"{self.base_url}/v1/api/iserver/auth/ssodh/init")
            return response.status_code == 200
        except Exception as e:
            print(f"Error initiating SSO login: {e}")
            return False
    
    def wait_for_authentication(self, max_wait: int = 60) -> bool:
        """Wait for user to authenticate via web interface."""
        print(f"\nPlease authenticate via the web interface at: {self.base_url}")
        print("Waiting for authentication...")
        
        for i in range(max_wait):
            auth_status = self.check_auth_status()
            if auth_status.get("authenticated", False):
                print("Authentication successful!")
                return True
            
            if i % 10 == 0:  # Print status every 10 seconds
                print(f"  Still waiting for authentication... ({i+1}/{max_wait})")
            
            time.sleep(1)
        
        print("Authentication timeout")
        return False
    
    def get_accounts(self) -> List[Dict]:
        """Fetch portfolio accounts."""
        try:
            response = self.session.get(f"{self.base_url}/v1/api/portfolio/accounts")
            if response.status_code == 200:
                return response.json()
            else:
                print(f"Error fetching accounts: Status code {response.status_code}")
                print(f"Response: {response.text}")
                return []
        except Exception as e:
            print(f"Error fetching accounts: {e}")
            return []

    def get_account_summary(self, account_id: str) -> Dict[str, Any]:
        """Fetch account summary metrics from IBKR CPAPI portfolio endpoint."""
        try:
            response = self.session.get(
                f"{self.base_url}/v1/api/portfolio/{account_id}/summary",
                timeout=15,
            )
            if response.status_code == 200:
                payload = response.json()
                return payload if isinstance(payload, dict) else {}
            return {}
        except Exception as e:
            print(f"Error fetching summary for {account_id}: {e}")
            return {}
    
    def get_positions(self, account_id: str) -> List[Dict]:
        """Fetch positions for a specific account."""
        try:
            all_positions: List[Dict] = []
            seen_rows: set[tuple[Any, ...]] = set()

            def _row_key(position: Dict[str, Any]) -> tuple[Any, ...]:
                return (
                    position.get("acctId"),
                    position.get("conid"),
                    position.get("contractDesc"),
                    position.get("position"),
                    position.get("avgCost"),
                    position.get("mktValue"),
                    position.get("unrealizedPnl"),
                )

            for page in range(0, 20):
                page_positions: List[Dict] | None = None
                last_error_text = ""
                for attempt in range(1, 4):
                    try:
                        response = self.session.get(
                            f"{self.base_url}/v1/api/portfolio/{account_id}/positions/{page}",
                            timeout=20,
                        )
                        if response.status_code != 200:
                            last_error_text = f"status {response.status_code}: {response.text}"
                            time.sleep(0.4 * attempt)
                            continue
                        payload = response.json()
                        page_positions = payload if isinstance(payload, list) else []
                        break
                    except Exception as exc:
                        last_error_text = str(exc)
                        time.sleep(0.4 * attempt)

                if page_positions is None:
                    print(
                        f"Error fetching positions for {account_id} page {page} after retries: {last_error_text}"
                    )
                    break

                if not page_positions:
                    break

                for position in page_positions:
                    key = _row_key(position)
                    if key in seen_rows:
                        continue
                    seen_rows.add(key)
                    all_positions.append(position)

            # Cache the ES/MES front-month conid and current price for get_spx_price()
            for p in all_positions:
                if p.get("assetClass") == "FUT":
                    desc = str(p.get("contractDesc", "")).upper()
                    m = re.match(r"^(MES|ES)\b", desc)
                    if m:
                        cid = p.get("conid")
                        mkt = p.get("mktPrice")
                        if cid:
                            self._last_es_conid = int(cid)
                        if mkt and float(mkt) > 5000:
                            self._last_es_price_hint = float(mkt)
                        if m.group(1) == "ES":  # Prefer outright ES over MES
                            break

            if not all_positions:
                time.sleep(1)
                response = self.session.get(
                    f"{self.base_url}/v1/api/portfolio/{account_id}/positions/0",
                    timeout=15,
                )
                if response.status_code == 200:
                    all_positions = response.json()

            return all_positions
        except Exception as e:
            print(f"Error fetching positions for {account_id}: {e}")
            return []

    def save_portfolio_snapshot(self, accounts: List[Dict], positions_map: Dict[str, List[Dict]], path: str = '.portfolio_snapshot.json') -> None:
        """Save accounts and positions to a local JSON snapshot for quick local-mode runs."""
        try:
            payload = {
                'accounts': accounts,
                'positions_map': positions_map,
                'saved_at': time.time()
            }
            with open(path, 'w') as f:
                json.dump(payload, f, default=str, indent=2)
        except Exception as e:
            raise

    def load_portfolio_snapshot(self, path: str = '.portfolio_snapshot.json') -> Tuple[List[Dict], Dict[str, List[Dict]]]:
        """Load accounts and positions from a local JSON snapshot file."""
        if not os.path.exists(path):
            return [], {}
        try:
            with open(path, 'r') as f:
                payload = json.load(f)
            accounts = payload.get('accounts', [])
            positions_map = payload.get('positions_map', {})
            return accounts, positions_map
        except Exception as e:
            print(f"Warning: could not load snapshot {path}: {e}")
            return [], {}
    
    # ---------------------------------------------------------------------------
    # IBKR Client Portal market-data snapshot helpers
    # ---------------------------------------------------------------------------
    # Field reference (Client Portal snapshot API):
    #   31  = last price        84  = bid        86  = ask
    #   7308 = delta            7309 = gamma
    #   7310 = theta            7311 = vega
    #   7633 = implied vol (%)
    # The first call *subscribes* to the feed; data arrives on the second call.
    # ---------------------------------------------------------------------------

    _SNAPSHOT_GREEKS_FIELDS = "31,7308,7309,7310,7311,7633"
    _SNAPSHOT_PRICE_FIELDS  = "31,84,86"
    # Batch size: stay well below the undocumented ~300 conid limit
    _SNAPSHOT_BATCH_SIZE = 50

    def get_market_snapshot(
        self,
        conids: list,
        fields: str | None = None,
        subscribe_sleep: float = 1.0,
    ) -> dict:
        """Fetch a marketdata snapshot for *conids* and return {conid(int): raw_dict}.

        IBKR Client Portal delivers market data immediately if the symbol has
        been recently subscribed.  We make the call directly; if the response
        contains empty items (first-time subscription), we wait ``subscribe_sleep``
        seconds and retry once.  Batches are processed in groups of
        ``_SNAPSHOT_BATCH_SIZE`` to stay within gateway limits.
        """
        if not conids:
            return {}
        fields_str = fields if fields is not None else self._SNAPSHOT_GREEKS_FIELDS
        url = f"{self.base_url}/v1/api/iserver/marketdata/snapshot"
        result: dict = {}
        batch_size = self._SNAPSHOT_BATCH_SIZE
        conid_list = [str(c) for c in conids]

        for i in range(0, len(conid_list), batch_size):
            chunk = conid_list[i : i + batch_size]
            params = {"conids": ",".join(chunk), "fields": fields_str}
            try:
                requested_fields = [f.strip() for f in str(fields_str).split(",") if f.strip()]

                def _has_requested_values(item: dict) -> bool:
                    for fld in requested_fields:
                        if item.get(fld) is not None:
                            return True
                    return False

                # First fetch — usually returns data immediately
                resp = self.session.get(url, params=params, verify=False, timeout=10)
                resp.raise_for_status()
                items = resp.json()

                # Retry once for conids that still have no requested field values
                missing_conids = [
                    str(item.get("conid"))
                    for item in items
                    if item.get("conid") is not None and not _has_requested_values(item)
                ]
                if missing_conids:
                    time.sleep(subscribe_sleep)
                    retry_params = {"conids": ",".join(missing_conids), "fields": fields_str}
                    retry_resp = self.session.get(url, params=retry_params, verify=False, timeout=10)
                    retry_resp.raise_for_status()
                    retry_items = retry_resp.json()
                    retry_by_conid = {
                        int(item.get("conid")): item
                        for item in retry_items
                        if item.get("conid") is not None
                    }
                    merged: list[dict] = []
                    for item in items:
                        cid = item.get("conid")
                        if cid is not None and int(cid) in retry_by_conid and _has_requested_values(retry_by_conid[int(cid)]):
                            merged.append(retry_by_conid[int(cid)])
                        else:
                            merged.append(item)
                    items = merged

                for item in items:
                    cid = item.get("conid")
                    if cid is not None:
                        result[int(cid)] = item
            except Exception as exc:
                logging.warning("IBKR snapshot batch %d failed: %s", i, exc)
                # A single invalid conid can cause a 400 for the whole batch.
                # Retry each conid individually so valid symbols still return data.
                if len(chunk) > 1:
                    for cid in chunk:
                        try:
                            single_resp = self.session.get(
                                url,
                                params={"conids": cid, "fields": fields_str},
                                verify=False,
                                timeout=10,
                            )
                            single_resp.raise_for_status()
                            single_items = single_resp.json()
                            for item in single_items:
                                one_cid = item.get("conid")
                                if one_cid is not None:
                                    result[int(one_cid)] = item
                        except Exception as single_exc:
                            logging.debug(
                                "IBKR snapshot single-conid retry failed for %s: %s",
                                cid,
                                single_exc,
                            )
        return result

    def get_market_greeks_batch(self, conids: list) -> dict:
        """Fetch per-contract Greeks from the IBKR market-data snapshot for *conids*.

        Returns {conid(int): {delta, gamma, theta, vega, iv, last, source}} where
        all numeric values are native floats (or None when unavailable).
        """

        def _f(val) -> float | None:
            if val is None:
                return None
            try:
                return float(str(val).replace("%", "").strip())
            except (ValueError, TypeError):
                return None

        requested_conids = [int(c) for c in conids]
        snapshot = self.get_market_snapshot(requested_conids, fields=self._SNAPSHOT_GREEKS_FIELDS)

        def _has_any_greek(raw: dict) -> bool:
            return any(raw.get(field) is not None for field in ("7308", "7309", "7310", "7311"))

        # Targeted retries for symbols that still have no greek fields.
        # This frequently happens on first subscription pass when the gateway
        # has not warmed quote subscriptions yet.
        missing = [cid for cid in requested_conids if cid not in snapshot or not _has_any_greek(snapshot.get(cid, {}))]
        for attempt in range(1, 4):
            if not missing:
                break
            time.sleep(0.5 * attempt)
            retry_snapshot = self.get_market_snapshot(
                missing,
                fields=self._SNAPSHOT_GREEKS_FIELDS,
                subscribe_sleep=0.6 + 0.4 * attempt,
            )
            for cid, payload in retry_snapshot.items():
                if _has_any_greek(payload):
                    snapshot[cid] = payload
            missing = [cid for cid in requested_conids if cid not in snapshot or not _has_any_greek(snapshot.get(cid, {}))]

        result: dict = {}
        for conid, data in snapshot.items():
            iv_raw = data.get("7633")
            iv: float | None = None
            if iv_raw is not None:
                try:
                    iv = float(str(iv_raw).replace("%", "").strip()) / 100.0
                except (ValueError, TypeError):
                    iv = None
            result[conid] = {
                "delta":  _f(data.get("7308")),
                "gamma":  _f(data.get("7309")),
                "theta":  _f(data.get("7310")),
                "vega":   _f(data.get("7311")),
                "iv":     iv,
                "last":   _f(data.get("31")),
                "source": "ibkr_snapshot",
            }
        return result

    def _lookup_es_conid(self) -> int | None:
        """Find the front-month ES futures conid using IBKR secdef search + info APIs.

        Two-step process:
          1. ``/iserver/secdef/search?symbol=ES&secType=FUT`` → parent conid
          2. ``/iserver/secdef/info?conid=<parent>&sectype=FUT&month=MAR26&exchange=CME``
             → actual contract conid for the front expiry month
        """
        import calendar as _cal
        from datetime import timezone as _tz

        def _front_month_code() -> str:
            """Return IBKR expiry-month code, e.g. 'MAR26', for the front ES contract."""
            now = datetime.now(_tz.utc)
            # ES quarterly cycle: March(3), June(6), Sep(9), Dec(12)
            quarterly = [3, 6, 9, 12]
            month, year = now.month, now.year
            # Third Friday of the candidate month (ES expiry)
            for candidate_month in quarterly:
                candidate_year = year if candidate_month >= month else year + 1
                # find third Friday
                _, days_in_month = _cal.monthrange(candidate_year, candidate_month)
                fridays = [
                    d for d in range(1, days_in_month + 1)
                    if _cal.weekday(candidate_year, candidate_month, d) == 4
                ]
                third_friday = datetime(candidate_year, candidate_month, fridays[2], tzinfo=_tz.utc)
                if third_friday > now:
                    abbr = _cal.month_abbr[candidate_month].upper()
                    return f"{abbr}{str(candidate_year)[2:]}"
            return "MAR26"

        try:
            # Step 1: get parent conid
            r1 = self.session.get(
                f"{self.base_url}/v1/api/iserver/secdef/search",
                params={"symbol": "ES", "secType": "FUT"},
                verify=False,
                timeout=5,
            )
            if r1.status_code != 200:
                return None
            items = r1.json() if isinstance(r1.json(), list) else []
            parent_conid = None
            for item in items:
                sections = item.get("sections") or []
                if any(s.get("secType") == "FUT" for s in sections):
                    parent_conid = item.get("conid")
                    break
            if not parent_conid:
                return None

            # Step 2: get front-month contract conid
            month_code = _front_month_code()
            r2 = self.session.get(
                f"{self.base_url}/v1/api/iserver/secdef/info",
                params={
                    "conid":    parent_conid,
                    "sectype":  "FUT",
                    "month":    month_code,
                    "exchange": "CME",
                },
                verify=False,
                timeout=5,
            )
            if r2.status_code == 200:
                contracts = r2.json() if isinstance(r2.json(), list) else []
                if contracts:
                    cid = contracts[0].get("conid")
                    if cid:
                        self._last_es_conid = int(cid)
                        return int(cid)
        except Exception as exc:
            logging.debug("ES conid lookup failed: %s", exc)
        return None

    def get_es_price_from_ibkr(self, es_conid: int | None = None) -> float | None:
        """Fetch the front-month ES futures price via the market-data snapshot.

        Fast-path: if ``get_positions`` was already called this session the
        mktPrice from that response is returned immediately without an extra
        HTTP round-trip.  Falls back to a snapshot API call if no cached price
        is available.
        """
        # Fast path: use the price already seen in the last positions fetch
        hint = getattr(self, "_last_es_price_hint", None)
        if hint and 5000 < hint < 9000:
            return hint

        conid = es_conid or getattr(self, "_last_es_conid", None)
        if not conid:
            # Last resort: ask IBKR for the front-month ES contract
            conid = self._lookup_es_conid()
        if not conid:
            return None
        try:
            snapshot = self.get_market_snapshot(
                [conid], fields=self._SNAPSHOT_PRICE_FIELDS, subscribe_sleep=1.5
            )
            if conid in snapshot:
                raw = snapshot[conid].get("31")
                if raw is not None:
                    price = float(str(raw).replace(",", "").strip())
                    if 5000 < price < 9000:  # Sanity check for ES range
                        return price
        except Exception as exc:
            logging.debug("IBKR ES price fetch failed: %s", exc)
        return None

    def get_option_greeks(self, conid: str) -> Dict:
        """Fetch Greeks for a single option contract via the IBKR market-data snapshot.

        Falls back to N/A placeholders on any error so callers are not disrupted.
        """
        try:
            batch = self.get_market_greeks_batch([int(conid)])
            if int(conid) in batch:
                g = batch[int(conid)]
                return {
                    "delta":      g.get("delta", "N/A"),
                    "gamma":      g.get("gamma", "N/A"),
                    "theta":      g.get("theta", "N/A"),
                    "vega":       g.get("vega",  "N/A"),
                    "impliedVol": g.get("iv",    "N/A"),
                }
        except Exception:
            pass
        return {"delta": "N/A", "gamma": "N/A", "theta": "N/A", "vega": "N/A", "impliedVol": "N/A"}
    
    async def get_tastytrade_option_greeks(
        self,
        underlying: str,
        expiry: str,
        strike: float,
        option_type: str,
        use_cache: bool = True,
        force_refresh_on_miss: bool = False,
    ) -> Dict[str, Any]:
        """Get option greeks from Tastytrade cache and optionally force live fetch on miss.

        Args:
            underlying: Underlying root/symbol used for lookup.
            expiry: Expiration in YYYY-MM-DD or YYYYMMDD.
            strike: Option strike.
            option_type: call/put.
            use_cache: If False, bypass cache and force a live fetch attempt.
            force_refresh_on_miss: If True, do a live fetch when cache misses.
        """
        try:
            # Defensive skip: if expiry is clearly past today, avoid noise
            try:
                today_norm = datetime.now().strftime('%Y%m%d')
                # Normalize incoming expiry potentially in YYYY-MM-DD or YYYYMMDD
                e = str(expiry).replace('-', '')[:8]
                if e and e < today_norm:
                    return {'delta': 'N/A', 'gamma': 'N/A', 'theta': 'N/A', 'vega': 'N/A', 'impliedVol': 'N/A', 'source': 'skipped'}
            except Exception:
                pass
            def _serialize_option_data(option_data: OptionData, source: str) -> Dict[str, Any]:
                return {
                    'delta': option_data.delta if option_data.delta != 0.0 else 'N/A',
                    'gamma': option_data.gamma if option_data.gamma != 0.0 else 'N/A',
                    'theta': option_data.theta if option_data.theta != 0.0 else 'N/A',
                    'vega': option_data.vega if option_data.vega != 0.0 else 'N/A',
                    'impliedVol': option_data.iv if option_data.iv != 0.0 else 'N/A',
                    'bid': option_data.bid,
                    'ask': option_data.ask,
                    'mid': option_data.mid,
                    'source': source,
                }

            strike_f = float(strike)
            opt_type_norm = (option_type or '').lower()
            exp_norm = self.options_cache._normalize_expiry(expiry)

            option_data = None
            if use_cache:
                option_data = self.options_cache.get_cached_option(underlying, expiry, strike_f, opt_type_norm)

            if option_data:
                return _serialize_option_data(option_data, 'tastytrade_cache')

            should_force_live_fetch = (not use_cache) or force_refresh_on_miss
            if should_force_live_fetch:
                requested = {(expiry, strike_f, opt_type_norm)}
                fetched = await self.options_cache.fetch_and_cache_options_for_underlying(
                    underlying,
                    only_options=requested,
                    force_refresh=True,
                )

                if fetched:
                    for option_data in fetched.values():
                        if (
                            option_data.expiration == exp_norm
                            and abs(float(option_data.strike) - strike_f) < 0.01
                            and option_data.option_type == opt_type_norm
                        ):
                            return _serialize_option_data(option_data, 'tastytrade_live')

                # Fallback to cache after live attempt (fetch may have merged into cache)
                option_data = self.options_cache.get_cached_option(underlying, expiry, strike_f, opt_type_norm)
                if option_data:
                    return _serialize_option_data(option_data, 'tastytrade_live')

                if self.options_cache.last_session_error:
                    return {
                        'delta': 'N/A', 'gamma': 'N/A', 'theta': 'N/A',
                        'vega': 'N/A', 'impliedVol': 'N/A', 'source': 'session_error'
                    }

                return {
                    'delta': 'N/A', 'gamma': 'N/A', 'theta': 'N/A',
                    'vega': 'N/A', 'impliedVol': 'N/A',
                    'source': 'live_miss' if not use_cache else 'cache_and_live_miss'
                }

            else:
                return {
                    'delta': 'N/A', 'gamma': 'N/A', 'theta': 'N/A',
                    'vega': 'N/A', 'impliedVol': 'N/A', 'source': 'cache_miss'
                }
        except Exception:
            return {
                'delta': 'N/A', 'gamma': 'N/A', 'theta': 'N/A',
                'vega': 'N/A', 'impliedVol': 'N/A', 'source': 'error'
            }
    
    def get_spx_price(self) -> float:
        """Get current SPX price.

        Source priority (IBKR-first):
          0. IBKR market-data snapshot for the ES front-month future.
          1. Yahoo Finance ES=F continuous contract.
          2. Yahoo Finance SPY × 10.
          3. Hardcoded market-calibrated estimate (last resort).

        Tastytrade SPY is intentionally skipped — it returns 404 unreliably.
        """
        from datetime import datetime, timedelta
        now = datetime.now()

        # Return cached value if still fresh
        if (self.spx_price is not None and
                self.spx_price_timestamp is not None and
                now < self.spx_price_timestamp + timedelta(minutes=self.spx_price_cache_minutes)):
            return self.spx_price

        def _cache_and_return(price: float, label: str) -> float:
            self.spx_price = price
            self.spx_price_timestamp = now
            print(f"Fetched SPX price from {label}: {price:.2f}")
            return price

        try:
            # ------------------------------------------------------------------
            # Method 0: IBKR market-data snapshot (ES front-month future)
            # ------------------------------------------------------------------
            try:
                price = self.get_es_price_from_ibkr()
                if price and 5000 < price < 9000:
                    return _cache_and_return(price, "IBKR ES snapshot")
            except Exception as e0:
                print(f"IBKR ES snapshot failed ({e0})")

            # ------------------------------------------------------------------
            # Method 1: Yahoo Finance ES=F continuous contract
            # ------------------------------------------------------------------
            try:
                print("Trying Yahoo Finance ES=F ...")
                hist = yf.Ticker("ES=F").history(period="1d", interval="1m")
                if not hist.empty:
                    es_price = float(hist["Close"].iloc[-1])
                    if 5000 < es_price < 9000:
                        return _cache_and_return(es_price, "Yahoo Finance ES=F")
                    raise ValueError(f"ES price out of range: {es_price}")
                raise ValueError("No ES=F data available")
            except Exception as e1:
                print(f"Yahoo Finance ES=F failed ({e1})")

            # ------------------------------------------------------------------
            # Method 2: Yahoo Finance SPY × 10
            # ------------------------------------------------------------------
            try:
                print("Trying Yahoo Finance SPY ×10 ...")
                hist = yf.Ticker("SPY").history(period="1d", interval="1m")
                if not hist.empty:
                    spy_price = float(hist["Close"].iloc[-1])
                    if 400 < spy_price < 900:
                        return _cache_and_return(spy_price * 10, "Yahoo Finance SPY×10")
                    raise ValueError(f"SPY price out of range: {spy_price}")
                raise ValueError("No SPY data available")
            except Exception as e2:
                print(f"Yahoo Finance SPY failed ({e2})")

            # ------------------------------------------------------------------
            # Method 3: Hardcoded estimate (last resort)
            # ------------------------------------------------------------------
            print("All real-time sources failed — using hardcoded estimate 6475.0")
            return _cache_and_return(6475.0, "hardcoded estimate")

        except Exception as exc:
            print(f"Warning: Could not fetch SPX price, using default: {exc}")
            self.spx_price = 6475.0
            self.spx_price_timestamp = now
            return self.spx_price

    
    def calculate_spx_weighted_delta(self, symbol: str, position_qty: float, price: float, 
                                   underlying_delta: float = 1.0, multiplier: float = 1.0) -> float:
        """Calculate SPX-weighted delta for a position.
        
        Formula: SPX_eq_delta = underlying_delta * quantity * beta * (price / SPX_price) * multiplier
        
        Args:
            symbol: The symbol (e.g., 'AAPL', 'ES', 'SPY')
            position_qty: Position quantity
            price: Current price of the instrument
            underlying_delta: Delta of the underlying (1.0 for stocks, actual delta for options)
            multiplier: Contract multiplier from IBKR position data
        
        Returns:
            SPX-weighted delta
        """
        try:
            beta = self.beta_config.get_beta(symbol)
            spx_price = self.get_spx_price()
            
            spx_weighted_delta = underlying_delta * position_qty * beta * (price / spx_price) * multiplier
            return spx_weighted_delta
            
        except Exception as e:
            print(f"Error calculating SPX weighted delta for {symbol}: {e}")
            return 0.0
    
    def summarize_by_instrument(self, positions_map: Dict[str, List[Dict]]) -> List[Dict[str, Any]]:
        """Aggregate portfolio by instrument (underlying) across all accounts.

        Returns a list of dicts with keys:
          symbol, qty, mkt_value, pnl, positions, net_delta, spx_delta, theta
        """
        # Use contract multipliers from IBKR position data
        rollup: Dict[str, Dict[str, Any]] = {}

        for account_id, positions in positions_map.items():
            for p in positions:
                is_opt = self.is_option_contract(p)

                if is_opt:
                    underlying, expiry_str, strike_f, option_type, _ = self._extract_option_details(p)
                    sym = self._normalize_symbol(underlying)
                else:
                    raw_sym = (p.get('ticker') or p.get('contractDesc') or '')
                    sym = self._normalize_symbol(raw_sym)

                # Parse numeric fields safely
                try:
                    qty = float(p.get('position', 0))
                except Exception:
                    qty = 0.0
                try:
                    mv = float(p.get('mktValue', 0.0))
                except Exception:
                    mv = 0.0
                try:
                    pnl = float(p.get('unrealizedPnl', 0.0))
                except Exception:
                    pnl = 0.0

                # Initialize/accumulate bucket
                b = rollup.setdefault(sym, {
                    'symbol': sym,
                    'qty': 0.0,
                    'mkt_value': 0.0,
                    'pnl': 0.0,
                    'positions': 0,
                    'net_delta': 0.0,
                    'spx_delta': 0.0,
                    'theta': 0.0,
                })

                b['qty'] += qty
                b['mkt_value'] += mv
                b['pnl'] += pnl
                b['positions'] += 1

                # Get multiplier from contract data; fall back to sensible defaults
                ibkr_multiplier = p.get('multiplier', None)
                try:
                    ibkr_multiplier = float(ibkr_multiplier) if ibkr_multiplier not in (None, '') else 0.0
                except Exception:
                    ibkr_multiplier = 0.0

                # If IBKR didn't supply a multiplier, use configured defaults
                if not ibkr_multiplier:
                    ibkr_multiplier = (
                        self.beta_config.default_option_multiplier
                        if is_opt
                        else self.beta_config.default_stock_multiplier
                    )
                
                # Determine price
                price = p.get('mktPrice') or p.get('avgCost') or 0.0
                try:
                    price = float(price) if price is not None else 0.0
                except Exception:
                    price = 0.0

                if is_opt:
                    # Use cached Tastytrade greeks only (no network calls here)
                    od = self.options_cache.get_cached_option(sym, expiry_str, strike_f, option_type)
                    if od and od.delta not in (None, 0.0) and qty != 0:
                        try:
                            pos_delta = float(od.delta) * qty
                            b['net_delta'] += pos_delta
                            # SPX-weighted delta: use per-contract delta and quantity separately
                            spx_d = self.calculate_spx_weighted_delta(
                                symbol=sym,
                                position_qty=qty,
                                price=price if price else (strike_f or 0.0),
                                underlying_delta=float(od.delta),
                                multiplier=ibkr_multiplier
                            )
                            b['spx_delta'] += spx_d
                        except Exception:
                            pass
                    # Theta
                    if od and od.theta not in (None, 0.0) and qty != 0:
                        try:
                            b['theta'] += float(od.theta) * qty
                        except Exception:
                            pass
                else:
                    # Non-option: handle futures vs stocks/ETFs differently
                    asset_class = p.get('assetClass', '').upper()
                    
                    if asset_class == 'FUT' or sym in ('ES', 'MES', 'NQ', 'RTY'):
                        # Futures contracts: delta = 1.0 per contract (they directly track the underlying)
                        try:
                            b['net_delta'] += qty  # Each futures contract has delta = 1.0
                        except Exception:
                            pass
                        
                        # SPX-weighted delta for futures
                        try:
                            spx_d = self.calculate_spx_weighted_delta(
                                symbol=sym,
                                position_qty=qty,
                                price=price,
                                underlying_delta=1.0,  # Futures have delta = 1.0
                                multiplier=ibkr_multiplier if ibkr_multiplier else 1.0
                            )
                            b['spx_delta'] += spx_d
                        except Exception:
                            pass
                    else:
                        # Stocks/ETFs: delta approximation using small multiplier
                        if ibkr_multiplier > 0:
                            # Use contract multiplier for delta calculation
                            try:
                                b['net_delta'] += ibkr_multiplier * qty * 0.01  # 0.01 as base delta per unit
                            except Exception:
                                pass
                        else:
                            # Fallback for contracts without multiplier (stocks/ETFs typically have 0.0)
                            try:
                                b['net_delta'] += 0.01 * qty  # Basic stock delta approximation
                            except Exception:
                                pass
                        
                        # SPX-weighted delta for stocks/ETFs
                        try:
                            spx_d = self.calculate_spx_weighted_delta(
                                symbol=sym,
                                position_qty=qty,
                                price=price,
                                underlying_delta=1.0,
                                multiplier=ibkr_multiplier if ibkr_multiplier else 1.0
                            )
                            b['spx_delta'] += spx_d
                        except Exception:
                            pass

        # Convert to list and sort by absolute SPX exposure desc, then by symbol
        items = list(rollup.values())
        items.sort(key=lambda x: (-(abs(x['spx_delta'])), x['symbol']))
        return items

    def print_summary_by_instrument(self, positions_map: Dict[str, List[Dict]]):
        rows = self.summarize_by_instrument(positions_map)
        if not rows:
            print("No positions found.")
            return
        print("\n=== Summary per Instrument (across all accounts) ===")
        print(f"{'Symbol':<10} {'Net Qty':>10} {'Mkt Value':>15} {'Unrl. PnL':>12} {'Net Δ':>12} {'SPX Δ':>12} {'Θ':>10} {'Pos':>6}")
        print("-" * 92)
        for r in rows:
            print(f"{r['symbol']:<10} {r['qty']:>10.2f} {r['mkt_value']:>15.2f} {r['pnl']:>12.2f} {r['net_delta']:>12.3f} {r['spx_delta']:>12.3f} {r['theta']:>10.3f} {int(r['positions']):>6}")

    
    def print_portfolio_spx_summary(self, account_summaries: List[Dict]):
        """Print a comprehensive SPX-weighted delta summary for all accounts."""
        print("\n" + "="*80)
        print("PORTFOLIO SPX-WEIGHTED DELTA SUMMARY")
        print("="*80)
        
        # Display current SPX price used for calculations
        spx_price = self.get_spx_price()
        print(f"SPX Price Used: {spx_price:.2f}")
        print("-" * 80)
        
        total_stock_spx_delta = 0.0
        total_options_spx_delta = 0.0
        total_options_theta = 0.0
        
        for summary in account_summaries:
            account_id = summary['account_id']
            stock_spx_delta = summary['stock_spx_delta']
            options_spx_delta = summary['options_spx_delta']
            options_theta = summary.get('options_theta', 0.0)
            
            print(f"Account {account_id}:")
            print(f"  Stock SPX Δ:     {stock_spx_delta:>8.3f}")
            print(f"  Options SPX Δ:   {options_spx_delta:>8.3f}")
            print(f"  Options Θ:       {options_theta:>8.3f}")
            print(f"  Total SPX Δ:     {stock_spx_delta + options_spx_delta:>8.3f}")
            print(f"  Positions:       {summary['stock_count']} stocks, {summary['options_count']} options")
            print()
            
            total_stock_spx_delta += stock_spx_delta
            total_options_spx_delta += options_spx_delta
            total_options_theta += options_theta
        
        print(f"PORTFOLIO TOTALS:")
        print(f"  Stock SPX Δ:     {total_stock_spx_delta:>8.3f}")
        print(f"  Options SPX Δ:   {total_options_spx_delta:>8.3f}")
        print(f"  Options Θ:       {total_options_theta:>8.3f}")
        print(f"  TOTAL SPX Δ:     {total_stock_spx_delta + total_options_spx_delta:>8.3f}")
        print("="*80)

    def is_option_contract(self, position: Dict) -> bool:
        """Check if a position is an options contract."""
        # Check various indicators that this is an option
        contract_desc = position.get('contractDesc', '').upper()
        asset_class = position.get('assetClass', '').upper()
        
        # Primary check: asset class explicitly marks options
        if asset_class in {'OPT', 'FOP'}:
            return True
        
        # Secondary check: contract description mentions CALL/PUT
        option_indicators = ['CALL', 'PUT', 'OPTION']
        if any(indicator in contract_desc for indicator in option_indicators):
            return True

        # Check explicit right flag for calls/puts (single-letter 'C'/'P')
        if (position.get('right') or '').upper() in ('C', 'P'):
            return True

        # Some option records from IBKR may not include the word 'CALL'/'PUT' in contractDesc
        # but will include strike/expiry/right fields. Only consider numeric strike > 0 as option indicator
        for strike_key in ('strike', 'strikePrice', 'strikePx'):
            if strike_key in position:
                strike_val = position.get(strike_key)
                try:
                    # Convert to float and check if it's > 0
                    strike_num = float(strike_val) if strike_val not in (None, '') else 0
                    if strike_num > 0:
                        return True
                except (ValueError, TypeError):
                    # If can't convert to number, skip this indicator
                    continue

        return False
    
    def _extract_option_details(self, position: Dict) -> tuple:
        """Extract consistent option details (underlying, expiry, strike, option_type) from position."""
        # Extract underlying symbol with robust fallback for empty/missing fields (common in FOP records)
        asset_class = position.get('assetClass', '').upper()
        if asset_class in {'OPT', 'FOP'}:
            underlying = (
                str(position.get('undSym') or '').strip()
                or str(position.get('ticker') or '').strip()
            )
        else:
            underlying = str(position.get('ticker') or '').strip()

        if not underlying:
            contract_desc = str(position.get('contractDesc') or '').strip()
            match = re.match(r'^([A-Z]{1,5})\b', contract_desc)
            if match:
                underlying = match.group(1)
            else:
                underlying = contract_desc
        
        # Extract expiry
        expiry = position.get('expiry') or position.get('expiryDate') or position.get('lastTradingDay') or position.get('expiration') or ''
        if isinstance(expiry, (int, float)):
            try:
                expiry = time.strftime('%Y-%m-%d', time.localtime(expiry/1000))
            except Exception:
                expiry = str(expiry)
        expiry_str = str(expiry)

        # FOP contracts: IBKR Client Portal API always returns expiry=None for
        # futures options.  All contract info lives in contractDesc, e.g.
        # "ES     FEB2026 6615 P (EW3)".  Extract YYYYMM when expiry is empty.
        if not expiry_str.strip():
            _fop_desc = str(position.get('contractDesc') or '').upper()
            _m = re.search(r'\b([A-Z]{3})(\d{4})\b', _fop_desc)
            if _m:
                _month_map = {
                    'JAN': '01', 'FEB': '02', 'MAR': '03', 'APR': '04',
                    'MAY': '05', 'JUN': '06', 'JUL': '07', 'AUG': '08',
                    'SEP': '09', 'OCT': '10', 'NOV': '11', 'DEC': '12',
                }
                _mn = _month_map.get(_m.group(1), '')
                if _mn:
                    expiry_str = f"{_m.group(2)}{_mn}"  # YYYYMM

        # Extract strike
        contract_desc = str(position.get('contractDesc') or '').strip()
        strike = position.get('strike') or position.get('strikePrice') or position.get('strikePx') or 0
        try:
            strike_f = float(strike)
        except Exception:
            strike_f = 0.0
        if strike_f <= 0.0 and contract_desc:
            strike_match = re.search(r'\b(\d+(?:\.\d+)?)\s+[CP]\b', contract_desc.upper())
            if strike_match:
                try:
                    strike_f = float(strike_match.group(1))
                except Exception:
                    pass
        
        # Extract option type - check all possible fields
        right = position.get('right') or position.get('optionType') or position.get('putOrCall') or ''
        right = (right or '').upper()
        if right in ['C', 'CALL']:
            option_type = 'call'
            side = 'C'
        elif right in ['P', 'PUT']:
            option_type = 'put'
            side = 'P'
        else:
            desc = contract_desc.upper()
            if 'CALL' in desc:
                option_type = 'call'
                side = 'C'
            elif 'PUT' in desc:
                option_type = 'put'
                side = 'P'
            else:
                side_match = re.search(r'\b([CP])\b(?:\s*\([^)]*\))?\s*$', desc)
                if side_match:
                    side = side_match.group(1)
                    option_type = 'call' if side == 'C' else 'put'
                else:
                    option_type = 'call'  # default
                    side = '?'
        
        return underlying, expiry_str, strike_f, option_type, side
    
    async def print_positions_async(self, account_id: str, positions: List[Dict]):
        """Print position details in a formatted way with async Tastytrade Greeks."""
        if not positions:
            print(f"\nNo positions found for account {account_id}")
            return
        
        print(f"\n=== Positions for Account {account_id} ===")
        
        # Separate options and non-options positions
        option_positions = []
        regular_positions = []
        
        for position in positions:
            if self.is_option_contract(position):
                option_positions.append(position)
            else:
                regular_positions.append(position)
        
        # Initialize SPX delta tracking
        stock_spx_delta_total = 0.0
        options_spx_delta_total = 0.0
        
        # Initialize theta tracking
        options_theta_total = 0.0
        
        # ... (keep existing regular positions logic unchanged)
        # Compute stock deltas using contract multipliers
        stock_total_delta = 0.0
        for p in regular_positions:
            try:
                qty = float(p.get('position', 0))
            except Exception:
                qty = 0.0
            
            # Get multiplier from contract data
            ibkr_multiplier = p.get('multiplier', 0.0)
            try:
                ibkr_multiplier = float(ibkr_multiplier) if ibkr_multiplier else 0.0
            except Exception:
                ibkr_multiplier = 0.0
            
            # Calculate delta using contract multiplier
            if ibkr_multiplier > 0:
                stock_total_delta += ibkr_multiplier * qty * 0.01  # 0.01 as base delta per unit
            else:
                stock_total_delta += 0.01 * qty  # Basic stock delta approximation

        # Build stock summary
        stock_summ_items = []
        for p in regular_positions:
            sym = p.get('ticker') or p.get('contractDesc') or 'N/A'
            try:
                qty = float(p.get('position', 0))
            except Exception:
                qty = p.get('position', 0)
            
            # Get multiplier from contract data for delta calculation
            ibkr_multiplier = p.get('multiplier', 0.0)
            try:
                ibkr_multiplier = float(ibkr_multiplier) if ibkr_multiplier else 0.0
            except Exception:
                ibkr_multiplier = 0.0
                
            if ibkr_multiplier > 0:
                delta_multiplier = ibkr_multiplier * 0.01  # 0.01 as base delta per unit
            else:
                delta_multiplier = 0.01  # Basic stock delta approximation
                
            try:
                delta_item = delta_multiplier * float(qty)
            except Exception:
                delta_item = 0.0
            stock_summ_items.append(f"{sym}:{qty} (Δ={delta_item:.2f})")

        # Fetch Tastytrade Greeks for all option positions concurrently
        option_greeks_data = {}
        if option_positions and TASTYTRADE_SDK_AVAILABLE:
            # Apply skip rules: expired options or zero market value to reduce noise
            filtered_positions = []
            skipped_count = 0
            today_norm = datetime.now().strftime('%Y-%m-%d')
            for position in option_positions:
                expiry_raw = position.get('expiry') or position.get('expiryDate') or position.get('lastTradingDay') or position.get('expiration') or ''
                # Normalize to YYYY-MM-DD for comparison if possible
                try:
                    exp_str = str(expiry_raw)
                    if isinstance(expiry_raw, (int, float)) and len(str(int(expiry_raw))) > 8:
                        exp_str = time.strftime('%Y-%m-%d', time.localtime(float(expiry_raw)/1000))
                except Exception:
                    exp_str = str(expiry_raw)

                mkt_value = position.get('mktValue', None)
                zero_value = False
                try:
                    zero_value = (mkt_value is not None) and (float(mkt_value) == 0.0)
                except Exception:
                    zero_value = False

                is_expired = False
                try:
                    # treat strictly earlier than today as expired
                    is_expired = (exp_str and exp_str < today_norm)
                except Exception:
                    is_expired = False

                if is_expired or zero_value:
                    # Mark as skipped in greeks data
                    underlying, expiry_str, strike_f, option_type, _ = self._extract_option_details(position)
                    option_key = f"{underlying}_{expiry_str}_{strike_f:.2f}_{option_type}"
                    option_greeks_data[option_key] = {'delta': 'N/A', 'theta': 'N/A', 'source': 'skipped'}
                    skipped_count += 1
                else:
                    filtered_positions.append(position)

            if filtered_positions:
                print(f"Fetching Greeks for {len(filtered_positions)} option positions from Tastytrade... (skipped {skipped_count})")
                # Create tasks for remaining option positions
                tasks = []
                for position in filtered_positions:
                    underlying, expiry_str, strike_f, option_type, _ = self._extract_option_details(position)
                    option_key = f"{underlying}_{expiry_str}_{strike_f:.2f}_{option_type}"
                    task = self.get_tastytrade_option_greeks(underlying, expiry_str, strike_f, option_type)
                    tasks.append((option_key, task))

                if tasks:
                    try:
                        import asyncio
                        results = await asyncio.gather(*[task for _, task in tasks], return_exceptions=True)
                        for i, (option_key, _) in enumerate(tasks):
                            if i < len(results) and not isinstance(results[i], Exception):
                                option_greeks_data[option_key] = results[i]
                            else:
                                option_greeks_data[option_key] = {'delta': 'N/A', 'theta': 'N/A', 'source': 'timeout'}
                        print(f"Successfully fetched Greeks for {len([r for r in results if not isinstance(r, Exception)])} options")
                    except Exception as e:
                        print(f"Error fetching Greeks: {e}")
            else:
                if skipped_count:
                    print(f"Greeks fetch skipped for {skipped_count} options (expired or zero market value)")

        # Build options summary with real delta values where available
        option_total_delta = 0.0
        option_summ_items = []
        for p in option_positions:
            # Use consistent option details extraction
            underlying, expiry_str, strike_f, option_type, side = self._extract_option_details(p)
            qty = p.get('position', 0)
            
            # Get delta from Tastytrade Greeks if available
            option_key = f"{underlying}_{expiry_str}_{strike_f:.2f}_{option_type}"
            
            delta_val = 0.0
            if option_key in option_greeks_data:
                greeks = option_greeks_data[option_key]
                delta_raw = greeks.get('delta', 'N/A')
                if delta_raw not in ('N/A', '', None):
                    try:
                        delta_val = float(delta_raw) * float(qty)
                        option_total_delta += delta_val
                    except Exception:
                        pass
            
            option_summ_items.append(f"{underlying} {expiry_str} {strike_f} {side} x{qty} (Δ={delta_val:.2f})")

        # Print compact summaries  
        def _compact_join(items, limit=12):
            if not items:
                return ''
            shown = items[:limit]
            s = ', '.join(shown)
            if len(items) > limit:
                s += f', ...(+{len(items)-limit} more)'
            return s

        print(f"\nAccount {account_id}: {len(regular_positions)} stock/ETF/futures, {len(option_positions)} option positions")
        if stock_summ_items:
            print(f"  Stocks: {_compact_join(stock_summ_items)}")
        if option_summ_items:
            print(f"  Options: {_compact_join(option_summ_items)}")

        # Print regular positions (with SPX-weighted delta)
        if regular_positions:
            print(f"\n--- Stock/ETF/Futures Positions ---")
            print(f"{'Symbol':<15} {'Position':<12} {'Avg Cost':<12} {'Market Value':<15} {'P&L':<12} {'Delta':<8} {'SPX Δ':<8}")
            print("-" * 98)
            
            for position in regular_positions:
                symbol = position.get('ticker', position.get('contractDesc', 'N/A'))
                pos_qty = position.get('position', 0)
                try:
                    pos_qty_f = float(pos_qty)
                except Exception:
                    pos_qty_f = 0.0
                avg_cost = position.get('avgCost', 0)
                market_value = position.get('mktValue', 0)
                mkt_price = position.get('mktPrice', 0)
                unrealized_pnl = position.get('unrealizedPnl', 0)
                try:
                    # Get IBKR multiplier from position data
                    ibkr_multiplier = position.get('multiplier', 0.0)
                    try:
                        ibkr_multiplier = float(ibkr_multiplier) if ibkr_multiplier else 0.0
                    except Exception:
                        ibkr_multiplier = 0.0
                    
                    # Calculate delta using contract multiplier
                    if ibkr_multiplier > 0:
                        delta_val = ibkr_multiplier * pos_qty_f * 0.01  # 0.01 as base delta per unit
                    else:
                        delta_val = 0.01 * pos_qty_f  # Basic stock delta approximation
                    
                    raw_sym = (position.get('ticker') or position.get('contractDesc') or '')
                    norm_sym = str(raw_sym).strip().split()[0].upper() if raw_sym else ''
                    
                    # Use contract multiplier for SPX calculation, fallback to 1.0 for stocks
                    spx_multiplier = ibkr_multiplier if ibkr_multiplier > 0 else 1.0
                    
                    # Calculate SPX-weighted delta
                    spx_delta = self.calculate_spx_weighted_delta(
                        symbol=norm_sym,
                        position_qty=pos_qty_f,
                        price=mkt_price if mkt_price else avg_cost,
                        underlying_delta=1.0,  # For stocks, delta = 1
                        multiplier=spx_multiplier
                    )
                    
                    # Debug calculation for key positions
                    if norm_sym in ['VOO', 'ITOT']:
                        beta = self.beta_config.get_beta(norm_sym)
                        spx_price = self.get_spx_price()
                        price_used = mkt_price if mkt_price else avg_cost
                        print(f"DEBUG {norm_sym}: qty={pos_qty_f:.3f}, price={price_used:.2f}, beta={beta:.3f}, spx={spx_price:.2f}, spx_delta={spx_delta:.3f}")
                    
                    stock_spx_delta_total += spx_delta
                except Exception:
                    delta_val = 0.0
                    spx_delta = 0.0
                    stock_spx_delta_total += spx_delta
                
                print(f"{symbol:<15} {pos_qty_f:<12.2f} {avg_cost:<12.2f} {market_value:<15.2f} {unrealized_pnl:<12.2f} {delta_val:<8.2f} {spx_delta:<8.2f}")

            print(f"\nAccount {account_id} stock net delta: {stock_total_delta:.3f}")
            print(f"Account {account_id} stock SPX delta: {stock_spx_delta_total:.3f}")
        
        # Print options positions with enhanced Greeks from Tastytrade
        if option_positions:
            print(f"\n--- Options Positions (with Tastytrade Greeks) ---")
            print(f"{'Symbol':<12} {'Side':<5} {'Expiry':<12} {'Strike':<10} {'Pos':<8} {'Avg Cost':<10} {'Mkt Val':<12} {'P&L':<10} {'Delta':<8} {'Theta':<8} {'SPX Δ':<8} {'Source':<8}")
            print("-" * 138)

            for position in option_positions:
                # Extract option details using consistent helper method
                underlying, expiry_str, strike_f, option_type, side = self._extract_option_details(position)
                
                # Basic display fields
                pos_qty = position.get('position', 0)
                avg_cost = position.get('avgCost', 0)
                market_value = position.get('mktValue', 0)
                mkt_price = position.get('mktPrice', 0)
                unrealized_pnl = position.get('unrealizedPnl', 0)

                # Get Greeks from our fetched data using consistent key creation
                option_key = f"{underlying}_{expiry_str}_{strike_f:.2f}_{option_type}"
                
                if option_key in option_greeks_data:
                    greeks = option_greeks_data[option_key]
                    delta = greeks.get('delta', 'N/A')
                    theta = greeks.get('theta', 'N/A')
                    source = greeks.get('source', 'tastytrade')
                else:
                    # Cache miss - try to fetch directly from cache if available
                    cached_option = self.options_cache.get_cached_option(underlying, expiry_str, strike_f, option_type)
                    if cached_option:
                        delta = cached_option.delta if cached_option.delta != 0.0 else 'N/A'
                        theta = cached_option.theta if cached_option.theta != 0.0 else 'N/A'
                        source = 'cache_direct'
                    else:
                        delta = 'N/A'
                        theta = 'N/A'
                        source = 'cache_miss'

                # Format values
                try:
                    delta_str = f"{float(delta):.3f}" if delta not in (None, 'N/A', '') else 'N/A'
                    # Scale by position quantity
                    if delta not in (None, 'N/A', '') and pos_qty != 0:
                        delta_value = float(delta) * float(pos_qty)
                        delta_str = f"{delta_value:.3f}"
                        
                        # Get IBKR multiplier from position data
                        ibkr_multiplier = position.get('multiplier', 100.0)  # Default 100 for options
                        
                        # Calculate SPX-weighted delta for options
                        spx_delta = self.calculate_spx_weighted_delta(
                            symbol=underlying,
                            position_qty=pos_qty,
                            price=mkt_price if mkt_price else strike_f,
                            underlying_delta=delta_value,  # Use the position-scaled delta
                            multiplier=ibkr_multiplier
                        )
                        spx_delta_str = f"{spx_delta:.3f}"
                        
                        # Accumulate SPX delta for options
                        try:
                            options_spx_delta_total += spx_delta
                        except Exception:
                            pass
                    else:
                        spx_delta_str = 'N/A'
                except Exception:
                    delta_str = 'N/A'
                    spx_delta_str = 'N/A'
                
                try:
                    theta_str = f"{float(theta):.3f}" if theta not in (None, 'N/A', '') else 'N/A'
                    # Scale by position quantity  
                    if theta not in (None, 'N/A', '') and pos_qty != 0:
                        theta_value = float(theta) * float(pos_qty)
                        theta_str = f"{theta_value:.3f}"
                        
                        # Accumulate theta for account total
                        options_theta_total += theta_value
                except Exception:
                    theta_str = 'N/A'

                strike_str = f"{strike_f:.2f}" if strike_f > 0 else ''

                print(f"{underlying:<12} {side:<5} {expiry_str:<12} {strike_str:<10} {pos_qty:<8.2f} {avg_cost:<10.2f} {market_value:<12.2f} {unrealized_pnl:<10.2f} {delta_str:<8} {theta_str:<8} {spx_delta_str:<8} {source:<8}")
            
            # Print portfolio-level options delta summary
            print(f"\nAccount {account_id} options net delta: {option_total_delta:.3f}")
            print(f"Account {account_id} options net theta: {options_theta_total:.3f}")
            print(f"Account {account_id} options SPX delta: {options_spx_delta_total:.3f}")
            print(f"Combined portfolio delta: {stock_total_delta + option_total_delta:.3f}")
            print(f"Combined portfolio SPX delta: {stock_spx_delta_total + options_spx_delta_total:.3f}")
            
            # Show cache stats
            cache_stats = self.options_cache.get_cache_stats()
            print(f"\nCache stats: {cache_stats['total_options_cached']} options cached, {cache_stats['total_cache_entries']} symbols, {cache_stats['default_expiry_minutes']}min TTL")
            if cache_stats['no_options_symbols'] > 0:
                print(f"Symbols without options ({cache_stats['no_options_symbols']}): {', '.join(cache_stats['no_options_list'][:10])}{' ...' if len(cache_stats['no_options_list']) > 10 else ''}")
        
        # Return summary data for portfolio reporting
        return {
            'account_id': account_id,
            'stock_spx_delta': stock_spx_delta_total,
            'options_spx_delta': options_spx_delta_total,
            'stock_delta': stock_total_delta,
            'options_delta': option_total_delta,
            'options_theta': options_theta_total,
            'stock_count': len(regular_positions),
            'options_count': len([p for p in option_positions if p.get('position', 0) != 0])
        }
    
    async def print_options_summary_async(self, account_id: str, positions: List[Dict]):
        """Print a summary of options Greeks for the portfolio using cached Tastytrade data."""
        option_positions = [pos for pos in positions if self.is_option_contract(pos)]
        
        if not option_positions:
            return
            
        print(f"\n=== Options Greeks Summary for Account {account_id} ===")
        total_delta = 0.0
        total_theta = 0.0
        valid_greeks_count = 0

        # breakdown by side
        delta_call = 0.0
        delta_put = 0.0
        theta_call = 0.0
        theta_put = 0.0

        # Get cached Greeks data for all positions
        for position in option_positions:
            pos_qty = position.get('position', 0)
            if pos_qty == 0:
                continue
                
            # Extract option details - use undSym for option contracts
            if position.get('assetClass', '').upper() == 'OPT':
                symbol = position.get('undSym', position.get('ticker', position.get('contractDesc', '')))
            else:
                symbol = position.get('ticker', position.get('contractDesc', ''))
                
            expiry = position.get('expiry') or position.get('expiryDate') or position.get('lastTradingDay') or position.get('expiration') or ''
            if isinstance(expiry, (int, float)):
                try:
                    expiry = time.strftime('%Y-%m-%d', time.localtime(expiry/1000))
                except Exception:
                    expiry = str(expiry)
            
            strike = position.get('strike') or position.get('strikePrice') or position.get('strikePx') or 0
            try:
                strike_f = float(strike)
            except Exception:
                strike_f = 0.0
            
            right = (position.get('right') or '').upper()
            if right in ['C', 'CALL']:
                option_type = 'call'
            elif right in ['P', 'PUT']:
                option_type = 'put'
            else:
                desc = (position.get('contractDesc') or '').upper()
                option_type = 'call' if 'CALL' in desc else ('put' if 'PUT' in desc else 'call')

            # Try to get cached Greeks
            try:
                greeks = await self.get_tastytrade_option_greeks(symbol, expiry, strike_f, option_type)
                
                delta = greeks.get('delta')
                theta = greeks.get('theta')

                if delta not in (None, 'N/A', ''):
                    try:
                        d = float(delta) * pos_qty
                        total_delta += d
                        if option_type == 'call':
                            delta_call += d
                        elif option_type == 'put':
                            delta_put += d
                        valid_greeks_count += 1
                    except (ValueError, TypeError):
                        pass

                if theta not in (None, 'N/A', ''):
                    try:
                        t = float(theta) * pos_qty
                        total_theta += t
                        if option_type == 'call':
                            theta_call += t
                        elif option_type == 'put':
                            theta_put += t
                    except (ValueError, TypeError):
                        pass
                        
            except Exception as e:
                print(f"Error getting Greeks for {symbol}: {e}")
                continue

        print(f"\n=== Options Greeks Summary for Account {account_id} ===")
        if valid_greeks_count > 0:
            print(f"Portfolio Net Delta: {total_delta:.3f} (Calls: {delta_call:.3f}, Puts: {delta_put:.3f})")
            print(f"Portfolio Net Theta: {total_theta:.3f} (Calls: {theta_call:.3f}, Puts: {theta_put:.3f})")
            print(f"Options positions analyzed: {len(option_positions)} (Greeks available: {valid_greeks_count})")
        else:
            print("No valid Greeks data available for options positions; showing contract breakdown")
            calls = sum(1 for p in option_positions if ((p.get('right') or '').upper().startswith('C') or 'CALL' in (p.get('contractDesc') or '').upper()))
            puts = len(option_positions) - calls
            print(f"Options count: {len(option_positions)} (Calls: {calls}, Puts: {puts})")

async def main_async():
    """Main async script execution with Tastytrade integration."""
    # Load environment variables
    load_dotenv()

    if str(os.getenv("STREAMING_INGESTION_ENABLED", "0")).strip().lower() in {"1", "true", "yes", "on"}:
        await initialize_streaming_runtime()
    
    parser = argparse.ArgumentParser()
    parser.add_argument('--skip-greeks', action='store_true', help='Skip Greeks lookup to speed up output')
    parser.add_argument('--cache-minutes', type=int, default=5, help='Cache expiry time in minutes (default: 5)')
    parser.add_argument('--enable-external', action='store_true', help='Enable external data sources (tastyworks / Yahoo). Disabled by default for IBKR-only operation')
    parser.add_argument('--force-refresh', action='store_true', help='Force refresh of options cache for symbols in this run')
    parser.add_argument('--dry-run', action='store_true', help='Dry-run: simulate prefetch and use cached values without external API calls')
    parser.add_argument('--local-mode', action='store_true', help='Quick local mode: run without contacting IBKR gateway; use snapshot if available')
    parser.add_argument('--save-portfolio', action='store_true', help='After fetching live portfolio, save a snapshot to --snapshot-file for future local runs')
    parser.add_argument('--snapshot-file', type=str, default='.portfolio_snapshot.json', help='Path to portfolio snapshot file used by local-mode')
    args = parser.parse_args()

    client = IBKRClient(cache_expiry_minutes=args.cache_minutes)
    
    # Set simulation mode flag for SPX price handling
    client._simulation_mode = args.dry_run or args.local_mode
    
    # respect CLI flag to enable external data lookups
    global USE_EXTERNAL
    USE_EXTERNAL = bool(args.enable_external)
    
    # Default to live IBKR; fall back to snapshot if unavailable. Local-mode forces snapshot.
    accounts = []
    positions_map = {}

    snapshot_path = args.snapshot_file
    used_snapshot = False

    if not args.local_mode:
        try:
            print("Attempting to fetch live portfolio from IBKR...")
            # Step 1: Check if gateway is running
            if not client.check_gateway_status():
                print("Gateway is not running. Starting it now...")
                if not client.start_gateway():
                    raise RuntimeError("Failed to start gateway")
            else:
                print("Gateway is already running!")

            # Step 2: Check authentication status
            auth_status = client.check_auth_status()
            print(f"Authentication status: {auth_status}")

            if not auth_status.get("authenticated", False):
                print("\nNot authenticated. Please log in via the web interface.")
                print(f"Open: {client.base_url}")
                # Wait for user to authenticate
                if not client.wait_for_authentication():
                    raise RuntimeError("Authentication timed out")
            else:
                print("Already authenticated!")

            # Step 3: Fetch accounts and positions
            print("\nFetching portfolio accounts...")
            accounts = client.get_accounts()
            if not accounts:
                raise RuntimeError("No accounts returned from IBKR")

            positions_map = {}
            for account in accounts:
                account_id = account.get('accountId', account.get('id'))
                if account_id:
                    positions_map[account_id] = client.get_positions(account_id)

            # Always save snapshot on successful live fetch
            try:
                client.save_portfolio_snapshot(accounts, positions_map, snapshot_path)
                print(f"Saved portfolio snapshot to {snapshot_path}")
            except Exception as e:
                print(f"Warning: could not save snapshot: {e}")

        except Exception as e:
            print(f"Live portfolio fetch failed: {e}")
            if os.path.exists(snapshot_path):
                print(f"Falling back to snapshot: {snapshot_path}")
                accounts, positions_map = client.load_portfolio_snapshot(snapshot_path)
                used_snapshot = True
            else:
                print("No snapshot available; exiting.")
                sys.exit(1)
    else:
        if os.path.exists(snapshot_path):
            print(f"Local mode: loading snapshot from {snapshot_path}")
            accounts, positions_map = client.load_portfolio_snapshot(snapshot_path)
            used_snapshot = True
        else:
            # Local mode requested but snapshot missing — fall back to a small simulated portfolio
            print(f"Local mode requested but snapshot {snapshot_path} not found — using simulated sample portfolio")
            accounts = [{"accountId": "SIM1", "accountTitle": "Simulated Account"}]
            positions_map = {
                "SIM1": [
                    {"ticker": "AAPL", "contractDesc": "AAPL", "position": 10, "avgCost": 150.0, "mktValue": 1500.0, "unrealizedPnl": 50.0},
                    {"ticker": "AAPL", "contractDesc": "AAPL 2025-09-19 C 170.00", "position": 1, "strike": 170.0, "expiry": "2025-09-19", "right": "C", "avgCost": 2.5, "mktValue": 110.0, "unrealizedPnl": 5.0}
                ]
            }
    
    # Report discovered accounts (live or snapshot)
    print(f"\nFound {len(accounts)} account(s){' (from snapshot)' if used_snapshot else ' (live)'}:")
    for account in accounts:
        account_id = account.get('accountId', account.get('id', 'Unknown'))
        account_name = account.get('accountTitle', account.get('displayName', 'Unknown'))
        print(f"  - {account_id}: {account_name}")
    
    # Step 4: Iterate accounts and use positions (from snapshot or live fetch)
    account_summaries = []
    for account in accounts:
        account_id = account.get('accountId', account.get('id'))
        if account_id:
            print(f"\nProcessing account {account_id}...")
            positions = positions_map.get(account_id, [])
            
            if args.skip_greeks:
                # Quick mode: no Greeks lookup, just use simple heuristics
                print("Skipping Greeks lookup (--skip-greeks enabled)")
                await client.print_positions_async(account_id, positions)
            else:
                # Build per-underlying requested options set from portfolio positions
                option_positions = [p for p in positions if client.is_option_contract(p)]
                per_underlying: Dict[str, Set[Tuple[str, float, str]]] = {}

                for pos in option_positions:
                    # Only fetch for contracts we hold (non-zero position)
                    try:
                        qty = float(pos.get('position', 0))
                    except Exception:
                        qty = 0.0
                    if qty == 0:
                        continue

                    # Use consistent option details extraction
                    underlying, expiry_str, strike_f, option_type, _ = client._extract_option_details(pos)
                    
                    if not underlying:
                        continue

                    per_underlying.setdefault(underlying, set()).add((expiry_str, strike_f, option_type))

                # Prefetch options data per underlying (only the strikes/expirations we own)
                if per_underlying:
                    print(f"Prefetching Tastytrade options for {len(per_underlying)} underlying(s) with cache TTL {args.cache_minutes}min (force_refresh={args.force_refresh}, dry_run={args.dry_run})")
                    for underlying, only_set in per_underlying.items():
                        try:
                            if args.dry_run:
                                # Create simulated cached data so we can debug display logic offline
                                client.options_cache.simulate_prefetch(underlying, only_options=only_set, expiry_minutes=args.cache_minutes)
                            else:
                                await client.options_cache.fetch_and_cache_options_for_underlying(underlying, only_options=only_set, expiry_minutes=args.cache_minutes, force_refresh=bool(args.force_refresh))
                        except Exception as e:
                            print(f"Warning: failed prefetch for {underlying}: {e}")

                # Now print positions and options summary (they will use cached data where available)
                print(f"Fetching Greeks with {args.cache_minutes}-minute cache...")
                summary = await client.print_positions_async(account_id, positions)
                if summary:
                    account_summaries.append(summary)
                await client.print_options_summary_async(account_id, positions)
    
    # Print comprehensive SPX-weighted delta summary
    if account_summaries:
        client.print_portfolio_spx_summary(account_summaries)

    if STREAM_PROCESSOR is not None:
        await STREAM_PROCESSOR.stop()


def main():
    """Wrapper to run async main function."""
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
