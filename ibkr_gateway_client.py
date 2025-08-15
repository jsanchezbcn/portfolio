#!/usr/bin/env python3
"""
Python script to interact with IBKR Client Portal Web API.

This script:
1. Checks if the gateway is running on https://localhost:5000/v1/tickle
2. If not running, automatically starts it in the background
3. Waits for gateway to become responsive
4. Handles authentication flow
5. Fetches portfolio accounts and positions

Requirements:
- requests library
- IBKR Client Portal Gateway JAR file
- Java runtime
"""

import requests
import urllib3
import os
import sys
import subprocess
import time
import json

# Disable SSL warnings for localhost
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configuration
GATEWAY_URL = "https://localhost:5001"  # Gateway runs on port 5001, not 5000
TICKLE_ENDPOINT = f"{GATEWAY_URL}/v1/tickle"
AUTH_STATUS_ENDPOINT = f"{GATEWAY_URL}/v1/iserver/auth/status"
AUTH_INIT_ENDPOINT = f"{GATEWAY_URL}/v1/iserver/auth/ssodh/init"
ACCOUNTS_ENDPOINT = f"{GATEWAY_URL}/v1/portal/portfolio/accounts"
JAR_FILE_PATH = "clientportal/dist/ibgroup.web.core.iblink.router.clientportal.gw.jar"

# Timeouts
GATEWAY_START_TIMEOUT = 60  # Increased timeout for proper startup
AUTH_TIMEOUT = 30


def check_gateway_status():
    """
    Check if the IBKR Client Portal Gateway is running.
    
    Returns:
        bool: True if gateway is running, False otherwise
    """
    try:
        response = requests.get(TICKLE_ENDPOINT, verify=False, timeout=5)
        return response.status_code == 200
    except requests.exceptions.RequestException:
        return False


def start_gateway():
    """
    Start the IBKR Client Portal Gateway in the background using the correct method.
    
    Returns:
        subprocess.Popen: The gateway process object
    """
    # Get the current working directory and build paths
    current_dir = os.getcwd()
    clientportal_dir = os.path.join(current_dir, "clientportal")
    run_script_path = os.path.join(clientportal_dir, "bin", "run.sh")
    config_file = os.path.join(clientportal_dir, "root", "conf.yaml")
    
    # Check if we're in the right directory
    if not os.path.exists(run_script_path):
        print(f"✗ Run script not found: {run_script_path}")
        print("Please run this script from the project root directory (where clientportal/ folder exists)")
        return None
    
    # Check for configuration file
    if not os.path.exists(config_file):
        print(f"✗ Configuration file NOT found: {config_file}")
        return None
    
    print(f"Starting IBKR Client Portal Gateway using run.sh script...")
    print(f"Config file: {config_file}")
    
    try:
        # Make sure the script is executable
        os.chmod(run_script_path, 0o755)
        
        # Run the script from the clientportal directory with proper config path
        process = subprocess.Popen(
            [run_script_path, "root/conf.yaml"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # Combine stderr with stdout
            cwd=clientportal_dir,  # Run from clientportal directory
            universal_newlines=True,
            bufsize=1
        )
        
        print(f"Gateway started with PID: {process.pid}")
        print("Gateway output will show warnings about illegal reflective access - this is normal.")
        return process
    except FileNotFoundError as e:
        print(f"✗ File not found: {e}")
        return None
    except Exception as e:
        print(f"✗ Error starting gateway: {e}")
        return None


def wait_for_gateway(timeout=GATEWAY_START_TIMEOUT):
    """
    Wait for the gateway to become responsive.
    
    Args:
        timeout (int): Maximum time to wait in seconds
    
    Returns:
        bool: True if gateway becomes responsive, False if timeout
    """
    print(f"Waiting up to {timeout} seconds for gateway to become responsive...")
    print("(This may take 30-60 seconds for first startup)")
    
    start_time = time.time()
    check_count = 0
    while time.time() - start_time < timeout:
        if check_gateway_status():
            elapsed = int(time.time() - start_time)
            print(f"\n✓ Gateway is now responsive! (took {elapsed} seconds)")
            return True
        
        check_count += 1
        if check_count % 5 == 0:  # Print status every 10 seconds
            elapsed = int(time.time() - start_time)
            print(f"\nStill waiting... ({elapsed}s elapsed)")
        else:
            print(".", end="", flush=True)
        time.sleep(2)
    
    print(f"\n✗ Gateway did not become responsive within {timeout} seconds")
    return False


def check_auth_status():
    """
    Check the current authentication status.
    
    Returns:
        dict: Authentication status response or None if error
    """
    try:
        response = requests.get(AUTH_STATUS_ENDPOINT, verify=False, timeout=10)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"Auth status check failed with status: {response.status_code}")
            return None
    except requests.exceptions.RequestException as e:
        print(f"Error checking auth status: {e}")
        return None


