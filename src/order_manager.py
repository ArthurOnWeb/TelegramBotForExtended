from typing import Any, Dict, Optional
from decimal import Decimal
import aiohttp
import uuid

from x10.errors import X10Error
from x10.perpetual.accounts import StarkPerpetualAccount
from x10.perpetual.configuration import StarknetDomain
from x10.perpetual.markets import MarketModel
from x10.perpetual.order_object import create_order_object
from x10.perpetual.orders import OrderSide, TimeInForce
from x10.perpetual.trading_client.base_module import BaseModule
from x10.utils.date import utc_now


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
    def __init__(
        self,
        endpoint_config,
        *,
        api_key: Optional[str] = None,
        stark_account: Optional[StarkPerpetualAccount] = None,
        override_domain: Optional[StarknetDomain] = None,
    ):
        super().__init__(endpoint_config, api_key=api_key, stark_account=stark_account)
        self._override_domain = override_domain
    """
    Poste un ordre CONDITIONNEL avec TP/SL (tpSlType=ORDER) sur /user/order,
    en réutilisant le même domaine de signature, les mêmes arrondis et la même
    session/clé API que la SDK officielle.
    """

    def _headers(self) -> Dict[str, str]:
        return {
            "X-Api-Key": self._get_api_key(), 
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    
    def _get_starknet_domain(self) -> StarknetDomain:
        if self._override_domain is not None:
            return self._override_domain

        cfg = self._get_endpoint_config()
        domain = getattr(cfg, "starknet_domain", None)
        if domain is not None:
            return domain

        #FALLBACK
        chain_id = 2 if "testnet" in cfg.signing_domain.lower() else 1
        return StarknetDomain(
            name=cfg.signing_domain,
            version="1",
            chain_id=chain_id,
            revision=0,
        )

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

        domain = self._get_starknet_domain()

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
            "id": (str(getattr(order_obj, "id", "")) or None),
        }

    async def place_bracket_order(
        self,
        *,
        market: MarketModel,      
        side: OrderSide,
        qty: Decimal,
        entry_price: Decimal,
        tp_trigger: Decimal,
        sl_trigger: Decimal,
        tp_price: Decimal,
        sl_price: Decimal,
        tif: TimeInForce = TimeInForce.GTT,
        reduce_only: bool = False,           # recommandé pour éviter d’augmenter la position
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
        # juste après avoir calculé parent_sig/tp_sig/sl_sig
        parent_id = parent_sig.get("id") or f"brkt-{uuid.uuid4()}"
        client_oid = client_order_id or parent_id


        # 2) payload final
        body: Dict[str, Any] = {
            "id": parent_id,
            "market": market.name,
            "type": "CONDITIONAL",
            "side": side.name,  # "BUY"/"SELL"
            "qty": str(qty),
            "price": str(entry_price),
            "timeInForce": tif.name,  # "GTT", etc.
            "expiryEpochMillis": parent_sig["expiryEpochMillis"],
            "fee": parent_sig["fee"],     
            "nonce": parent_sig["nonce"],      
            "reduceOnly": reduce_only,
            "postOnly": False,
            "selfTradeProtectionLevel": "ACCOUNT",
            "trigger": {            
                "triggerPrice": str(entry_price),    
                "triggerPriceType": "LAST",
                "direction": direction,
                "executionPriceType": "LIMIT",
            },
            "tpSlType": "ORDER",
            "settlement": parent_sig["settlement"],
            "takeProfit": {
                "triggerPrice": str(tp_trigger),
                "triggerPriceType": "LAST",
                "price": str(tp_price),
                "priceType": "LIMIT",
                "settlement": tp_sig["settlement"],
            },
            "stopLoss": {
                "triggerPrice": str(sl_trigger),
                "triggerPriceType": "LAST",
                "price": str(sl_price),
                "priceType": "MARKET",
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
                err = await r.text() or "<empty body>"
                raise X10Error(f"Order post failed ({r.status}): {err}")
            return await r.json()


async def place_bracket_order(
    *,
    client,             
    account,           
    market_name: str,
    quantity: Decimal,
    entry_price: Decimal,
    side: OrderSide,
    tp_trigger: Decimal, tp_price: Decimal,
    sl_trigger: Decimal, sl_price: Decimal,

) -> Dict[str, Any]:
    # 1) MarketModel via la SDK
    markets = await client.markets_info.get_markets()
    market = next(m for m in markets.data if m.name == market_name)

    domain = None
    try:
        cfg = getattr(client, "_PerpetualTradingClient__config", None)
        if cfg:
            domain = getattr(cfg, "starknet_domain", None)
    except Exception:
        domain = None

    stark_account = account.get_account()
    raw = OrdersRawModule(
        endpoint_config=account.get_endpoint_config(),
        api_key=stark_account.api_key,
        stark_account=stark_account,
        override_domain=domain,
    )

    # 3) Send
    resp = await raw.place_bracket_order(
        market=market,
        side=side,
        qty=quantity,
        entry_price=entry_price,
        tp_trigger=tp_trigger, tp_price=tp_price,
        sl_trigger=sl_trigger, sl_price=sl_price,
        reduce_only=False,
    )
    return resp