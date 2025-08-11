# diag_parent_hash.py
import math
from fast_stark_crypto import get_order_msg_hash

def _hash_with(
    *, base_asset_id:int, quote_asset_id:int, pos_id:int, pubkey:int,
    base_amt:int, quote_amt:int, fee_amt:int, expiration:int, salt:int,
    domain
):
    return get_order_msg_hash(
        position_id=pos_id,
        base_asset_id=base_asset_id,
        base_amount=base_amt,
        quote_asset_id=quote_asset_id,
        quote_amount=quote_amt,
        fee_amount=fee_amt,
        fee_asset_id=quote_asset_id,
        expiration=expiration,
        salt=salt,
        user_public_key=pubkey,
        domain_name=domain.name,
        domain_version=domain.version,
        domain_chain_id=domain.chain_id,
        domain_revision=domain.revision,
    )

def debug_parent_hash_variants(*, parent_limit, market, domain, account, server_hash_hex:str):
    dbg = parent_limit.debugging_amounts
    base_signed  = int(dbg.synthetic_amount)
    quote_signed = int(dbg.collateral_amount)
    fee_signed   = int(dbg.fee_amount)

    base_abs  = abs(base_signed)
    quote_abs = abs(quote_signed)
    fee_abs   = abs(fee_signed)

    base_asset_id  = int(market.synthetic_asset.settlement_external_id, 16)
    quote_asset_id = int(market.collateral_asset.settlement_external_id, 16)

    base_seconds = int(parent_limit.expiry_epoch_millis // 1000)
    exp_seconds  = math.ceil(base_seconds) + 14*24*3600
    exp_hours    = math.ceil(base_seconds/3600) + 14*24

    variants = [
        ("ABS + HOURS",   base_abs,   quote_abs,   fee_abs,   exp_hours),
        ("ABS + SECONDS", base_abs,   quote_abs,   fee_abs,   exp_seconds),
        ("SIGN + HOURS",  base_signed,quote_signed,fee_signed,exp_hours),
        ("SIGN + SECONDS",base_signed,quote_signed,fee_signed,exp_seconds),
    ]

    print("DOMAIN =", domain.name, domain.version, domain.chain_id, domain.revision)
    print("ASSET IDS =", hex(base_asset_id), hex(quote_asset_id))
    print("AMOUNTS signed =", base_signed, quote_signed, fee_signed)
    print("AMOUNTS abs    =", base_abs, quote_abs, fee_abs)
    print("EXP seconds/hours =", exp_seconds, exp_hours)
    print("SERVER HASH =", server_hash_hex)

    for label, b, q, f, exp in variants:
        h = _hash_with(
            base_asset_id=base_asset_id, quote_asset_id=quote_asset_id,
            pos_id=int(account.vault), pubkey=int(account.public_key),
            base_amt=b, quote_amt=q, fee_amt=f, expiration=exp,
            salt=int(parent_limit.nonce), domain=domain
        )
        print(f"{label:>16} -> {hex(h)}")