def initiate_authentication():
    """
    Initiate the authentication process.
    
    Returns:
        bool: True if authentication initiated successfully
    """
    try:
        print("Initiating authentication...")
        response = requests.post(AUTH_INIT_ENDPOINT, verify=False, timeout=10)
        if response.status_code == 200:
            print("✓ Authentication process initiated")
            return True
        else:
            print(f"Authentication initiation failed with status: {response.status_code}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"Error initiating authentication: {e}")
        return False


def wait_for_authentication(timeout=AUTH_TIMEOUT):
    """
    Wait for user to complete authentication.
    
    Args:
        timeout (int): Maximum time to wait in seconds
    
    Returns:
        bool: True if authenticated, False if timeout or error
    """
    print(f"Waiting up to {timeout} seconds for authentication to complete...")
    print("Please complete authentication in your browser if prompted.")
    
    start_time = time.time()
    while time.time() - start_time < timeout:
        auth_status = check_auth_status()
        if auth_status and auth_status.get('authenticated', False):
            print("✓ Authentication completed successfully!")
            return True
        
        print(".", end="", flush=True)
        time.sleep(3)
    
    print(f"\n✗ Authentication not completed within {timeout} seconds")
    return False


def get_portfolio_accounts():
    """
    Fetch portfolio accounts.
    
    Returns:
        list: List of account dictionaries or None if error
    """
    try:
        print("Fetching portfolio accounts...")
        response = requests.get(ACCOUNTS_ENDPOINT, verify=False, timeout=10)
        if response.status_code == 200:
            accounts = response.json()
            print(f"✓ Found {len(accounts)} account(s)")
            return accounts
        else:
            print(f"Failed to fetch accounts. Status: {response.status_code}")
            return None
    except requests.exceptions.RequestException as e:
        print(f"Error fetching accounts: {e}")
        return None


def get_positions(account_id):
    """
    Fetch positions for a specific account.
    
    Args:
        account_id (str): The account ID
    
    Returns:
        list: List of position dictionaries or None if error
    """
    try:
        positions_endpoint = f"{GATEWAY_URL}/v1/portal/portfolio/{account_id}/positions"
        print(f"Fetching positions for account: {account_id}")
        response = requests.get(positions_endpoint, verify=False, timeout=10)
        if response.status_code == 200:
            positions = response.json()
            print(f"✓ Found {len(positions)} position(s)")
            return positions
        else:
            print(f"Failed to fetch positions. Status: {response.status_code}")
            return None
    except requests.exceptions.RequestException as e:
        print(f"Error fetching positions: {e}")
        return None


def print_positions(positions):
    """
    Print position details in a formatted way.
    
    Args:
        positions (list): List of position dictionaries
    """
    if not positions:
        print("No positions found.")
        return
    
    print("\n" + "="*80)
    print("PORTFOLIO POSITIONS")
    print("="*80)
    print(f"{'Symbol':<15} {'Quantity':<15} {'Avg Cost':<15} {'Market Value':<15}")
    print("-"*80)
    
    for position in positions:
        symbol = position.get('contractDesc', position.get('ticker', 'Unknown'))
        quantity = position.get('position', 'N/A')
        avg_cost = position.get('avgCost', position.get('avgPrice', 'N/A'))
        market_value = position.get('mktValue', 'N/A')
        
        print(f"{symbol:<15} {quantity:<15} {avg_cost:<15} {market_value:<15}")
    
    print("="*80)


def main():
    """Main function to orchestrate the entire workflow."""
    print("IBKR Client Portal Web API Script")
    print("="*50)
    
    # Step 1: Check if gateway is running
    print("Step 1: Checking gateway status...")
    if not check_gateway_status():
        print("Gateway is not running.")
        print("\nTo start the gateway manually:")
        print("1. Open a terminal")
        print("2. Navigate to the clientportal directory")
        print("3. Run: bin/run.sh root/conf.yaml")
        print("4. Wait for it to show: 'Open https://localhost:5001 to login'")
        print("5. Re-run this script")
        return False
    
    print("✓ Gateway is running!")
    
    # Step 4: Check authentication status
    print("\nStep 4: Checking authentication status...")
    auth_status = check_auth_status()
    if not auth_status:
        print("✗ Failed to check authentication status. Exiting.")
        return False
    
    if not auth_status.get('authenticated', False):
        print("Not authenticated. Initiating authentication...")
        if not initiate_authentication():
            print("✗ Failed to initiate authentication. Exiting.")
            return False
        
        # Wait for user to complete authentication
        if not wait_for_authentication():
            print("✗ Authentication not completed. Exiting.")
            return False
    else:
        print("✓ Already authenticated!")
    
    # Step 5: Fetch portfolio accounts
    print("\nStep 5: Fetching portfolio accounts...")
    accounts = get_portfolio_accounts()
    if not accounts:
        print("✗ Failed to fetch accounts. Exiting.")
        return False
    
    # Step 6: Get positions for the first account
    print("\nStep 6: Fetching positions...")
    if accounts:
        first_account = accounts[0]
        account_id = first_account.get('accountId', first_account.get('id'))
        if account_id:
            print(f"Using account: {account_id}")
            positions = get_positions(account_id)
            
            # Step 7: Print positions
            if positions:
                print_positions(positions)
            else:
                print("No positions found or error fetching positions.")
        else:
            print("✗ Could not determine account ID from first account.")
            return False
    
    print("\n✓ Script completed successfully!")
    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
