# main.py
import asyncio
from decimal import Decimal

from x10.perpetual.orders import OrderSide

from account import stark_account
from account import blocking_client

async def main():

    # 1. Create a limit BUY order
    placed_order = await blocking_client.create_and_place_order(
                        market_name="BTC-USD",
                        amount_of_synthetic=Decimal("0.01"),
                        price=70000,
                        side=OrderSide.BUY,
                        post_only=True,

    )
    print("Ordre placé :", placed_order)

    # 2. Cancel the order
    await blocking_client.cancel_order(order_id=placed_order.id)
    print(f"Ordre {placed_order.id} annulé")

    await blocking_client.close()
if __name__ == "__main__":
    asyncio.run(main())

