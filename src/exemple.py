from x10.perpetual.accounts import StarkPerpetualAccount
from x10.perpetual.configuration import MAINNET_CONFIG
from x10.perpetual.orders import OrderSide
from x10.perpetual.trading_client import PerpetualTradingClient
from account import
from utils import setup_logging
from account import TradingAccount
import asyncio

async def main():
    setup_logging()
    account = TradingAccount()
    client = account.get_async_client()
    placed_order = await trading_client.place_order(
        market_name="BTC-USD",
        amount_of_synthetic=Decimal("0.01"),
        price=Decimal("63000.1"),
        side=OrderSide.BUY,
    )

    await trading_client.orders.cancel_order(order_id=placed_order.id)

if __name__ == "__main__":
    asyncio.run(main())