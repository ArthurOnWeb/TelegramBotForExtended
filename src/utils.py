import logging
import logging.config
import logging.handlers
from account import trading_client

async def clean_it(trading_client):
    logger = logging.getLogger("placed_order_example")
    positions    = await trading_client.account.get_positions()
    balance      = await trading_client.account.get_balance()
    open_orders  = await trading_client.account.get_open_orders()
    await trading_client.orders.mass_cancel(…)