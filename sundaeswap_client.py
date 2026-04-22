"""
SundaeSwap V3 Direct DEX Client

Direct on-chain interaction with SundaeSwap V3 AMM pools — no aggregator.
Queries pool state via the SundaeSwap GraphQL API, computes swap output
locally using the constant-product formula with bid/ask fees, builds the
order UTxO with the correct Plutus V2 datum, signs locally, and submits
via Blockfrost.

Flow: discover pool → estimate swap → build order tx → sign → submit

SundaeSwap V3 uses a batcher model: user creates an Order UTxO at the
order script address with an inline datum describing the swap. Authorized
scoopers pick it up and execute it against the pool in batches of up to
35 orders.

Protocol spec: https://cdn.sundaeswap.finance/SundaeV3.pdf
Contracts:     https://github.com/SundaeSwap-finance/sundae-contracts
SDK:           https://github.com/SundaeSwap-finance/sundae-sdk
"""

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from hashlib import blake2b
from typing import Optional

import cbor2
import requests
from dotenv import load_dotenv
from pycardano import (
    Address,
    Asset,
    AssetName,
    MultiAsset,
    PaymentSigningKey,
    PaymentVerificationKey,
    ScriptHash,
    TransactionBody,
    TransactionId,
    TransactionInput,
    TransactionOutput,
    UTxO,
    Value,
)

from blockfrost_client import BlockfrostClient

load_dotenv()
logger = logging.getLogger(__name__)

ADA_LOVELACE = 1_000_000

# ---------------------------------------------------------------------------
# Mainnet contract constants (from SundaeSwap GraphQL /protocols endpoint)
# ---------------------------------------------------------------------------

ORDER_SCRIPT_HASH = "fa6a58bbe2d0ff05534431c8e2f0ef2cbdc1602a8456e4b13c8f3077"
POOL_SCRIPT_HASH = "e0302560ced2fdcbfcb2602697df970cd0d6a38f94b32703f51c312b"
SETTINGS_POLICY_ID = "6d9d7acac59a4469ec52bb207106167c5cbfa689008ffa6ee92acc50"
POOL_STAKE_HASH = "4399813dad91bb78a5eb17c26ff50852bc75d3fa7b6e9ae87232ccc1"
ORDER_STAKE_HASH = "99e5aacf401fed0eb0e2993d72d423947f42342e8f848353d03efe61"

ORDER_REF_UTXO = {
    "tx_hash": "f5f1bdfad3eb4d67d2fc36f36f47fc2938cf6f001689184ab320735a28642cf2",
    "output_index": 0,
}
POOL_REF_UTXO = {
    "tx_hash": "fa46a1d162c59cece3308c5a9d4db9ff2ea17f9c0146ff821c9b445588b017c9",
    "output_index": 0,
}

SETTINGS_NFT_NAME_HEX = "73657474696e6773"

POOL_NFT_PREFIX = "000de140"
POOL_LP_PREFIX = "0014df10"
POOL_REF_PREFIX = "000643b0"

FEE_DENOMINATOR = 10_000

SUNDAE_GRAPHQL_URL = os.environ.get(
    "SUNDAE_GRAPHQL_URL", "https://api.sundae.fi/graphql"
)

# Protocol fees from the settings UTXO (base_fee + simple_fee = max scooper fee)
DEFAULT_BASE_FEE = 612_000
DEFAULT_SIMPLE_FEE = 668_000
DEFAULT_MAX_PROTOCOL_FEE = DEFAULT_BASE_FEE + DEFAULT_SIMPLE_FEE  # 1_280_000

ORDER_DEPOSIT = 2_000_000

MAX_RETRIES = 3
RETRY_BACKOFF = 2.0


# ---------------------------------------------------------------------------
# GraphQL helpers
# ---------------------------------------------------------------------------

