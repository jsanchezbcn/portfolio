#!/usr/bin/env python3
"""
Interactive menu app to manage your IBKR portfolio workflow:
 - Fetch portfolio from IB (and save snapshot)
 - Load portfolio from snapshot
 - Refresh Greeks from Tastytrade for current holdings
 - Show summary by account (SPX-weighted delta and theta)
 - Show summary per instrument (basic rollup)
 - Show PnL per account
"""

import os
import sys
import time
from typing import Dict, List, Tuple, Any, Set

import asyncio

# Import the existing client and module-global flags
import ibkr_portfolio_client as ib


SNAPSHOT_DEFAULT = '.portfolio_snapshot.json'


def _normalize_symbol(sym: str) -> str:
    if not sym:
        return 'N/A'
    return str(sym).strip().split()[0].upper().lstrip('/')


def fetch_portfolio_live(client: ib.IBKRClient, snapshot_path: str) -> Tuple[List[Dict], Dict[str, List[Dict]], bool]:
    """Fetch live accounts and positions via IBKR; save snapshot on success."""
    try:
        print("Attempting to fetch live portfolio from IBKR...")
        if not client.check_gateway_status():
            print("Gateway is not running. Starting it now...")
            if not client.start_gateway():
                print("Failed to start gateway.")
                return [], {}, False
        else:
            print("Gateway is already running!")

        auth_status = client.check_auth_status()
        print(f"Authentication status: {auth_status}")
        if not auth_status.get("authenticated", False):
            print(f"\nNot authenticated. Please log in via: {client.base_url}")
            if not client.wait_for_authentication():
                print("Authentication timed out.")
                return [], {}, False
        else:
            print("Already authenticated!")

        print("\nFetching portfolio accounts...")
        accounts = client.get_accounts()
        if not accounts:
            print("No accounts returned from IBKR.")
            return [], {}, False

        positions_map: Dict[str, List[Dict]] = {}
        for account in accounts:
            account_id = account.get('accountId', account.get('id'))
            if not account_id:
                continue
            positions_map[account_id] = client.get_positions(account_id)

        try:
            client.save_portfolio_snapshot(accounts, positions_map, snapshot_path)
            print(f"Saved portfolio snapshot to {snapshot_path}")
        except Exception as e:
            print(f"Warning: could not save snapshot: {e}")

        return accounts, positions_map, True

    except KeyboardInterrupt:
        print("\nCancelled by user.")
        return [], {}, False
    except Exception as e:
        print(f"Live fetch error: {e}")
        return [], {}, False


def load_snapshot(client: ib.IBKRClient, snapshot_path: str) -> Tuple[List[Dict], Dict[str, List[Dict]]]:
    if not os.path.exists(snapshot_path):
        print(f"Snapshot not found: {snapshot_path}")
        return [], {}
    accounts, positions_map = client.load_portfolio_snapshot(snapshot_path)
    if accounts:
        print(f"Loaded snapshot with {len(accounts)} account(s) from {snapshot_path}")
    return accounts, positions_map


async def _prefetch_greeks(client: ib.IBKRClient, accounts: List[Dict], positions_map: Dict[str, List[Dict]],
                           cache_minutes: int = 1, force_refresh: bool = False, dry_run: bool = False) -> None:
    """Prefetch Tastytrade options for only the contracts held in the portfolio."""
    per_underlying: Dict[str, Set[Tuple[str, float, str]]] = {}

    for account in accounts:
        account_id = account.get('accountId', account.get('id'))
        if not account_id:
            continue
        positions = positions_map.get(account_id, [])
        option_positions = [p for p in positions if client.is_option_contract(p)]
        for pos in option_positions:
            try:
                qty = float(pos.get('position', 0))
            except Exception:
                qty = 0.0
            if qty == 0:
                continue
            underlying, expiry_str, strike_f, option_type, _ = client._extract_option_details(pos)
            if not underlying:
                continue
            per_underlying.setdefault(underlying, set()).add((expiry_str, strike_f, option_type))

    if not per_underlying:
        print("No option holdings found to prefetch.")
        return

    print(f"Prefetching Tastytrade options for {len(per_underlying)} underlying(s) with cache TTL {cache_minutes}min (force_refresh={force_refresh}, dry_run={dry_run})")
    for underlying, only_set in per_underlying.items():
        try:
            if dry_run:
                client.options_cache.simulate_prefetch(underlying, only_options=only_set, expiry_minutes=cache_minutes)
            else:
                await client.options_cache.fetch_and_cache_options_for_underlying(
                    underlying, only_options=only_set, expiry_minutes=cache_minutes, force_refresh=bool(force_refresh)
                )
        except Exception as e:
            print(f"Warning: failed prefetch for {underlying}: {e}")


