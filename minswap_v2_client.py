"""
Minswap V2 Direct DEX Client

Direct on-chain interaction with Minswap V2 AMM pools — no aggregator.
Queries pool state via Blockfrost, computes swap output locally using the
constant-product formula, builds the order UTxO with the correct Plutus datum,
signs locally, and submits via Blockfrost.

Flow: discover pool → estimate swap → build order tx → sign → submit

Minswap V2 uses a batcher model: user creates an Order UTxO at the order
script address with an inline datum describing the swap. The Minswap batcher
picks it up and executes it against the pool. Batcher fee is 2 ADA,
deposit is 2 ADA (returned after execution).

On-chain specs: https://github.com/minswap/minswap-dex-v2/blob/main/amm-v2-docs/amm-v2-specs.md
SDK reference: https://github.com/minswap/sdk
"""

import logging
import os
import time
from dataclasses import dataclass, field
from hashlib import blake2b, sha256, sha3_256
from typing import Optional

import cbor2
from pycardano import (
    Address,
    Asset,
    AssetName,
    ExecutionUnits,
    MultiAsset,
    PaymentSigningKey,
    PaymentVerificationKey,
    Redeemer,
    ScriptHash,
    TransactionBuilder,
    TransactionId,
    TransactionInput,
    TransactionOutput,
    UTxO,
    Value,
    VerificationKeyHash,
)
from pycardano.backend.blockfrost import BlockFrostChainContext

from blockfrost_client import BlockfrostClient

logger = logging.getLogger(__name__)

ADA_LOVELACE = 1_000_000

# ---------------------------------------------------------------------------
# Mainnet contract constants (from Minswap SDK constants.ts)
# ---------------------------------------------------------------------------

AUTHEN_POLICY_ID = "f5808c2c990d86da54bfc97d89cee6efa20cd8461616359478d96b4c"
POOL_NFT_ASSET_NAME_HEX = "4d5350"  # "MSP"
FACTORY_ASSET_NAME_HEX = "4d5346"  # "MSF"
LP_POLICY_ID = AUTHEN_POLICY_ID

POOL_SCRIPT_HASH = "ea07b733d932129c378af627436e7cbc2ef0bf96e0036bb51b3bde6b"
ORDER_SCRIPT_HASH = "c3e28c36c3447315ba5a56f33da6a6ddc1770a876a8d9f0cb3a97c4c"
ORDER_ENTERPRISE_ADDRESS = "addr1w8p79rpkcdz8x9d6tft0x0dx5mwuzac2sa4gm8cvkw5hcnqst2ctf"

POOL_BATCHING_STAKE_ADDRESS = "stake17y02a946720zw6pw50upt2arvxsvvpvaghjtl054h0f0gjsfyjz59"

POOL_ADDRESS = "addr1z84q0denmyep98ph3tmzwsmw0j7zau9ljmsqx6a4rvaau66j2c79gy9l76sdg0xwhd7r0c0kna0tycz4y5s6mlenh8pq777e2a"
POOL_AUTHEN_UNIT = AUTHEN_POLICY_ID + POOL_NFT_ASSET_NAME_HEX

DEPLOYED_SCRIPTS = {
    "order": {
        "tx_hash": "cf4ecddde0d81f9ce8fcc881a85eb1f8ccdaf6807f03fea4cd02da896a621776",
        "output_index": 0,
    },
    "pool": {
        "tx_hash": "2536194d2a976370a932174c10975493ab58fd7c16395d50e62b7c0e1949baea",
        "output_index": 0,
    },
    "pool_batching": {
        "tx_hash": "d46bd227bd2cf93dedd22ae9b6d92d30140cf0d68b756f6608e38d680c61ad17",
        "output_index": 0,
    },
}

BATCHER_FEE = 2_000_000
FIXED_DEPOSIT_ADA = 2_000_000
DEFAULT_POOL_ADA = 3_000_000

FEE_DENOMINATOR = 10_000


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PoolV2State:
    """Live Minswap V2 pool state from on-chain UTxO."""
    pool_address: str
    tx_hash: str
    output_index: int
    asset_a: str
    asset_b: str
    reserve_a: int
    reserve_b: int
    total_liquidity: int
    base_fee_a_numerator: int
    base_fee_b_numerator: int
    fee_sharing_numerator: Optional[int]
    allow_dynamic_fee: bool
    lp_asset_name: str
    pool_nft_unit: str
    raw_datum: dict = field(default_factory=dict, repr=False)

    @property
    def lp_asset(self) -> str:
        return LP_POLICY_ID + self.lp_asset_name

    @property
    def fee_a_pct(self) -> float:
        return self.base_fee_a_numerator / FEE_DENOMINATOR * 100

    @property
    def fee_b_pct(self) -> float:
        return self.base_fee_b_numerator / FEE_DENOMINATOR * 100


@dataclass
class SwapEstimate:
    """Estimated output for a swap against a specific pool."""
    direction: str
    amount_in: int
    amount_out: int
    price_impact_pct: float
    fee_numerator: int
    lp_fee_paid: int
    new_reserve_a: int
    new_reserve_b: int


# ---------------------------------------------------------------------------
# LP asset name computation
# ---------------------------------------------------------------------------

