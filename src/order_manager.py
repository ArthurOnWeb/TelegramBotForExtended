from typing import Any, Dict, Optional
from decimal import Decimal
import aiohttp

from x10.errors import X10Error
from x10.perpetual.accounts import StarkPerpetualAccount
from x10.perpetual.markets import MarketModel
from x10.perpetual.order_object import create_order_object
from x10.perpetual.orders import OrderSide, TimeInForce
from x10.perpetual.trading_client import PerpetualTradingClient
from x10.perpetual.configuration import EndpointConfig
from x10.utils.date import utc_now
from x10.perpetual.base_module import BaseModule 


def _settlement_to_api_dict(order_obj) -> Dict[str, Any]:
    st = order_obj.settlement
    return {
        "signature": {
            "r": hex(int(st.signature.r)),
            "s": hex(int(st.signature.s)),
        },
        "starkKey": hex(int(st.stark_key)),
        "collateralPosition": str(int(st.collateral_position)),
    }


class OrdersRawModule(BaseModule):
    """
    Poste un ordre CONDITIONNEL avec TP/SL (tpSlType=ORDER) sur /user/order,
    en réutilisant le même domaine de signature, les mêmes arrondis et la même
    session/clé API que la SDK officielle.
    """

    def _headers(self) -> Dict[str, str]:
        return {
            "X-Api-Key": self._get_api_key(), 
            "Content-Type": "application/json",
        }

    async def _sign_like_limit(
        self,
        *,
        account: StarkPerpetualAccount,
        market: MarketModel,
        qty: Decimal,
        price: Decimal,
        side: OrderSide,
        tif: TimeInForce = TimeInForce.GTT,
        expire_time=None,
        nonce: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Produit une signature identique à un LIMIT 'classique' via create_order_object(...).
        On ne réutilise que settlement/fee/expiry/nonce dans le JSON final (CONDITIONAL/TP/SL).
        """
        if expire_time is None:
            expire_time = utc_now()  # create_order_object ajoutera +1h; le hash ajoute +14j (comme la SDK)

        domain = self._get_endpoint_config().starknet_domain  # <-- clé : même domaine que place_order

        order_obj = create_order_object(
            account=account,
            market=market,
            amount_of_synthetic=qty,
            price=price,
            side=side,
            starknet_domain=domain,
            post_only=False,
            time_in_force=tif,
            expire_time=expire_time,
            nonce=nonce,
        )
        return {
            "settlement": _settlement_to_api_dict(order_obj),
            "fee": str(order_obj.fee),
            "expiryEpochMillis": int(order_obj.expiry_epoch_millis),
            "nonce": (str(int(order_obj.nonce)) if order_obj.nonce is not None else None),
        }

    async def place_bracket_order(
        self,
        *,
        market: MarketModel,                 # passe l'objet MarketModel (cf. exemple d’usage plus bas)
        side: OrderSide,
        qty: Decimal,
        entry_price: Decimal,
        tp_price: Decimal,
        sl_price: Decimal,
        tif: TimeInForce = TimeInForce.GTT,
        reduce_only: bool = True,           # recommandé pour éviter d’augmenter la position
        client_order_id: Optional[str] = None,
        nonce: Optional[int] = None,
    ) -> Dict[str, Any]:
        account = self._get_stark_account()
        opposite = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
        direction = "UP" if side == OrderSide.BUY else "DOWN"

        # 1) signatures LIMIT-like (parent/TP/SL)
        parent_sig = await self._sign_like_limit(
            account=account, market=market, qty=qty, price=entry_price, side=side, tif=tif, nonce=nonce
        )
        tp_sig = await self._sign_like_limit(
            account=account, market=market, qty=qty, price=tp_price, side=opposite, tif=tif
        )
        sl_sig = await self._sign_like_limit(
            account=account, market=market, qty=qty, price=sl_price, side=opposite, tif=tif
        )

        # 2) payload final
        body: Dict[str, Any] = {
            "market": market.name,
            "type": "LIMIT",
            "side": side.name,  # "BUY"/"SELL"
            "qty": str(qty),
            "price": str(entry_price),
            "timeInForce": tif.name,  # "GTT", etc.
            "expiryEpochMillis": parent_sig["expiryEpochMillis"],
            "fee": parent_sig["fee"],          # si l'API l'ignore, ce n'est pas bloquant
            "nonce": parent_sig["nonce"],      # idem
            "reduceOnly": reduce_only,
            "postOnly": False,
            "selfTradeProtectionLevel": "ACCOUNT",
            "tpSlType": "ORDER",
            "settlement": parent_sig["settlement"],
            "takeProfit": {
                "price": str(tp_price),
                "priceType": "LIMIT",
                "settlement": tp_sig["settlement"],
            },
            "stopLoss": {
                "price": str(sl_price),
                "priceType": "LIMIT",
                "settlement": sl_sig["settlement"],
            },
        }
        if client_order_id:
            body["clientOrderId"] = client_order_id

        # 3) POST
        url = self._get_url("/user/order")
        sess = await self.get_session()
        async with sess.post(url, json=body, headers=self._headers()) as r:
            if r.status >= 400:
                try:
                    err = await r.json()
                except aiohttp.ContentTypeError:
                    err = await r.text()
                raise X10Error(f"Order post failed ({r.status}): {err}")
            return await r.json()
