# bracket_sdk.py
import uuid
from decimal import Decimal
from typing import Optional

from x10.perpetual.accounts import StarkPerpetualAccount
from x10.perpetual.markets import MarketModel
from x10.perpetual.order_object import create_order_object
from x10.perpetual.configuration import StarknetDomain
from x10.perpetual.orders import (
    OrderSide, OrderType, TimeInForce, SelfTradeProtectionLevel,
    PerpetualOrderModel, OrderTriggerPriceType, OrderTriggerDirection,
    OrderPriceType, OrderTpslType, CreateOrderConditionalTriggerModel,
    CreateOrderTpslTriggerModel,PerpetualOrderModel, StarkSettlementModel, SettlementSignatureModel
)
from x10.utils.date import utc_now

def build_bracket_order_model(
    *,
    account: StarkPerpetualAccount,
    market: MarketModel,
    starknet_domain,      # client.__config.starknet_domain
    side: OrderSide,
    qty: Decimal,
    entry_price: Decimal,
    tp_trigger: Decimal, tp_price: Decimal,
    sl_trigger: Decimal, sl_price: Decimal,
    entry_trigger: Optional[Decimal] = None,  
    tif: TimeInForce = TimeInForce.GTT,
    stp: SelfTradeProtectionLevel = SelfTradeProtectionLevel.ACCOUNT,
    external_id: Optional[str] = None,
) -> PerpetualOrderModel:

    expire_time = utc_now()
    opp = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY

    # 1) LIMIT-like pour récupérer signature/fee/nonce/expiry
    parent_lim = create_order_object(
        account=account, market=market,
        amount_of_synthetic=qty, price=entry_price, side=side,
        starknet_domain=starknet_domain,
        time_in_force=tif, expire_time=expire_time,
        self_trade_protection_level=stp,
        order_external_id=external_id,
    )
    tp_lim = create_order_object(
        account=account, market=market,
        amount_of_synthetic=qty, price=tp_price, side=opp,
        starknet_domain=starknet_domain,
        time_in_force=tif, expire_time=expire_time,
        self_trade_protection_level=stp,
    )
    sl_lim = create_order_object(
        account=account, market=market,
        amount_of_synthetic=qty, price=sl_price, side=opp,
        starknet_domain=starknet_domain,
        time_in_force=tif, expire_time=expire_time,
        self_trade_protection_level=stp,
    )

    parent_id = parent_lim.id or (external_id or f"brkt-{uuid.uuid4()}")
    trig_price = entry_trigger if entry_trigger is not None else entry_price

    parent_conditional_settlement = sign_parent_conditional_from_limit(
    parent_limit=parent_lim,
    account=account,
    market=market,
    domain=starknet_domain,   # récupéré depuis client.__config.starknet_domain
)
    # 2) Parent CONDITIONAL qui réutilise les champs critiques
    parent = PerpetualOrderModel(
        id=parent_id,
        market=market.name,
        type=OrderType.CONDITIONAL,
        side=side,
        qty=parent_lim.qty,
        price=entry_price,
        reduce_only=False,            # parent d’entrée → pas reduceOnly
        post_only=False,
        time_in_force=tif,
        expiry_epoch_millis=parent_lim.expiry_epoch_millis,
        fee=parent_lim.fee,           # requis par l’API
        nonce=parent_lim.nonce,       # requis par l’API
        self_trade_protection_level=stp,
        settlement=parent_conditional_settlement,  # signature StarkEx parent
        trigger=CreateOrderConditionalTriggerModel(
            trigger_price=trig_price,
            trigger_price_type=OrderTriggerPriceType.LAST,
            direction=OrderTriggerDirection.UP if side == OrderSide.BUY else OrderTriggerDirection.DOWN,
            execution_price_type=OrderPriceType.LIMIT,  # on signe LIMIT → on exécute LIMIT
        ),
        tp_sl_type=OrderTpslType.ORDER,
        take_profit=CreateOrderTpslTriggerModel(
            trigger_price=tp_trigger,
            trigger_price_type=OrderTriggerPriceType.LAST,
            price=tp_price,
            price_type=OrderPriceType.LIMIT,
            settlement=tp_lim.settlement,   # signature TP
        ),
        stop_loss=CreateOrderTpslTriggerModel(
            trigger_price=sl_trigger,
            trigger_price_type=OrderTriggerPriceType.LAST,
            price=sl_price,
            price_type=OrderPriceType.LIMIT,
            settlement=sl_lim.settlement,   # signature SL
        ),
    )
    return parent
    
# bracket_parent_conditional.py
import math
from decimal import Decimal
from fast_stark_crypto import get_order_msg_hash
from x10.perpetual.accounts import StarkPerpetualAccount
from x10.perpetual.markets import MarketModel
from x10.perpetual.configuration import StarknetDomain
from x10.perpetual.orders import StarkSettlementModel, SettlementSignatureModel, PerpetualOrderModel

def sign_parent_conditional_from_limit(
    *,
    parent_limit: PerpetualOrderModel,
    account: StarkPerpetualAccount,
    market: MarketModel,
    domain: StarknetDomain,
) -> StarkSettlementModel:
    # 1) Montants STARK depuis la factory (puis en VALEUR ABS)
    dbg = parent_limit.debugging_amounts
    base_amount  = abs(int(dbg.synthetic_amount))
    quote_amount = abs(int(dbg.collateral_amount))
    fee_amount   = abs(int(dbg.fee_amount))

    # 2) Expiration en HEURES (ceil(ms->s->h) + 14 jours)
    base_seconds = int(parent_limit.expiry_epoch_millis // 1000)
    expiration_hours = math.ceil(base_seconds / 3600) + 24 * 14

    # 3) Asset IDs
    base_asset_id  = int(market.synthetic_asset.settlement_external_id, 16)
    quote_asset_id = int(market.collateral_asset.settlement_external_id, 16)

    # 4) Hash & signature (même domaine/nonce/clé publique/position)
    msg_hash = get_order_msg_hash(
        position_id=account.vault,
        base_asset_id=base_asset_id,
        base_amount=base_amount,
        quote_asset_id=quote_asset_id,
        quote_amount=quote_amount,
        fee_amount=fee_amount,
        fee_asset_id=quote_asset_id,
        expiration=expiration_hours,
        salt=int(parent_limit.nonce),
        user_public_key=account.public_key,
        domain_name=domain.name,
        domain_version=domain.version,
        domain_chain_id=domain.chain_id,
        domain_revision=domain.revision,
    )
    r, s = account.sign(msg_hash)
    # (debug)
    print("LOCAL PARENT HASH =", hex(msg_hash), "EXP_HOURS =", expiration_hours)

    return StarkSettlementModel(
        signature=SettlementSignatureModel(r=r, s=s),
        stark_key=account.public_key,
        collateral_position=Decimal(account.vault),
    )