def compute_lp_asset_name(asset_a: str, asset_b: str) -> str:
    """Compute LP token name: SHA3_256(SHA3_256(AssetA_hex) || SHA3_256(AssetB_hex)).

    Assets must be sorted. ADA is represented as empty hex string.
    Matches Minswap SDK: sha3(policyId + tokenName) where both are hex strings.
    """
    def asset_to_hex(asset: str) -> str:
        if not asset or asset == "lovelace":
            return ""
        return asset

    k1 = sha3_256(bytes.fromhex(asset_to_hex(asset_a)) if asset_to_hex(asset_a) else b"").hexdigest()
    k2 = sha3_256(bytes.fromhex(asset_to_hex(asset_b)) if asset_to_hex(asset_b) else b"").hexdigest()
    return sha3_256(bytes.fromhex(k1 + k2)).hexdigest()


def sort_assets(asset_a: str, asset_b: str) -> tuple[str, str]:
    """Sort two assets in Minswap canonical order (ADA/lovelace always first)."""
    a = "" if asset_a == "lovelace" else asset_a
    b = "" if asset_b == "lovelace" else asset_b
    if a < b:
        return (asset_a, asset_b)
    return (asset_b, asset_a)


# ---------------------------------------------------------------------------
# Pool discovery via Blockfrost
# ---------------------------------------------------------------------------

def find_pool_by_token(
    blockfrost: BlockfrostClient,
    token_id: str,
) -> Optional[PoolV2State]:
    """Find the ADA/token Minswap V2 pool for a given token.

    Queries the known Minswap V2 pool address for UTxOs containing the
    target token, then finds the pool UTxO (has pool authen NFT) with
    the matching datum.
    """
    return find_pool(blockfrost, "lovelace", token_id)


def find_pool(
    blockfrost: BlockfrostClient,
    asset_a: str,
    asset_b: str,
) -> Optional[PoolV2State]:
    """Find a Minswap V2 pool for a given asset pair.

    Uses Blockfrost's address/utxos/asset endpoint to efficiently query
    only UTxOs at the pool address that contain the non-ADA asset.
    For ADA/token pairs, queries by the token. For token/token pairs,
    queries by asset_b.
    """
    sorted_a, sorted_b = sort_assets(asset_a, asset_b)
    lp_name = compute_lp_asset_name(sorted_a, sorted_b)

    search_asset = sorted_b if sorted_a == "lovelace" else sorted_b

    try:
        utxos = blockfrost._get(f"/addresses/{POOL_ADDRESS}/utxos/{search_asset}")
    except Exception as e:
        logger.debug("Failed to query pool UTxOs for %s: %s", search_asset, e)
        return None

    best_utxo = None
    best_ada = 0

    for utxo in utxos:
        has_authen = any(
            a["unit"] == POOL_AUTHEN_UNIT and int(a["quantity"]) == 1
            for a in utxo.get("amount", [])
        )
        if not has_authen:
            continue

        inline_datum = utxo.get("inline_datum")
        if not inline_datum:
            continue

        try:
            datum = _decode_pool_datum(inline_datum)
        except Exception as e:
            logger.debug("Failed to decode pool datum: %s", e)
            continue

        if datum["asset_a"] != sorted_a or datum["asset_b"] != sorted_b:
            continue

        ada = _get_amount(utxo, "lovelace")
        if ada > best_ada:
            best_ada = ada
            best_utxo = (utxo, datum, lp_name)

    if not best_utxo:
        return None

    utxo, datum, lp_name = best_utxo
    return PoolV2State(
        pool_address=POOL_ADDRESS,
        tx_hash=utxo["tx_hash"],
        output_index=utxo["output_index"],
        asset_a=sorted_a,
        asset_b=sorted_b,
        reserve_a=datum["reserve_a"],
        reserve_b=datum["reserve_b"],
        total_liquidity=datum["total_liquidity"],
        base_fee_a_numerator=datum["base_fee_a_numerator"],
        base_fee_b_numerator=datum["base_fee_b_numerator"],
        fee_sharing_numerator=datum.get("fee_sharing_numerator"),
        allow_dynamic_fee=datum.get("allow_dynamic_fee", False),
        lp_asset_name=lp_name,
        pool_nft_unit=POOL_AUTHEN_UNIT,
        raw_datum=datum,
    )


def _get_amount(utxo: dict, asset: str) -> int:
    """Extract the quantity of a specific asset from a UTxO."""
    unit = "lovelace" if asset == "lovelace" else asset
    for a in utxo.get("amount", []):
        if a["unit"] == unit:
            return int(a["quantity"])
    return 0


def _decode_pool_datum(inline_datum_hex: str) -> dict:
    """Decode a Minswap V2 pool inline datum from CBOR hex.

    Pool datum fields (Constr0):
      [0] pool_batching_stake_credential
      [1] asset_a: Constr0 [bytes<policy>, bytes<name>]
      [2] asset_b: Constr0 [bytes<policy>, bytes<name>]
      [3] total_liquidity: uint
      [4] reserve_a: uint
      [5] reserve_b: uint
      [6] base_fee_a_numerator: uint
      [7] base_fee_b_numerator: uint
      [8] fee_sharing_numerator_opt: Constr0 [uint] | Constr1 []
      [9] allow_dynamic_fee: Constr0 [] (True) | Constr1 [] (False)
    """
    decoded = cbor2.loads(bytes.fromhex(inline_datum_hex))
    fields = decoded.value

    def parse_asset(constr) -> str:
        raw_policy = constr.value[0]
        raw_name = constr.value[1]
        policy = raw_policy.hex() if isinstance(raw_policy, bytes) and raw_policy else ""
        name = raw_name.hex() if isinstance(raw_name, bytes) and raw_name else ""
        if not policy and not name:
            return "lovelace"
        return policy + name

    fee_sharing = None
    if hasattr(fields[8], 'value') and fields[8].tag == 121:
        fee_sharing = int(fields[8].value[0]) if fields[8].value else None

    allow_dynamic = False
    if hasattr(fields[9], 'tag'):
        allow_dynamic = fields[9].tag == 121

    return {
        "asset_a": parse_asset(fields[1]),
        "asset_b": parse_asset(fields[2]),
        "total_liquidity": int(fields[3]),
        "reserve_a": int(fields[4]),
        "reserve_b": int(fields[5]),
        "base_fee_a_numerator": int(fields[6]),
        "base_fee_b_numerator": int(fields[7]),
        "fee_sharing_numerator": fee_sharing,
        "allow_dynamic_fee": allow_dynamic,
    }


