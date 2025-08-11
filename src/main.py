# main.py
import asyncio
from decimal import Decimal

from x10.errors import X10Error
from x10.perpetual.orders import OrderSide
from account import TradingAccount
from bracket import build_bracket_order_model

async def main():
    acc = TradingAccount()
    client = acc.get_async_client()

    try:
        # 1) Market exact
        markets = await client.markets_info.get_markets()
        market = next(m for m in markets.data if m.name in ("BTC-USDT", "BTC-USD"))  # ajuste selon ce que tu vois

        # 2) Domaine de signature EXACT du client
        cfg = getattr(client, "_PerpetualTradingClient__config")
        domain = cfg.starknet_domain

        # 3) Construire le modèle parent CONDITIONAL
        order = build_bracket_order_model(
            account=acc.get_account(),
            market=market,
            starknet_domain=domain,
            side=OrderSide.BUY,
            qty=Decimal("0.01"),
            entry_price=Decimal("70000"),
            tp_trigger=Decimal("71000"), tp_price=Decimal("71500"),
            sl_trigger=Decimal("69000"), sl_price=Decimal("68500"),
        )

        # 4) (debug) Afficher la payload API (via la sérialisation officielle)
        payload = order.to_api_request_json()
        print(">>> ABOUT TO SEND /user/order")
        print(payload)

        # 5) Envoyer via la SDK (place_order du module)
        resp = await client.orders.place_order(order)
        print("✅ Placed:", resp.data if hasattr(resp, "data") else resp)

    except X10Error as e:
        # Erreurs réseau/API renvoyées par la SDK
        print("❌ X10Error:", e)
        raise
    except Exception as e:
        print("❌ Unexpected error:", repr(e))
        raise
    finally:
        await acc.close()  # évite les 'Unclosed client session'

if __name__ == "__main__":
    asyncio.run(main())
