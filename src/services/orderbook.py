from x10.perpetual.orderbook import OrderBook
from x10.perpetual.configuration import MAINNET_CONFIG


class OrderBookWatcher:
    """Light wrapper around the SDK order book stream.

    Use :meth:`start` to begin streaming and :meth:`stop` to gracefully
    close the underlying connection when finished.
    """

    def __init__(self, market_name: str = "BTC-USD"):
        self.market_name = market_name
        self.orderbook = None

    async def start(self) -> None:
        """Start watching the market's order book."""
        self.orderbook = await OrderBook.create(
            endpoint_config=MAINNET_CONFIG,
            market_name=self.market_name,
        )
        await self.orderbook.start_orderbook()

    async def stop(self) -> None:
        """Stop the underlying order book stream and release resources."""
        ob = self.orderbook
        if not ob:
            return
        try:
            if hasattr(ob, "stop_orderbook"):
                await ob.stop_orderbook()
            elif hasattr(ob, "aclose"):
                await ob.aclose()
            else:
                await ob.close()
        finally:
            self.orderbook = None

    def get_best_bid(self):
        return self.orderbook.best_bid()

    def get_best_ask(self):
        return self.orderbook.best_ask()

