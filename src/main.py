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
        quantity=Decimal("0.001"),
        price=Decimal("125000"),
        side=OrderSide.BUY,
        post_only=False
    )
    order = await place_limit_order(
        client=client,
        market="BTC-USD",
        quantity=Decimal("0.001"),
        price=Decimal("125000"),
        side=OrderSide.sell,
    )
    order = await place_limit_order(
        client=client,
        market="BTC-USD",
        quantity=Decimal("0.001"),
        price=Decimal("111111"),
        side=OrderSide.sell,
    )


    # 2. Cancel the order
    await cancel_order(client, order_id=order.data.id)
    
if __name__ == "__main__":
    asyncio.run(main())

