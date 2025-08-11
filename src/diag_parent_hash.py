# diag_parent_hash.py
import math
from fast_stark_crypto import get_order_msg_hash


def _hash_with(
    *, base_asset_id:int, quote_asset_id:int, pos_id:int, pubkey:int,
    base_amt:int, quote_amt:int, fee_amt:int, expiration:int, salt:int,
    domain, fee_asset_use_base:bool
):
    fee_asset_id = base_asset_id if fee_asset_use_base else quote_asset_id
    return get_order_msg_hash(
        position_id=pos_id,
        base_asset_id=base_asset_id,
        base_amount=base_amt,
        quote_asset_id=quote_asset_id,
        quote_amount=quote_amt,
        fee_amount=fee_amt,
        fee_asset_id=fee_asset_id,
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
    b_signed  = int(dbg.synthetic_amount)
    q_signed  = int(dbg.collateral_amount)
    f_signed  = int(dbg.fee_amount)

    b_abs, q_abs, f_abs = abs(b_signed), abs(q_signed), abs(f_signed)

    base_asset_id  = int(market.synthetic_asset.settlement_external_id, 16)
    quote_asset_id = int(market.collateral_asset.settlement_external_id, 16)

    base_seconds = int(parent_limit.expiry_epoch_millis // 1000)
    exp_seconds  = math.ceil(base_seconds) + 14*24*3600
    exp_hours    = math.ceil(base_seconds/3600) + 14*24

    combos = []
    for use_abs, label_sign in [(True,"ABS"), (False,"SIGN")]:
        b = b_abs if use_abs else b_signed
        q = q_abs if use_abs else q_signed
        f = f_abs if use_abs else f_signed
        for use_hours, label_exp in [(True,"HOURS"), (False,"SECONDS")]:
            exp = exp_hours if use_hours else exp_seconds
            # fee asset: quote (collat) vs base (synthetic)
            for fee_on_base in [False, True]:
                fee_label = "FEE=QUOTE" if not fee_on_base else "FEE=BASE"
                # normal mapping
                combos.append((f"{label_sign}+{label_exp}+{fee_label}", base_asset_id, quote_asset_id, b, q, f, exp, fee_on_base))
                # mapping inversé base/quote (juste au cas où)
                combos.append((f"{label_sign}+{label_exp}+{fee_label}+SWAPPED", quote_asset_id, base_asset_id, q, b, f, exp, fee_on_base))

    print("SERVER HASH =", server_hash_hex)
    print("DOMAIN =", domain.name, domain.version, domain.chain_id, getattr(domain, "revision", None))
    for label, ba, qa, b, q, f, exp, fee_on_base in combos:
        h = _hash_with(
            base_asset_id=ba, quote_asset_id=qa,
            pos_id=int(account.vault), pubkey=int(account.public_key),
            base_amt=b, quote_amt=q, fee_amt=f,
            expiration=exp, salt=int(parent_limit.nonce),
            domain=domain, fee_asset_use_base=fee_on_base
        )
        print(f"{label:>28} -> {hex(h)}")
