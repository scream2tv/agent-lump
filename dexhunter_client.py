"""
DexHunter v3 Swap Aggregator Client

Full pipeline: search tokens → estimate → build → sign → submit.
Routes across 15+ Cardano DEXes for best execution.

API docs: https://dexhunter.gitbook.io/dexhunter-partners
Base URL: https://api-us.dexhunterv3.app
"""

import os
import time
from dataclasses import dataclass
from hashlib import blake2b
from typing import Optional

import cbor2
import requests
from pycardano import (
    PaymentSigningKey,
    PaymentVerificationKey,
    Transaction,
)

DEXHUNTER_BASE_URL = os.environ.get(
    "DEXHUNTER_BASE_URL", "https://api-us.dexhunterv3.app"
)
DEXHUNTER_PARTNER_ID = os.environ.get(
    "DEXHUNTER_PARTNER_ID", os.environ.get("DEXHUNTER_API_KEY", "")
)


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if DEXHUNTER_PARTNER_ID:
        h["X-Partner-Id"] = DEXHUNTER_PARTNER_ID
    return h


MAX_RETRIES = 3
RETRY_BACKOFF = 2.0


def _request(method: str, path: str, payload: Optional[dict] = None, params: Optional[dict] = None) -> dict:
    url = f"{DEXHUNTER_BASE_URL}{path}"
    timeout = 60 if method == "POST" else 30

    for attempt in range(MAX_RETRIES):
        try:
            if method == "GET":
                resp = requests.get(url, params=params, headers=_headers(), timeout=timeout)
            else:
                resp = requests.post(url, json=payload, headers=_headers(), timeout=timeout)

            if resp.status_code in (429, 500, 502, 503, 520, 521, 522, 524):
                wait = RETRY_BACKOFF * (attempt + 1)
                print(f"  DexHunter {resp.status_code} on {path}, retrying in {wait:.0f}s... ({attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp.json()

        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF * (attempt + 1)
                print(f"  Timeout on {path}, retrying in {wait:.0f}s... ({attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            raise

    resp.raise_for_status()
    return resp.json()


def _post(path: str, payload: dict) -> dict:
    return _request("POST", path, payload=payload)


def _get(path: str, params: Optional[dict] = None) -> dict:
    return _request("GET", path, params=params)


# ---------------------------------------------------------------------------
# Token Discovery
# ---------------------------------------------------------------------------


def search_tokens(query: str, verified: Optional[bool] = None) -> list[dict]:
    """Search tokens by ticker, name, or policy ID.

    Args:
        query: Ticker (e.g. "NIGHT"), policy ID (56 chars), or full token ID (>56 chars).
        verified: If True, return only verified tokens.

    Returns:
        List of token dicts with token_id, ticker, token_policy, is_verified, etc.
    """
    params = {"query": query}
    if verified is not None:
        params["verified"] = str(verified).lower()
    return _get("/swap/tokens", params=params)


def get_token(token_id: str) -> dict:
    """Get metadata for a single token by its full ID (policyId + assetNameHex)."""
    return _get(f"/swap/token/{token_id}")


def get_average_price(base: str, quote: str) -> dict:
    """Get average price for a token pair.

    Args:
        base: "ADA" or full token ID.
        quote: "ADA" or full token ID.
    """
    b = "ADA" if (not base or base.lower() == "lovelace") else base
    q = "ADA" if (not quote or quote.lower() == "lovelace") else quote
    return _get(f"/swap/averagePrice/{b}/{q}")


# ---------------------------------------------------------------------------
# Swap Estimation
# ---------------------------------------------------------------------------


@dataclass
class SwapEstimate:
    """Parsed estimate response with routing details."""

    total_output: str
    net_price: str
    average_price: str
    total_fee: str
    price_impact: Optional[str]
    splits: list[dict]
    raw: dict


def estimate_swap(
    token_in: str,
    token_out: str,
    amount_in: float,
    slippage: float = 1.0,
) -> SwapEstimate:
    """Estimate a swap without building a transaction.

    Args:
        token_in: Source token ID. Empty string "" for ADA.
        token_out: Destination token ID (policyId + assetNameHex).
        amount_in: Amount in display units (e.g. 10 = 10 ADA).
        slippage: Slippage tolerance in percent (default 1%).

    Returns:
        SwapEstimate with routing breakdown and pricing.
    """
    payload = {
        "token_in": token_in,
        "token_out": token_out,
        "amount_in": amount_in,
        "slippage": slippage,
        "include_routes": True,
    }
    data = _post("/swap/estimate", payload)
    return SwapEstimate(
        total_output=data.get("total_output", ""),
        net_price=data.get("net_price", ""),
        average_price=data.get("average_price", ""),
        total_fee=data.get("total_fee", ""),
        price_impact=data.get("price_impact"),
        splits=data.get("splits", []),
        raw=data,
    )


# ---------------------------------------------------------------------------
# Wallet Registration (required before first build)
# ---------------------------------------------------------------------------


def register_wallet(address: str, is_stake: bool = False) -> dict:
    """Register a wallet address with DexHunter.

    Must be called at least once before building a swap for a new address.
    """
    return _post("/swap/wallet", {"addresses": [address], "is_stake": is_stake})


# ---------------------------------------------------------------------------
# Transaction Building
# ---------------------------------------------------------------------------


@dataclass
class SwapBuild:
    """Parsed build response containing the unsigned CBOR transaction."""

    cbor: str
    expected_output: Optional[str]
    dexes: list[str]
    raw: dict


def build_swap(
    buyer_address: str,
    token_in: str,
    token_out: str,
    amount_in: float,
    slippage: float = 1.0,
) -> SwapBuild:
    """Build an unsigned swap transaction with optimal routing.

    Automatically registers the wallet if needed.

    Args:
        buyer_address: Bech32 Cardano address.
        token_in: Source token ID. Empty string "" for ADA.
        token_out: Destination token ID (policyId + assetNameHex).
        amount_in: Amount in display units (e.g. 10 = 10 ADA).
        slippage: Slippage tolerance in percent (default 1%).

    Returns:
        SwapBuild with unsigned CBOR hex ready for signing.
    """
    try:
        register_wallet(buyer_address)
    except requests.HTTPError:
        pass

    payload = {
        "buyer_address": buyer_address,
        "token_in": token_in,
        "token_out": token_out,
        "amount_in": amount_in,
        "slippage": slippage,
        "tx_optimization": True,
    }
    data = _post("/swap/build", payload)
    return SwapBuild(
        cbor=data.get("cbor", ""),
        expected_output=data.get("expected_output"),
        dexes=data.get("dexes", []),
        raw=data,
    )


# ---------------------------------------------------------------------------
# Signing (local, pycardano)
# ---------------------------------------------------------------------------


def sign_transaction(unsigned_cbor_hex: str, signing_key: PaymentSigningKey) -> str:
    """Sign an unsigned CBOR transaction locally using raw bytes.

    Hashes the original CBOR body bytes directly (no deserialize/re-serialize)
    to avoid transaction hash mismatches. This mirrors the FixedTransaction
    approach used by CSL in the reference JS client.

    Args:
        unsigned_cbor_hex: Hex-encoded unsigned transaction CBOR from build_swap.
        signing_key: pycardano PaymentSigningKey.

    Returns:
        Hex-encoded signed transaction CBOR ready for the /swap/sign endpoint.
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


# ---------------------------------------------------------------------------
# Witness Assembly + Submission
# ---------------------------------------------------------------------------


def add_witness(unsigned_cbor_hex: str, signatures: str) -> dict:
    """Send local signatures to DexHunter to assemble the final transaction.

    DexHunter may add its own witnesses (e.g. for smart contract interactions).

    Args:
        unsigned_cbor_hex: Original unsigned CBOR hex from build_swap.
        signatures: Hex-encoded witness set from sign_transaction.

    Returns:
        Dict with 'cbor' key containing the fully-signed transaction.
    """
    payload = {
        "txCbor": unsigned_cbor_hex,
        "Signatures": signatures,
    }
    return _post("/swap/sign", payload)


def submit_transaction(signed_cbor_hex: str, context) -> str:
    """Submit a signed transaction to the Cardano network via Blockfrost.

    Submits raw CBOR bytes directly to avoid any re-serialization issues.

    Args:
        signed_cbor_hex: Fully-signed transaction CBOR hex.
        context: pycardano BlockFrostChainContext.

    Returns:
        Transaction hash string.
    """
    tx_bytes = bytes.fromhex(signed_cbor_hex)
    return str(context.submit_tx(tx_bytes))


# ---------------------------------------------------------------------------
# Full Pipeline
# ---------------------------------------------------------------------------


def execute_swap(
    buyer_address: str,
    token_in: str,
    token_out: str,
    amount_in: float,
    signing_key: PaymentSigningKey,
    context,
    slippage: float = 1.0,
) -> dict:
    """End-to-end swap: estimate → build → sign → witness → submit.

    Args:
        buyer_address: Bech32 Cardano address.
        token_in: Source token ID ("" for ADA).
        token_out: Destination token ID.
        amount_in: Amount in display units.
        signing_key: pycardano PaymentSigningKey.
        context: pycardano BlockFrostChainContext for submission.
        slippage: Slippage tolerance in percent.

    Returns:
        Dict with estimate, build, and tx_hash.
    """
    estimate = estimate_swap(token_in, token_out, amount_in, slippage)

    price_impact = float(estimate.price_impact or 0)
    if price_impact > 2.0:
        raise ValueError(
            f"Price impact {price_impact:.2f}% exceeds 2% safety threshold. "
            f"Reduce amount or increase slippage."
        )

    build = build_swap(buyer_address, token_in, token_out, amount_in, slippage)
    if not build.cbor:
        raise RuntimeError("DexHunter returned empty CBOR. Check token IDs and amount.")

    signatures = sign_transaction(build.cbor, signing_key)
    witnessed = add_witness(build.cbor, signatures)
    signed_cbor = witnessed.get("cbor", "")
    if not signed_cbor:
        raise RuntimeError("Witness assembly returned no CBOR.")

    tx_hash = submit_transaction(signed_cbor, context)

    return {
        "tx_hash": tx_hash,
        "estimate": {
            "total_output": estimate.total_output,
            "net_price": estimate.net_price,
            "price_impact": estimate.price_impact,
            "splits": estimate.splits,
        },
        "dexes": build.dexes,
    }
