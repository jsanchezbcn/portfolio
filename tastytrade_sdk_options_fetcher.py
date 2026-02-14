#!/usr/bin/env python3
"""
Tastytrade Options Chain Fetcher using Official SDK

This script fetches option chain data for a given symbol using the official
tastytrade Python SDK, which is much more reliable than raw API calls.

Usage:
    python tastytrade_sdk_options_fetcher.py AAPL
    python tastytrade_sdk_options_fetcher.py AAPL --dry-run
    
Environment variables required:
    TASTYTRADE_USERNAME or TASTYWORKS_USER
    TASTYTRADE_PASSWORD or TASTYWORKS_PASS
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Dict, Any, Optional
import pandas as pd

# Import the official tastytrade SDK
try:
    from tastytrade import Session
    from tastytrade.instruments import get_option_chain, NestedFutureOptionChain
    from tastytrade.dxfeed import Quote, Greeks
    from tastytrade import DXLinkStreamer
    from tastytrade.utils import get_tasty_monthly
except ImportError:
    print("ERROR: tastytrade SDK not installed. Run: pip install tastytrade")
    sys.exit(1)


class TastytradeOptionsError(Exception):
    """Custom exception for Tastytrade options fetching errors."""
    pass


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


def get_credentials() -> tuple[str, str]:
    """Get username and password from environment variables."""
    # Try different environment variable name patterns
    username_keys = ['TASTYTRADE_USERNAME', 'TASTYWORKS_USER', 'TASTYTRADE_USER']
    password_keys = ['TASTYTRADE_PASSWORD', 'TASTYWORKS_PASS', 'TASTYTRADE_PASSWORD']
    
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
    
    if not username:
        raise TastytradeOptionsError(f"Username not found in environment variables: {username_keys}")
    if not password:
        raise TastytradeOptionsError(f"Password not found in environment variables: {password_keys}")
    
    return username, password


async def fetch_options_with_market_data(session: Session, symbol: str) -> List[Dict[str, Any]]:
    """
    Fetch options chain with real-time market data using the tastytrade SDK.
    
    Args:
        session: Authenticated tastytrade session
        symbol: Symbol to fetch options for (e.g., 'AAPL')
    
    Returns:
        List of option records with market data
    """
    print(f"Fetching option chain for {symbol}...")
    
    # Determine if this is a futures symbol (starts with /)
    is_future = symbol.startswith('/')
    
    # Get the option chain using the correct method
    try:
        print(f"Calling {'NestedFutureOptionChain.get' if is_future else 'get_option_chain'}(session, '{symbol}')...")
        
        if is_future:
            # Use the futures option chain method
            nested_chain = NestedFutureOptionChain.get(session, symbol)
            if not nested_chain or not nested_chain.option_chains:
                print(f"No futures option chain found for {symbol}")
                raise TastytradeOptionsError(f"No option chain found for {symbol}")
            
            # Get the first available option chain from the nested structure
            chain_data = nested_chain.option_chains[0]
            if not chain_data.expirations:
                print(f"No expirations found in futures option chain for {symbol}")
                raise TastytradeOptionsError(f"No option chain found for {symbol}")
            
            # Convert to a format similar to equity options
            chain = {}
            for exp in chain_data.expirations:
                chain[exp.expiration_date] = exp.strikes
        else:
            # Use the regular equity option chain method
            chain = get_option_chain(session, symbol)
            
        print(f"get_option_chain returned: {type(chain)}, length: {len(chain) if chain else 'None'}")
        if chain:
            print(f"Chain keys: {list(chain.keys())[:3] if len(chain) > 0 else 'empty'}")
    except Exception as e:
        print(f"Exception details: {type(e).__name__}: {e}")
        raise TastytradeOptionsError(f"Failed to fetch option chain for {symbol}: {e}")
    
    if not chain or len(chain) == 0:
        print(f"Chain is empty for {symbol}")
        raise TastytradeOptionsError(f"No option chain found for {symbol}")
    
    print(f"Found option chain with {len(chain)} expirations")
    
    # List available expirations for debugging
    available_expirations = sorted(chain.keys())
    print(f"Available expirations: {available_expirations[:5]}")  # Show first 5
    
    # Get the nearest available expiration (not necessarily min, but first available)
    if not available_expirations:
        raise TastytradeOptionsError(f"No expirations found for {symbol}")
    
    # Use the first available expiration (closest to current date)
    nearest_exp = available_expirations[0]
    options_list = chain[nearest_exp]
    
    print(f"Using expiration: {nearest_exp}")
    print(f"Found {len(options_list)} options for this expiration")
    
    # Debug: Show structure of first option object
    if options_list:
        first_option = options_list[0]
        print(f"First option attributes: {[attr for attr in dir(first_option) if not attr.startswith('_')]}")
        print(f"First option type: {type(first_option)}")
    
    # Create initial records with basic option info
    option_records = []
    streamer_symbol_to_record = {}
    
    for option in options_list:
        # Handle different field names for futures vs equity options
        if is_future:
            # Futures options have different field structure
            call_streamer_symbol = getattr(option, 'call_streamer_symbol', None)
            put_streamer_symbol = getattr(option, 'put_streamer_symbol', None)
            strike_price = getattr(option, 'strike_price', 0)
            
            if call_streamer_symbol:
                # Create call option record
                call_record = {
                    'symbol': call_streamer_symbol,
                    'underlying_symbol': symbol,
                    'strike': float(strike_price),
                    'type': 'call',
                    'expiration': str(nearest_exp),
                    'bid': 0.0,
                    'ask': 0.0,
                    'mid': 0.0,
                    'iv': 0.0,
                    'delta': 0.0,
                    'gamma': 0.0,
                    'theta': 0.0,
                    'vega': 0.0,
                    'rho': 0.0
                }
                option_records.append(call_record)
                streamer_symbol_to_record[call_streamer_symbol] = call_record
                
            if put_streamer_symbol:
                # Create put option record
                put_record = {
                    'symbol': put_streamer_symbol,
                    'underlying_symbol': symbol,
                    'strike': float(strike_price),
                    'type': 'put',
                    'expiration': str(nearest_exp),
                    'bid': 0.0,
                    'ask': 0.0,
                    'mid': 0.0,
                    'iv': 0.0,
                    'delta': 0.0,
                    'gamma': 0.0,
                    'theta': 0.0,
                    'vega': 0.0,
                    'rho': 0.0
                }
                option_records.append(put_record)
                streamer_symbol_to_record[put_streamer_symbol] = put_record
        else:
            # Equity options
            record = {
                'symbol': option.streamer_symbol,
                'underlying_symbol': symbol,
                'strike': float(option.strike_price),
                'type': 'call' if option.option_type.value == 'C' else 'put',
                'expiration': str(nearest_exp),
                'bid': 0.0,
                'ask': 0.0,
                'mid': 0.0,
                'iv': 0.0,
                'delta': 0.0,
                'gamma': 0.0,
                'theta': 0.0,
                'vega': 0.0,
                'rho': 0.0
            }
            option_records.append(record)
            streamer_symbol_to_record[option.streamer_symbol] = record
    
    # Now fetch real market data using the streamer
    if is_future:
        # For futures, collect all call and put streamer symbols
        streamer_symbols = []
        for opt in options_list:
            call_sym = getattr(opt, 'call_streamer_symbol', None)
            put_sym = getattr(opt, 'put_streamer_symbol', None)
            if call_sym:
                streamer_symbols.append(call_sym)
            if put_sym:
                streamer_symbols.append(put_sym)
    else:
        # For equity options
        streamer_symbols = [opt.streamer_symbol for opt in options_list]
    
    try:
        print("Connecting to market data streamer for real-time data...")
        
        async with DXLinkStreamer(session) as streamer:
            # Subscribe to both quotes and greeks
            await streamer.subscribe(Quote, streamer_symbols)
            await streamer.subscribe(Greeks, streamer_symbols)
            
            print("Subscribed to market data, waiting for data...")
            await asyncio.sleep(3)  # Give time for data to arrive
            
            # Collect market data for a reasonable time
            data_timeout = 15  # seconds - increased timeout
            start_time = asyncio.get_event_loop().time()
            quotes_received = 0
            greeks_received = 0
            unique_symbols_with_data = set()
            
            print(f"Collecting market data for {len(streamer_symbols)} options...")
            
            while (asyncio.get_event_loop().time() - start_time) < data_timeout:
                try:
                    # Try to get quote events
                    quote_events = await asyncio.wait_for(streamer.get_event(Quote), timeout=1.0)
                    
                    # Handle both single events and lists
                    if not isinstance(quote_events, list):
                        quote_events = [quote_events]
                    
                    for quote in quote_events:
                        try:
                            event_symbol = getattr(quote, 'event_symbol', None)
                            if event_symbol and event_symbol in streamer_symbol_to_record:
                                record = streamer_symbol_to_record[event_symbol]
                                bid_price = getattr(quote, 'bid_price', None)
                                ask_price = getattr(quote, 'ask_price', None)
                                if bid_price is not None and ask_price is not None:
                                    record['bid'] = float(bid_price) if bid_price else 0.0
                                    record['ask'] = float(ask_price) if ask_price else 0.0
                                    if record['bid'] > 0 and record['ask'] > 0:
                                        record['mid'] = (record['bid'] + record['ask']) / 2
                                    quotes_received += 1
                                    unique_symbols_with_data.add(event_symbol)
                                    if quotes_received <= 5:  # Debug first few
                                        print(f"Quote for {event_symbol}: bid={record['bid']}, ask={record['ask']}")
                        except Exception as e:
                            continue  # Skip this quote if there's an issue
                
                except asyncio.TimeoutError:
                    pass  # No quote data this round
                except Exception as e:
                    print(f"Quote error: {e}")
                
                try:
                    # Try to get greeks events
                    greeks_events = await asyncio.wait_for(streamer.get_event(Greeks), timeout=1.0)
                    
                    # Handle both single events and lists
                    if not isinstance(greeks_events, list):
                        greeks_events = [greeks_events]
                    
                    for greeks in greeks_events:
                        try:
                            event_symbol = getattr(greeks, 'event_symbol', None)
                            if event_symbol and event_symbol in streamer_symbol_to_record:
                                record = streamer_symbol_to_record[event_symbol]
                                # Update greeks data using getattr for safety
                                volatility = getattr(greeks, 'volatility', None)
                                if volatility is not None:
                                    record['iv'] = float(volatility) if volatility else 0.0
                                
                                delta = getattr(greeks, 'delta', None)
                                if delta is not None:
                                    record['delta'] = float(delta) if delta else 0.0
                                
                                gamma = getattr(greeks, 'gamma', None)
                                if gamma is not None:
                                    record['gamma'] = float(gamma) if gamma else 0.0
                                
                                theta = getattr(greeks, 'theta', None)
                                if theta is not None:
                                    record['theta'] = float(theta) if theta else 0.0
                                
                                vega = getattr(greeks, 'vega', None)
                                if vega is not None:
                                    record['vega'] = float(vega) if vega else 0.0
                                
                                rho = getattr(greeks, 'rho', None)
                                if rho is not None:
                                    record['rho'] = float(rho) if rho else 0.0
                                
                                greeks_received += 1
                        except Exception as e:
                            continue  # Skip this greeks update if there's an issue
                
                except asyncio.TimeoutError:
                    pass  # No greeks data this round
                except Exception as e:
                    print(f"Greeks error: {e}")
                
                # Check if we have enough data
                if quotes_received > len(options_list) // 4:  # At least 25% coverage
                    print(f"Got substantial market data, ending collection early")
                    break
                    
                # Show progress every few seconds
                if int(asyncio.get_event_loop().time() - start_time) % 5 == 0 and quotes_received > 0:
                    print(f"Progress: {quotes_received} quotes, {greeks_received} greeks, {len(unique_symbols_with_data)} symbols with data")
            
            print(f"Market data collection complete. Quotes: {quotes_received}, Greeks: {greeks_received}, Symbols with data: {len(unique_symbols_with_data)}")
    
    except Exception as e:
        print(f"Warning: Could not get real-time market data: {e}")
        print("Returning basic option chain without market data")
    
    return option_records


def write_options_csv(options_data: List[Dict[str, Any]], filename: str) -> None:
    """Write options data to CSV file."""
    if not options_data:
        raise TastytradeOptionsError("No options data to write")
    
    # Create DataFrame with the required columns
    df = pd.DataFrame(options_data)
    
    # Reorder columns to match the required format
    required_columns = ['strike', 'type', 'bid', 'ask', 'mid', 'iv', 'delta', 'gamma', 'theta', 'vega']
    df_output = df[required_columns]
    
    # Sort by strike price and option type
    df_output = df_output.sort_values(['strike', 'type'])
    
    # Write to CSV
    df_output.to_csv(filename, index=False)
    print(f"Wrote {len(df_output)} rows to {filename}")


async def main():
    """Main function to fetch options chain and write to CSV."""
    parser = argparse.ArgumentParser(
        description='Fetch Tastytrade options chain data using official SDK'
    )
    parser.add_argument('symbol', help='Stock symbol (e.g., AAPL)')
    parser.add_argument('--dry-run', action='store_true', 
                       help='Test authentication without fetching data')
    parser.add_argument('--output', '-o', 
                       help='Output CSV filename (default: {symbol}_options.csv)')
    
    args = parser.parse_args()
    
    # Load environment variables
    load_dotenv()
    
    try:
        # Get credentials
        username, password = get_credentials()
        
        if args.dry_run:
            print(f"Dry run mode: would fetch options for {args.symbol}")
            print(f"Using username: {username}")
            return
        
        print("Creating Tastytrade session...")
        
        # Create session using the official SDK
        session = Session(username, password)
        
        print("Session created successfully!")
        
        # Fetch options data
        options_data = await fetch_options_with_market_data(session, args.symbol.upper())
        
        # Determine output filename
        output_file = args.output or f"./{args.symbol.upper()}_options.csv"
        
        # Write to CSV
        write_options_csv(options_data, output_file)
        
    except TastytradeOptionsError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    # Run the async main function
    asyncio.run(main())