def refresh_greeks(client: ib.IBKRClient, accounts: List[Dict], positions_map: Dict[str, List[Dict]],
                   cache_minutes: int = 1, force_refresh: bool = True, dry_run: bool = False) -> None:
    if not ib.TASTYTRADE_AVAILABLE:
        print("Tastytrade package not available; cannot refresh Greeks.")
        return
    # Ensure external lookups are enabled
    ib.USE_EXTERNAL = True
    asyncio.run(_prefetch_greeks(client, accounts, positions_map, cache_minutes, force_refresh, dry_run))
    stats = client.options_cache.get_cache_stats()
    print(f"Cache ready: {stats['total_options_cached']} options cached across {stats['total_cache_entries']} symbols; TTL {stats['default_expiry_minutes']} min")


def show_summary_by_account(client: ib.IBKRClient, accounts: List[Dict], positions_map: Dict[str, List[Dict]],
                            cache_minutes: int = 1) -> None:
    if not accounts:
        print("No accounts loaded. Fetch or load a snapshot first.")
        return
    summaries: List[Dict[str, Any]] = []

    async def _run():
        for account in accounts:
            account_id = account.get('accountId', account.get('id'))
            if not account_id:
                continue
            positions = positions_map.get(account_id, [])
            summary = await client.print_positions_async(account_id, positions)
            if summary:
                summaries.append(summary)
            await client.print_options_summary_async(account_id, positions)

    asyncio.run(_run())
    if summaries:
        client.print_portfolio_spx_summary(summaries)


def show_summary_per_instrument(client: ib.IBKRClient, accounts: List[Dict], positions_map: Dict[str, List[Dict]]) -> None:
    if not accounts:
        print("No accounts loaded. Fetch or load a snapshot first.")
        return
    client.print_summary_by_instrument(positions_map)


def show_pnl_per_account(accounts: List[Dict], positions_map: Dict[str, List[Dict]]) -> None:
    if not accounts:
        print("No accounts loaded. Fetch or load a snapshot first.")
        return
    print("\n=== PnL per Account ===")
    print(f"{'Account':<14} {'Mkt Value':>15} {'Unrl. PnL':>12} {'Positions':>10}")
    print("-" * 55)
    for account in accounts:
        account_id = account.get('accountId', account.get('id', 'Unknown'))
        pos = positions_map.get(account_id, [])
        mv = 0.0
        pnl = 0.0
        for p in pos:
            try:
                mv += float(p.get('mktValue', 0.0))
            except Exception:
                pass
            try:
                pnl += float(p.get('unrealizedPnl', 0.0))
            except Exception:
                pass
        print(f"{account_id:<14} {mv:>15.2f} {pnl:>12.2f} {len(pos):>10}")


def toggle_external(on: bool) -> None:
    ib.USE_EXTERNAL = bool(on)
    print(f"External data sources (Tastytrade/Yahoo): {'ENABLED' if ib.USE_EXTERNAL else 'DISABLED'}")


def main():
    snapshot_path = os.environ.get('PORTFOLIO_SNAPSHOT', SNAPSHOT_DEFAULT)
    client = ib.IBKRClient()

    accounts: List[Dict] = []
    positions_map: Dict[str, List[Dict]] = {}

    # Default: try to load snapshot if present
    if os.path.exists(snapshot_path):
        acc, pos = load_snapshot(client, snapshot_path)
        if acc:
            accounts, positions_map = acc, pos

    while True:
        try:
            print("\n================ Portfolio Menu ================")
            print("1) Fetch portfolio from IB (live)")
            print("2) Load portfolio from snapshot")
            print("3) Refresh Greeks from Tastytrade (current holdings)")
            print("4) Show summary by account (SPX Δ, Θ)")
            print("5) Show summary per instrument")
            print("6) Show PnL per account")
            print(f"7) Toggle external data (now: {'ON' if ib.USE_EXTERNAL else 'OFF'})")
            print("8) Exit")
            choice = input("Select an option: ").strip()

            if choice == '1':
                acc, pos, ok = fetch_portfolio_live(client, snapshot_path)
                if ok:
                    accounts, positions_map = acc, pos
                    print(f"Loaded {len(accounts)} account(s) from live IB.")
                else:
                    print("Live fetch failed. Consider loading snapshot.")

            elif choice == '2':
                acc, pos = load_snapshot(client, snapshot_path)
                if acc:
                    accounts, positions_map = acc, pos
                else:
                    print("Snapshot load failed or empty.")

            elif choice == '3':
                if not accounts:
                    print("Load accounts/positions first.")
                else:
                    refresh_greeks(client, accounts, positions_map, cache_minutes=1, force_refresh=True, dry_run=False)

            elif choice == '4':
                show_summary_by_account(client, accounts, positions_map, cache_minutes=1)

            elif choice == '5':
                show_summary_per_instrument(client, accounts, positions_map)

            elif choice == '6':
                show_pnl_per_account(accounts, positions_map)

            elif choice == '7':
                toggle_external(not ib.USE_EXTERNAL)

            elif choice == '8':
                print("Bye.")
                break

            else:
                print("Invalid option.")

        except KeyboardInterrupt:
            print("\nInterrupted. Returning to menu...")
            time.sleep(0.2)
        except EOFError:
            print("\nEOF received. Exiting.")
            break
        except Exception as e:
            print(f"Error: {e}")


if __name__ == '__main__':
    main()
