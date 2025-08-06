from x10.perpetual.accounts import StarkPerpetualAccount
from x10.perpetual.configuration import TESTNET_CONFIG
from x10.perpetual.orders import OrderSide
from x10.perpetual.trading_client import PerpetualTradingClient

from account import stark_account

trading_client = PerpetualTradingClient.create(TESTNET_CONFIG, stark_account)
placed_order = await trading_client.place_order(
    market_name="BTC-USD",
    amount_of_synthetic=Decimal("1"),
    price=Decimal("63000.1"),
    side=OrderSide.BUY,
)
await trading_client.orders.cancel_order(order_id=placed_order.id)
print(placed_order)