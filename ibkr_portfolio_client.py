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
    from tastytrade import Session
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
        # Cache for symbols that don't have options (to avoid repeated failed lookups)
        self.no_options_cache: set = set()
        self._load_cache()
    
    def _load_cache(self):
        """Load cache from disk."""
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'rb') as f:
                    cache_data = pickle.load(f)
                    # Handle both old and new cache formats
                    if isinstance(cache_data, dict) and 'cache' in cache_data:
                        self.cache = cache_data['cache']
                        self.no_options_cache = cache_data.get('no_options_cache', set())
                    else:
                        self.cache = cache_data
                        self.no_options_cache = set()
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
            
        username, password = self._get_credentials()
        if not username or not password:
            return None
        
        try:
            self.session = Session(username, password)
            return self.session
        except Exception as e:
            print(f"Warning: Could not create Tastytrade session: {e}")
            return None
    
    def _make_cache_key(self, underlying: str, expiry_minutes: Optional[int] = None) -> str:
        """Create cache key for an underlying symbol."""
        expiry = expiry_minutes or self.default_expiry_minutes
        return f"{underlying.upper()}_{expiry}min"
    
    def _make_option_key(self, underlying: str, expiry: str, strike: float, option_type: str) -> str:
        """Create unique key for an option."""
        return f"{underlying.upper()}_{expiry}_{strike:.2f}_{option_type.upper()}"

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
                expiration=str(exp_str),
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
    
    async def _has_options_chain(self, session: Session, underlying: str) -> bool:
        """Quick check if an underlying symbol has options available."""
        try:
            is_future = underlying.startswith('/')
            
            if is_future:
                nested_chain = NestedFutureOptionChain.get(session, underlying)
                return bool(nested_chain and nested_chain.option_chains and 
                           len(nested_chain.option_chains) > 0 and
                           len(nested_chain.option_chains[0].expirations) > 0)
            else:
                chain = get_option_chain(session, underlying)
                return bool(chain and len(chain) > 0)
                
        except Exception as e:
            print(f"Error checking options availability for {underlying}: {e}")
            return False

    async def _fetch_options_data(self, session: Session, underlying: str, only_options: Optional[Set[Tuple[str, float, str]]] = None) -> Dict[str, OptionData]:
        """Fetch options data from Tastytrade API.

        If `only_options` is provided it should be a set of tuples:
            (expiry_str, strike_float, option_type_str ('call'|'put'))

        When provided, the function will only create OptionData records for
        matching expiry/strike/type combinations.
        """
        
        # Check if we already know this symbol has no options
        if underlying.upper() in self.no_options_cache:
            print(f"Skipping {underlying} - known to have no options")
            return {}
        
        print(f"Fetching fresh options data for {underlying} from Tastytrade...")
        
        # First check if this symbol has options at all
        if not await self._has_options_chain(session, underlying):
            print(f"No options chain available for {underlying}, adding to no-options cache...")
            self.no_options_cache.add(underlying.upper())
            self._save_cache()
            return {}
        
        # Determine if this is a futures symbol
        is_future = underlying.startswith('/')
        
        try:
            if is_future:
                nested_chain = NestedFutureOptionChain.get(session, underlying)
                if not nested_chain or not nested_chain.option_chains:
                    return {}
                
                chain_data = nested_chain.option_chains[0]
                if not chain_data.expirations:
                    return {}
                
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

            expirations_to_process = available_expirations
            if only_options:
                requested_exps = {e for (e, _, _) in only_options}
                # normalize dates in chain keys to strings to compare
                str_exp_map = {str(exp): exp for exp in available_expirations}
                expirations_to_process = [str_exp_map[e] for e in requested_exps if e in str_exp_map]
                if not expirations_to_process:
                    # No matching expirations in the chain
                    return {}
            
            # Create option data records
            option_data = {}
            streamer_symbols = []
            symbol_to_option_key = {}
            
            # Iterate expirations and build records only for requested strikes
            for exp in expirations_to_process:
                options_list = chain[exp]
                exp_str = str(exp)
                for option in options_list:
                    if is_future:
                        call_streamer_symbol = getattr(option, 'call_streamer_symbol', None)
                        put_streamer_symbol = getattr(option, 'put_streamer_symbol', None)
                        strike_price = getattr(option, 'strike_price', 0)
                        # Only include if not filtering, or if this strike/type is requested
                        if only_options:
                            want_call = (exp_str, float(strike_price), 'call') in only_options
                            want_put = (exp_str, float(strike_price), 'put') in only_options
                        else:
                            want_call = bool(call_streamer_symbol)
                            want_put = bool(put_streamer_symbol)

                        if call_streamer_symbol and want_call:
                            key = self._make_option_key(underlying, exp_str, float(strike_price), 'call')
                            option_data[key] = OptionData(
                                symbol=call_streamer_symbol,
                                underlying=underlying,
                                strike=float(strike_price),
                                option_type='call',
                                expiration=exp_str
                            )
                            streamer_symbols.append(call_streamer_symbol)
                            symbol_to_option_key[call_streamer_symbol] = key

                        if put_streamer_symbol and want_put:
                            key = self._make_option_key(underlying, exp_str, float(strike_price), 'put')
                            option_data[key] = OptionData(
                                symbol=put_streamer_symbol,
                                underlying=underlying,
                                strike=float(strike_price),
                                option_type='put',
                                expiration=exp_str
                            )
                            streamer_symbols.append(put_streamer_symbol)
                            symbol_to_option_key[put_streamer_symbol] = key
                    else:
                        strike_price = float(option.strike_price)
                        opt_type = 'call' if option.option_type.value == 'C' else 'put'
                        if only_options:
                            want = (exp_str, strike_price, opt_type) in only_options
                        else:
                            want = True

                        if want:
                            key = self._make_option_key(underlying, exp_str, strike_price, opt_type)
                            option_data[key] = OptionData(
                                symbol=option.streamer_symbol,
                                underlying=underlying,
                                strike=strike_price,
                                option_type=opt_type,
                                expiration=exp_str
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
                    await asyncio.sleep(3)
                    
                    data_timeout = 15
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
            print(f"Error fetching options data for {underlying}: {e}")
            return {}
    
    async def get_option_data(self, underlying: str, expiry: str, strike: float, 
                            option_type: str, expiry_minutes: Optional[int] = None) -> Optional[OptionData]:
        """Get option data with caching."""
        
        # Quick check if we know this symbol has no options
        if underlying.upper() in self.no_options_cache:
            return None
        
        cache_key = self._make_cache_key(underlying, expiry_minutes)
        option_key = self._make_option_key(underlying, expiry, strike, option_type)
        
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

    async def fetch_and_cache_options_for_underlying(self, underlying: str, only_options: Optional[Set[Tuple[str, float, str]]] = None, expiry_minutes: Optional[int] = None, force_refresh: bool = False) -> Dict[str, OptionData]:
        """Public helper to fetch (and cache) options for a single underlying.

        `only_options` is a set of tuples (expiry_str, strike_float, option_type)
        - If provided, only those entries will be fetched/created.
        - If force_refresh is True, bypass existing cache TTL.
        """
        cache_key = self._make_cache_key(underlying, expiry_minutes)

        # If symbol known to have no options, short-circuit
        if underlying.upper() in self.no_options_cache:
            return {}

        # Use cached entry if present and not expired unless force_refresh
        if not force_refresh and cache_key in self.cache and self.cache[cache_key].is_valid():
            # If only_options is supplied, filter the cached data
            if only_options:
                filtered = {k: v for k, v in self.cache[cache_key].data.items() if (v.expiration, v.strike, v.option_type) in only_options}
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
                value = value.strip().strip('"\'')
                os.environ[key] = value

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
    
    def get_positions(self, account_id: str) -> List[Dict]:
        """Fetch positions for a specific account."""
        try:
            # First call to get positions (may return empty initially)
            response = self.session.get(f"{self.base_url}/v1/api/portfolio/{account_id}/positions/0")
            if response.status_code == 200:
                positions = response.json()
                # If empty, try a second call (IBKR sometimes requires this)
                if not positions:
                    time.sleep(1)
                    response = self.session.get(f"{self.base_url}/v1/api/portfolio/{account_id}/positions/0")
                    if response.status_code == 200:
                        positions = response.json()
                return positions
            else:
                print(f"Error fetching positions for {account_id}: Status code {response.status_code}")
                print(f"Response: {response.text}")
                return []
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
    
    def get_option_greeks(self, conid: str) -> Dict:
        """Fetch Greeks for an option contract."""
        try:
            # Return safe placeholder values so the UI shows 'N/A' instead of making network calls.
            # The real Greeks will be fetched via Tastytrade integration
            return {'delta': 'N/A', 'gamma': 'N/A', 'theta': 'N/A', 'vega': 'N/A', 'impliedVol': 'N/A'}
        except Exception:
            # Shouldn't happen, but return placeholders on any unexpected error
            return {'delta': 'N/A', 'gamma': 'N/A', 'theta': 'N/A', 'vega': 'N/A', 'impliedVol': 'N/A'}
    
    async def get_tastytrade_option_greeks(self, underlying: str, expiry: str, strike: float, 
                                         option_type: str) -> Dict[str, Any]:
        """Get option greeks from Tastytrade using ONLY cached data (no fresh fetches)."""
        try:
            # Use get_cached_option which only checks cache, doesn't fetch new data
            option_data = self.options_cache.get_cached_option(underlying, expiry, strike, option_type)
            
            if option_data:
                return {
                    'delta': option_data.delta if option_data.delta != 0.0 else 'N/A',
                    'gamma': option_data.gamma if option_data.gamma != 0.0 else 'N/A', 
                    'theta': option_data.theta if option_data.theta != 0.0 else 'N/A',
                    'vega': option_data.vega if option_data.vega != 0.0 else 'N/A',
                    'impliedVol': option_data.iv if option_data.iv != 0.0 else 'N/A',
                    'bid': option_data.bid,
                    'ask': option_data.ask,
                    'mid': option_data.mid,
                    'source': 'tastytrade'
                }
            else:
                return {
                    'delta': 'N/A', 'gamma': 'N/A', 'theta': 'N/A', 
                    'vega': 'N/A', 'impliedVol': 'N/A', 'source': 'cache_miss'
                }
        except Exception as e:
            return {
                'delta': 'N/A', 'gamma': 'N/A', 'theta': 'N/A', 
                'vega': 'N/A', 'impliedVol': 'N/A', 'source': 'error'
            }
    
    def get_spx_price(self) -> float:
        """Get current SPX price, trying multiple sources: Tastytrade SPY, Yahoo Finance ES, then fallback."""
        from datetime import datetime, timedelta
        now = datetime.now()
        
        # Check if cached price is still valid
        if (self.spx_price is not None and 
            self.spx_price_timestamp is not None and
            now < self.spx_price_timestamp + timedelta(minutes=self.spx_price_cache_minutes)):
            return self.spx_price
        
        try:
            # Try to get SPX price from multiple sources
            if hasattr(self, '_simulation_mode') and self._simulation_mode:
                self.spx_price = 5500.0  # Reasonable SPX value for simulation
            else:
                # Method 1: Try Tastytrade SPY (SPY ≈ SPX/10)
                try:
                    tw_session = self.options_cache._get_session() if hasattr(self.options_cache, '_get_session') else None
                    
                    if tw_session and hasattr(tw_session, 'session_token'):
                        import requests
                        # Get SPY quote via REST API
                        headers = {"Authorization": f"Bearer {tw_session.session_token}"}
                        response = requests.get("https://api.tastyworks.com/quote/equity", 
                                              params={"symbols": "SPY"}, 
                                              headers=headers, 
                                              timeout=10)
                        
                        if response.status_code == 200:
                            data = response.json()
                            if 'data' in data and 'items' in data['data'] and len(data['data']['items']) > 0:
                                spy_data = data['data']['items'][0]
                                spy_price = None
                                
                                # Try different price fields
                                if 'last' in spy_data and spy_data['last']:
                                    spy_price = float(spy_data['last'])
                                elif 'mark' in spy_data and spy_data['mark']:
                                    spy_price = float(spy_data['mark'])
                                elif 'mid' in spy_data and spy_data['mid']:
                                    spy_price = float(spy_data['mid'])
                                elif 'bid' in spy_data and spy_data['bid'] and 'ask' in spy_data and spy_data['ask']:
                                    spy_price = (float(spy_data['bid']) + float(spy_data['ask'])) / 2
                                
                                if spy_price and spy_price > 400:  # Sanity check for SPY range
                                    # Convert SPY to SPX: SPY * 10 is approximately SPX
                                    self.spx_price = spy_price * 10
                                    print(f"Fetched SPX price from Tastytrade SPY ({spy_price:.2f} * 10): {self.spx_price:.2f}")
                                    self.spx_price_timestamp = now
                                    return self.spx_price
                                else:
                                    raise Exception(f"Invalid SPY price: {spy_price}")
                            else:
                                raise Exception("No SPY data in response")
                        else:
                            raise Exception(f"SPY API returned {response.status_code}: {response.text}")
                            
                except Exception as spy_error:
                    print(f"Tastytrade SPY method failed ({spy_error})")
                
                # Method 2: Try Yahoo Finance ES futures (ES=F is continuous contract)
                try:
                    print("Trying Yahoo Finance ES futures...")
                    es_future = yf.Ticker("ES=F")
                    
                    # Get the most recent price data
                    hist = es_future.history(period="1d", interval="1m")
                    if not hist.empty:
                        # Get the last available close price
                        es_price = float(hist['Close'].iloc[-1])
                        
                        if es_price > 5000 and es_price < 8000:  # Sanity check for ES range
                            self.spx_price = es_price
                            print(f"Fetched SPX price from Yahoo Finance ES futures: {self.spx_price:.2f}")
                            self.spx_price_timestamp = now
                            return self.spx_price
                        else:
                            raise Exception(f"ES price out of expected range: {es_price}")
                    else:
                        raise Exception("No ES futures data available")
                        
                except Exception as es_error:
                    print(f"Yahoo Finance ES method failed ({es_error})")
                
                # Method 3: Try Yahoo Finance SPY as backup
                try:
                    print("Trying Yahoo Finance SPY as backup...")
                    spy_ticker = yf.Ticker("SPY")
                    
                    # Get the most recent price data
                    hist = spy_ticker.history(period="1d", interval="1m")
                    if not hist.empty:
                        spy_price = float(hist['Close'].iloc[-1])
                        
                        if spy_price > 400 and spy_price < 800:  # Sanity check for SPY range
                            # Convert SPY to SPX
                            self.spx_price = spy_price * 10
                            print(f"Fetched SPX price from Yahoo Finance SPY ({spy_price:.2f} * 10): {self.spx_price:.2f}")
                            self.spx_price_timestamp = now
                            return self.spx_price
                        else:
                            raise Exception(f"SPY price out of expected range: {spy_price}")
                    else:
                        raise Exception("No SPY data available")
                        
                except Exception as spy_yf_error:
                    print(f"Yahoo Finance SPY method failed ({spy_yf_error})")
                
                # Fallback to market-calibrated estimate
                print("All real-time sources failed, using market-calibrated estimate")
                # Calibrated to match IB calculations (VOO should be ~0.551)
                current_market_estimate = 6475.0
                print(f"Using market-calibrated estimate: {current_market_estimate:.2f}")
                self.spx_price = current_market_estimate
            
            self.spx_price_timestamp = now
            return self.spx_price if self.spx_price is not None else 6475.0
            
        except Exception as e:
            print(f"Warning: Could not fetch SPX price, using default: {e}")
            self.spx_price = 6475.0  # Fallback value
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
        
        # Primary check: asset class explicitly marks OPT
        if asset_class == 'OPT':
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
        # Extract underlying symbol - prefer undSym for option contracts
        if position.get('assetClass', '').upper() == 'OPT':
            underlying = position.get('undSym', position.get('ticker', position.get('contractDesc', ''))).strip()
        else:
            underlying = position.get('ticker', position.get('contractDesc', '')).strip()
        
        # Extract expiry
        expiry = position.get('expiry') or position.get('expiryDate') or position.get('lastTradingDay') or position.get('expiration') or ''
        if isinstance(expiry, (int, float)):
            try:
                expiry = time.strftime('%Y-%m-%d', time.localtime(expiry/1000))
            except Exception:
                expiry = str(expiry)
        expiry_str = str(expiry)
        
        # Extract strike
        strike = position.get('strike') or position.get('strikePrice') or position.get('strikePx') or 0
        try:
            strike_f = float(strike)
        except Exception:
            strike_f = 0.0
        
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
            desc = (position.get('contractDesc') or '').upper()
            if 'CALL' in desc:
                option_type = 'call'
                side = 'C'
            elif 'PUT' in desc:
                option_type = 'put'
                side = 'P'
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
        # Compute stock deltas and print summaries (unchanged)
        stock_total_delta = 0.0
        DELTA_MULTIPLIERS = {
            'ES': 50.0,
            'MES': 5.0,
        }
        DEFAULT_SHARE_DELTA = 0.01
        for p in regular_positions:
            try:
                qty = float(p.get('position', 0))
            except Exception:
                qty = 0.0
            raw_sym = (p.get('ticker') or p.get('contractDesc') or '')
            norm_sym = str(raw_sym).strip().split()[0].upper() if raw_sym else ''
            multiplier = DELTA_MULTIPLIERS.get(norm_sym, DEFAULT_SHARE_DELTA)
            stock_total_delta += multiplier * qty

        # Build stock summary
        stock_summ_items = []
        for p in regular_positions:
            sym = p.get('ticker') or p.get('contractDesc') or 'N/A'
            try:
                qty = float(p.get('position', 0))
            except Exception:
                qty = p.get('position', 0)
            raw_sym = (p.get('ticker') or p.get('contractDesc') or '')
            norm_sym = str(raw_sym).strip().split()[0].upper() if raw_sym else ''
            mult = DELTA_MULTIPLIERS.get(norm_sym, DEFAULT_SHARE_DELTA)
            try:
                delta_item = mult * float(qty)
            except Exception:
                delta_item = 0.0
            stock_summ_items.append(f"{sym}:{qty} (Δ={delta_item:.2f})")

        # Fetch Tastytrade Greeks for all option positions concurrently
        option_greeks_data = {}
        if option_positions and TASTYTRADE_SDK_AVAILABLE:
            print(f"Fetching Greeks for {len(option_positions)} option positions from Tastytrade...")
            
            # Create tasks for all option positions
            tasks = []
            for position in option_positions:
                # Use consistent option details extraction
                underlying, expiry_str, strike_f, option_type, _ = self._extract_option_details(position)
                
                # Create unique key for this option
                option_key = f"{underlying}_{expiry_str}_{strike_f:.2f}_{option_type}"
                
                # Create task for fetching Greeks
                task = self.get_tastytrade_option_greeks(underlying, expiry_str, strike_f, option_type)
                tasks.append((option_key, task))
            
            # Execute all tasks concurrently with timeout
            if tasks:
                try:
                    # Run tasks with overall timeout
                    import asyncio
                    results = await asyncio.gather(*[task for _, task in tasks], return_exceptions=True)
                    
                    # Map results back to option keys
                    for i, (option_key, _) in enumerate(tasks):
                        if i < len(results) and not isinstance(results[i], Exception):
                            option_greeks_data[option_key] = results[i]
                        else:
                            # Fallback to N/A values
                            option_greeks_data[option_key] = {
                                'delta': 'N/A', 'theta': 'N/A', 'source': 'timeout'
                            }
                    
                    print(f"Successfully fetched Greeks for {len([r for r in results if not isinstance(r, Exception)])} options")
                    
                except Exception as e:
                    print(f"Error fetching Greeks: {e}")

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
                    raw_sym = (position.get('ticker') or position.get('contractDesc') or '')
                    norm_sym = str(raw_sym).strip().split()[0].upper() if raw_sym else ''
                    multiplier = DELTA_MULTIPLIERS.get(norm_sym, DEFAULT_SHARE_DELTA)
                    delta_val = multiplier * pos_qty_f
                    
                    # Get IBKR multiplier from position data (default to 1.0 for stocks)
                    ibkr_multiplier = position.get('multiplier', 1.0)
                    if ibkr_multiplier == 0.0:
                        ibkr_multiplier = 1.0  # Default for stocks
                    
                    # Calculate SPX-weighted delta
                    spx_delta = self.calculate_spx_weighted_delta(
                        symbol=norm_sym,
                        position_qty=pos_qty_f,
                        price=mkt_price if mkt_price else avg_cost,
                        underlying_delta=1.0,  # For stocks, delta = 1
                        multiplier=ibkr_multiplier
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
        """Print a summary of options Greeks for the portfolio."""
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

        for position in option_positions:
            conid = position.get('conid', '')
            pos_qty = position.get('position', 0)
            # determine side
            right = (position.get('right') or '').upper()
            if not right:
                desc = (position.get('contractDesc') or '').upper()
                right = 'CALL' if 'CALL' in desc else ('PUT' if 'PUT' in desc else '')

                if conid and pos_qty != 0:
                    if TASTYWORKS_AVAILABLE and USE_EXTERNAL:
                        underlying = position.get('ticker', position.get('contractDesc', ''))
                        expiry_val = position.get('expiry') or position.get('expiryDate') or position.get('expiration') or ''
                        expiry_key = str(expiry_val).replace('-', '') if expiry_val else ''
                        # Ensure strike is a float for tastyworks helper
                        try:
                            strike_val = position.get('strike')
                            strike_f = float(strike_val) if strike_val not in (None, '') else 0.0
                        except Exception:
                            strike_f = 0.0
                        greeks = get_option_greeks_from_tasty(underlying, expiry_key, strike_f, (position.get('right') or '').upper())
                        if not greeks:
                            greeks = self.get_option_greeks(str(conid))
                    else:
                        # Use local placeholder values; do not call external marketdata
                        greeks = self.get_option_greeks(str(conid))

                delta = greeks.get('delta')
                theta = greeks.get('theta')

                try:
                    if delta not in (None, 'N/A', ''):
                        d = float(delta) * pos_qty
                        total_delta += d
                        if right.startswith('C'):
                            delta_call += d
                        elif right.startswith('P'):
                            delta_put += d
                        valid_greeks_count += 1
                except (ValueError, TypeError):
                    pass

                try:
                    if theta not in (None, 'N/A', ''):
                        t = float(theta) * pos_qty
                        total_theta += t
                        if right.startswith('C'):
                            theta_call += t
                        elif right.startswith('P'):
                            theta_put += t
                except (ValueError, TypeError):
                    pass

        print(f"\n=== Options Greeks Summary for Account {account_id} ===")
        if valid_greeks_count > 0:
            print(f"Portfolio Net Delta: {total_delta:.3f} (Calls: {delta_call:.3f}, Puts: {delta_put:.3f})")
            print(f"Portfolio Net Theta: {total_theta:.3f} (Calls: {theta_call:.3f}, Puts: {theta_put:.3f})")
            print(f"Options positions analyzed: {len(option_positions)}")
        else:
            print("No valid Greeks data available for options positions; showing contract breakdown")
            # provide basic contract breakdown
            calls = sum(1 for p in option_positions if ((p.get('right') or '').upper().startswith('C') or 'CALL' in (p.get('contractDesc') or '').upper()))
            puts = len(option_positions) - calls
            print(f"Options count: {len(option_positions)} (Calls: {calls}, Puts: {puts})")

async def main_async():
    """Main async script execution with Tastytrade integration."""
    # Load environment variables
    load_dotenv()
    
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
    
    # If local-mode or snapshot exists, use snapshot to avoid contacting IBKR
    accounts = []
    positions_map = {}

    snapshot_path = args.snapshot_file
    use_snapshot = args.local_mode or os.path.exists(snapshot_path)

    if use_snapshot and os.path.exists(snapshot_path):
        print(f"Loading portfolio snapshot from {snapshot_path} (local mode)")
        accounts, positions_map = client.load_portfolio_snapshot(snapshot_path)
    elif args.local_mode and not os.path.exists(snapshot_path):
        # Local mode requested but snapshot missing — fall back to a small simulated portfolio
        print(f"Local mode requested but snapshot {snapshot_path} not found — using simulated sample portfolio")
        accounts = [{"accountId": "SIM1", "accountTitle": "Simulated Account"}]
        positions_map = {
            "SIM1": [
                {"ticker": "AAPL", "contractDesc": "AAPL", "position": 10, "avgCost": 150.0, "mktValue": 1500.0, "unrealizedPnl": 50.0},
                {"ticker": "AAPL", "contractDesc": "AAPL 2025-09-19 C 170.00", "position": 1, "strike": 170.0, "expiry": "2025-09-19", "right": "C", "avgCost": 2.5, "mktValue": 110.0, "unrealizedPnl": 5.0}
            ]
        }
    else:
        # Step 1: Check if gateway is running
        print("Checking if IBKR Client Portal Gateway is running...")
        if not client.check_gateway_status():
            print("Gateway is not running. Starting it now...")
            if not client.start_gateway():
                print("Failed to start gateway. Exiting.")
                sys.exit(1)
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
                print("Authentication failed. Exiting.")
                sys.exit(1)
        else:
            print("Already authenticated!")

        # Step 3: Fetch accounts
        print("\nFetching portfolio accounts...")
        accounts = client.get_accounts()
        if not accounts:
            print("No accounts found or error fetching accounts.")
            sys.exit(1)

        # Build positions map for later use; optionally save snapshot
        positions_map = {}
        for account in accounts:
            account_id = account.get('accountId', account.get('id'))
            if account_id:
                positions_map[account_id] = client.get_positions(account_id)

        if args.save_portfolio:
            try:
                client.save_portfolio_snapshot(accounts, positions_map, snapshot_path)
                print(f"Saved portfolio snapshot to {snapshot_path}")
            except Exception as e:
                print(f"Warning: could not save snapshot: {e}")
    
    # Step 3: Fetch accounts
    print("\nFetching portfolio accounts...")
    accounts = client.get_accounts()
    
    if not accounts:
        print("No accounts found or error fetching accounts.")
        sys.exit(1)
    
    print(f"Found {len(accounts)} account(s):")
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