# ---------------------------------------------------------------------------
# Swap estimation (constant-product with fees)
# ---------------------------------------------------------------------------

def estimate_swap(
    pool: PoolV2State,
    asset_in: str,
    amount_in: int,
) -> SwapEstimate:
    """Estimate swap output using the constant-product formula with fees.

    Minswap V2 fee: amount_in is reduced by the fee before computing output.
    fee_numerator / 10000 is the fee percentage (e.g. 30 = 0.3%).

    Formula:
        effective_in = amount_in * (10000 - fee_numerator) / 10000
        amount_out = reserve_out * effective_in / (reserve_in + effective_in)
    """
    in_is_a = (asset_in == pool.asset_a) or (asset_in == "lovelace" and pool.asset_a == "lovelace")

    if in_is_a:
        reserve_in = pool.reserve_a
        reserve_out = pool.reserve_b
        fee_num = pool.base_fee_a_numerator
        direction = "a_to_b"
    else:
        reserve_in = pool.reserve_b
        reserve_out = pool.reserve_a
        fee_num = pool.base_fee_b_numerator
        direction = "b_to_a"

    effective_in = amount_in * (FEE_DENOMINATOR - fee_num) // FEE_DENOMINATOR
    lp_fee = amount_in - effective_in

    if reserve_in + effective_in == 0:
        return SwapEstimate(
            direction=direction, amount_in=amount_in, amount_out=0,
            price_impact_pct=100.0, fee_numerator=fee_num, lp_fee_paid=lp_fee,
            new_reserve_a=pool.reserve_a, new_reserve_b=pool.reserve_b,
        )

    amount_out = reserve_out * effective_in // (reserve_in + effective_in)

    if in_is_a:
        new_reserve_a = reserve_in + amount_in
        new_reserve_b = reserve_out - amount_out
    else:
        new_reserve_a = reserve_out - amount_out
        new_reserve_b = reserve_in + amount_in

    mid_price = reserve_out / reserve_in if reserve_in > 0 else 0
    exec_price = amount_out / amount_in if amount_in > 0 else 0
    price_impact = abs(1 - exec_price / mid_price) * 100 if mid_price > 0 else 0

    return SwapEstimate(
        direction=direction,
        amount_in=amount_in,
        amount_out=amount_out,
        price_impact_pct=price_impact,
        fee_numerator=fee_num,
        lp_fee_paid=lp_fee,
        new_reserve_a=new_reserve_a,
        new_reserve_b=new_reserve_b,
    )


# ---------------------------------------------------------------------------
# Order datum encoding (Plutus V2 CBOR)
# ---------------------------------------------------------------------------

def _constr(tag: int, fields: list):
    """Build a CBOR Tagged value for a Plutus constructor."""
    return cbor2.CBORTag(121 + tag, fields)


def _asset_to_plutus(asset_id: str):
    """Convert asset ID to Plutus Constr0 [bytes<policy>, bytes<name>]."""
    if not asset_id or asset_id == "lovelace":
        return _constr(0, [b"", b""])
    policy = asset_id[:56]
    name = asset_id[56:]
    return _constr(0, [bytes.fromhex(policy), bytes.fromhex(name)])


def _address_to_plutus_credential(address: str):
    """Convert a bech32 address to Plutus payment credential (PubKeyHash)."""
    addr = Address.from_primitive(address)
    pkh = addr.payment_part.payload
    return _constr(0, [pkh])  # Constr0 = PubKeyCredential


def _address_to_plutus_full(address: str):
    """Convert a bech32 address to full Plutus address representation.

    Plutus address: Constr0 [credential, maybe_stake_credential]
    """
    addr = Address.from_primitive(address)
    payment_cred = _constr(0, [addr.payment_part.payload])

    if addr.staking_part:
        stake_cred = _constr(0, [addr.staking_part.payload])
        stake_inline = _constr(0, [stake_cred])
        maybe_stake = _constr(0, [stake_inline])
    else:
        maybe_stake = _constr(1, [])

    return _constr(0, [payment_cred, maybe_stake])


