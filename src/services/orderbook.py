from x10.perpetual.orderbook import OrderBook
from x10.perpetual.configuration import MAINNET_CONFIG

class OrderBookWatcher:
    def __init__(self, market_name="BTC-USD"):
        self.market_name = market_name
        self.orderbook = None

    async def start(self):
        self.orderbook = await OrderBook.create(
            endpoint_config=MAINNET_CONFIG,
            market_name=self.market_name
        )
        await self.orderbook.start_orderbook()

    def get_best_bid(self):
        return self.orderbook.best_bid()

    def get_best_ask(self):
        return self.orderbook.best_ask()

