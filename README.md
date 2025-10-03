# Market Maker – X10 Perpetuals (StarkNet)

A small, production‑minded market‑making loop for **Extended / X10** perpetuals on StarkNet.
It keeps *N* quotes on both sides of the book, replaces them as the best bid/ask move, rate‑limits REST calls, and periodically reconciles local state with the server.

> **Note**: Telegram controls are planned but not included yet.

---

## Features

* Places and maintains symmetric quotes around best bid/ask
* Maker‑only (`post_only=True`) replaces, with fallback to fresh create on edit failure
* Token‑bucket **rate limiter** + in‑flight **semaphore** throttle
* Background **reconciler** (default 5s, configurable) cleans ghosts & cancels server orphans
* Graceful shutdown (SIGINT/SIGTERM): mass‑cancel + close sockets
* Works with the X10 Python SDK (`BlockingTradingClient` + `OrderBook`)

---

## How it works (high level)

1. Subscribes to order book best bid/ask via `OrderBook.create(...)`.
2. On each best‑price change computes target quote prices:

   * Offset = `(1 + idx) / OFFSET_DIVISOR` away from best price
   * Rounds to tick (ceiling for asks, floor for bids)
3. For each level (*slot*), if price changed:

   * Replace existing order using `previous_order_external_id`
   * If server responds **“Edit order not found”** (code `1142`), send a **fresh create** (no previous)
4. A background **reconciler** every 5s (tweak via `MM_RECONCILE_INTERVAL`):

   * Frees local slots for orders that disappeared server‑side
   * Cancels **server orphans** (orders with our prefix not tracked locally)
5. **Rate limiter** (token bucket) limits REST calls per second; **semaphore** limits concurrency.

---

## Repo structure (relevant files)

```
src/
  maker_main.py         # main loop (quotes + reconcile)
  account.py            # loads keys, builds TradingAccount & clients
  rate_limit.py         # token-bucket limiter
  backoff_utils.py      # retry with exponential backoff
  services/orderbook.py # lightweight order book watcher
```

Example of the order book watcher:

```python
from services.orderbook import OrderBookWatcher

watcher = OrderBookWatcher("BTC-USD")
await watcher.start()
print(watcher.get_best_bid(), watcher.get_best_ask())
await watcher.stop()  # release network resources
```

---

## Requirements

* Python **3.11+** (tested on 3.12)
* X10 / Extended Python SDK
* An **API key**, **Stark key pair**, and **vault id** with deposit on X10
* Network access to Extended REST & stream endpoints

---

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
# If you have a requirements.txt, use it; otherwise:
pip install x10-python-sdk python-dotenv
```

> If the SDK is not on PyPI in your setup, install it per X10’s docs or from GitHub.

---

## Configuration

Create a `.env` at the project root:

```ini
# --- X10 account ---
API_KEY=xxxxxxxxxxxxxxxx
PUBLIC_KEY=0x...
PRIVATE_KEY=0x...
VAULT=110913

# --- Market making knobs ---
# Optional: bypass the prompt. Example: BTC-USD, ETH-USD, ...
MM_MARKET=BTC-USD

# Number of levels (quotes) per side
MM_LEVELS=2

# Target USD notional per order (size computed = USD/price, then tick-rounded)
MM_TARGET_USD=250

# Price offset divisor (bigger = tighter around the top of book)
# Offset = (1+idx)/OFFSET_DIVISOR
MM_OFFSET_DIVISOR=400

# Max concurrent in-flight order requests
MM_MAX_IN_FLIGHT=4

# Reconciliation interval in seconds (lower = catch duplicates sooner)
MM_RECONCILE_INTERVAL=5

# --- Rate limiting (token bucket) ---
# Set MM_MARKET_MAKER=1 if you have the MM whitelist (higher exchange limits)
MM_MARKET_MAKER=0