def encode_swap_exact_in_datum(
    *,
    sender_address: str,
    lp_asset_name: str,
    direction_a_to_b: bool,
    swap_amount: int,
    minimum_receive: int,
    kill_on_failed: bool = True,
    batcher_fee: int = BATCHER_FEE,
    expired_time_ms: Optional[int] = None,
    max_cancel_tip: int = 300_000,
) -> bytes:
    """Encode a Minswap V2 SwapExactIn order datum as CBOR bytes.

    Order V2 datum (Constr0):
      [0] canceller: AuthorizationMethod (Constr0 [pkh] = Signature)
      [1] refund_receiver: Address
      [2] refund_receiver_datum: ExtraDatum (Constr0 [] = NoDatum)
      [3] success_receiver: Address
      [4] success_receiver_datum: ExtraDatum (Constr0 [] = NoDatum)
      [5] lp_asset: Constr0 [bytes<policy>, bytes<name>]
      [6] step: SwapExactIn = Constr0 [direction, swap_amount_option, minimum_receive, killable]
      [7] max_batcher_fee: uint
      [8] expired_setting_opt: Constr1 [] (None) | Constr0 [expired_time, max_tip]
    """
    addr = Address.from_primitive(sender_address)
    pkh = addr.payment_part.payload

    canceller = _constr(0, [pkh])

    receiver_addr = _address_to_plutus_full(sender_address)
    no_datum = _constr(0, [])

    lp_asset = _constr(0, [
        bytes.fromhex(LP_POLICY_ID),
        bytes.fromhex(lp_asset_name),
    ])

    direction = _constr(1, []) if direction_a_to_b else _constr(0, [])

    swap_amount_option = _constr(0, [swap_amount])

    killable = _constr(1, []) if kill_on_failed else _constr(0, [])

    step = _constr(0, [direction, swap_amount_option, minimum_receive, killable])

    if expired_time_ms:
        expired_setting = _constr(0, [expired_time_ms, max_cancel_tip])
    else:
        expired_setting = _constr(1, [])

    datum_fields = [
        canceller,
        receiver_addr,
        no_datum,
        receiver_addr,
        no_datum,
        lp_asset,
        step,
        batcher_fee,
        expired_setting,
    ]

    return cbor2.dumps(_constr(0, datum_fields))


# ---------------------------------------------------------------------------
# Order address computation
# ---------------------------------------------------------------------------

def get_order_address(sender_address: str) -> str:
    """Compute the order address by combining order script payment with sender's stake credential.

    Minswap V2 orders use the order script hash as payment credential
    and the sender's stake credential, so the ADA deposit returns to
    the sender's staking rewards address.
    """
    addr = Address.from_primitive(sender_address)
    order_payment = ScriptHash(bytes.fromhex(ORDER_SCRIPT_HASH))

    if addr.staking_part:
        order_addr = Address(
            payment_part=order_payment,
            staking_part=addr.staking_part,
            network=addr.network,
        )
    else:
        order_addr = Address(
            payment_part=order_payment,
            network=addr.network,
        )

    return order_addr.encode()


# ---------------------------------------------------------------------------
# Transaction building with pycardano
# ---------------------------------------------------------------------------

def _build_multi_asset(native_assets: dict) -> MultiAsset:
    """Build a pycardano MultiAsset from a {unit: quantity} dict."""
    multi = MultiAsset()
    for unit, qty in native_assets.items():
        policy_hex = unit[:56]
        name_hex = unit[56:]
        sh = ScriptHash(bytes.fromhex(policy_hex))
        an = AssetName(bytes.fromhex(name_hex))
        if sh not in multi:
            multi[sh] = Asset()
        multi[sh][an] = qty
    return multi


def build_swap_order_tx(
    *,
    blockfrost: BlockfrostClient,
    sender_address: str,
    pool: PoolV2State,
    asset_in: str,
    amount_in: int,
    minimum_receive: int,
    kill_on_failed: bool = True,
    expired_minutes: int = 60,
) -> str:
    """Build an unsigned swap order transaction.

    Creates a UTxO at the Minswap V2 order script address with the
    SwapExactIn datum. The batcher picks this up and executes against the pool.

    Returns unsigned CBOR hex.
    """
    in_is_a = (asset_in == pool.asset_a) or (asset_in == "lovelace" and pool.asset_a == "lovelace")
    direction_a_to_b = in_is_a

    expired_time_ms = int((time.time() + expired_minutes * 60) * 1000)

    datum_bytes = encode_swap_exact_in_datum(
        sender_address=sender_address,
        lp_asset_name=pool.lp_asset_name,
        direction_a_to_b=direction_a_to_b,
        swap_amount=amount_in,
        minimum_receive=minimum_receive,
        kill_on_failed=kill_on_failed,
        expired_time_ms=expired_time_ms,
    )

    order_address = get_order_address(sender_address)

    order_value = _build_order_value(asset_in, amount_in)

    sender_utxos = blockfrost.get_utxos(sender_address)
    if not sender_utxos:
        raise RuntimeError(f"No UTxOs found at {sender_address}")

    sender_addr = Address.from_primitive(sender_address)
    order_addr = Address.from_primitive(order_address)

    inputs = []
    input_value_lovelace = 0
    input_native = {}
    for utxo in sender_utxos:
        tx_in = TransactionInput(
            TransactionId(bytes.fromhex(utxo["tx_hash"])),
            utxo["output_index"],
        )
        inputs.append((tx_in, utxo))
        for a in utxo["amount"]:
            if a["unit"] == "lovelace":
                input_value_lovelace += int(a["quantity"])
            else:
                input_native[a["unit"]] = input_native.get(a["unit"], 0) + int(a["quantity"])

    order_lovelace = order_value.get("lovelace", 0)
    order_native = {k: v for k, v in order_value.items() if k != "lovelace"}

    order_multi = None
    if order_native:
        order_multi = _build_multi_asset(order_native)
        order_pycardano_value = Value(order_lovelace, order_multi)
    else:
        order_pycardano_value = Value(order_lovelace)

    order_output = TransactionOutput(
        order_addr,
        order_pycardano_value,
        datum=cbor2.loads(datum_bytes),
    )

    tx_fee_estimate = 300_000
    change_lovelace = input_value_lovelace - order_lovelace - tx_fee_estimate

    change_native = {}
    for unit, qty in input_native.items():
        remaining = qty - order_native.get(unit, 0)
        if remaining > 0:
            change_native[unit] = remaining

    if change_lovelace < 1_000_000:
        raise RuntimeError(
            f"Insufficient ADA. Need ~{order_lovelace + tx_fee_estimate + 1_000_000} lovelace, "
            f"have {input_value_lovelace}"
        )

    change_multi = None
    if change_native:
        change_multi = _build_multi_asset(change_native)
        change_value = Value(change_lovelace, change_multi)
    else:
        change_value = Value(change_lovelace)

    change_output = TransactionOutput(sender_addr, change_value)

    from pycardano import TransactionBody
    tx_body = TransactionBody(
        inputs=[tx_in for tx_in, _ in inputs],
        outputs=[order_output, change_output],
        fee=tx_fee_estimate,
    )

    tx_array = [tx_body.to_primitive(), {}, True, None]
    return cbor2.dumps(tx_array).hex()


