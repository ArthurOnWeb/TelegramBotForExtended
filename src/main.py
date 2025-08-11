# main.py
import asyncio
from decimal import Decimal

from x10.perpetual.orders import OrderSide
from utils import setup_logging
from account import TradingAccount
from order_manager import place_bracket_order  # + tes autres imports si tu gardes limit/cancel

async def main():
    setup_logging()
    account = TradingAccount()
    client = account.get_async_client()
    
    markets = await client.markets_info.get_markets()
        print("📊 Markets:", markets)
    # --- Place a bracket order (CONDITIONAL + TP/SL) ---
    resp = await place_bracket_order(
        client=client,
        account=account,
        market_name="BTC-USD",
        quantity=Decimal("0.01"),
        entry_price=Decimal("70000"),
        side=OrderSide.BUY,
        tp_trigger=Decimal("71000"),
        tp_price=Decimal("71500"),
        sl_trigger=Decimal("69000"),
        sl_price=Decimal("68500"),
    )
    print("✅ Bracket order envoyé:", resp)

    # Optionnel: ferme les sessions HTTP proprement
    await account.close()

if __name__ == "__main__":
    asyncio.run(main())
