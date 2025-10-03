#!/usr/bin/env python3
"""
Test script for Grid Trading Strategy

This script demonstrates how to configure and run the grid strategy
with different market parameters.
"""

import asyncio
import os
import sys
from decimal import Decimal

# Add src directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

# Set environment variables for testing
os.environ["GRID_MARKET"] = "ETH-USD"
os.environ["GRID_MIN_PRICE"] = "2500"
os.environ["GRID_MAX_PRICE"] = "3500"
os.environ["GRID_LEVELS"] = "10"
os.environ["GRID_ORDER_USD"] = "50"
os.environ["GRID_MAX_POSITION_USD"] = "500"
os.environ["GRID_REFRESH_INTERVAL"] = "2.0"

from src.grid_main import GridTrader, GridLevel
from account import TradingAccount


async def test_grid_calculation():
    """Test grid level calculation without actual trading."""
    print("Testing grid level calculation...")
    
    account = TradingAccount()
    trader = GridTrader(account=account, market_name="ETH-USD")
    
    # Mock market configuration for testing
    class MockMarket:
        class TradingConfig:
            min_price_change = Decimal("0.01")
            price_precision = 2
            min_order_size = Decimal("0.001")
    
    trader._market = MockMarket()
    trader._tick = Decimal("0.01")
    
    # Test grid calculation
    try:
        levels = trader._calculate_grid_levels()
        print(f"Grid levels calculated: {len(levels)}")
        for i, price in enumerate(levels):
            print(f"Level {i}: {price:.2f}")
        
        # Initialize grid levels for testing
        trader._grid_levels = [GridLevel(price=price) for price in levels]
        
        # Test closest level finding
        test_prices = [Decimal("2700"), Decimal("3100"), Decimal("3400")]
        for price in test_prices:
            if trader._grid_levels:  # Check if levels list is not empty
                idx, level = trader._find_closest_grid_level(price)
                print(f"Closest to {price}: Level {idx} at {level.price}")
            else:
                print(f"No levels available for price {price}")
                
    except Exception as e:
        print(f"Error in grid calculation: {e}")


async def demo_grid_strategy():
    """Demonstrate the grid strategy with mock data."""
    print("\nDemonstrating grid strategy logic...")
    
    account = TradingAccount()
    trader = GridTrader(account=account, market_name="ETH-USD")
    
    # Mock setup
    class MockMarket:
        class TradingConfig:
            min_price_change = Decimal("0.01")
            price_precision = 2
            min_order_size = Decimal("0.001")
    
    trader._market = MockMarket()
    trader._tick = Decimal("0.01")
    
    # Create grid levels
    grid_prices = [
        Decimal("2500"), Decimal("2600"), Decimal("2700"), Decimal("2800"),
        Decimal("2900"), Decimal("3000"), Decimal("3100"), Decimal("3200"),
        Decimal("3300"), Decimal("3400"), Decimal("3500")
    ]
    trader._grid_levels = [GridLevel(price=price) for price in grid_prices]
    
    # Test different price scenarios
    scenarios = [
        ("Below grid", Decimal("2400")),
        ("Middle grid", Decimal("3000")),
        ("Above grid", Decimal("3600")),
        ("Near level", Decimal("2995")),
    ]
    
    for scenario_name, price in scenarios:
        print(f"\nScenario: {scenario_name} at {price}")
        trader._current_price = price
        
        # Test rebalance decision
        should_rebalance = trader._should_rebalance()
        print(f"Should rebalance: {should_rebalance}")
        
        # Test closest level
        idx, closest = trader._find_closest_grid_level(price)
        print(f"Closest level: {idx} (Level {idx + 1} of {len(trader._grid_levels)}) at {closest.price}")
        
        # Test outside grid detection
        min_price = trader._grid_levels[0].price
        max_price = trader._grid_levels[-1].price
        outside_grid = price < min_price or price > max_price
        print(f"Outside grid: {outside_grid}")
        
        if outside_grid:
            # Test what order side would be used
            if price < min_price:
                print("Would place SELL orders (price below grid)")
            else:
                print("Would place BUY orders (price above grid)")


if __name__ == "__main__":
    print("Grid Strategy Test Script")
    print("=" * 50)
    
    asyncio.run(test_grid_calculation())
    asyncio.run(demo_grid_strategy())
    
    print("\nTest completed successfully!")