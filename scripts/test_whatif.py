#!/usr/bin/env python3
"""CLI tool to test WhatIf margin simulation.

Usage:
    python scripts/test_whatif.py --symbol SPY --action BUY --qty 1 [--price 123.45]
    python scripts/test_whatif.py --conid 756646 --action BUY --qty 10  # SPY stock direct
    python scripts/test_whatif.py --combo "ES:BUY:10" "SPY:SELL:100"   # Multi-leg
"""
import asyncio
import argparse
import json
import logging
import os
import sys
from typing import Any

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)


async def test_whatif_single_leg() -> None:
    """Test WhatIf with a single-leg order (stock)."""
    from desktop.engine.ib_engine import IBEngine
    
    logger.info("=" * 70)
    logger.info("TEST: WhatIf Single-Leg (SPY Stock)")
    logger.info("=" * 70)
    
    engine = IBEngine(
        host=os.getenv("IB_HOST", "127.0.0.1"),
        port=int(os.getenv("IB_PORT", "4001")),
        client_id=30,
        db_dsn=os.getenv("DB_DSN", "postgresql://portfoliouser:portfoliopass@localhost:5432/portfolio_engine"),
    )
    
    try:
        await engine.connect()
        logger.info("✓ Connected to IB Gateway")
        
        legs = [
            {
                "symbol": "SPY",
                "sec_type": "STK",
                "exchange": "SMART",
                "qty": 10,
                "action": "BUY",
                "conid": None,  # Will be resolved
            }
        ]
        
        logger.info(f"Submitting WhatIf: {legs}")
        result = await engine.whatif_order(
            legs=legs,
            order_type="LIMIT",
            limit_price=550.00,
        )
        
        logger.info(f"\n✓ WhatIf Response:")
        print(json.dumps(result, indent=2, default=str))
        
        # Validate result
        if result.get("status") == "success":
            init_change = result.get("init_margin_change")
            maint_change = result.get("maint_margin_change")
            logger.info(f"\n✓ MARGIN IMPACT:")
            logger.info(f"  Initial Margin Change: ${init_change:,.2f}")
            logger.info(f"  Maintenance Margin Change: ${maint_change:,.2f}")
        else:
            logger.error(f"✗ WhatIf failed: {result.get('error')}")
            
    finally:
        await engine.disconnect()
        logger.info("Disconnected")


async def test_whatif_multileg() -> None:
    """Test WhatIf with a multi-leg combo order."""
    from desktop.engine.ib_engine import IBEngine
    
    logger.info("=" * 70)
    logger.info("TEST: WhatIf Multi-Leg (Call Spread)")
    logger.info("=" * 70)
    
    engine = IBEngine(
        host=os.getenv("IB_HOST", "127.0.0.1"),
        port=int(os.getenv("IB_PORT", "4001")),
        client_id=30,
        db_dsn=os.getenv("DB_DSN", "postgresql://portfoliouser:portfoliopass@localhost:5432/portfolio_engine"),
    )
    
    try:
        await engine.connect()
        logger.info("✓ Connected to IB Gateway")
        
        # Example: BUY 450 CALL, SELL 460 CALL (call spread on SPY)
        legs = [
            {
                "symbol": "SPY",
                "underlying": "SPY",
                "sec_type": "OPT",
                "exchange": "SMART",
                "qty": 1,
                "action": "BUY",
                "strike": 450.0,
                "right": "C",
                "expiry": "20260319",  # March 2026
                "conid": None,
            },
            {
                "symbol": "SPY",
                "underlying": "SPY",
                "sec_type": "OPT",
                "exchange": "SMART",
                "qty": 1,
                "action": "SELL",
                "strike": 460.0,
                "right": "C",
                "expiry": "20260319",
                "conid": None,
            },
        ]
        
        logger.info(f"Submitting WhatIf combo: {len(legs)} legs")
        result = await engine.whatif_order(
            legs=legs,
            order_type="LIMIT",
            limit_price=500.0,  # Single price for whole combo
        )
        
        logger.info(f"\n✓ WhatIf Response:")
        print(json.dumps(result, indent=2, default=str))
        
        if result.get("status") == "success":
            init_change = result.get("init_margin_change")
            maint_change = result.get("maint_margin_change")
            logger.info(f"\n✓ MARGIN IMPACT:")
            logger.info(f"  Initial Margin Change: ${init_change:,.2f}")
            logger.info(f"  Maintenance Margin Change: ${maint_change:,.2f}")
        else:
            logger.error(f"✗ WhatIf failed: {result.get('error')}")
            
    finally:
        await engine.disconnect()
        logger.info("Disconnected")


async def test_whatif_via_conid() -> None:
    """Test WhatIf using contract ID directly (no symbol resolution needed)."""
    from desktop.engine.ib_engine import IBEngine
    
    logger.info("=" * 70)
    logger.info("TEST: WhatIf via ConID (Direct Contract ID)")
    logger.info("=" * 70)
    
    engine = IBEngine(
        host=os.getenv("IB_HOST", "127.0.0.1"),
        port=int(os.getenv("IB_PORT", "4001")),
        client_id=30,
        db_dsn=os.getenv("DB_DSN", "postgresql://portfoliouser:portfoliopass@localhost:5432/portfolio_engine"),
    )
    
    try:
        await engine.connect()
        logger.info("✓ Connected to IB Gateway")
        
        # SPY stock has conId 756646 (well-known)
        legs = [
            {
                "conid": 756646,  # SPY stock
                "qty": 5,
                "action": "BUY",
            }
        ]
        
        logger.info(f"Submitting WhatIf: BUY 5x SPY (conId=756646)")
        result = await engine.whatif_order(
            legs=legs,
            order_type="LIMIT",
            limit_price=551.00,
        )
        
        logger.info(f"\n✓ WhatIf Response:")
        print(json.dumps(result, indent=2, default=str))
        
        if result.get("status") == "success":
            init_change = result.get("init_margin_change")
            maint_change = result.get("maint_margin_change")
            logger.info(f"\n✓ MARGIN IMPACT:")
            logger.info(f"  Initial Margin Change: ${init_change:,.2f}")
            logger.info(f"  Maintenance Margin Change: ${maint_change:,.2f}")
        else:
            logger.error(f"✗ WhatIf failed: {result.get('error')}")
            
    finally:
        await engine.disconnect()
        logger.info("Disconnected")


async def main():
    parser = argparse.ArgumentParser(
        description="Test WhatIf margin simulation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single stock order
  python scripts/test_whatif.py --test single
  
  # Multi-leg combo order
  python scripts/test_whatif.py --test multileg
  
  # Direct contract ID
  python scripts/test_whatif.py --test conid
  
  # Run all tests
  python scripts/test_whatif.py --test all
        """,
    )
    parser.add_argument(
        "--test",
        choices=["single", "multileg", "conid", "all"],
        default="all",
        help="Which test to run (default: all)",
    )
    
    args = parser.parse_args()
    
    tests = {
        "single": test_whatif_single_leg,
        "multileg": test_whatif_multileg,
        "conid": test_whatif_via_conid,
    }
    
    if args.test == "all":
        for test_name, test_func in tests.items():
            try:
                await test_func()
                print("\n")
            except Exception as exc:
                logger.error(f"✗ Test '{test_name}' failed: {exc}", exc_info=True)
                print("\n")
    else:
        try:
            await tests[args.test]()
        except Exception as exc:
            logger.error(f"✗ Test failed: {exc}", exc_info=True)
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
