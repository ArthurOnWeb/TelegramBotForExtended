# Grid Trading Strategy Guide

## Overview
The grid strategy implements an arithmetic grid trading approach where:
- Price levels are equally spaced between min and max prices
- Buy orders are placed below current price
- Sell orders are placed above current price
- The closest grid level remains empty
- Orders are rebalanced when price moves significantly

## Quick Start

1. **Setup Environment**
```bash
cp grid_config.example.env .env
# Edit .env with your API keys and grid parameters
```

2. **Run the Strategy**
```bash
python -m src.grid_main
```

## Configuration Parameters

### Required API Credentials
```ini
API_KEY=your_api_key
PUBLIC_KEY=your_public_key  
PRIVATE_KEY=your_private_key
VAULT=your_vault_id
```

### Grid Parameters
```ini
# Market to trade
GRID_MARKET=ETH-USD

# Grid boundaries
GRID_MIN_PRICE=2500.00
GRID_MAX_PRICE=3500.00

# Number of grid levels (including min and max)
GRID_LEVELS=10

# Order size in USD
GRID_ORDER_USD=100
```

### Risk Management
```ini
# Maximum position exposure in USD
GRID_MAX_POSITION_USD=1000

# Price movement threshold for rebalancing (1% = 0.01)
GRID_REBALANCE_THRESHOLD=0.01

# Maximum order age before forced rebalance (seconds)
GRID_MAX_ORDER_AGE=300
```

### Performance Settings
```ini
# Refresh interval for price checks (seconds)
GRID_REFRESH_INTERVAL=1.0
```

## Example Configurations

### ETH-USD (Moderate Volatility)
```ini
GRID_MARKET=ETH-USD
GRID_MIN_PRICE=2500
GRID_MAX_PRICE=3500  
GRID_LEVELS=15
GRID_ORDER_USD=50
GRID_MAX_POSITION_USD=500
GRID_REBALANCE_THRESHOLD=0.008
```

### BTC-USD (Lower Volatility)
```ini
GRID_MARKET=BTC-USD
GRID_MIN_PRICE=40000
GRID_MAX_PRICE=50000
GRID_LEVELS=20
GRID_ORDER_USD=200
GRID_MAX_POSITION_USD=2000
GRID_REBALANCE_THRESHOLD=0.005
```

### SOL-USD (Higher Volatility)
```ini
GRID_MARKET=SOL-USD
GRID_MIN_PRICE=80
GRID_MAX_PRICE=120
GRID_LEVELS=8
GRID_ORDER_USD=25
GRID_MAX_POSITION_USD=200
GRID_REBALANCE_THRESHOLD=0.015
```

## Strategy Logic

### Grid Calculation
For `GRID_LEVELS=N`, the strategy creates N equally spaced price levels:
```
Step = (MAX_PRICE - MIN_PRICE) / (N - 1)
Level 0: MIN_PRICE
Level 1: MIN_PRICE + Step
...
Level N-1: MAX_PRICE
```

### Order Placement
- **Current price between levels**: Buy orders below, sell orders above
- **Current price at level**: That level remains empty
- **Price outside grid**: All levels become buy (below) or sell (above) orders

### Rebalancing Triggers
1. Price moves more than `REBALANCE_THRESHOLD` from closest grid level
2. Orders are older than `MAX_ORDER_AGE` seconds
3. Manual intervention needed

## Risk Considerations

### Position Risk
- The strategy can accumulate inventory in trending markets
- Use `GRID_MAX_POSITION_USD` to limit exposure
- Monitor grid boundaries - if price moves outside, all orders become one-sided

### Liquidity Risk
- Grid orders are post-only to avoid market impact
- In fast markets, orders may not fill at desired prices

### Parameter Sensitivity
- Too many levels → smaller profits per trade, more management overhead
- Too few levels → larger price gaps, fewer trading opportunities
- Too tight grid → frequent rebalancing, higher trading costs
- Too wide grid → infrequent trading, capital inefficiency

## Monitoring and Adjustment

### Key Metrics to Monitor
- Number of active orders
- Current grid position vs market price
- Total position value
- Order fill rates
- Rebalance frequency

### When to Adjust Parameters
- **Increase levels**: When price is frequently between grid levels
- **Widen grid**: When rebalancing too frequently  
- **Tighten grid**: When not getting enough fills
- **Adjust order size**: Based on volatility and desired position size

## Troubleshooting

### Common Issues

**"No orders being placed"**
- Check market connectivity
- Verify API credentials
- Ensure grid parameters are valid (MIN < MAX)

**"Orders not filling"**
- Grid may be too wide for current volatility
- Market may be trending strongly

**"Frequent rebalancing"**
- Reduce `GRID_REBALANCE_THRESHOLD`
- Widen grid boundaries
- Increase number of levels

**"Position too large"**
- Reduce `GRID_ORDER_USD`
- Lower `GRID_MAX_POSITION_USD`
- Consider manual intervention

## Best Practices

1. **Start Small**: Begin with conservative parameters and small order sizes
2. **Test First**: Use the test script to validate grid calculations
3. **Monitor Closely**: Especially during high volatility periods
4. **Adjust Gradually**: Make small parameter changes and observe effects
5. **Set Limits**: Always use position limits to manage risk
6. **Keep Records**: Log performance to inform parameter optimization

## Advanced Features

The grid strategy includes:
- Rate limiting to respect exchange limits
- Error handling for network issues
- Order reconciliation to handle fills/cancellations
- Graceful shutdown procedures
- Comprehensive logging for monitoring

## Support

For issues or questions:
1. Check logs for error messages
2. Verify API credentials and network connectivity  
3. Test grid calculations with `test_grid_strategy.py`
4. Review parameter settings for appropriateness