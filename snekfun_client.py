"""
Snek.fun Bonding Curve Client

Full pipeline for trading on snek.fun bonding curve pools:
pool state → price estimation → buy/sell/cancel via builder API → sign → submit.

Snek.fun uses a batcher model: orders are placed as UTXOs at a script address,
then the snek.fun batcher executes them against the pool. The builder API at
builder.snek.fun constructs the order transactions.

For graduated tokens (completed bonding curve), use the CPMM trade endpoints.

API docs: see snek-fun-api-endpoints.md
"""

import os
import time
from dataclasses import dataclass
from hashlib import blake2b
from typing import Optional

import cbor2
import requests
from pycardano import (
    Address,
    PaymentSigningKey,
    PaymentVerificationKey,
)

from blockfrost_client import BlockfrostClient

# ---------------------------------------------------------------------------
# Constants (on-chain script hashes and protocol parameters)
# ---------------------------------------------------------------------------

POOL_SCRIPT_HASH = "63f947b8d9535bc4e4ce6919e3dc056547e8d30ada12f29aa5f826b8"

POOL_ADDRESS = (
    "addr1xxg94wrfjcdsjncmsxtj0r87zk69e0jfl28n934sznu95tdj764lvrxdayh2ux30fl0ktuh27csgmpevdu89jlxppvrs2993lw"
)

PERMITTED_EXECUTOR = "e865941988edcca559268b57b7ee939974fd42fd26c7e1acd7a50678"

PROTOCOL_FEE_PKH_1 = "8807fbe6e36b1c35ad6f36f0993e2fc67ab6f2db06041cfa3a53c04a"
PROTOCOL_FEE_PKH_2 = "30c1003aa7dec834e0d0a78db547ba8840e58060725dbfae352f0d64"

ORDER_SCRIPT_BASE_HASH = "d9143ac63473b17a215d1b7484dfb6ac6b4a0005beb0e26a6ca02c96"

REFERENCE_SCRIPTS = {
    "pool_validator": {
        "tx_hash": "c4a540ac2e06c217dd4fb3f39ca3863da394ba134677dafa9b98830ca71d584d",
        "output_index": 3,
        "script_hash": "905ab869961b094f1b8197278cfe15b45cbe49fa8f32c6b014f85a2d",
    },
    "order_validator": {
        "tx_hash": "e2ed9e953ebf98ca701fc93588d73cb9769f87b9d13712474f566a0743963e8b",
        "output_index": 0,
        "script_hash": "d9143ac63473b17a215d1b7484dfb6ac6b4a0005beb0e26a6ca02c96",
    },
    "minting_policy": {
        "tx_hash": "e2ed9e953ebf98ca701fc93588d73cb9769f87b9d13712474f566a0743963e8b",
        "output_index": 1,
        "script_hash": "a5643b4a22a192d7691d05baf4a9bbb8acdbb5daa60be1f333e128f1",
    },
}

MIN_UTXO_LOVELACE = 1_500_000
EXECUTOR_FEE = 1_100_000
DEPOSIT_ADA = 1_500_000

BUILDER_URL = os.environ.get("SNEKFUN_BUILDER_URL", "https://builder.snek.fun")
ANALYTICS_URL = os.environ.get("SNEKFUN_ANALYTICS_URL", "https://analytics.snek.fun")
BALANCES_URL = os.environ.get("SNEKFUN_BALANCES_URL", f"{ANALYTICS_URL}/balances")
VESTING_URL = os.environ.get("SNEKFUN_VESTING_URL", "https://token-vesting.snek.fun")
CHARTS_URL = os.environ.get("SNEKFUN_CHARTS_URL", "https://charts.snek.fun")
UTXO_MONITOR_URL = os.environ.get("SNEKFUN_UTXO_MONITOR_URL", "https://utxo-monitor.snek.fun")

SLIPPAGE_OPTIONS = ["15", "30", "50", "75", "infinity"]
SIDE_OPTIONS = ["BUY", "BUY_WITH_OUTPUT", "SELL"]

