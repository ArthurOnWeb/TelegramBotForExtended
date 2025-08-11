from decimal import Decimal
from x10.perpetual.orders import OrderSide
from account import TradingAccount
from bracket_sdk import build_bracket_order_model

async def main():
    acc = TradingAccount()
    client = acc.get_async_client()

    # Récupère le MarketModel exact
    markets = await client.markets_info.get_markets()
    market = next(m for m in markets.data if m.name == "BTC-USDT")  # vérifie le vrai nom côté exchange

    # Domaine de signature EXACT du client
    cfg = getattr(client, "_PerpetualTradingClient__config")
    domain = cfg.starknet_domain

    order = build_bracket_order_model(
        account=acc.get_account(),
        market=market,
        starknet_domain=domain,
        side=OrderSide.BUY,
        qty=Decimal("0.01"),
        entry_price=Decimal("70000"),
        tp_trigger=Decimal("71000"), tp_price=Decimal("71500"),
        sl_trigger=Decimal("69000"), sl_price=Decimal("68500"),
        # entry_trigger=Decimal("70000"),  # si tu veux le distinguer
    )

    resp = await client.orders.place_order(order)
    print("✅ Bracket envoyé:", resp)

    await acc.close()
