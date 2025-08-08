
from decimal import Decimal
from typing import Any, Dict, Optional

from x10.perpetual.orders import OrderSide
from x10.perpetual.trading_client import PerpetualTradingClient

async def place_limit_order(
    client: PerpetualTradingClient,
    market: str,
    quantity: Decimal,
    price: Decimal,
    side: OrderSide,
    post_only: bool = True,
    tp_sl_type: Optional[str] = None,
    take_profit: Optional[Dict[str, Any]] = None,
    stop_loss: Optional[Dict[str, Any]] = None,
):
    """Place a limit order (BUY or SELL) with optional Take Profit and Stop Loss.

    Parameters
    ----------
    client: PerpetualTradingClient
        Trading client instance used to communicate with the API.
    market: str
        Market name (e.g. ``"BTC-USD"``).
    quantity: Decimal
        Size of the order.
    price: Decimal
        Limit price.
    side: OrderSide
        ``OrderSide.BUY`` or ``OrderSide.SELL``.
    post_only: bool
        ``True`` for maker-only orders.
    tp_sl_type: Optional[str]
        ``"order"`` or ``"position"`` depending on how the TP/SL size
        should be determined.
    take_profit: Optional[Dict[str, Any]]
        Dictionary describing the take profit parameters. Expected keys are
        ``triggerPrice``, ``triggerPriceType``, ``price`` and ``priceType``.
    stop_loss: Optional[Dict[str, Any]]
        Dictionary describing the stop loss parameters with the same keys as
        :data:`take_profit`.

    Returns
    -------
    Placed order object returned by the client.
    """

    order_kwargs: Dict[str, Any] = {
        "market_name": market,
        "amount_of_synthetic": quantity,
        "price": price,
        "side": side,
        "post_only": post_only,
    }

    if tp_sl_type:
        order_kwargs["tp_sl_type"] = tp_sl_type
    if take_profit:
        order_kwargs["take_profit"] = take_profit
    if stop_loss:
        order_kwargs["stop_loss"] = stop_loss

    placed_order = await client.place_order(**order_kwargs)
    print(
        f"✅ Order {side.name} placed at {price} on {market} (id: {placed_order.data.id})"
    )
    return placed_order


async def cancel_order(client: PerpetualTradingClient, order_id: str):
    """
    Cancel an order using its ID

    :param client: BlockingTradingClient
    :param order_id: Id of the order to cancel
    """
    await client.orders.cancel_order(order_id=order_id)
    print(f"❌ Order {order_id} canceled")