def _build_order_value(asset_in: str, amount_in: int) -> dict:
    """Compute the value to lock in the order UTxO.

    Includes: batcher_fee + deposit + swap amount.
    If swapping ADA, the lovelace includes everything.
    If swapping a token, lovelace = batcher_fee + deposit, plus the token amount.
    """
    value = {}

    if asset_in == "lovelace":
        value["lovelace"] = amount_in + BATCHER_FEE + FIXED_DEPOSIT_ADA
    else:
        value["lovelace"] = BATCHER_FEE + FIXED_DEPOSIT_ADA
        value[asset_in] = amount_in

    return value


# ---------------------------------------------------------------------------
# Transaction signing
# ---------------------------------------------------------------------------

def sign_transaction(unsigned_cbor_hex: str, signing_key: PaymentSigningKey) -> str:
    """Sign an unsigned CBOR transaction and return the fully-signed hex.

    Hashes the original body bytes directly (no re-serialization) to
    preserve the transaction hash.
    """
    tx_bytes = bytes.fromhex(unsigned_cbor_hex)
    tx_array = cbor2.loads(tx_bytes)
    body_bytes = cbor2.dumps(tx_array[0])
    tx_hash = blake2b(body_bytes, digest_size=32).digest()

    vk = PaymentVerificationKey.from_signing_key(signing_key)
    signature = signing_key.sign(tx_hash)

    vkey_witness = [vk.payload, signature]
    existing_witnesses = tx_array[1] if isinstance(tx_array[1], dict) else {}
    existing_vkeys = existing_witnesses.get(0, [])
    existing_vkeys.append(vkey_witness)
    existing_witnesses[0] = existing_vkeys
    tx_array[1] = existing_witnesses

    return cbor2.dumps(tx_array).hex()


def get_tx_hash(unsigned_cbor_hex: str) -> str:
    """Compute the transaction hash from unsigned CBOR."""
    tx_bytes = bytes.fromhex(unsigned_cbor_hex)
    tx_array = cbor2.loads(tx_bytes)
    body_bytes = cbor2.dumps(tx_array[0])
    return blake2b(body_bytes, digest_size=32).hexdigest()


# ---------------------------------------------------------------------------
# Cancel stuck orders
# ---------------------------------------------------------------------------

def _blockfrost_utxo_to_pycardano(raw: dict) -> UTxO:
    """Convert a Blockfrost UTxO dict to a pycardano UTxO object."""
    tx_in = TransactionInput(
        TransactionId(bytes.fromhex(raw["tx_hash"])),
        raw["output_index"],
    )
    lovelace = 0
    multi = MultiAsset()
    for a in raw["amount"]:
        if a["unit"] == "lovelace":
            lovelace = int(a["quantity"])
        else:
            policy_hex = a["unit"][:56]
            name_hex = a["unit"][56:]
            sh = ScriptHash(bytes.fromhex(policy_hex))
            an = AssetName(bytes.fromhex(name_hex))
            if sh not in multi:
                multi[sh] = Asset()
            multi[sh][an] = int(a["quantity"])

    val = Value(lovelace, multi) if multi else Value(lovelace)
    addr = Address.from_primitive(raw["address"])

    from pycardano import RawCBOR
    script = None
    datum = None
    if raw.get("inline_datum"):
        datum = RawCBOR(bytes.fromhex(raw["inline_datum"]))

    if raw.get("reference_script_hash"):
        script_hex = raw.get("reference_script_hex")
        if script_hex:
            from pycardano import PlutusV2Script
            script = PlutusV2Script(bytes.fromhex(script_hex))

    return UTxO(tx_in, TransactionOutput(addr, val, datum_hash=None, datum=datum, script=script))


def _fetch_order_script(blockfrost: BlockfrostClient):
    """Fetch the Minswap V2 order Plutus V2 script from Blockfrost.

    Blockfrost returns the double-CBOR-encoded script. We must keep
    the outer CBOR wrapper so that pycardano computes the correct
    script hash (blake2b-224 of 0x02 || cbor_bytes).
    """
    from pycardano import PlutusV2Script
    script_cbor_resp = blockfrost._get(f"/scripts/{ORDER_SCRIPT_HASH}/cbor")
    return PlutusV2Script(bytes.fromhex(script_cbor_resp["cbor"]))


