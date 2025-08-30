import logging
import os
from typing import Optional, Union, Any
from x10.perpetual.trading_client import PerpetualTradingClient

logger = logging.getLogger("extended_bot")

# Internal guard to avoid re-initialising logging repeatedly
_LOGGING_CONFIGURED = False

def _resolve_level(level: Optional[Union[int, str]]) -> int:
    if isinstance(level, int):
        return level
    if isinstance(level, str):
        return logging.getLevelName(level.upper()) if isinstance(logging.getLevelName(level.upper()), int) else logging.INFO
    env = os.getenv("LOG_LEVEL") or os.getenv("EXT_LOG_LEVEL") or os.getenv("BOT_LOG_LEVEL")
    if env:
        resolved = logging.getLevelName(env.upper())
        if isinstance(resolved, int):
            return resolved
    return logging.INFO

def setup_logging(log_level: Optional[Union[int, str]] = None) -> None:
    """Initialise console logging in an idempotent, dependency-safe way.

    - Configures root logger once with a sane format.
    - Attaches a StreamHandler to the app logger and disables propagation to prevent duplicates.
    - Respects a provided level, otherwise falls back to env vars or INFO.
    """
    global _LOGGING_CONFIGURED

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    level = _resolve_level(log_level)

    if not _LOGGING_CONFIGURED:
        # Configure root just once; avoid 'force' to keep 3rd party handlers intact
        logging.basicConfig(level=level, format=fmt)
        _LOGGING_CONFIGURED = True

    # Ensure our app logger is always usable and not duplicated
    logger.setLevel(level)
    logger.propagate = False
    if all(isinstance(h, logging.NullHandler) for h in logger.handlers):
        h = logging.StreamHandler()
        h.setLevel(level)
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


async def close_orderbook(orderbook: Any) -> None:
    """Gracefully close an OrderBook regardless of SDK version.

    The X10 SDK has changed the shutdown method name across releases.  This
    helper attempts the available variants in order of preference and quietly
    ignores errors so callers can rely on it without littering their code with
    attribute checks.
    """
    if not orderbook:
        return
    try:
        if hasattr(orderbook, "stop_orderbook"):
            await orderbook.stop_orderbook()
        elif hasattr(orderbook, "aclose"):
            await orderbook.aclose()
        else:
            await orderbook.close()
    except Exception as exc:  # pragma: no cover - best effort cleanup
        logger.debug("error closing orderbook: %s", exc)