def _graphql(query: str, variables: Optional[dict] = None) -> dict:
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(
                SUNDAE_GRAPHQL_URL,
                json={"query": query, "variables": variables or {}},
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            if resp.status_code in (429, 500, 502, 503):
                wait = RETRY_BACKOFF * (attempt + 1)
                logger.warning(
                    "SundaeSwap GraphQL %d, retrying in %.0fs (%d/%d)",
                    resp.status_code, wait, attempt + 1, MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                raise RuntimeError(f"SundaeSwap GraphQL errors: {data['errors']}")
            return data.get("data", {})
        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"SundaeSwap GraphQL failed after {MAX_RETRIES} retries")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SundaePool:
    """SundaeSwap V3 pool state."""
    ident: str
    asset_a: str
    asset_b: str
    asset_a_ticker: str
    asset_b_ticker: str
    asset_a_decimals: int
    asset_b_decimals: int
    reserve_a: int
    reserve_b: int
    circulating_lp: int
    bid_fee_per_10k: int
    ask_fee_per_10k: int
    version: str

    @property
    def pool_nft_name(self) -> str:
        return POOL_NFT_PREFIX + self.ident

    @property
    def pool_nft_unit(self) -> str:
        return POOL_SCRIPT_HASH + self.pool_nft_name

    @property
    def fee_bid_pct(self) -> float:
        return self.bid_fee_per_10k / FEE_DENOMINATOR * 100

    @property
    def fee_ask_pct(self) -> float:
        return self.ask_fee_per_10k / FEE_DENOMINATOR * 100


@dataclass
class SwapEstimate:
    """Estimated output for a swap against a SundaeSwap V3 pool."""
    direction: str
    amount_in: int
    amount_out: int
    min_receive: int
    price_impact_pct: float
    fee_per_10k: int
    lp_fee_paid: int
    source: str = "sundaeswap_v3"


# ---------------------------------------------------------------------------
# Pool discovery via GraphQL API
# ---------------------------------------------------------------------------

_POOL_FIELDS = """
    id
    assetA { assetId: id decimals ticker }
    assetB { assetId: id decimals ticker }
    current {
        quantityA { quantity }
        quantityB { quantity }
        quantityLP { quantity }
    }
    bidFee
    askFee
    finalFee
    version
"""


def _parse_asset_id(raw: str) -> str:
    """Convert SundaeSwap asset ID format to standard hex format.

    SundaeSwap uses 'ada.lovelace' for ADA and 'policyId.assetName' for tokens.
    We convert to '' for ADA (lovelace) and 'policyId + assetName' concatenated.
    """
    if raw == "ada.lovelace":
        return "lovelace"
    if "." in raw:
        policy, name = raw.split(".", 1)
        return policy + name
    return raw


def _parse_fee(fee_val) -> int:
    """Parse fee from GraphQL response — can be [numerator, denominator] or a number."""
    if isinstance(fee_val, list) and len(fee_val) == 2:
        return int(fee_val[0])
    return int(fee_val)


def _parse_pool(pool_data: dict) -> SundaePool:
    """Parse a pool from GraphQL response into a SundaePool."""
    if "bidFee" in pool_data and pool_data["bidFee"]:
        bid_fee = _parse_fee(pool_data["bidFee"])
    else:
        bid_fee = _parse_fee(pool_data.get("finalFee", [30, 10000]))

    if "askFee" in pool_data and pool_data["askFee"]:
        ask_fee = _parse_fee(pool_data["askFee"])
    else:
        ask_fee = bid_fee

    return SundaePool(
        ident=pool_data["id"],
        asset_a=_parse_asset_id(pool_data["assetA"]["assetId"]),
        asset_b=_parse_asset_id(pool_data["assetB"]["assetId"]),
        asset_a_ticker=pool_data["assetA"].get("ticker", ""),
        asset_b_ticker=pool_data["assetB"].get("ticker", ""),
        asset_a_decimals=pool_data["assetA"].get("decimals", 0),
        asset_b_decimals=pool_data["assetB"].get("decimals", 0),
        reserve_a=int(pool_data["current"]["quantityA"]["quantity"]),
        reserve_b=int(pool_data["current"]["quantityB"]["quantity"]),
        circulating_lp=int(pool_data["current"]["quantityLP"]["quantity"]),
        bid_fee_per_10k=bid_fee,
        ask_fee_per_10k=ask_fee,
        version=pool_data.get("version", "V3"),
    )


def find_pool_by_ident(ident: str) -> SundaePool:
    """Look up a SundaeSwap pool by its unique identifier."""
    data = _graphql(
        f"query($id: ID!) {{ pools {{ byId(id: $id) {{ {_POOL_FIELDS} }} }} }}",
        {"id": ident},
    )
    return _parse_pool(data["pools"]["byId"])


def find_pools_by_token(token_id: str) -> list[SundaePool]:
    """Find all SundaeSwap pools containing a given token.

    token_id: hex policy+name for tokens, or 'ada.lovelace' / 'lovelace' for ADA.
    """
    asset_id = token_id
    if token_id == "lovelace":
        asset_id = "ada.lovelace"
    elif "." not in token_id and len(token_id) > 56:
        asset_id = token_id[:56] + "." + token_id[56:]

    data = _graphql(
        f"query($assetId: ID!) {{ pools {{ byAsset(asset: $assetId) {{ {_POOL_FIELDS} }} }} }}",
        {"assetId": asset_id},
    )
    pools = [_parse_pool(p) for p in data["pools"]["byAsset"]]
    return [p for p in pools if p.version == "V3"]


def find_pool_by_pair(asset_a: str, asset_b: str) -> Optional[SundaePool]:
    """Find the best V3 pool for a given asset pair (by liquidity).

    Assets should be in standard format: 'lovelace' for ADA, hex for tokens.
    """
    def to_sundae_id(asset: str) -> str:
        if asset == "lovelace":
            return "ada.lovelace"
        if "." not in asset and len(asset) > 56:
            return asset[:56] + "." + asset[56:]
        return asset

    data = _graphql(
        f"""query($assetA: ID!, $assetB: ID!) {{
            pools {{ byPair(assetA: $assetA, assetB: $assetB) {{ {_POOL_FIELDS} }} }}
        }}""",
        {"assetA": to_sundae_id(asset_a), "assetB": to_sundae_id(asset_b)},
    )
    all_pools = [_parse_pool(p) for p in data["pools"]["byPair"]]
    v3_pools = [p for p in all_pools if p.version == "V3"]
    if not v3_pools:
        return None
    return max(v3_pools, key=lambda p: p.reserve_a)


def search_pools(term: str) -> list[SundaePool]:
    """Search for pools by ticker, name, or policy ID."""
    data = _graphql(
        f"query($term: String!) {{ pools {{ search(term: $term) {{ {_POOL_FIELDS} }} }} }}",
        {"term": term},
    )
    pools = [_parse_pool(p) for p in data["pools"]["search"]]
    return [p for p in pools if p.version == "V3"]


# ---------------------------------------------------------------------------
# Swap estimation (constant-product with bid/ask fees)
# ---------------------------------------------------------------------------

def estimate_swap(
    pool: SundaePool,
    asset_in: str,
    amount_in: int,
    slippage_pct: float = 0.5,
) -> SwapEstimate:
    """Estimate swap output using the constant-product formula.

    SundaeSwap V3 supports separate bid (A→B) and ask (B→A) fees.
    Fee is in basis points out of 10,000.

    Formula:
        effective_in = amount_in * (10000 - fee) / 10000
        amount_out = reserve_out * effective_in / (reserve_in + effective_in)
    """
    in_is_a = (asset_in == pool.asset_a) or (
        asset_in == "lovelace" and pool.asset_a == "lovelace"
    )

    if in_is_a:
        reserve_in = pool.reserve_a
        reserve_out = pool.reserve_b
        fee_per_10k = pool.bid_fee_per_10k
        direction = "a_to_b"
    else:
        reserve_in = pool.reserve_b
        reserve_out = pool.reserve_a
        fee_per_10k = pool.ask_fee_per_10k
        direction = "b_to_a"

    effective_in = amount_in * (FEE_DENOMINATOR - fee_per_10k) // FEE_DENOMINATOR
    lp_fee = amount_in - effective_in

    if reserve_in + effective_in == 0:
        return SwapEstimate(
            direction=direction, amount_in=amount_in, amount_out=0,
            min_receive=0, price_impact_pct=100.0,
            fee_per_10k=fee_per_10k, lp_fee_paid=lp_fee,
        )

    amount_out = reserve_out * effective_in // (reserve_in + effective_in)

    mid_price = reserve_out / reserve_in if reserve_in > 0 else 0
    exec_price = amount_out / amount_in if amount_in > 0 else 0
    price_impact = abs(1 - exec_price / mid_price) * 100 if mid_price > 0 else 0

    min_receive = int(amount_out * (1 - slippage_pct / 100))

    return SwapEstimate(
        direction=direction,
        amount_in=amount_in,
        amount_out=amount_out,
        min_receive=min_receive,
        price_impact_pct=price_impact,
        fee_per_10k=fee_per_10k,
        lp_fee_paid=lp_fee,
    )


# ---------------------------------------------------------------------------
# Plutus CBOR helpers
# ---------------------------------------------------------------------------

def _constr(tag: int, fields: list):
    """Build a CBOR Tagged value for a Plutus constructor.

    Constr 0..6 → tags 121..127
    Constr 7+   → tag 102 with [tag, fields]
    """
    if tag <= 6:
        return cbor2.CBORTag(121 + tag, fields)
    return cbor2.CBORTag(102, [tag, fields])


def _asset_to_singleton_value(asset_id: str, amount: int) -> list:
    """Convert an asset + amount to a SingletonValue (policyId, assetName, amount).

    SundaeSwap V3 SingletonValue is a 3-tuple: (ByteArray, ByteArray, Int)
    encoded as a CBOR list [policy_bytes, name_bytes, int].
    """
    if not asset_id or asset_id == "lovelace":
        return [b"", b"", amount]
    policy = asset_id[:56]
    name = asset_id[56:]
    return [bytes.fromhex(policy), bytes.fromhex(name) if name else b"", amount]


def _address_to_plutus(address: str):
    """Convert a bech32 address to Plutus address representation.

    Plutus address: Constr0 [payment_credential, maybe_stake_credential]
    Payment credential: Constr0 [pkh] for VerificationKey, Constr1 [sh] for Script
    Stake credential: Constr0 [Constr0 [Inline [cred]]] or None
    """
    addr = Address.from_primitive(address)

    if hasattr(addr.payment_part, 'payload'):
        pkh = addr.payment_part.payload
    else:
        pkh = bytes(addr.payment_part)

    from pycardano import VerificationKeyHash
    if isinstance(addr.payment_part, VerificationKeyHash):
        payment_cred = _constr(0, [pkh])
    else:
        payment_cred = _constr(1, [pkh])

    if addr.staking_part:
        if hasattr(addr.staking_part, 'payload'):
            stake_bytes = addr.staking_part.payload
        else:
            stake_bytes = bytes(addr.staking_part)

        from pycardano import VerificationKeyHash as VKH2
        if isinstance(addr.staking_part, VKH2):
            stake_cred = _constr(0, [stake_bytes])
        else:
            stake_cred = _constr(1, [stake_bytes])

        inline_stake = _constr(0, [stake_cred])
        maybe_stake = _constr(0, [inline_stake])
    else:
        maybe_stake = _constr(1, [])

    return _constr(0, [payment_cred, maybe_stake])


# ---------------------------------------------------------------------------
# Order datum encoding (SundaeSwap V3)
# ---------------------------------------------------------------------------

def encode_swap_datum(
    *,
    sender_address: str,
    pool_ident: str,
    asset_offer: str,
    offer_amount: int,
    asset_receive: str,
    min_receive: int,
    max_protocol_fee: int = DEFAULT_MAX_PROTOCOL_FEE,
) -> bytes:
    """Encode a SundaeSwap V3 Swap order datum as CBOR bytes.

    OrderDatum (Constr0):
      [0] pool_ident: Option<ByteArray>  — Constr0 [bytes] for Some, Constr1 [] for None
      [1] owner: MultisigScript          — Signature = Constr0 [Constr0 [pkh]]
      [2] max_protocol_fee: Int
      [3] destination: Destination        — Fixed = Constr0 [Constr0 [address, datum]]
      [4] details: Order                  — Swap = Constr1 [offer_sv, min_received_sv]
      [5] extension: Data                 — Void = Constr0 []
    """
    addr = Address.from_primitive(sender_address)
    pkh = addr.payment_part.payload

    # pool_ident: Some(ident_bytes)
    pool_ident_val = _constr(0, [bytes.fromhex(pool_ident)])

    # owner: Signature { keyHash: pkh }
    owner = _constr(0, [_constr(0, [pkh])])

    # destination: Fixed { address, datum: NoDatum }
    dest_address = _address_to_plutus(sender_address)
    no_datum = _constr(0, [])  # NoDatum
    destination = _constr(0, [_constr(0, [dest_address, no_datum])])

    # details: Swap { offer: SingletonValue, minReceived: SingletonValue }
    offer_sv = _asset_to_singleton_value(asset_offer, offer_amount)
    min_recv_sv = _asset_to_singleton_value(asset_receive, min_receive)
    details = _constr(1, [offer_sv, min_recv_sv])

    # extension: Void (Constr0 [])
    extension = _constr(0, [])

    datum = _constr(0, [
        pool_ident_val,
        owner,
        max_protocol_fee,
        destination,
        details,
        extension,
    ])

    return cbor2.dumps(datum)


# ---------------------------------------------------------------------------
# Order address computation
# ---------------------------------------------------------------------------

def get_order_address(sender_address: str) -> str:
    """Compute the SundaeSwap V3 order address.

    Uses the order script hash as payment credential and the sender's
    stake credential, so the deposit returns via the sender's staking address.
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
# Transaction building
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
    pool: SundaePool,
    asset_in: str,
    amount_in: int,
    min_receive: int,
    max_protocol_fee: int = DEFAULT_MAX_PROTOCOL_FEE,
) -> str:
    """Build an unsigned swap order transaction.

    Creates a UTxO at the SundaeSwap V3 order script address with the
    Swap datum. The batcher picks this up and executes against the pool.

    Returns unsigned CBOR hex.
    """
    in_is_a = (asset_in == pool.asset_a) or (
        asset_in == "lovelace" and pool.asset_a == "lovelace"
    )
    asset_out = pool.asset_b if in_is_a else pool.asset_a

    datum_bytes = encode_swap_datum(
        sender_address=sender_address,
        pool_ident=pool.ident,
        asset_offer=asset_in,
        offer_amount=amount_in,
        asset_receive=asset_out,
        min_receive=min_receive,
        max_protocol_fee=max_protocol_fee,
    )

    order_address = get_order_address(sender_address)

    lovelace_to_lock = ORDER_DEPOSIT + max_protocol_fee
    native_assets = {}

    if asset_in == "lovelace":
        lovelace_to_lock += amount_in
    else:
        native_assets[asset_in] = amount_in

    sender_utxos = blockfrost.get_utxos(sender_address)
    if not sender_utxos:
        raise RuntimeError(f"No UTxOs found at {sender_address}")

    sender_addr = Address.from_primitive(sender_address)
    order_addr = Address.from_primitive(order_address)

    inputs = []
    input_lovelace = 0
    input_native = {}
    for utxo in sender_utxos:
        tx_in = TransactionInput(
            TransactionId(bytes.fromhex(utxo["tx_hash"])),
            utxo["output_index"],
        )
        inputs.append((tx_in, utxo))
        for a in utxo["amount"]:
            if a["unit"] == "lovelace":
                input_lovelace += int(a["quantity"])
            else:
                input_native[a["unit"]] = input_native.get(a["unit"], 0) + int(a["quantity"])

    estimated_fee = 300_000

    if native_assets:
        order_multi = _build_multi_asset(native_assets)
        order_value = Value(lovelace_to_lock, order_multi)
    else:
        order_value = Value(lovelace_to_lock)

    from pycardano import RawPlutusData
    raw_datum = RawPlutusData(cbor2.loads(datum_bytes))

    order_output = TransactionOutput(order_addr, order_value, datum=raw_datum)

    change_lovelace = input_lovelace - lovelace_to_lock - estimated_fee
    if change_lovelace < 0:
        raise RuntimeError(
            f"Insufficient ADA: have {input_lovelace / ADA_LOVELACE:.2f}, "
            f"need {(lovelace_to_lock + estimated_fee) / ADA_LOVELACE:.2f}"
        )

    change_native = {}
    for unit, qty in input_native.items():
        remaining = qty - native_assets.get(unit, 0)
        if remaining > 0:
            change_native[unit] = remaining

    if change_native:
        change_multi = _build_multi_asset(change_native)
        change_value = Value(change_lovelace, change_multi)
    else:
        change_value = Value(change_lovelace)

    change_output = TransactionOutput(sender_addr, change_value)

    tx_body = TransactionBody(
        inputs=[tx_in for tx_in, _ in inputs],
        outputs=[order_output, change_output],
        fee=estimated_fee,
    )

    from pycardano import Transaction, TransactionWitnessSet
    tx = Transaction(tx_body, TransactionWitnessSet())
    return tx.to_cbor().hex()


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

def sign_transaction(unsigned_cbor_hex: str, signing_key: PaymentSigningKey) -> str:
    """Sign an unsigned CBOR transaction locally.

    Returns fully signed CBOR hex ready for submission.
    """
    tx_bytes = bytes.fromhex(unsigned_cbor_hex)
    tx_array = cbor2.loads(tx_bytes)
    body_bytes = cbor2.dumps(tx_array[0])
    tx_hash = blake2b(body_bytes, digest_size=32).digest()

    vk = PaymentVerificationKey.from_signing_key(signing_key)
    signature = signing_key.sign(tx_hash)

    vkey_witness = [vk.payload, signature]
    witness_set = {0: [vkey_witness]}

    if len(tx_array) > 1 and isinstance(tx_array[1], dict):
        merged = dict(tx_array[1])
        merged[0] = [vkey_witness]
    else:
        merged = witness_set

    signed = [tx_array[0], merged]
    if len(tx_array) > 2:
        signed.append(tx_array[2])
    if len(tx_array) > 3:
        signed.append(tx_array[3])

    return cbor2.dumps(signed).hex()


def compute_tx_hash(unsigned_cbor_hex: str) -> str:
    """Compute the transaction hash from unsigned CBOR."""
    tx_bytes = bytes.fromhex(unsigned_cbor_hex)
    tx_array = cbor2.loads(tx_bytes)
    body_bytes = cbor2.dumps(tx_array[0])
    return blake2b(body_bytes, digest_size=32).hexdigest()


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def execute_swap(
    *,
    sender_address: str,
    signing_key: PaymentSigningKey,
    pool: SundaePool,
    asset_in: str,
    amount_in: int,
    slippage_pct: float = 0.5,
    max_price_impact: float = 2.0,
    blockfrost: Optional[BlockfrostClient] = None,
    dry_run: bool = False,
) -> dict:
    """End-to-end swap: estimate → build → sign → submit.

    Returns dict with tx_hash, estimate, and order details.
    """
    bf = blockfrost or BlockfrostClient()

    est = estimate_swap(pool, asset_in, amount_in, slippage_pct)

    if est.price_impact_pct > max_price_impact:
        raise ValueError(
            f"Price impact {est.price_impact_pct:.2f}% exceeds "
            f"{max_price_impact}% threshold"
        )

    if est.amount_out == 0:
        raise ValueError("Estimated output is 0 — pool may have no liquidity")

    logger.info(
        "SundaeSwap V3 swap: %s %s → ~%s %s (min %s, impact %.2f%%)",
        amount_in, asset_in[:20], est.amount_out,
        pool.asset_b if est.direction == "a_to_b" else pool.asset_a,
        est.min_receive, est.price_impact_pct,
    )

    unsigned_cbor = build_swap_order_tx(
        blockfrost=bf,
        sender_address=sender_address,
        pool=pool,
        asset_in=asset_in,
        amount_in=amount_in,
        min_receive=est.min_receive,
    )

    result = {
        "pool_ident": pool.ident,
        "direction": est.direction,
        "asset_in": asset_in,
        "amount_in": amount_in,
        "estimated_out": est.amount_out,
        "min_receive": est.min_receive,
        "price_impact_pct": est.price_impact_pct,
        "fee_pct": est.fee_per_10k / FEE_DENOMINATOR * 100,
        "lp_fee_paid": est.lp_fee_paid,
        "protocol_fee": DEFAULT_MAX_PROTOCOL_FEE,
        "tx_hash": None,
    }

    if dry_run:
        result["tx_hash"] = compute_tx_hash(unsigned_cbor)
        logger.info("DRY RUN — tx hash: %s", result["tx_hash"])
        return result

    signed_cbor = sign_transaction(unsigned_cbor, signing_key)
    tx_hash = bf.submit_tx(signed_cbor)
    result["tx_hash"] = tx_hash
    logger.info("Submitted: %s", tx_hash)

    return result


def execute_sell(
    *,
    sender_address: str,
    signing_key: PaymentSigningKey,
    token_id: str,
    token_amount: int,
    slippage_pct: float = 0.5,
    max_price_impact: float = 2.0,
    blockfrost: Optional[BlockfrostClient] = None,
    dry_run: bool = False,
) -> dict:
    """Sell tokens for ADA via SundaeSwap V3.

    Finds the best ADA/token pool and sells the specified token amount.
    """
    pool = find_pool_by_pair("lovelace", token_id)
    if not pool:
        raise RuntimeError(f"No SundaeSwap V3 pool found for token {token_id}")

    return execute_swap(
        sender_address=sender_address,
        signing_key=signing_key,
        pool=pool,
        asset_in=token_id,
        amount_in=token_amount,
        slippage_pct=slippage_pct,
        max_price_impact=max_price_impact,
        blockfrost=blockfrost,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Cancel stuck orders
# ---------------------------------------------------------------------------

def cancel_orders(
    *,
    sender_address: str,
    signing_key: PaymentSigningKey,
    blockfrost: Optional[BlockfrostClient] = None,
    order_tx_hashes: Optional[list] = None,
) -> str:
    """Cancel SundaeSwap V3 orders and reclaim funds.

    Builds a Plutus script spending transaction that uses the Cancel
    redeemer (Constr1 []) to spend order UTxOs back to the sender.

    The order script checks that the owner MultisigScript condition is
    satisfied — for Signature owners, the sender must sign the tx.
    """
    bf = blockfrost or BlockfrostClient()

    order_address = get_order_address(sender_address)
    raw_utxos = bf.get_utxos(order_address)

    if order_tx_hashes:
        tx_set = set(order_tx_hashes)
        raw_utxos = [u for u in raw_utxos if u["tx_hash"] in tx_set]

    if not raw_utxos:
        raise RuntimeError(f"No order UTxOs found at {order_address}")

    logger.info("Found %d order UTxO(s) to cancel", len(raw_utxos))

    from pycardano import (
        ExecutionUnits,
        Redeemer,
        TransactionBuilder,
    )
    from pycardano.backend.blockfrost import BlockFrostChainContext

    context = BlockFrostChainContext(
        project_id=os.environ["BLOCKFROST_PROJECT_ID"],
        base_url=os.environ.get("BLOCKFROST_BASE_URL", "https://cardano-mainnet.blockfrost.io/api"),
    )

    order_ref_input = TransactionInput(
        TransactionId(bytes.fromhex(ORDER_REF_UTXO["tx_hash"])),
        ORDER_REF_UTXO["output_index"],
    )
    ref_utxo = context.utxos(str(order_ref_input))[0] if hasattr(context, 'utxos') else None

    cancel_redeemer_data = _constr(1, [])  # Cancel = Constr1 []
    cancel_redeemer = Redeemer(
        cbor2.loads(cbor2.dumps(cancel_redeemer_data)),
        ExecutionUnits(500_000_000, 2_000_000),
    )

    sender_addr = Address.from_primitive(sender_address)
    builder = TransactionBuilder(context)

    for utxo_data in raw_utxos:
        tx_in = TransactionInput(
            TransactionId(bytes.fromhex(utxo_data["tx_hash"])),
            utxo_data["output_index"],
        )

        lovelace = 0
        multi = MultiAsset()
        for a in utxo_data["amount"]:
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

        from pycardano import RawCBOR
        datum = None
        if utxo_data.get("inline_datum"):
            datum = RawCBOR(bytes.fromhex(utxo_data["inline_datum"]))

        utxo = UTxO(tx_in, TransactionOutput(
            Address.from_primitive(order_address), val,
            datum=datum,
        ))
        builder.add_script_input(utxo, redeemer=cancel_redeemer)

    builder.add_input_address(sender_address)
    builder.required_signers = [sender_addr.payment_part]

    if ref_utxo:
        builder.reference_inputs.add(ref_utxo)

    tx = builder.build_and_sign(
        signing_keys=[signing_key],
        change_address=sender_addr,
    )

    tx_hash = bf.submit_tx(tx.to_cbor().hex())
    logger.info("Cancel tx submitted: %s", tx_hash)
    return tx_hash


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    parser = argparse.ArgumentParser(
        description="SundaeSwap V3 Direct DEX Client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # --- search ---
    search_p = sub.add_parser("search", help="Search for pools by term")
    search_p.add_argument("term", help="Search term (ticker, name, policy ID)")

    # --- pool ---
    pool_p = sub.add_parser("pool", help="Get pool details by ident")
    pool_p.add_argument("ident", help="Pool identifier")

    # --- estimate ---
    est_p = sub.add_parser("estimate", help="Estimate a swap")
    est_p.add_argument("--pool", required=True, help="Pool identifier")
    est_p.add_argument("--asset-in", default="lovelace", help="Input asset (default: lovelace)")
    est_p.add_argument("--amount", required=True, type=float, help="Amount in display units")
    est_p.add_argument("--slippage", type=float, default=0.5, help="Slippage %% (default: 0.5)")

    # --- buy ---
    buy_p = sub.add_parser("buy", help="Buy tokens with ADA")
    buy_p.add_argument("--token", required=True, help="Token ID (policyId + assetName hex)")
    buy_p.add_argument("--ada", required=True, type=float, help="ADA amount")
    buy_p.add_argument("--slippage", type=float, default=0.5, help="Slippage %%")
    buy_p.add_argument("--dry-run", action="store_true")

    # --- sell ---
    sell_p = sub.add_parser("sell", help="Sell tokens for ADA")
    sell_p.add_argument("--token", required=True, help="Token ID")
    sell_p.add_argument("--amount", required=True, type=float, help="Token amount (display units)")
    sell_p.add_argument("--decimals", type=int, default=6, help="Token decimals (default: 6)")
    sell_p.add_argument("--slippage", type=float, default=0.5, help="Slippage %%")
    sell_p.add_argument("--dry-run", action="store_true")

    # --- cancel ---
    cancel_p = sub.add_parser("cancel", help="Cancel stuck orders")
    cancel_p.add_argument("--tx-hash", nargs="*", default=None, help="Specific tx hashes to cancel")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.command == "search":
        pools = search_pools(args.term)
        if not pools:
            print(f"No V3 pools found for '{args.term}'")
            return
        print(f"\n  SundaeSwap V3 — {len(pools)} pool(s) for '{args.term}':\n")
        for p in sorted(pools, key=lambda x: x.reserve_a, reverse=True):
            ada_reserve = p.reserve_a / ADA_LOVELACE if p.asset_a == "lovelace" else p.reserve_b / ADA_LOVELACE
            print(f"  {p.ident[:16]}...  {p.asset_a_ticker}/{p.asset_b_ticker}  "
                  f"reserves: {p.reserve_a:,} / {p.reserve_b:,}  "
                  f"TVL: ~{ada_reserve:,.0f} ADA  "
                  f"fee: {p.fee_bid_pct:.2f}%")

    elif args.command == "pool":
        pool = find_pool_by_ident(args.ident)
        print(f"\n  SundaeSwap V3 Pool: {pool.ident}")
        print(f"  Pair: {pool.asset_a_ticker}/{pool.asset_b_ticker}")
        print(f"  Asset A: {pool.asset_a}")
        print(f"  Asset B: {pool.asset_b}")
        print(f"  Reserve A: {pool.reserve_a:,}")
        print(f"  Reserve B: {pool.reserve_b:,}")
        print(f"  Circulating LP: {pool.circulating_lp:,}")
        print(f"  Bid fee: {pool.fee_bid_pct:.2f}%  Ask fee: {pool.fee_ask_pct:.2f}%")

    elif args.command == "estimate":
        pool = find_pool_by_ident(args.pool)
        asset_in = args.asset_in
        if asset_in == "lovelace":
            amount_base = int(args.amount * ADA_LOVELACE)
        else:
            in_decimals = pool.asset_a_decimals if asset_in == pool.asset_a else pool.asset_b_decimals
            amount_base = int(args.amount * (10 ** in_decimals))

        est = estimate_swap(pool, asset_in, amount_base, args.slippage)

        out_ticker = pool.asset_b_ticker if est.direction == "a_to_b" else pool.asset_a_ticker
        out_decimals = pool.asset_b_decimals if est.direction == "a_to_b" else pool.asset_a_decimals
        out_display = est.amount_out / (10 ** out_decimals) if out_decimals else est.amount_out
        min_display = est.min_receive / (10 ** out_decimals) if out_decimals else est.min_receive

        print(f"\n  SundaeSwap V3 Estimate")
        print(f"  Pool: {pool.ident}  ({pool.asset_a_ticker}/{pool.asset_b_ticker})")
        print(f"  Input: {args.amount} → Output: ~{out_display:,.6f} {out_ticker}")
        print(f"  Min receive ({args.slippage}% slippage): {min_display:,.6f} {out_ticker}")
        print(f"  Price impact: {est.price_impact_pct:.4f}%")
        print(f"  LP fee: {est.fee_per_10k / 100:.2f}% ({est.lp_fee_paid:,} base units)")

    elif args.command == "buy":
        address = os.environ.get("CARDANO_PAYMENT_ADDRESS", "")
        key_path = os.environ.get("CARDANO_PRIVATE_KEY_PATH", "")
        if not address or not key_path:
            print("Error: Set CARDANO_PAYMENT_ADDRESS and CARDANO_PRIVATE_KEY_PATH in .env")
            return

        signing_key = PaymentSigningKey.load(key_path)
        pool = find_pool_by_pair("lovelace", args.token)
        if not pool:
            print(f"No SundaeSwap V3 pool found for token {args.token}")
            return

        amount_lovelace = int(args.ada * ADA_LOVELACE)
        est = estimate_swap(pool, "lovelace", amount_lovelace, args.slippage)

        out_decimals = pool.asset_b_decimals if pool.asset_a == "lovelace" else pool.asset_a_decimals
        out_display = est.amount_out / (10 ** out_decimals) if out_decimals else est.amount_out

        print(f"\n  SundaeSwap V3 — Buy")
        print(f"  Pool: {pool.ident}  ({pool.asset_a_ticker}/{pool.asset_b_ticker})")
        print(f"  Spending: {args.ada} ADA")
        print(f"  Expected: ~{out_display:,.6f} {pool.asset_b_ticker}")
        print(f"  Min receive: {est.min_receive:,} base units")
        print(f"  Impact: {est.price_impact_pct:.4f}%")
        print(f"  Protocol fee: {DEFAULT_MAX_PROTOCOL_FEE / ADA_LOVELACE:.2f} ADA (max)")

        result = execute_swap(
            sender_address=address,
            signing_key=signing_key,
            pool=pool,
            asset_in="lovelace",
            amount_in=amount_lovelace,
            slippage_pct=args.slippage,
            dry_run=args.dry_run,
        )

        if args.dry_run:
            print(f"\n  DRY RUN — tx hash: {result['tx_hash']}")
        else:
            print(f"\n  Submitted! Tx hash: {result['tx_hash']}")

    elif args.command == "sell":
        address = os.environ.get("CARDANO_PAYMENT_ADDRESS", "")
        key_path = os.environ.get("CARDANO_PRIVATE_KEY_PATH", "")
        if not address or not key_path:
            print("Error: Set CARDANO_PAYMENT_ADDRESS and CARDANO_PRIVATE_KEY_PATH in .env")
            return

        signing_key = PaymentSigningKey.load(key_path)
        token_amount = int(args.amount * (10 ** args.decimals))

        result = execute_sell(
            sender_address=address,
            signing_key=signing_key,
            token_id=args.token,
            token_amount=token_amount,
            slippage_pct=args.slippage,
            dry_run=args.dry_run,
        )

        if args.dry_run:
            print(f"\n  DRY RUN — tx hash: {result['tx_hash']}")
        else:
            print(f"\n  Submitted! Tx hash: {result['tx_hash']}")

    elif args.command == "cancel":
        address = os.environ.get("CARDANO_PAYMENT_ADDRESS", "")
        key_path = os.environ.get("CARDANO_PRIVATE_KEY_PATH", "")
        if not address or not key_path:
            print("Error: Set CARDANO_PAYMENT_ADDRESS and CARDANO_PRIVATE_KEY_PATH in .env")
            return

        signing_key = PaymentSigningKey.load(key_path)
        print(f"\n  SundaeSwap V3 — Cancel Orders")
        print(f"  Address: {address}")

        tx_hash = cancel_orders(
            sender_address=address,
            signing_key=signing_key,
            order_tx_hashes=args.tx_hash,
        )
        print(f"\n  Cancel tx submitted: {tx_hash}")


if __name__ == "__main__":
    _cli()