def cancel_orders(
    *,
    sender_address: str,
    signing_key: PaymentSigningKey,
    blockfrost: Optional[BlockfrostClient] = None,
    order_tx_hashes: Optional[list] = None,
) -> str:
    """Cancel stuck Minswap V2 orders and reclaim funds.

    Builds a Plutus script spending transaction that uses the
    CANCEL_ORDER_BY_OWNER redeemer (Constr index 1) to spend
    order UTxOs back to the sender.

    Args:
        sender_address: Bech32 Cardano address (must match the canceller in the datum).
        signing_key: pycardano PaymentSigningKey.
        blockfrost: Optional BlockfrostClient.
        order_tx_hashes: If provided, only cancel orders with these tx hashes.

    Returns:
        Transaction hash of the cancel tx.
    """
    if blockfrost is None:
        blockfrost = BlockfrostClient()

    order_address = get_order_address(sender_address)
    logger.info("Looking for orders at %s", order_address)

    raw_utxos = blockfrost.get_utxos(order_address)
    if not raw_utxos:
        raise RuntimeError("No order UTxOs found at the order address")

    if order_tx_hashes:
        raw_utxos = [u for u in raw_utxos if u["tx_hash"] in order_tx_hashes]
        if not raw_utxos:
            raise RuntimeError("None of the specified tx hashes found at the order address")

    logger.info("Found %d order UTxO(s) to cancel", len(raw_utxos))

    project_id = os.environ["BLOCKFROST_PROJECT_ID"]
    context = BlockFrostChainContext(
        project_id=project_id,
        base_url="https://cardano-mainnet.blockfrost.io/api/",
    )

    ref_tx_in = TransactionInput(
        TransactionId(bytes.fromhex(DEPLOYED_SCRIPTS["order"]["tx_hash"])),
        DEPLOYED_SCRIPTS["order"]["output_index"],
    )
    order_script = _fetch_order_script(blockfrost)
    ref_utxo = UTxO(ref_tx_in, TransactionOutput(
        Address.from_primitive("addr1qyqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqzj2c79gy9l76sdg0xwhd7r0c0kna0tycz4y5s6mlenh8pq6a0h00"),
        Value(12_490_000),
        script=order_script,
    ))

    builder = TransactionBuilder(context)

    for raw_utxo in raw_utxos:
        order_utxo = _blockfrost_utxo_to_pycardano(raw_utxo)
        cancel_redeemer = Redeemer(
            cbor2.CBORTag(122, []),
            ExecutionUnits(4_000_000, 1_500_000_000),
        )
        builder.add_script_input(
            utxo=order_utxo,
            script=ref_utxo,
            redeemer=cancel_redeemer,
        )

    addr = Address.from_primitive(sender_address)
    pkh = addr.payment_part
    builder.required_signers = [VerificationKeyHash(pkh.payload)]

    sender_utxos = blockfrost.get_utxos(sender_address)
    for raw_utxo in sender_utxos:
        utxo = _blockfrost_utxo_to_pycardano(raw_utxo)
        builder.add_input(utxo)

    builder.collateral = [
        _blockfrost_utxo_to_pycardano(sender_utxos[0])
    ]

    tx = builder.build_and_sign(
        signing_keys=[signing_key],
        change_address=addr,
    )

    tx_hex = tx.to_cbor().hex()
    tx_hash = blockfrost.submit_tx(tx_hex)
    logger.info("Cancel tx submitted: %s", tx_hash)
    return tx_hash


# ---------------------------------------------------------------------------
# Full pipelines
# ---------------------------------------------------------------------------

def execute_swap(
    *,
    token_id: str,
    amount_ada: float,
    sender_address: str,
    signing_key: PaymentSigningKey,
    slippage_pct: float = 1.0,
    blockfrost: Optional[BlockfrostClient] = None,
    dry_run: bool = False,
) -> dict:
    """End-to-end swap: find pool → estimate → build → sign → submit.

    Swaps ADA for the specified token on Minswap V2 directly.

    Args:
        token_id: Full token ID (policyId + assetNameHex).
        amount_ada: ADA to spend (display units).
        sender_address: Bech32 Cardano address.
        signing_key: pycardano PaymentSigningKey.
        slippage_pct: Slippage tolerance in percent.
        blockfrost: Optional BlockfrostClient.
        dry_run: If True, build but don't submit.

    Returns:
        Dict with pool info, estimate, and tx_hash.
    """
    if blockfrost is None:
        blockfrost = BlockfrostClient()

    amount_lovelace = int(amount_ada * ADA_LOVELACE)

    pool = find_pool_by_token(blockfrost, token_id)
    if not pool:
        raise RuntimeError(f"No Minswap V2 pool found for {token_id}")

    est = estimate_swap(pool, "lovelace", amount_lovelace)
    if est.amount_out <= 0:
        raise RuntimeError("Estimated output is zero")

    minimum_receive = int(est.amount_out * (1 - slippage_pct / 100))

    result = {
        "pool_address": pool.pool_address,
        "reserve_ada": pool.reserve_a,
        "reserve_token": pool.reserve_b,
        "fee_pct": pool.fee_a_pct,
        "estimated_output": est.amount_out,
        "minimum_receive": minimum_receive,
        "price_impact_pct": est.price_impact_pct,
        "lp_fee_lovelace": est.lp_fee_paid,
        "batcher_fee_lovelace": BATCHER_FEE,
        "deposit_lovelace": FIXED_DEPOSIT_ADA,
        "tx_hash": None,
    }

    if dry_run:
        return result

    unsigned_cbor = build_swap_order_tx(
        blockfrost=blockfrost,
        sender_address=sender_address,
        pool=pool,
        asset_in="lovelace",
        amount_in=amount_lovelace,
        minimum_receive=minimum_receive,
    )

    signed_cbor = sign_transaction(unsigned_cbor, signing_key)
    tx_hash = blockfrost.submit_tx(signed_cbor)

    result["tx_hash"] = tx_hash
    return result


