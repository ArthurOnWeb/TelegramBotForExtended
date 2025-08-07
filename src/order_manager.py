# order_manager.py
from decimal import Decimal
from x10.perpetual.orders import OrderSide
from x10.perpetual.simple_client.simple_trading_client import BlockingTradingClient


async def place_limit_order(
    client: BlockingTradingClient,
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
    placed_order = await client.create_and_place_order(
        market_name=market,
        amount_of_synthetic=quantity,
        price=price,
        side=side,
        post_only=post_only,
    )
    print(f"✅ Order {side.name} placed at {price} on {market} (id: {placed_order.id})")
    return placed_order


async def cancel_order(client: BlockingTradingClient, order_id: str):
    """
    Cancel an order using its ID

    :param client: BlockingTradingClient
    :param order_id: Id of the order to cancel
    """
    await client.cancel_order(order_id=order_id)
    print(f"❌ Order {order_id} canceled")


async def place_order_with_tp_sl(
    client: BlockingTradingClient,
    market: str,
    quantity: Decimal,
    price: Decimal,
    side: OrderSide,
) -> object:
    """
    Place an order with embedded take profit and stop loss.

    The take profit and stop loss are placed at ±0.05% of the entry
    price and sent in a single request.

    :param client: BlockingTradingClient
    :param market: market name (ex: BTC-USD)
    :param quantity: size of the order
    :param price: entry price
    :param side: OrderSide.BUY or OrderSide.SELL for the entry
    :return: object of the placed order
    """

    # Determine TP and SL prices relative to the entry
    tp_factor = Decimal("1.0005")
    sl_factor = Decimal("0.9995")

    if side == OrderSide.BUY:
        tp_price = price * tp_factor
        sl_price = price * sl_factor
    else:
        tp_price = price * sl_factor
        sl_price = price * tp_factor

    # Place the entry order with embedded TP and SL
    entry_order = await client.create_and_place_order(
        market_name=market,
        amount_of_synthetic=quantity,
        price=price,
        side=side,
        post_only=True,
        tpSlType="BOTH",
        takeProfit={"triggerPrice": tp_price, "price": tp_price},
        stopLoss={"triggerPrice": sl_price, "price": sl_price},
    )

    print(
        f"✅ Order {side.name} placed at {price} on {market} (id: {entry_order.id})"
    )

    return entry_order
