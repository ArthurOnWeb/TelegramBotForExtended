import asyncio
from decimal import Decimal
import os
import dotenv


from x10.perpetual.accounts import StarkPerpetualAccount
from x10.perpetual.configuration import MAINNET_CONFIG
from x10.perpetual.orders import OrderSide
from x10.perpetual.simple_client.simple_trading_client import BlockingTradingClient


async def setup_and_run():
    api_key = os.getenv("API_KEY")
    public_key = os.getenv("PUBLIC_KEY")
    private_key = os.getenv("PRIVATE_KEY")
    vault = int(os.getenv("VAULT"))

    stark_account = StarkPerpetualAccount(
        vault=vault,
        private_key=private_key,
        public_key=public_key,
        api_key=api_key,
    )

    client = await BlockingTradingClient.create(endpoint_config=MAINNET_CONFIG, account=stark_account)

    placed_order = await client.create_and_place_order(
        amount_of_synthetic=Decimal("1"),
        price=Decimal("62133.6"),
        market_name="BTC-USD",
        side=OrderSide.BUY,
        post_only=False,
        external_id="test_order_123",
    )

    print(placed_order)

    await client.cancel_order(order_external_id=placed_order.external_id)


if __name__ == "__main__":
    asyncio.run(main=setup_and_run())