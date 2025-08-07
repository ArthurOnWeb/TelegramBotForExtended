# order_manager.py
from decimal import Decimal
from typing import Tuple
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
) -> Tuple[object, object, object]:
    """
    Place an order and automatically attach a take profit and stop loss.

    :param client: BlockingTradingClient
    :param market: market name (ex: BTC-USD)
    :param quantity: size of the order
    :param price: entry price
    :param side: OrderSide.BUY or OrderSide.SELL for the entry
    :return: tuple containing entry, tp and sl orders
    """

    # 1. Place the entry order as maker
    entry_order = await place_limit_order(
        client=client,
        market=market,
        quantity=quantity,
        price=price,
        side=side,
        post_only=True,
    )

    # Determine TP and SL prices relative to the entry
    tp_factor = Decimal("1.0005")
    sl_factor = Decimal("0.9995")

    if side == OrderSide.BUY:
        tp_price = price * tp_factor
        sl_price = price * sl_factor
        exit_side = OrderSide.SELL
    else:
        tp_price = price * sl_factor
        sl_price = price * tp_factor
        exit_side = OrderSide.BUY

    # 2. Place the take profit order (maker)
    tp_order = await place_limit_order(
        client=client,
        market=market,
        quantity=quantity,
        price=tp_price,
        side=exit_side,
        post_only=True,
    )

    # 3. Place the stop loss order (taker)
    sl_order = await place_limit_order(
        client=client,
        market=market,
        quantity=quantity,
        price=sl_price,
        side=exit_side,
        post_only=False,
    )

    return entry_order, tp_order, sl_order