MAX_RETRIES = 3
RETRY_BACKOFF = 2.0


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post(url: str, payload, timeout: int = 30, params: Optional[dict] = None) -> dict:
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(
                url, json=payload,
                params=params,
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
            if resp.status_code in (429, 500, 502, 503):
                time.sleep(RETRY_BACKOFF * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (attempt + 1))
                continue
            raise
    resp.raise_for_status()
    return resp.json()


def _get(url: str, timeout: int = 15, params: Optional[dict] = None):
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PoolDatum:
    """Decoded on-chain pool datum (9 fields)."""
    pool_nft_policy: str
    pool_nft_name: str
    asset_x_policy: str
    asset_x_name: str
    asset_y_policy: str
    asset_y_name: str
    a_num: int
    b_num: int
    permitted_executor: str
    ada_cap_threshold: int
    protocol_fee_pkh_1: str
    protocol_fee_pkh_2: str


@dataclass
class PoolState:
    """Live pool state combining on-chain UTXO data with decoded datum."""
    tx_hash: str
    output_index: int
    reserve_ada: int
    reserve_token: int
    datum: PoolDatum
    token_unit: str
    pool_nft_unit: str

    @property
    def bonding_progress_pct(self) -> float:
        if self.datum.ada_cap_threshold > 0:
            return (self.reserve_ada / self.datum.ada_cap_threshold) * 100
        return 0.0


@dataclass
class PriceEstimate:
    """Estimated output for a buy order."""
    token_output: int
    new_reserve_ada: int
    new_reserve_token: int
    price_per_token_lovelace: float
    price_per_ada_tokens: float


@dataclass
class SellEstimate:
    """Estimated output for a sell order."""
    ada_output: int
    new_reserve_ada: int
    new_reserve_token: int
    price_per_token_lovelace: float
    price_per_ada_tokens: float


@dataclass
class TradeResult:
    """Response from the snek.fun builder /order endpoint."""
    trade_id: str
    cbor: str
    input_amount: str
    output_amount: str
    beacon: Optional[str] = None
    input_asset: Optional[str] = None
    output_asset: Optional[str] = None
    price_numerator: Optional[str] = None
    price_denominator: Optional[str] = None
    price_quote: Optional[str] = None
    price_base: Optional[str] = None

    @classmethod
    def from_response(cls, data: dict) -> "TradeResult":
        price = data.get("price") or {}
        return cls(
            trade_id=data.get("id", ""),
            cbor=data.get("cbor", ""),
            input_amount=data.get("inputAmount", ""),
            output_amount=data.get("outputAmount", ""),
            beacon=data.get("beacon"),
            input_asset=data.get("inputAsset"),
            output_asset=data.get("outputAsset"),
            price_numerator=price.get("numerator"),
            price_denominator=price.get("denominator"),
            price_quote=data.get("priceQuote"),
            price_base=data.get("priceBase"),
        )


# ---------------------------------------------------------------------------
# Pool datum decoding
# ---------------------------------------------------------------------------

def parse_pool_datum(inline_datum_hex: str) -> PoolDatum:
    """Decode a snek.fun bonding curve pool datum from CBOR hex.

    The datum is a Plutus Constr0 with 9 fields:
      [0] pool NFT   : Constr0 [bytes<policy>, bytes<name>]
      [1] asset X     : Constr0 [bytes<policy>, bytes<name>]  (ADA = empty)
      [2] asset Y     : Constr0 [bytes<policy>, bytes<name>]  (token)
      [3] aNum        : uint
      [4] bNum        : uint
      [5] permitted executor : bytes (28-byte PKH)
      [6] ada cap threshold  : uint (lovelace)
      [7] protocol fee PKH 1 : bytes
      [8] protocol fee PKH 2 : bytes
    """
    decoded = cbor2.loads(bytes.fromhex(inline_datum_hex))
    fields = decoded.value

    return PoolDatum(
        pool_nft_policy=fields[0].value[0].hex(),
        pool_nft_name=fields[0].value[1].hex(),
        asset_x_policy=fields[1].value[0].hex(),
        asset_x_name=fields[1].value[1].hex(),
        asset_y_policy=fields[2].value[0].hex(),
        asset_y_name=fields[2].value[1].hex(),
        a_num=int(fields[3]),
        b_num=int(fields[4]),
        permitted_executor=fields[5].hex(),
        ada_cap_threshold=int(fields[6]),
        protocol_fee_pkh_1=fields[7].hex(),
        protocol_fee_pkh_2=fields[8].hex(),
    )


# ---------------------------------------------------------------------------
# Pool state from Blockfrost
# ---------------------------------------------------------------------------

def get_pool_state(
    blockfrost: BlockfrostClient,
    token_policy_id: str,
    token_asset_name: str,
    pool_nft_asset_name: str,
) -> PoolState:
    """Fetch live pool state from the on-chain pool UTXO.

    Args:
        blockfrost: BlockfrostClient instance.
        token_policy_id: Hex policy ID of the token (e.g. LUMP's policy).
        token_asset_name: Hex asset name of the token (e.g. "4c756d70").
        pool_nft_asset_name: Hex asset name of the pool NFT.
    """
    pool_nft_unit = POOL_SCRIPT_HASH + pool_nft_asset_name
    token_unit = token_policy_id + token_asset_name

    utxos = blockfrost.get_utxos(POOL_ADDRESS)

    pool_utxo = None
    for utxo in utxos:
        for asset in utxo["amount"]:
            if asset["unit"] == pool_nft_unit and asset["quantity"] == "1":
                pool_utxo = utxo
                break
        if pool_utxo:
            break

    if not pool_utxo:
        raise RuntimeError(f"Pool UTXO not found for NFT {pool_nft_unit}")

    ada_amount = next(a for a in pool_utxo["amount"] if a["unit"] == "lovelace")
    token_amount = next(a for a in pool_utxo["amount"] if a["unit"] == token_unit)

    datum = parse_pool_datum(pool_utxo["inline_datum"])

    return PoolState(
        tx_hash=pool_utxo["tx_hash"],
        output_index=pool_utxo["output_index"],
        reserve_ada=int(ada_amount["quantity"]),
        reserve_token=int(token_amount["quantity"]),
        datum=datum,
        token_unit=token_unit,
        pool_nft_unit=pool_nft_unit,
    )


# ---------------------------------------------------------------------------
# Pool state from snek.fun analytics API
# ---------------------------------------------------------------------------

def get_token_state(asset_id: str) -> Optional[dict]:
    """Fetch the single-token pool snapshot from the Snekfun Provider API.

    GET {analytics}/v1/pools-feed/initial/state?asset={policyId}.{hexAssetName}

    Returns {pool, metrics, info}. See:
    https://docs.snek.fun/api-reference/snekfun-provider/http/pools-feed
    """
    try:
        return _get(f"{ANALYTICS_URL}/v1/pools-feed/initial/state?asset={asset_id}")
    except Exception:
        return None


def get_curve_progress(asset_id: str) -> Optional[float]:
    """Return bonding curve completion percentage for an asset.

    GET {analytics}/v1/pools-feed/curve/progress?asset={assetId} -> {"percent": "75.5"}
    """
    try:
        data = _get(f"{ANALYTICS_URL}/v1/pools-feed/curve/progress?asset={asset_id}")
        return float(data.get("percent", 0))
    except Exception:
        return None


def get_parameters() -> dict:
    """Fetch builder protocol parameters (fees, limits, bonding-curve config).

    GET {builder}/parameters
    """
    return _get(f"{BUILDER_URL}/parameters")


# ---------------------------------------------------------------------------
# Price estimation
# ---------------------------------------------------------------------------

def estimate_buy(pool: PoolState, ada_lovelace: int) -> PriceEstimate:
    """Estimate token output for a given ADA input on the bonding curve.

    Uses constant-product formula: dy = y - (x * y) / (x + dx)
    """
    x = pool.reserve_ada
    y = pool.reserve_token
    dx = ada_lovelace
    new_x = x + dx
    dy = y - (x * y) // new_x

    return PriceEstimate(
        token_output=dy,
        new_reserve_ada=new_x,
        new_reserve_token=y - dy,
        price_per_token_lovelace=dx / dy if dy > 0 else 0,
        price_per_ada_tokens=dy / dx if dx > 0 else 0,
    )


def estimate_sell(pool: PoolState, token_amount: int) -> SellEstimate:
    """Estimate ADA output for a given token input on the bonding curve.

    Uses constant-product formula: dx = x - (x * y) / (y + dy)
    """
    x = pool.reserve_ada
    y = pool.reserve_token
    dy = token_amount
    new_y = y + dy
    dx = x - (x * y) // new_y

    return SellEstimate(
        ada_output=dx,
        new_reserve_ada=x - dx,
        new_reserve_token=new_y,
        price_per_token_lovelace=dx / dy if dy > 0 else 0,
        price_per_ada_tokens=dy / dx if dx > 0 else 0,
    )


# ---------------------------------------------------------------------------
# Order datum encoding (Plutus CBOR)
# ---------------------------------------------------------------------------

def _constr(tag: int, fields: list):
    """Build a CBOR Tagged value for a Plutus constructor."""
    return cbor2.CBORTag(121 + tag, fields)


def encode_order_datum(
    *,
    direction: str = "buy",
    owner_pkh: str,
    return_pkh: str,
    return_skh: Optional[str] = None,
    token_policy_id: str,
    token_asset_name: str,
    min_receive: int,
    max_spend: int,
    executor_fee: int = EXECUTOR_FEE,
    deposit_ada: int = DEPOSIT_ADA,
    permitted_executor: str = PERMITTED_EXECUTOR,
    deadline: int = 0,
) -> bytes:
    """Encode a snek.fun order datum as CBOR bytes.

    The datum is Constr0 with 10 fields:
      [0] direction          : bytes (0x01 = buy, 0x00 = sell)
      [1] order_info         : Constr0 [Constr0 [owner_pkh], Constr0 [Constr0 [Constr0 [return_pkh]]]]
      [2] asset_x            : Constr0 [bytes<>, bytes<>]  (ADA)
      [3] asset_y            : Constr0 [bytes<policy>, bytes<name>]
      [4] amounts            : Constr0 [uint<min_receive>, uint<max_spend>]
      [5] executor_fee       : uint
      [6] deposit_ada        : uint
      [7] permitted_executor : bytes (28-byte PKH)
      [8] deadline           : uint (posix ms)
      [9] owner_pkh          : bytes (28-byte PKH)
    """
    direction_byte = bytes([0x01]) if direction == "buy" else bytes([0x00])

    effective_owner = owner_pkh or return_pkh
    return_addr_field = _constr(0, [_constr(0, [_constr(0, [bytes.fromhex(return_pkh)])])])
    order_info = _constr(0, [_constr(0, [bytes.fromhex(effective_owner)]), return_addr_field])

    asset_x = _constr(0, [b"", b""])
    asset_y = _constr(0, [bytes.fromhex(token_policy_id), bytes.fromhex(token_asset_name)])
    amounts = _constr(0, [min_receive, max_spend])

    if deadline == 0:
        deadline = int(time.time() * 1000) + 20 * 60 * 1000

    fields = [
        direction_byte,
        order_info,
        asset_x,
        asset_y,
        amounts,
        executor_fee,
        deposit_ada,
        bytes.fromhex(permitted_executor),
        deadline,
        bytes.fromhex(effective_owner),
    ]

    return cbor2.dumps(_constr(0, fields))


# ---------------------------------------------------------------------------
# UTXO → CIP-30 CBOR encoding (for builder API)
# ---------------------------------------------------------------------------

def _utxos_to_cip30_hex(utxos: list[dict], address: str) -> list[str]:
    """Convert Blockfrost UTXOs to CIP-30 CBOR hex strings.

    The builder API accepts UTXOs as an array of CBOR-encoded
    TransactionUnspentOutput hex strings.
    """
    from pycardano import (
        Asset,
        TransactionInput,
        TransactionId,
        TransactionOutput,
        Value,
        MultiAsset,
        ScriptHash,
        AssetName,
    )

    result = []
    addr = Address.from_primitive(address)

    for utxo in utxos:
        tx_in = TransactionInput(
            TransactionId(bytes.fromhex(utxo["tx_hash"])),
            utxo["output_index"],
        )

        lovelace = int(next(a["quantity"] for a in utxo["amount"] if a["unit"] == "lovelace"))
        native_assets = [a for a in utxo["amount"] if a["unit"] != "lovelace"]

        if native_assets:
            multi = MultiAsset()
            for asset in native_assets:
                policy_hex = asset["unit"][:56]
                name_hex = asset["unit"][56:]
                sh = ScriptHash(bytes.fromhex(policy_hex))
                an = AssetName(bytes.fromhex(name_hex))
                if sh not in multi:
                    multi[sh] = Asset()
                multi[sh][an] = int(asset["quantity"])
            value = Value(lovelace, multi)
        else:
            value = Value(lovelace)

        tx_out = TransactionOutput(addr, value)

        utxo_cbor = cbor2.dumps([tx_in.to_primitive(), tx_out.to_primitive()])
        result.append(utxo_cbor.hex())

    return result


# ---------------------------------------------------------------------------
# Transaction signing
# ---------------------------------------------------------------------------

def sign_transaction(unsigned_cbor_hex: str, signing_key: PaymentSigningKey) -> str:
    """Sign an unsigned CBOR transaction and return the fully-signed hex.

    Hashes the original body bytes directly (no re-serialization) to
    preserve the transaction hash from the builder API.
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
    # cbor2 can decode a CBOR set-tag (258) as a Python set/frozenset.
    if not isinstance(existing_vkeys, list):
        existing_vkeys = list(existing_vkeys)
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
# Builder API: Buy
# ---------------------------------------------------------------------------

def buy_via_builder(
    *,
    asset_id: str,
    ada_amount: float,
    sender_address: str,
    slippage: str = "15",
    sender_utxo_cbor: Optional[list[str]] = None,
    blockfrost: Optional[BlockfrostClient] = None,
) -> TradeResult:
    """Build a buy order via the snek.fun builder API.

    The builder constructs the transaction server-side. You sign locally
    and submit via Blockfrost.

    Args:
        asset_id: Token asset ID as policyId.assetName (dot separator).
        ada_amount: ADA to spend (display units, e.g. 5.0 = 5 ADA).
        sender_address: Bech32 Cardano address.
        slippage: One of "15", "30", "50", "75", "infinity".
        sender_utxo_cbor: Pre-encoded CIP-30 UTXO hex list. If None,
            fetched from Blockfrost automatically.
        blockfrost: BlockfrostClient for UTXO fetching (if sender_utxo_cbor is None).
    """
    if slippage not in SLIPPAGE_OPTIONS:
        raise ValueError(f"Slippage must be one of: {SLIPPAGE_OPTIONS}")

    lovelace = int(ada_amount * 1_000_000)

    if sender_utxo_cbor is None:
        if blockfrost is None:
            blockfrost = BlockfrostClient()
        utxos = blockfrost.get_utxos(sender_address)
        sender_utxo_cbor = _utxos_to_cip30_hex(utxos, sender_address)

    payload = {
        "assetId": asset_id,
        "amount": str(lovelace),
        "side": "BUY",
        "changeAddress": sender_address,
        "slippage": slippage,
        "utxos": sender_utxo_cbor,
    }

    data = _post(f"{BUILDER_URL}/order", payload, timeout=30)
    return TradeResult.from_response(data)


def buy_cpmm_via_builder(
    *,
    asset_id: str,
    ada_amount: float,
    sender_address: str,
    slippage: str = "15",
    sender_utxo_cbor: Optional[list[str]] = None,
    blockfrost: Optional[BlockfrostClient] = None,
) -> TradeResult:
    """Kept for back-compat. The official /order endpoint auto-routes bonding
    curve vs AMM; this delegates to buy_via_builder.
    """
    return buy_via_builder(
        asset_id=asset_id,
        ada_amount=ada_amount,
        sender_address=sender_address,
        slippage=slippage,
        sender_utxo_cbor=sender_utxo_cbor,
        blockfrost=blockfrost,
    )


# ---------------------------------------------------------------------------
# Builder API: Sell
# ---------------------------------------------------------------------------

def sell_via_builder(
    *,
    asset_id: str,
    token_amount: int,
    sender_address: str,
    slippage: str = "15",
    sender_utxo_cbor: Optional[list[str]] = None,
    blockfrost: Optional[BlockfrostClient] = None,
) -> TradeResult:
    """Build a sell order via the snek.fun builder API.

    Args:
        asset_id: Token asset ID as policyId.assetName (dot separator).
        token_amount: Token quantity to sell (raw/base units).
        sender_address: Bech32 Cardano address.
        slippage: One of "15", "30", "50", "75", "infinity".
        sender_utxo_cbor: Pre-encoded CIP-30 UTXO hex list. If None,
            fetched from Blockfrost automatically.
        blockfrost: BlockfrostClient for UTXO fetching (if sender_utxo_cbor is None).
    """
    if slippage not in SLIPPAGE_OPTIONS:
        raise ValueError(f"Slippage must be one of: {SLIPPAGE_OPTIONS}")

    if sender_utxo_cbor is None:
        if blockfrost is None:
            blockfrost = BlockfrostClient()
        utxos = blockfrost.get_utxos(sender_address)
        sender_utxo_cbor = _utxos_to_cip30_hex(utxos, sender_address)

    payload = {
        "assetId": asset_id,
        "amount": str(token_amount),
        "side": "SELL",
        "changeAddress": sender_address,
        "slippage": slippage,
        "utxos": sender_utxo_cbor,
    }

    data = _post(f"{BUILDER_URL}/order", payload, timeout=30)
    return TradeResult.from_response(data)


def sell_cpmm_via_builder(
    *,
    asset_id: str,
    token_amount: int,
    sender_address: str,
    slippage: str = "15",
    sender_utxo_cbor: Optional[list[str]] = None,
    blockfrost: Optional[BlockfrostClient] = None,
) -> TradeResult:
    """Kept for back-compat. The official /order endpoint auto-routes bonding
    curve vs AMM; this delegates to sell_via_builder.
    """
    return sell_via_builder(
        asset_id=asset_id,
        token_amount=token_amount,
        sender_address=sender_address,
        slippage=slippage,
        sender_utxo_cbor=sender_utxo_cbor,
        blockfrost=blockfrost,
    )


# ---------------------------------------------------------------------------
# Builder API: Cancel
# ---------------------------------------------------------------------------

def buy_with_output_via_builder(
    *,
    asset_id: str,
    token_output: int,
    sender_address: str,
    slippage: str = "15",
    sender_utxo_cbor: Optional[list[str]] = None,
    blockfrost: Optional[BlockfrostClient] = None,
) -> TradeResult:
    """Buy a specific token output amount; builder computes ADA input.

    Uses side=BUY_WITH_OUTPUT per snek.fun docs.
    """
    if slippage not in SLIPPAGE_OPTIONS:
        raise ValueError(f"Slippage must be one of: {SLIPPAGE_OPTIONS}")

    if sender_utxo_cbor is None:
        if blockfrost is None:
            blockfrost = BlockfrostClient()
        utxos = blockfrost.get_utxos(sender_address)
        sender_utxo_cbor = _utxos_to_cip30_hex(utxos, sender_address)

    payload = {
        "assetId": asset_id,
        "amount": str(token_output),
        "side": "BUY_WITH_OUTPUT",
        "changeAddress": sender_address,
        "slippage": slippage,
        "utxos": sender_utxo_cbor,
    }
    data = _post(f"{BUILDER_URL}/order", payload, timeout=30)
    return TradeResult.from_response(data)


def cancel_via_builder(
    *,
    tx_hash: str,
    output_index: int = 0,
    sender_address: str,
    sender_utxo_cbor: Optional[list[str]] = None,
    blockfrost: Optional[BlockfrostClient] = None,
) -> str:
    """Cancel a pending snek.fun order via the builder API.

    NOTE: /cancel is not in the public docs at https://docs.snek.fun — it is
    an undocumented builder endpoint. May change without notice.

    Returns the unsigned CBOR hex for the cancel transaction.
    """
    if sender_utxo_cbor is None:
        if blockfrost is None:
            blockfrost = BlockfrostClient()
        utxos = blockfrost.get_utxos(sender_address)
        sender_utxo_cbor = _utxos_to_cip30_hex(utxos, sender_address)

    payload = {
        "txHash": tx_hash,
        "index": str(output_index),
        "changeAddress": sender_address,
        "utxos": sender_utxo_cbor,
    }

    data = _post(f"{BUILDER_URL}/cancel", payload, timeout=30)
    return data.get("cbor", "")


# ---------------------------------------------------------------------------
# Builder API: Sign + Submit
# ---------------------------------------------------------------------------

def sign_and_submit_via_builder(
    unsigned_cbor_hex: str,
    witness_hex: str,
    sender_address: str,
) -> str:
    """Sign and submit a transaction via snek.fun's co-signing endpoint.

    Some snek.fun transactions require the batcher to co-sign. This endpoint
    adds the batcher's witness and submits to the network.

    Returns the transaction hash.
    """
    payload = {
        "cbor": unsigned_cbor_hex,
        "witness": witness_hex,
        "changeAddress": sender_address,
    }
    data = _post(f"{BUILDER_URL}/sign-and-submit", payload, timeout=30)
    return data.get("txHash", "")


def sign_via_builder(
    unsigned_cbor_hex: str,
    witness_hex: str,
    sender_address: str,
) -> str:
    """Attach a witness to a transaction without submitting.

    POST {builder}/sign -> {"cbor": "<signed hex>"}
    """
    payload = {
        "cbor": unsigned_cbor_hex,
        "witness": witness_hex,
        "changeAddress": sender_address,
    }
    data = _post(f"{BUILDER_URL}/sign", payload, timeout=30)
    return data.get("cbor", "")


def submit_via_builder(signed_cbor_hex: str) -> str:
    """Submit a fully-signed transaction via the builder.

    POST {builder}/submit -> {"txHash": "..."}
    """
    data = _post(f"{BUILDER_URL}/submit", {"cbor": signed_cbor_hex}, timeout=30)
    return data.get("txHash", "")


# ---------------------------------------------------------------------------
# Builder API: Transfer
# Docs: https://docs.snek.fun/api-reference/overview  (POST /transfer)
# ---------------------------------------------------------------------------

def transfer_via_builder(
    *,
    change_address: str,
    dist_address: str,
    transfer_assets: dict,
    utxos: object,
) -> dict:
    """Build a transfer tx (ADA and/or tokens) between two wallets.

    POST {builder}/transfer

    Args:
        change_address:  bech32 source address.
        dist_address:    bech32 destination address.
        transfer_assets: per the docs — either
                         {"lovelace": "<str>" or "max"} (funding-wallet mode)
                         or {"lovelace": "...", "assets": [...]} (splash-wallet mode).
        utxos:           either a list of CBOR-hex strings from the sender's
                         UTxOs, or the string "splash-wallet" to let the
                         builder use the in-app trading wallet.

    Returns the raw builder response (contains `cbor`).
    """
    payload = {
        "utxos": utxos,
        "changeAddress": change_address,
        "distAddress": dist_address,
        "transferAssets": transfer_assets,
    }
    return _post(f"{BUILDER_URL}/transfer", payload, timeout=30)


# ---------------------------------------------------------------------------
# Balances API  (base: https://analytics.snek.fun/balances)
# Docs: https://docs.snek.fun/api-reference/balances-api/overview
# ---------------------------------------------------------------------------

def get_pool_holders(
    asset_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Paged list of holders for an asset, plus dev balance and total count.

    GET {balances}/v1/pool/holders?assetId=...&limit=&offset=
    Response: {"holders": [{"address", "quantity"}, ...], "dev": {...}, "count": int}
    """
    return _get(
        f"{BALANCES_URL}/v1/pool/holders",
        params={"assetId": asset_id, "limit": limit, "offset": offset},
    )


def get_pnl_card(asset_id: str, payment_key_hashes: list[str]) -> Optional[dict]:
    """Profit/loss summary for one asset across one or more wallet PKHs.

    POST {balances}/v1/user/pnl-card?assetId=...  body: ["<pkh_hex>", ...]
    Returns the response object or None when no data is available.
    """
    if not payment_key_hashes:
        raise ValueError("payment_key_hashes must contain at least one 56-char hex PKH")
    return _post(
        f"{BALANCES_URL}/v1/user/pnl-card",
        payment_key_hashes,
        params={"assetId": asset_id},
        timeout=15,
    )


def get_asset_balance(asset_id: str, payment_key_hashes: list[str]) -> Optional[float]:
    """Aggregate ADA-denominated balance for one asset across one or more wallet PKHs.

    POST {balances}/v1/asset/asset-balance?assetId=...  body: ["<pkh_hex>", ...]
    Returns the `balance` number, or None when no balance data exists.
    """
    if not payment_key_hashes:
        raise ValueError("payment_key_hashes must contain at least one 56-char hex PKH")
    data = _post(
        f"{BALANCES_URL}/v1/asset/asset-balance",
        payment_key_hashes,
        params={"assetId": asset_id},
        timeout=15,
    )
    if data is None:
        return None
    return data.get("balance")


# ---------------------------------------------------------------------------
# Vesting API
# Docs: https://docs.snek.fun/api-reference/overview
#       (POST /create-lock, POST /withdraw on token-vesting.snek.fun)
# ---------------------------------------------------------------------------

def create_vesting_lock(
    *,
    address: str,
    asset_id: str,
    amount: int,
    lock_end_ms: int,
    stages_count: int,
) -> dict:
    """Create a token-vesting lock on snek.fun.

    POST {vesting}/create-lock -> {"cbor": "<unsigned tx hex>"}

    Args:
        address:     bech32 owner address.
        asset_id:    token id as "policyId.assetName".
        amount:      base-unit token amount to lock (sent as bigint string).
        lock_end_ms: unix timestamp in MILLISECONDS when the lock fully releases.
        stages_count: number of vesting stages (1-10); tokens unlock in equal parts.
    """
    if not 1 <= stages_count <= 10:
        raise ValueError("stages_count must be between 1 and 10")
    payload = {
        "address": address,
        "assetId": asset_id,
        "amount": str(amount),
        "lockEnd": int(lock_end_ms),
        "stagesCount": int(stages_count),
    }
    return _post(f"{VESTING_URL}/create-lock", payload, timeout=30)


def withdraw_vesting(*, lock_id: str, address: str) -> dict:
    """Withdraw from a previously-created vesting lock.

    POST {vesting}/withdraw
    """
    return _post(
        f"{VESTING_URL}/withdraw",
        {"id": lock_id, "address": address},
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Vesting Query API  (base: https://token-vesting.snek.fun)
# Docs: https://docs.snek.fun/api-reference/snekfun-api/overview
# ---------------------------------------------------------------------------

def get_vestings_by_redeemer(redeemer_vkh: str) -> list[dict]:
    """List vesting locks redeemable by a single redeemer PKH.

    POST {vesting}/v1/vesting/get-by-redeemer?redeemerVkhs=<hex>
    """
    data = _post(
        f"{VESTING_URL}/v1/vesting/get-by-redeemer",
        {},
        params={"redeemerVkhs": redeemer_vkh},
        timeout=15,
    )
    return data.get("vestings", []) or []


def get_vestings_by_asset(asset_id: str) -> list[dict]:
    """List vesting locks for a given native asset.

    POST {vesting}/v1/vesting/get-by-asset/<policyId.hexAssetName>
    """
    data = _post(
        f"{VESTING_URL}/v1/vesting/get-by-asset/{asset_id}",
        {},
        timeout=15,
    )
    return data.get("vestings", []) or []


# ---------------------------------------------------------------------------
# UTXO Monitor API  (base: https://utxo-monitor.snek.fun)
# Docs: https://docs.snek.fun/api-reference/utxo-monitor/overview
# ---------------------------------------------------------------------------

def get_utxos_by_pkh(
    pkh: str,
    *,
    offset: int = 0,
    limit: int = 100,
    query: str = "unspent",
) -> list[dict]:
    """Fetch unspent outputs for a wallet payment-key hash.

    POST {utxo-monitor}/getUtxos  body: {"pkh", "offset", "limit", "query"}
    Returns a list of {txHash, index, address, value: [{unit, amount}]}.

    Paginate by requesting `limit` at a time; a short page signals end-of-data.
    """
    return _post(
        f"{UTXO_MONITOR_URL}/getUtxos",
        {"pkh": pkh, "offset": offset, "limit": limit, "query": query},
        timeout=15,
    )


# ---------------------------------------------------------------------------
# Charts API  (base: https://charts.snek.fun)
# Docs: https://docs.snek.fun/api-reference/charts-ws/http
# ---------------------------------------------------------------------------

CHART_RESOLUTIONS = ("min1", "min5", "hour1", "day1", "week1", "month1")


def _charts_params(base: str, quote: str, start: int, end: int, resolution: str) -> dict:
    if resolution not in CHART_RESOLUTIONS:
        raise ValueError(f"resolution must be one of {CHART_RESOLUTIONS}")
    return {"base": base, "quote": quote, "from": start, "to": end, "resolution": resolution}


def get_chart_history(
    *, base: str, quote: str, start: int, end: int, resolution: str
) -> list[dict]:
    """OHLCV bars for a pair over a time range.

    GET {charts}/v1/charts/history
    Returns list of {pair, time, low, high, open, close, volume}.
    """
    return _get(
        f"{CHARTS_URL}/v1/charts/history",
        params=_charts_params(base, quote, start, end, resolution),
    )


def get_chart_initial_state(*, base: str, quote: str, resolution: str) -> dict:
    """Latest OHLCV bar snapshot for a pair at a resolution.

    GET {charts}/v1/charts/initial-state?base=&quote=&resolution=
    Returns {"bar": {...}, "isRelevant": bool}.
    """
    if resolution not in CHART_RESOLUTIONS:
        raise ValueError(f"resolution must be one of {CHART_RESOLUTIONS}")
    return _get(
        f"{CHARTS_URL}/v1/charts/initial-state",
        params={"base": base, "quote": quote, "resolution": resolution},
    )


def get_mcap_history(
    *, base: str, quote: str, start: int, end: int, resolution: str
) -> list[dict]:
    """Market-cap bars for a pair over a time range.

    GET {charts}/v1/charts/mcap/history
    """
    return _get(
        f"{CHARTS_URL}/v1/charts/mcap/history",
        params=_charts_params(base, quote, start, end, resolution),
    )


def get_mcap_initial_state(*, base: str, quote: str, resolution: str) -> dict:
    """Latest market-cap bar snapshot for a pair at a resolution.

    GET {charts}/v1/charts/mcap/initial-state?base=&quote=&resolution=
    """
    if resolution not in CHART_RESOLUTIONS:
        raise ValueError(f"resolution must be one of {CHART_RESOLUTIONS}")
    return _get(
        f"{CHARTS_URL}/v1/charts/mcap/initial-state",
        params={"base": base, "quote": quote, "resolution": resolution},
    )


# ---------------------------------------------------------------------------
# Builder API: Launch
# ---------------------------------------------------------------------------

def launch_token(
    *,
    image_path: str,
    name: str,
    ticker: str,
    description: str,
    change_address: str,
    asset_type: str = "Meme",
    initial_deposit_lovelace: int = 0,
    launch_type: str = "DEFAULT",
    twitter: Optional[str] = None,
    discord: Optional[str] = None,
    telegram: Optional[str] = None,
    website: Optional[str] = None,
    sender_utxo_cbor: Optional[list[str]] = None,
    blockfrost: Optional[BlockfrostClient] = None,
) -> dict:
    """Create a new bonding-curve token on snek.fun.

    POST {builder}/launch (multipart/form-data). Returns:
      {id, cbor, partial, assetId, assetSubject, policyId, logoCID, userOutputTokens}

    Per docs: when `collaterals` is omitted the builder signs and returns a
    pre-signed cbor; we still add the user's payment-key witness via
    /sign-and-submit.
    """
    if len(name) > 16:
        raise ValueError("name must be ≤16 chars")
    if len(ticker) > 6 or not ticker.isalnum():
        raise ValueError("ticker must be ≤6 alphanumeric chars")
    if len(description) > 500:
        raise ValueError("description must be ≤500 chars")
    if asset_type not in ("Meme", "AI"):
        raise ValueError("asset_type must be 'Meme' or 'AI'")
    if launch_type not in ("DEFAULT", "HYPED"):
        raise ValueError("launch_type must be 'DEFAULT' or 'HYPED'")

    if sender_utxo_cbor is None:
        if blockfrost is None:
            blockfrost = BlockfrostClient()
        utxos = blockfrost.get_utxos(change_address)
        sender_utxo_cbor = _utxos_to_cip30_hex(utxos, change_address)

    info: dict = {
        "assetType": asset_type,
        "name": name,
        "ticker": ticker,
        "description": description,
        "launchType": launch_type,
        "changeAddress": change_address,
        "utxos": sender_utxo_cbor,
    }
    if initial_deposit_lovelace > 0:
        info["initialDeposit"] = str(initial_deposit_lovelace)
    for k, v in (("twitter", twitter), ("discord", discord),
                 ("telegram", telegram), ("website", website)):
        if v:
            info[k] = v

    import json as _json
    with open(image_path, "rb") as fh:
        files = {"image": (os.path.basename(image_path), fh.read(),
                           _guess_mime(image_path))}
    data = {"info": _json.dumps(info)}

    for attempt in range(MAX_RETRIES):
        resp = requests.post(
            f"{BUILDER_URL}/launch",
            files=files, data=data, timeout=60,
        )
        if resp.status_code in (429, 500, 502, 503) and attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_BACKOFF * (attempt + 1))
            continue
        if not resp.ok:
            raise RuntimeError(f"/launch {resp.status_code}: {resp.text}")
        return resp.json()
    raise RuntimeError("/launch failed after retries")


def _guess_mime(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".gif": "image/gif"}.get(ext, "application/octet-stream")


# ---------------------------------------------------------------------------
# Full pipelines
# ---------------------------------------------------------------------------

def execute_buy(
    *,
    asset_id: str,
    ada_amount: float,
    sender_address: str,
    signing_key: PaymentSigningKey,
    slippage: str = "15",
    blockfrost: Optional[BlockfrostClient] = None,
    dry_run: bool = False,
) -> dict:
    """End-to-end buy: fetch UTXOs → builder API → sign → submit.

    Args:
        asset_id: Token as policyId.assetName (dot separator).
        ada_amount: ADA to spend (display units).
        sender_address: Bech32 Cardano address.
        signing_key: pycardano PaymentSigningKey.
        slippage: Slippage option string.
        blockfrost: Optional BlockfrostClient (created from env if None).
        dry_run: If True, build but don't submit.

    Returns:
        Dict with trade details and tx_hash (None if dry_run).
    """
    if blockfrost is None:
        blockfrost = BlockfrostClient()

    trade = buy_via_builder(
        asset_id=asset_id,
        ada_amount=ada_amount,
        sender_address=sender_address,
        slippage=slippage,
        blockfrost=blockfrost,
    )

    if not trade.cbor:
        raise RuntimeError("Builder API returned empty CBOR")

    result = {
        "trade_id": trade.trade_id,
        "input_amount": trade.input_amount,
        "output_amount": trade.output_amount,
        "cbor_size": len(trade.cbor) // 2,
        "tx_hash": None,
    }

    if dry_run:
        return result

    signed_cbor = sign_transaction(trade.cbor, signing_key)
    tx_hash = blockfrost.submit_tx(signed_cbor)

    result["tx_hash"] = tx_hash
    return result


def execute_sell(
    *,
    asset_id: str,
    token_amount: int,
    sender_address: str,
    signing_key: PaymentSigningKey,
    slippage: str = "15",
    blockfrost: Optional[BlockfrostClient] = None,
    dry_run: bool = False,
) -> dict:
    """End-to-end sell: fetch UTXOs → builder API → sign → submit.

    Args:
        asset_id: Token as policyId.assetName (dot separator).
        token_amount: Token quantity to sell (raw/base units).
        sender_address: Bech32 Cardano address.
        signing_key: pycardano PaymentSigningKey.
        slippage: Slippage option string.
        blockfrost: Optional BlockfrostClient (created from env if None).
        dry_run: If True, build but don't submit.

    Returns:
        Dict with trade details and tx_hash (None if dry_run).
    """
    if blockfrost is None:
        blockfrost = BlockfrostClient()

    trade = sell_via_builder(
        asset_id=asset_id,
        token_amount=token_amount,
        sender_address=sender_address,
        slippage=slippage,
        blockfrost=blockfrost,
    )

    if not trade.cbor:
        raise RuntimeError("Builder API returned empty CBOR")

    result = {
        "trade_id": trade.trade_id,
        "input_amount": trade.input_amount,
        "output_amount": trade.output_amount,
        "cbor_size": len(trade.cbor) // 2,
        "tx_hash": None,
    }

    if dry_run:
        return result

    signed_cbor = sign_transaction(trade.cbor, signing_key)
    tx_hash = blockfrost.submit_tx(signed_cbor)

    result["tx_hash"] = tx_hash
    return result


def execute_cancel(
    *,
    order_tx_hash: str,
    output_index: int = 0,
    sender_address: str,
    signing_key: PaymentSigningKey,
    blockfrost: Optional[BlockfrostClient] = None,
    dry_run: bool = False,
) -> dict:
    """End-to-end cancel: fetch UTXOs → builder API → sign → submit.

    Returns:
        Dict with cancel details and tx_hash (None if dry_run).
    """
    if blockfrost is None:
        blockfrost = BlockfrostClient()

    cancel_cbor = cancel_via_builder(
        tx_hash=order_tx_hash,
        output_index=output_index,
        sender_address=sender_address,
        blockfrost=blockfrost,
    )

    if not cancel_cbor:
        raise RuntimeError("Builder API returned empty cancel CBOR")

    result = {
        "order_tx_hash": order_tx_hash,
        "output_index": output_index,
        "cbor_size": len(cancel_cbor) // 2,
        "tx_hash": None,
    }

    if dry_run:
        return result

    signed_cbor = sign_transaction(cancel_cbor, signing_key)
    tx_hash = blockfrost.submit_tx(signed_cbor)

    result["tx_hash"] = tx_hash
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    """Command-line interface for snek.fun operations."""
    import argparse
    from dotenv import load_dotenv

    load_dotenv()

    parser = argparse.ArgumentParser(description="snek.fun bonding curve trading")
    sub = parser.add_subparsers(dest="command")

    # --- pool-state ---
    ps = sub.add_parser("pool-state", help="Fetch and display pool state")
    ps.add_argument("--policy-id", required=True, help="Token policy ID (hex)")
    ps.add_argument("--asset-name", required=True, help="Token asset name (hex)")
    ps.add_argument("--pool-nft", required=True, help="Pool NFT asset name (hex)")

    # --- buy ---
    buy = sub.add_parser("buy", help="Buy tokens via builder API")
    buy.add_argument("--asset-id", required=True, help="Token as policyId.assetName")
    buy.add_argument("--ada", type=float, required=True, help="ADA amount to spend")
    buy.add_argument("--slippage", default="15", choices=SLIPPAGE_OPTIONS)
    buy.add_argument("--dry-run", action="store_true")

    # --- sell ---
    sell = sub.add_parser("sell", help="Sell tokens via builder API")
    sell.add_argument("--asset-id", required=True, help="Token as policyId.assetName")
    sell.add_argument("--tokens", type=int, required=True, help="Token amount to sell (raw units)")
    sell.add_argument("--slippage", default="15", choices=SLIPPAGE_OPTIONS)
    sell.add_argument("--dry-run", action="store_true")

    # --- cancel ---
    cancel = sub.add_parser("cancel", help="Cancel a pending order")
    cancel.add_argument("tx_ref", help="Order tx hash (optionally #outputIndex)")
    cancel.add_argument("--dry-run", action="store_true")

    # --- estimate ---
    est = sub.add_parser("estimate", help="Estimate buy or sell output")
    est.add_argument("--policy-id", required=True)
    est.add_argument("--asset-name", required=True)
    est.add_argument("--pool-nft", required=True)
    est.add_argument("--ada", type=float, nargs="+", default=None, help="ADA amounts for buy estimate")
    est.add_argument("--tokens", type=int, nargs="+", default=None, help="Token amounts for sell estimate")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "pool-state":
        bf = BlockfrostClient()
        pool = get_pool_state(bf, args.policy_id, args.asset_name, args.pool_nft)
        print(f"Pool UTXO: {pool.tx_hash}#{pool.output_index}")
        print(f"ADA Reserve: {pool.reserve_ada:,} lovelace ({pool.reserve_ada / 1e6:.2f} ADA)")
        print(f"Token Reserve: {pool.reserve_token:,}")
        print(f"Curve aNum: {pool.datum.a_num}")
        print(f"Curve bNum: {pool.datum.b_num}")
        print(f"ADA Cap: {pool.datum.ada_cap_threshold:,} ({pool.datum.ada_cap_threshold / 1e6:.2f} ADA)")
        print(f"Bonding Progress: {pool.bonding_progress_pct:.2f}%")

    elif args.command == "estimate":
        bf = BlockfrostClient()
        pool = get_pool_state(bf, args.policy_id, args.asset_name, args.pool_nft)
        print(f"Pool: {pool.reserve_ada / 1e6:.2f} ADA / {pool.reserve_token:,} tokens")
        print(f"Progress: {pool.bonding_progress_pct:.2f}%\n")

        if args.tokens:
            print("  Sell estimates:")
            for tok in args.tokens:
                est = estimate_sell(pool, tok)
                print(f"  {tok:,} tokens → ~{est.ada_output / 1e6:.4f} ADA ({est.price_per_token_lovelace:.6f} lovelace/token)")
        else:
            ada_amounts = args.ada or [25, 50, 100, 250]
            print("  Buy estimates:")
            for ada in ada_amounts:
                est = estimate_buy(pool, int(ada * 1_000_000))
                print(f"  {ada} ADA → ~{est.token_output:,} tokens ({est.price_per_token_lovelace:.6f} lovelace/token)")

    elif args.command == "buy":
        address = os.environ["CARDANO_PAYMENT_ADDRESS"]
        key_path = os.environ["CARDANO_PRIVATE_KEY_PATH"]
        bf = BlockfrostClient()

        ticker = args.asset_id.split(".")[-1] if "." in args.asset_id else "token"
        print(f"{'='*50}")
        print(f"  snek.fun Buy — {args.ada} ADA → {ticker}")
        print(f"{'='*50}")
        print(f"  Address: {address[:20]}...{address[-10:]}")
        print(f"  Slippage: {args.slippage}%")
        print(f"  Mode: {'DRY RUN' if args.dry_run else 'LIVE'}\n")

        skey = PaymentSigningKey.load(key_path)
        result = execute_buy(
            asset_id=args.asset_id,
            ada_amount=args.ada,
            sender_address=address,
            signing_key=skey,
            slippage=args.slippage,
            blockfrost=bf,
            dry_run=args.dry_run,
        )

        print(f"  Trade ID: {result['trade_id']}")
        print(f"  Expected output: {result['output_amount']}")
        print(f"  Input: {result['input_amount']}")
        print(f"  CBOR size: {result['cbor_size']} bytes")

        if result["tx_hash"]:
            print(f"\n  Transaction submitted!")
            print(f"  Tx hash: {result['tx_hash']}")
            print(f"  https://cardanoscan.io/transaction/{result['tx_hash']}")
        elif args.dry_run:
            print(f"\n  DRY RUN — transaction built but not submitted.")

    elif args.command == "sell":
        address = os.environ["CARDANO_PAYMENT_ADDRESS"]
        key_path = os.environ["CARDANO_PRIVATE_KEY_PATH"]
        bf = BlockfrostClient()

        ticker = args.asset_id.split(".")[-1] if "." in args.asset_id else "token"
        print(f"{'='*50}")
        print(f"  snek.fun Sell — {args.tokens:,} {ticker} → ADA")
        print(f"{'='*50}")
        print(f"  Address: {address[:20]}...{address[-10:]}")
        print(f"  Slippage: {args.slippage}%")
        print(f"  Mode: {'DRY RUN' if args.dry_run else 'LIVE'}\n")

        skey = PaymentSigningKey.load(key_path)
        result = execute_sell(
            asset_id=args.asset_id,
            token_amount=args.tokens,
            sender_address=address,
            signing_key=skey,
            slippage=args.slippage,
            blockfrost=bf,
            dry_run=args.dry_run,
        )

        print(f"  Trade ID: {result['trade_id']}")
        print(f"  Expected output: {result['output_amount']}")
        print(f"  Input: {result['input_amount']}")
        print(f"  CBOR size: {result['cbor_size']} bytes")

        if result["tx_hash"]:
            print(f"\n  Transaction submitted!")
            print(f"  Tx hash: {result['tx_hash']}")
            print(f"  https://cardanoscan.io/transaction/{result['tx_hash']}")
        elif args.dry_run:
            print(f"\n  DRY RUN — transaction built but not submitted.")

    elif args.command == "cancel":
        address = os.environ["CARDANO_PAYMENT_ADDRESS"]
        key_path = os.environ["CARDANO_PRIVATE_KEY_PATH"]
        bf = BlockfrostClient()

        parts = args.tx_ref.split("#")
        tx_hash = parts[0]
        output_index = int(parts[1]) if len(parts) > 1 else 0

        print(f"Cancelling order: {tx_hash}#{output_index}")

        skey = PaymentSigningKey.load(key_path)
        result = execute_cancel(
            order_tx_hash=tx_hash,
            output_index=output_index,
            sender_address=address,
            signing_key=skey,
            blockfrost=bf,
            dry_run=args.dry_run,
        )

        if result["tx_hash"]:
            print(f"  Cancel submitted! Tx hash: {result['tx_hash']}")
            print(f"  https://cardanoscan.io/transaction/{result['tx_hash']}")
        elif args.dry_run:
            print(f"  DRY RUN — cancel built but not submitted.")


if __name__ == "__main__":
    _cli()
