# main.py
import asyncio
from decimal import Decimal

from x10.perpetual.orders import OrderSide

from utils import setup_logging

from account import TradingAccount

from order_manager import place_limit_order,cancel_order

async def main():
    setup_logging()
    account = TradingAccount()
    client = account.get_async_client()

    # 1. Create a limit BUY order
    order = await place_limit_order(
        client=client,
        market="BTC-USD",
        quantity=Decimal("0.01"),
        price=Decimal("70000"),
        side=OrderSide.BUY,
    )

    # 2. Cancel the order
    await cancel_order(client, order_id=order.id)
    
if __name__ == "__main__":
    asyncio.run(main())