def execute_sell(
    *,
    token_id: str,
    token_amount: int,
    sender_address: str,
    signing_key: PaymentSigningKey,
    slippage_pct: float = 1.0,
    blockfrost: Optional[BlockfrostClient] = None,
    dry_run: bool = False,
) -> dict:
    """End-to-end sell: find pool → estimate → build → sign → submit.

    Sells tokens for ADA on Minswap V2 directly.
    """
    if blockfrost is None:
        blockfrost = BlockfrostClient()

    pool = find_pool_by_token(blockfrost, token_id)
    if not pool:
        raise RuntimeError(f"No Minswap V2 pool found for {token_id}")

    est = estimate_swap(pool, token_id, token_amount)
    if est.amount_out <= 0:
        raise RuntimeError("Estimated output is zero")

    minimum_receive = int(est.amount_out * (1 - slippage_pct / 100))

    result = {
        "pool_address": pool.pool_address,
        "reserve_ada": pool.reserve_a,
        "reserve_token": pool.reserve_b,
        "fee_pct": pool.fee_b_pct,
        "estimated_output_ada": est.amount_out / ADA_LOVELACE,
        "minimum_receive_lovelace": minimum_receive,
        "price_impact_pct": est.price_impact_pct,
        "lp_fee_tokens": est.lp_fee_paid,
        "batcher_fee_lovelace": BATCHER_FEE,
        "deposit_lovelace": FIXED_DEPOSIT_ADA,
        "tx_hash": None,
    }

    if dry_run:
        return result

    unsigned_cbor = build_swap_order_tx(
        blockfrost=blockfrost,
        sender_address=sender_address,
        pool=pool,
        asset_in=token_id,
        amount_in=token_amount,
        minimum_receive=minimum_receive,
    )

    signed_cbor = sign_transaction(unsigned_cbor, signing_key)
    tx_hash = blockfrost.submit_tx(signed_cbor)

    result["tx_hash"] = tx_hash
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    """Command-line interface for direct Minswap V2 operations."""
    import argparse
    from dotenv import load_dotenv

    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Minswap V2 direct DEX client")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    pool_p = sub.add_parser("pool", help="Show pool state for a token")
    pool_p.add_argument("token_id", help="Full token ID (policyId + assetNameHex)")

    est_p = sub.add_parser("estimate", help="Estimate a swap")
    est_p.add_argument("token_id", help="Full token ID")
    est_p.add_argument("--ada", type=float, nargs="+", default=[10, 50, 100, 500],
                       help="ADA amounts to estimate (buy direction)")
    est_p.add_argument("--sell-tokens", type=int, nargs="+", default=None,
                       help="Token amounts to estimate (sell direction)")

    buy_p = sub.add_parser("buy", help="Buy tokens with ADA")
    buy_p.add_argument("token_id", help="Full token ID")
    buy_p.add_argument("--ada", type=float, required=True, help="ADA to spend")
    buy_p.add_argument("--slippage", type=float, default=1.0, help="Slippage %")
    buy_p.add_argument("--dry-run", action="store_true")

    sell_p = sub.add_parser("sell", help="Sell tokens for ADA")
    sell_p.add_argument("token_id", help="Full token ID")
    sell_p.add_argument("--tokens", type=int, required=True, help="Token amount (raw units)")
    sell_p.add_argument("--slippage", type=float, default=1.0, help="Slippage %")
    sell_p.add_argument("--dry-run", action="store_true")

    cancel_p = sub.add_parser("cancel", help="Cancel stuck orders and reclaim funds")
    cancel_p.add_argument("--tx-hash", nargs="*", default=None,
                          help="Specific order tx hashes to cancel (default: all)")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.command:
        parser.print_help()
        return

    bf = BlockfrostClient()

    if args.command == "pool":
        pool = find_pool_by_token(bf, args.token_id)
        if not pool:
            print(f"No Minswap V2 pool found for {args.token_id}")
            return
        print(f"Pool: {pool.pool_address}")
        print(f"  UTxO: {pool.tx_hash}#{pool.output_index}")
        print(f"  Asset A: {pool.asset_a}")
        print(f"  Asset B: {pool.asset_b}")
        print(f"  Reserve A: {pool.reserve_a:,} ({pool.reserve_a / ADA_LOVELACE:.2f} ADA)" if pool.asset_a == "lovelace" else f"  Reserve A: {pool.reserve_a:,}")
        print(f"  Reserve B: {pool.reserve_b:,}")
        print(f"  Fee A: {pool.fee_a_pct:.2f}% | Fee B: {pool.fee_b_pct:.2f}%")
        print(f"  LP Asset: {pool.lp_asset}")
        print(f"  Total Liquidity: {pool.total_liquidity:,}")

    elif args.command == "estimate":
        pool = find_pool_by_token(bf, args.token_id)
        if not pool:
            print(f"No Minswap V2 pool found for {args.token_id}")
            return
        print(f"Pool: {pool.reserve_a / ADA_LOVELACE:.2f} ADA / {pool.reserve_b:,} tokens")
        print(f"Fee: {pool.fee_a_pct:.2f}%\n")

        if args.sell_tokens:
            print("  Sell estimates (token → ADA):")
            for tok in args.sell_tokens:
                est = estimate_swap(pool, args.token_id, tok)
                print(f"    {tok:>12,} tokens → {est.amount_out / ADA_LOVELACE:.4f} ADA "
                      f"(impact: {est.price_impact_pct:.2f}%, fee: {est.lp_fee_paid:,})")
        else:
            print("  Buy estimates (ADA → token):")
            for ada in args.ada:
                lovelace = int(ada * ADA_LOVELACE)
                est = estimate_swap(pool, "lovelace", lovelace)
                print(f"    {ada:>8.0f} ADA → {est.amount_out:>12,} tokens "
                      f"(impact: {est.price_impact_pct:.2f}%, fee: {est.lp_fee_paid / ADA_LOVELACE:.4f} ADA)")

    elif args.command == "buy":
        address = os.environ["CARDANO_PAYMENT_ADDRESS"]
        key_path = os.environ["CARDANO_PRIVATE_KEY_PATH"]
        skey = PaymentSigningKey.load(key_path)

        print(f"{'='*55}")
        print(f"  Minswap V2 Direct Buy — {args.ada} ADA")
        print(f"{'='*55}")
        print(f"  Address: {address[:20]}...{address[-10:]}")
        print(f"  Slippage: {args.slippage}%")
        print(f"  Mode: {'DRY RUN' if args.dry_run else 'LIVE'}\n")

        result = execute_swap(
            token_id=args.token_id,
            amount_ada=args.ada,
            sender_address=address,
            signing_key=skey,
            slippage_pct=args.slippage,
            blockfrost=bf,
            dry_run=args.dry_run,
        )

        print(f"  Pool: {result['pool_address'][:30]}...")
        print(f"  Reserves: {result['reserve_ada'] / ADA_LOVELACE:.2f} ADA / {result['reserve_token']:,} tokens")
        print(f"  Estimated output: {result['estimated_output']:,} tokens")
        print(f"  Minimum receive: {result['minimum_receive']:,} tokens")
        print(f"  Price impact: {result['price_impact_pct']:.2f}%")
        print(f"  LP fee: {result['lp_fee_lovelace'] / ADA_LOVELACE:.4f} ADA")
        print(f"  Batcher fee: {BATCHER_FEE / ADA_LOVELACE:.0f} ADA")
        print(f"  Deposit: {FIXED_DEPOSIT_ADA / ADA_LOVELACE:.0f} ADA (returned)")

        if result["tx_hash"]:
            print(f"\n  Transaction submitted!")
            print(f"  Tx hash: {result['tx_hash']}")
            print(f"  https://cardanoscan.io/transaction/{result['tx_hash']}")
        elif args.dry_run:
            print(f"\n  DRY RUN — transaction built but not submitted.")

    elif args.command == "sell":
        address = os.environ["CARDANO_PAYMENT_ADDRESS"]
        key_path = os.environ["CARDANO_PRIVATE_KEY_PATH"]
        skey = PaymentSigningKey.load(key_path)

        print(f"{'='*55}")
        print(f"  Minswap V2 Direct Sell — {args.tokens:,} tokens")
        print(f"{'='*55}")
        print(f"  Address: {address[:20]}...{address[-10:]}")
        print(f"  Slippage: {args.slippage}%")
        print(f"  Mode: {'DRY RUN' if args.dry_run else 'LIVE'}\n")

        result = execute_sell(
            token_id=args.token_id,
            token_amount=args.tokens,
            sender_address=address,
            signing_key=skey,
            slippage_pct=args.slippage,
            blockfrost=bf,
            dry_run=args.dry_run,
        )

        print(f"  Pool: {result['pool_address'][:30]}...")
        print(f"  Reserves: {result['reserve_ada'] / ADA_LOVELACE:.2f} ADA / {result['reserve_token']:,} tokens")
        print(f"  Estimated output: {result['estimated_output_ada']:.4f} ADA")
        print(f"  Minimum receive: {result['minimum_receive_lovelace'] / ADA_LOVELACE:.4f} ADA")
        print(f"  Price impact: {result['price_impact_pct']:.2f}%")
        print(f"  Batcher fee: {BATCHER_FEE / ADA_LOVELACE:.0f} ADA")
        print(f"  Deposit: {FIXED_DEPOSIT_ADA / ADA_LOVELACE:.0f} ADA (returned)")

        if result["tx_hash"]:
            print(f"\n  Transaction submitted!")
            print(f"  Tx hash: {result['tx_hash']}")
            print(f"  https://cardanoscan.io/transaction/{result['tx_hash']}")
        elif args.dry_run:
            print(f"\n  DRY RUN — transaction built but not submitted.")

    elif args.command == "cancel":
        address = os.environ["CARDANO_PAYMENT_ADDRESS"]
        key_path = os.environ["CARDANO_PRIVATE_KEY_PATH"]
        skey = PaymentSigningKey.load(key_path)

        print(f"{'='*55}")
        print(f"  Minswap V2 — Cancel Stuck Orders")
        print(f"{'='*55}")
        print(f"  Address: {address[:20]}...{address[-10:]}")

        tx_hash = cancel_orders(
            sender_address=address,
            signing_key=skey,
            blockfrost=bf,
            order_tx_hashes=args.tx_hash,
        )
        print(f"\n  Cancel tx submitted!")
        print(f"  Tx hash: {tx_hash}")
        print(f"  https://cardanoscan.io/transaction/{tx_hash}")


if __name__ == "__main__":
    _cli()
