#!/usr/bin/env python3
"""Test script to check market connection."""

import asyncio
import os
from account import TradingAccount

async def test_market_connection():
    """Test if we can connect to markets and find HYPE-USD."""
    account = TradingAccount()
    client = account.get_blocking_client()
    
    print("Testing market connection...")
    
    try:
        # Try to get markets
        markets = await client.get_markets()
        print(f"Found {len(markets)} markets")
        
        # Check if HYPE-USD exists
        if "HYPE-USD" in markets:
            print("✓ HYPE-USD market found")
            market = markets["HYPE-USD"]
            print(f"Market config: {market.trading_config}")
        else:
            print("✗ HYPE-USD market not found")
            print("Available markets:")
            for market_name in sorted(markets.keys()):
                print(f"  - {market_name}")
                
    except Exception as e:
        print(f"Error connecting to markets: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_market_connection())