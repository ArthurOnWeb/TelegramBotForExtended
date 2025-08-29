import logging
from typing import Optional
from x10.perpetual.trading_client import PerpetualTradingClient

logger = logging.getLogger("extended_bot")

def setup_logging(log_level: int = logging.INFO):
    """Initialise console logging reliably.

    - Forces root logging configuration so third-party defaults don’t suppress output.
    - Attaches a StreamHandler to our named logger and disables propagation to avoid duplicates.
    """
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    # Force root configuration (Python 3.8+)
    try:
        logging.basicConfig(level=log_level, format=fmt, force=True)
    except TypeError:
        # Older Python: best effort without force
        logging.basicConfig(level=log_level, format=fmt)

    # Ensure our app logger has a console handler regardless of root state
    logger.setLevel(log_level)
    logger.propagate = False
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setLevel(log_level)
        h.setFormatter(logging.Formatter(fmt))
        logger.addHandler(h)

async def clean_account(trading_client: PerpetualTradingClient, verbose: bool = False):
    """
    Close all positions and orders

    :param trading_client: asynchron Extended (PerpetualTradingClient)
    :param verbose: If true, Display all positions
    """
    if verbose:
        positions = await trading_client.account.get_positions()
        balance   = await trading_client.account.get_balance()

        logger.info("Positions:\n%s", positions.to_pretty_json())
        logger.info("Balance:\n%s", balance.to_pretty_json())

    open_orders = await trading_client.account.get_open_orders()
    order_ids = [order.id for order in open_orders.data]

    if order_ids:
        await trading_client.orders.mass_cancel(order_ids=order_ids)
        logger.info("Cancel %d waiting orders.", len(order_ids))
    else:
        logger.info("No orders to cancel.")