# Override defaults (optional)
# MM_RATE_LIMIT_RPS=16
# MM_RATE_LIMIT_BURST=32
```

> `account.py` currently uses the **StarkNet Mainnet** endpoint.
> If you want Testnet, switch to `STARKNET_TESTNET_CONFIG` there or add an env toggle.

---

## Running

```bash
source .venv/bin/activate
python -m src.maker_main
```

You’ll be prompted for a market name unless `MM_MARKET` is set.

Run unattended (no prompt):

```bash
MM_MARKET=BTC-USD python -m src.maker_main
```

Stop with `Ctrl+C` – the bot mass‑cancels and closes cleanly.

---

## Tuning guide

* **Tighter markets** → decrease `MM_OFFSET_DIVISOR` (e.g., 400 → 300 → 200).
* **More depth** → increase `MM_LEVELS` (quote more levels per side).
* **More/less size** → adjust `MM_TARGET_USD`.
* **Throttle**:

  * `MM_MAX_IN_FLIGHT` limits parallel POSTs.
  * `MM_RATE_LIMIT_RPS` & `MM_RATE_LIMIT_BURST` cap request rate; if on the **market‑maker** whitelist, set `MM_MARKET_MAKER=1` or bump the numbers.

---

## Rate limits (exchange)

* Default: **1,000 requests/min** per IP across all endpoints.
* MM program: **60,000 requests / 5 min**. Ask the team for access.

This bot is gentle by default:

* Token‑bucket limiter (defaults: 16 rps if not whitelisted; 200 rps if `MM_MARKET_MAKER=1`)
* In‑flight semaphore to avoid bursts

You can always lower RPS if you see `429`.

---

## Troubleshooting

### “Invalid StarkEx signature”

* Ensure correct **endpoint config** (Mainnet vs Testnet).
* Check **VAULT**, **PUBLIC\_KEY**, **PRIVATE\_KEY**, **API\_KEY** match the environment.
* Use the latest compatible SDK; mismatched domain params can break signatures.
* Avoid manual hash/signature—this bot leans on the SDK.

### “Edit order not found” (code 1142)

* The server no longer recognizes the `previous_order_external_id` (order filled/cancelled/expired).
* The bot auto‑falls back to a **fresh create** and the **reconciler** clears local ghosts. No action needed.

### `RuntimeError: Lock is not acquired`

* Known SDK race in the waiter when a REST call errors. We **catch** it and continue.
* If noisy, consider upgrading the SDK; the bot already treats it as transient.

### Orders vanish but terminal shows nothing

* Reconcile runs every 5s and will free slots / cancel orphans.
* Ensure the SDK’s internal stream is healthy; add logging if needed.

---

## Logging & Observability

* Currently prints concise messages to stdout.
* For production, wire Python `logging` to rotation or a collector (e.g., Loki, ELK).
* Add counters if you need RPS/error‑rate dashboards.

---

## Safety notes

* Market making carries inventory risk. Start with **small** `MM_TARGET_USD`.
* Respect min order size; the bot skips if computed size < exchange minimum.
* Test new configs on **Testnet** first.

---

## Roadmap

* **Telegram bot** (coming):

  * `/status` – live slots & open orders
  * `/halt` & `/resume`
  * `/stats` – fills, PnL approximation, error rates
  * `/set` – live tuning (levels, target usd, offsets)

---

## License

MIT

---

### Quick Start (copy‑paste)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip x10-python-sdk python-dotenv

cat > .env << 'EOF'
API_KEY=xxxx
PUBLIC_KEY=0x...
PRIVATE_KEY=0x...
VAULT=110913
MM_MARKET=BTC-USD
MM_LEVELS=2
MM_TARGET_USD=250
MM_OFFSET_DIVISOR=400
MM_MAX_IN_FLIGHT=4
MM_MARKET_MAKER=0
EOF

python -m src.maker_main
```

## Grid Trading Strategy

The repository now includes an arithmetic grid trading strategy (`src/grid_main.py`) that implements:

### Features
- **Arithmetic Grid Levels**: Equally spaced price levels between min and max prices
- **Automatic Rebalancing**: Replaces orders when price moves significantly
- **Boundary Handling**: Converts all levels to buy/sell when price moves outside grid
- **Position Management**: Respects maximum position limits
- **Order Management**: Tracks order fills and manages active orders

### Configuration
Create a `.env` file with grid-specific parameters:

```ini
# Grid Strategy Configuration
GRID_MARKET=ETH-USD
GRID_MIN_PRICE=2500.00
GRID_MAX_PRICE=3500.00
GRID_LEVELS=10
GRID_ORDER_USD=100
GRID_MAX_POSITION_USD=1000
GRID_REBALANCE_THRESHOLD=0.01
GRID_REFRESH_INTERVAL=1.0
GRID_MAX_ORDER_AGE=300
```

### Running the Grid Strategy

```bash
# Using environment variables
GRID_MARKET=ETH-USD python -m src.grid_main

# Or with interactive prompt
python -m src.grid_main
```

### How It Works

1. **Grid Setup**: Calculates arithmetic grid levels between min and max prices
2. **Price Monitoring**: Continuously monitors current market price
3. **Order Placement**: 
   - Places buy orders below current price
   - Places sell orders above current price  
   - Leaves closest grid level empty
4. **Rebalancing**: Replaces orders when price moves beyond threshold
5. **Boundary Handling**: Converts all levels to buy/sell when price moves outside grid

### Testing

A test script is available to validate grid calculations:

```bash
python test_grid_strategy.py
```

### Example Scenario

With `GRID_MIN_PRICE=2500`, `GRID_MAX_PRICE=3500`, `GRID_LEVELS=10`:
- Grid levels: 2500, 2600, 2700, ..., 3500
- Current price: 3000 → Buy orders at 2500-2900, Sell orders at 3100-3500
- Price moves to 3050 → Rebalance triggered
- New setup: Buy orders at 2500-3000, Sell orders at 3100-3500
- Price drops to 2400 → All levels become buy orders

### Risk Management
- Respects `GRID_MAX_POSITION_USD` to prevent over-exposure
- Uses post-only orders to avoid market impact
- Implements rate limiting and error handling
