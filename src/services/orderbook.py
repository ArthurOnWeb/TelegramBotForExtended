from x10.perpetual.orderbook import OrderBook

orderbook = await OrderBook.create(…)
await orderbook.start_orderbook()


best_bid = orderbook.best_bid()
best_ask = orderbook.best_ask()
