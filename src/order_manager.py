
# order_manager.py
from decimal import Decimal
from x10.perpetual.orders import OrderSide
from x10.perpetual.trading_client import PerpetualTradingClient

async def place_limit_order(
    client: PerpetualTradingClient,
    market: str,
    quantity: Decimal,
    price: Decimal,
    side: OrderSide,
    post_only: bool = True,
):
    """
    Place a limit order (BUY or SELL) using blocking client.

    :param client: BlockingTradingClient
    :param market: market name (ex: BTC-USD)
    :param quantity: size of the order
    :param price: limit price
    :param side: OrderSide.BUY ou OrderSide.SELL
    :param post_only: True = maker only
    :return: object of the placed order
    """
    placed_order = await client.place_order(
        market_name=market,
        amount_of_synthetic=quantity,
        price=price,
        side=side,
        post_only=post_only,
    )
    print(f"✅ Order {side.name} placed at {price} on {market} 
    "
    """
    (id: {placed_order.id})
    """
    )
    return placed_order


async def cancel_order(client: PerpetualTradingClient, order_id: str):
    """
    Cancel an order using its ID

    :param client: BlockingTradingClient
    :param order_id: Id of the order to cancel
    """
    await client.cancel_order(order_id=order_id)
    print(f"❌ Order {order_id} canceled")