#!/usr/bin/env python3
"""
Quick test script to check option position display without Greeks calculations
"""
import requests
import urllib3
import logging

# Set up logging to reduce debug noise
logging.basicConfig(level=logging.INFO)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# Disable SSL warnings for localhost
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://localhost:5001/v1/api"

def get_accounts():
    """Get account information."""
    try:
        response = requests.get(f"{BASE_URL}/portfolio/accounts", verify=False)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Error fetching accounts: {e}")
        return []

def get_positions(account_id: str):
    """Get positions for an account."""
    try:
        response = requests.get(f"{BASE_URL}/portfolio/{account_id}/positions/0", verify=False)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Error fetching positions: {e}")
        return []

def analyze_option_position(pos):
    """Analyze option position to extract side info."""
    contract_desc = pos.get('contractDesc', '')
    
    # Extract side from contract description
    side = ""
    if 'CALL' in contract_desc.upper():
        side = 'CALL'
    elif 'PUT' in contract_desc.upper():
        side = 'PUT'
    elif pos.get('right') == 'C':
        side = 'CALL'
    elif pos.get('right') == 'P':
        side = 'PUT'
    
    # Extract other info
    symbol = pos.get('ticker', pos.get('contractDesc', '').split(' ')[0] if ' ' in pos.get('contractDesc', '') else 'Unknown')
    expiry = pos.get('expiry', 'Unknown')
    strike = pos.get('strike', 'Unknown')
    position_size = pos.get('position', 0)
    
    return {
        'symbol': symbol,
        'side': side,
        'expiry': expiry, 
        'strike': strike,
        'position_size': position_size,
        'contract_desc': contract_desc,
        'raw_data': {k: v for k, v in pos.items() if k in ['right', 'ticker', 'contractDesc', 'expiry', 'strike']}
    }

def main():
    print("Testing option position display...")
    
    # Get accounts
    accounts = get_accounts()
    if not accounts:
        print("No accounts found")
        return
        
    account_id = accounts[0]['id']
    print(f"Testing account: {account_id}")
    
    # Get positions
    positions = get_positions(account_id)
    if not positions:
        print("No positions found")
        return
    
    # Find option positions
    option_positions = [pos for pos in positions if pos.get('model') == 'OPT']
    print(f"Found {len(option_positions)} option positions")
    
    if not option_positions:
        print("No option positions found")
        return
    
    # Analyze each option position
    print("\n--- Option Position Analysis ---")
    print("Symbol       Side     Expiry       Strike     Pos      Contract Description")
    print("-" * 85)
    
    for pos in option_positions:
        analysis = analyze_option_position(pos)
        print(f"{analysis['symbol']:<12} {analysis['side']:<8} {analysis['expiry']:<12} {analysis['strike']:<10} {analysis['position_size']:<8} {analysis['contract_desc']}")
        
        if not analysis['side']:
            print(f"  DEBUG - Raw data: {analysis['raw_data']}")
    
    print(f"\nProcessed {len(option_positions)} option positions")

if __name__ == "__main__":
    main()
