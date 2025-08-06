# main.py
import asyncio
from decimal import Decimal

from dotenv import load_dotenv
from x10.perpetual.configuration import MAINNET_CONFIG
from x10.perpetual.orders import OrderSide
from x10.perpetual.trading_client import PerpetualTradingClient

from account import stark_account

load_dotenv()

async def main():
    # 1. Instanciate Asynchron Client
    trading_client = PerpetualTradingClient(
        endpoint_config=MAINNET_CONFIG,
        stark_account=stark_account,
    )

    # 2. Create a limit BUY order
    placed_order = await trading_client.orders.place_order(
        market_name="BTC-USD",
        amount_of_synthetic=Decimal("1"),
        price=Decimal("63000.1"),
        side=OrderSide.BUY,
        post_only=True,            # si tu veux un ordre post-only
    )
    print("Ordre placé :", placed_order)

    # 3. Cancel the order
    await trading_client.orders.cancel_order(order_id=placed_order.id)
    print(f"Ordre {placed_order.id} annulé")

if __name__ == "__main__":
    asyncio.run(main())

