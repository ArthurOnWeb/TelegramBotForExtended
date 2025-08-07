# main.py
import asyncio
from decimal import Decimal

from x10.perpetual.orders import OrderSide

from utils import setup_logging

from account import TradingAccount

from order_manager import place_order_with_tp_sl

async def main():
    setup_logging()
    account = TradingAccount()
    client = account.get_blocking_client()

    # Place a BUY order with attached TP and SL
    await place_order_with_tp_sl(
        client=client,
        market="BTC-USD",
        quantity=Decimal("0.01"),
        price=Decimal("70000"),
        side=OrderSide.BUY,
    )
    
if __name__ == "__main__":
    asyncio.run(main())

