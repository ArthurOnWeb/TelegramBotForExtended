# bracket.py
import uuid
from decimal import Decimal
from typing import Optional

from x10.perpetual.accounts import StarkPerpetualAccount
from x10.perpetual.markets import MarketModel
from x10.perpetual.order_object import create_order_object
from x10.perpetual.orders import (
    OrderSide,
    OrderType,
    TimeInForce,
    SelfTradeProtectionLevel,
    PerpetualOrderModel,
    # ces modèles/enums existent dans orders.py :
    # TriggerPriceType, ExecutionPriceType, OrderTpslType,
    CreateOrderConditionalTriggerModel,
    CreateOrderTpslTriggerModel,
    OrderTpslType,
)
from x10.utils.date import utc_now


def build_bracket_order_model(
    *,
    account: StarkPerpetualAccount,
    market: MarketModel,
    starknet_domain,                       # ← prends-le depuis client.__config.starknet_domain
    side: OrderSide,
    qty: Decimal,
    entry_price: Decimal,
    tp_trigger: Decimal, tp_price: Decimal,
    sl_trigger: Decimal, sl_price: Decimal,
    tif: TimeInForce = TimeInForce.GTT,
    self_trade: SelfTradeProtectionLevel = SelfTradeProtectionLevel.ACCOUNT,
    client_order_id: Optional[str] = None,
):
    """
    Construit un PerpetualOrderModel (type=CONDITIONAL) prêt pour place_order(...)
    en s'appuyant sur create_order_object pour signer parent/TP/SL.
    """

    # 1) Signe les 3 "LIMIT-like" pour récupérer settlement/fee/nonce/expiry
    expire_time = utc_now()  # la factory ajout. +1h (et +14j dans le hash), garde tel quel
    opp = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY

    parent_lim = create_order_object(
        account=account, market=market,
        amount_of_synthetic=qty, price=entry_price, side=side,
        starknet_domain=starknet_domain,
        time_in_force=tif, expire_time=expire_time,
        self_trade_protection_level=self_trade,
        order_external_id=client_order_id,   # utile si tu veux un id contrôlé
    )
    tp_lim = create_order_object(
        account=account, market=market,
        amount_of_synthetic=qty, price=tp_price, side=opp,
        starknet_domain=starknet_domain,
        time_in_force=tif, expire_time=expire_time,
        self_trade_protection_level=self_trade,
    )
    sl_lim = create_order_object(
        account=account, market=market,
        amount_of_synthetic=qty, price=sl_price, side=opp,
        starknet_domain=starknet_domain,
        time_in_force=tif, expire_time=expire_time,
        self_trade_protection_level=self_trade,
    )

    # 2) Construit le parent CONDITIONAL en réutilisant les champs critiques du parent LIMIT
    parent_id = parent_lim.id or (client_order_id or f"brkt-{uuid.uuid4()}")

    parent = PerpetualOrderModel(
        id=parent_id,
        market=market.name,
        type=OrderType.CONDITIONAL,
        side=side,
        qty=parent_lim.qty,                   # garde la même qty (Decimal)
        price=entry_price,
        reduce_only=False,                    # parent d'entrée ne peut PAS être reduceOnly
        post_only=False,
        time_in_force=tif,
        expiry_epoch_millis=parent_lim.expiry_epoch_millis,
        fee=parent_lim.fee,                   # requis par l’API
        nonce=parent_lim.nonce,               # requis par l’API
        self_trade_protection_level=self_trade,
        settlement=parent_lim.settlement,     # ← la signature StarkEx du parent
        trigger=CreateOrderConditionalTriggerModel(
            trigger_price=entry_price,
            trigger_price_type="LAST",        # ou TriggerPriceType.LAST si exposé
            direction="UP" if side == OrderSide.BUY else "DOWN",
            execution_price_type="LIMIT",     # on signe LIMIT => on exécute LIMIT
        ),
        tp_sl_type=OrderTpslType.ORDER,
        take_profit=CreateOrderTpslTriggerModel(
            trigger_price=tp_trigger,
            trigger_price_type="LAST",
            price=tp_price,
            price_type="LIMIT",
            reduce_only=True,
            settlement=tp_lim.settlement,     # signature de la jambe TP
        ),
        stop_loss=CreateOrderTpslTriggerModel(
            trigger_price=sl_trigger,
            trigger_price_type="LAST",
            price=sl_price,
            price_type="LIMIT",
            reduce_only=True,
            settlement=sl_lim.settlement,     # signature de la jambe SL
        ),
        # debugging_amounts=parent_lim.debugging_amounts,  # optionnel
    )

    return parent
