"""
Minswap Aggregator Client

Routes swaps across 14 Cardano DEXes via Minswap's aggregator API.
No API key required. Amounts in lovelace (base units).

Supported DEXes: MinswapV2, SundaeSwapV3, WingRidersV2, CswapV1,
VyFinance, Splash, MuesliSwap, Spectrum, and more.

API base: https://agg-api.minswap.org/aggregator
Docs: https://docs.minswap.org/developer/aggregator-api
"""

import logging
import os
import random
import time
from dataclasses import dataclass, field
from hashlib import blake2b
from typing import Optional

import cbor2
import requests
from pycardano import PaymentSigningKey, PaymentVerificationKey

logger = logging.getLogger(__name__)

MINSWAP_AGG_URL = os.environ.get(
    "MINSWAP_AGG_URL", "https://agg-api.minswap.org/aggregator"
)

ADA_LOVELACE = 1_000_000

MAX_RETRIES = 3
RETRY_BACKOFF = 2.0
RETRY_JITTER = 0.5


def _post(path: str, payload: dict) -> dict:
    url = f"{MINSWAP_AGG_URL}{path}"
    last_resp = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=60,
            )
            last_resp = resp
            if resp.status_code in (429, 500, 502, 503, 520, 524):
                base_wait = RETRY_BACKOFF * (attempt + 1)
                jitter = random.uniform(0, RETRY_JITTER * base_wait)
                wait = base_wait + jitter
                logger.warning(
                    "Minswap %d on %s (attempt %d/%d), retrying in %.1fs",
                    resp.status_code, path, attempt + 1, MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES - 1:
                base_wait = RETRY_BACKOFF * (attempt + 1)
                jitter = random.uniform(0, RETRY_JITTER * base_wait)
                wait = base_wait + jitter
                logger.warning(
                    "Minswap timeout on %s (attempt %d/%d), retrying in %.1fs",
                    path, attempt + 1, MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue
            raise

    body_preview = ""
    if last_resp is not None:
        try:
            body_preview = f" | body: {last_resp.text[:200]}"
        except Exception:
            pass
    if last_resp is not None:
        raise requests.exceptions.HTTPError(
            f"Minswap {last_resp.status_code} on {path} after {MAX_RETRIES} retries{body_preview}",
            response=last_resp,
        )
    raise RuntimeError(f"Minswap {path} failed after {MAX_RETRIES} retries (no response)")


def _get(path: str, params: Optional[dict] = None) -> dict:
    url = f"{MINSWAP_AGG_URL}{path}"
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Token Search
# ---------------------------------------------------------------------------


def search_tokens(query: str, only_verified: bool = True) -> list[dict]:
    """Search tokens by ticker, name, or policy ID."""
    data = _post("/tokens", {"query": query, "only_verified": only_verified})
    return data.get("tokens", [])


# ---------------------------------------------------------------------------
# Swap Estimation
# ---------------------------------------------------------------------------

PROTOCOLS = [
    "MinswapV2", "Minswap", "MinswapStable", "MuesliSwap", "Splash",
    "SundaeSwapV3", "SundaeSwap", "VyFinance", "CswapV1",
    "WingRidersV2", "WingRiders", "WingRidersStableV2", "Spectrum", "SplashStable",
]


@dataclass
class MinswapEstimate:
    """Parsed estimate from Minswap aggregator."""

    amount_in: str
    amount_out: str
    min_amount_out: str
    avg_price_impact: float
    total_lp_fee: str
    total_dex_fee: str
    aggregator_fee: str
    aggregator_fee_percent: float
    deposits: str
    paths: list
    raw: dict


def estimate_swap(
    token_in: str,
    token_out: str,
    amount_lovelace: int,
    slippage: float = 0.5,
    allow_multi_hops: bool = True,
) -> MinswapEstimate:
    """Estimate a swap via Minswap's aggregator.

    Args:
        token_in: "lovelace" for ADA, or full token ID (policyId + assetNameHex).
        token_out: Full token ID for the output token.
        amount_lovelace: Amount in base units (lovelace for ADA).
        slippage: Slippage tolerance in percent (default 0.5%).
        allow_multi_hops: Allow multi-hop routing for better prices.

    Returns:
        MinswapEstimate with routing, pricing, and fee breakdown.
    """
    payload = {
        "amount": str(amount_lovelace),
        "token_in": token_in,
        "token_out": token_out,
        "slippage": slippage,
        "allow_multi_hops": allow_multi_hops,
    }
    data = _post("/estimate", payload)
    return MinswapEstimate(
        amount_in=data.get("amount_in", ""),
        amount_out=data.get("amount_out", ""),
        min_amount_out=data.get("min_amount_out", ""),
        avg_price_impact=data.get("avg_price_impact", 0),
        total_lp_fee=data.get("total_lp_fee", "0"),
        total_dex_fee=data.get("total_dex_fee", "0"),
        aggregator_fee=data.get("aggregator_fee", "0"),
        aggregator_fee_percent=data.get("aggregator_fee_percent", 0),
        deposits=data.get("deposits", "0"),
        paths=data.get("paths", []),
        raw=data,
    )


# ---------------------------------------------------------------------------
# Transaction Building
# ---------------------------------------------------------------------------


def build_swap(
    sender: str,
    token_in: str,
    token_out: str,
    amount_lovelace: int,
    min_amount_out: str,
    slippage: float = 0.5,
    allow_multi_hops: bool = True,
) -> str:
    """Build an unsigned swap transaction via Minswap aggregator.

    Args:
        sender: Bech32 Cardano address.
        token_in: "lovelace" for ADA, or full token ID.
        token_out: Full token ID for output token.
        amount_lovelace: Amount in base units.
        min_amount_out: Minimum output from estimate (string).
        slippage: Must match the estimate call.
        allow_multi_hops: Must match the estimate call.

    Returns:
        Unsigned CBOR hex string.
    """
    payload = {
        "sender": sender,
        "min_amount_out": min_amount_out,
        "estimate": {
            "amount": str(amount_lovelace),
            "token_in": token_in,
            "token_out": token_out,
            "slippage": slippage,
            "allow_multi_hops": allow_multi_hops,
        },
    }
    data = _post("/build-tx", payload)
    cbor = data.get("cbor", "")
    if not cbor:
        raise RuntimeError(f"Minswap build-tx returned no CBOR: {data}")
    return cbor


# ---------------------------------------------------------------------------
# Signing (raw CBOR bytes, same approach as dexhunter_client)
# ---------------------------------------------------------------------------


def sign_transaction(unsigned_cbor_hex: str, signing_key: PaymentSigningKey) -> str:
    """Sign an unsigned CBOR transaction locally using raw bytes.

    Returns the witness set hex for the /finalize-and-submit-tx endpoint.
    """
    tx_bytes = bytes.fromhex(unsigned_cbor_hex)
    tx_array = cbor2.loads(tx_bytes)
    body_bytes = cbor2.dumps(tx_array[0])
    tx_hash = blake2b(body_bytes, digest_size=32).digest()

    vk = PaymentVerificationKey.from_signing_key(signing_key)
    signature = signing_key.sign(tx_hash)

    vkey_witness = [vk.payload, signature]
    witness_set = {0: [vkey_witness]}
    return cbor2.dumps(witness_set).hex()


# ---------------------------------------------------------------------------
# Submission via Minswap
# ---------------------------------------------------------------------------


def submit_via_minswap(unsigned_cbor_hex: str, witness_set_hex: str) -> str:
    """Submit a signed transaction via Minswap's finalize-and-submit endpoint.

    Minswap assembles the final transaction and submits to the network.

    Args:
        unsigned_cbor_hex: Original unsigned CBOR from build_swap.
        witness_set_hex: Witness set hex from sign_transaction.

    Returns:
        Transaction hash (tx_id).
    """
    payload = {
        "cbor": unsigned_cbor_hex,
        "witness_set": witness_set_hex,
    }
    data = _post("/finalize-and-submit-tx", payload)
    tx_id = data.get("tx_id", "")
    if not tx_id:
        raise RuntimeError(f"Minswap submit returned no tx_id: {data}")
    return tx_id


def submit_via_blockfrost(signed_cbor_hex: str, context) -> str:
    """Alternative: submit directly via Blockfrost."""
    tx_bytes = bytes.fromhex(signed_cbor_hex)
    return str(context.submit_tx(tx_bytes))


# ---------------------------------------------------------------------------
# Full Pipeline
# ---------------------------------------------------------------------------


def execute_swap(
    sender: str,
    token_in: str,
    token_out: str,
    amount_lovelace: int,
    signing_key: PaymentSigningKey,
    slippage: float = 0.5,
    max_price_impact: float = 2.0,
) -> dict:
    """End-to-end swap: estimate → build → sign → submit via Minswap.

    Args:
        sender: Bech32 Cardano address.
        token_in: "lovelace" for ADA, or full token ID.
        token_out: Full token ID for output token.
        amount_lovelace: Amount in base units (lovelace for ADA).
        signing_key: pycardano PaymentSigningKey.
        slippage: Slippage tolerance in percent.
        max_price_impact: Abort if impact exceeds this (default 2%).

    Returns:
        Dict with tx_id, estimate details, and routing.
    """
    estimate = estimate_swap(token_in, token_out, amount_lovelace, slippage)

    if estimate.avg_price_impact > max_price_impact:
        raise ValueError(
            f"Price impact {estimate.avg_price_impact:.2f}% exceeds "
            f"{max_price_impact}% threshold."
        )

    cbor = build_swap(
        sender, token_in, token_out, amount_lovelace,
        estimate.min_amount_out, slippage,
    )

    witness_hex = sign_transaction(cbor, signing_key)
    tx_id = submit_via_minswap(cbor, witness_hex)

    return {
        "tx_id": tx_id,
        "amount_in_ada": int(estimate.amount_in) / ADA_LOVELACE,
        "amount_out": estimate.amount_out,
        "min_amount_out": estimate.min_amount_out,
        "price_impact": estimate.avg_price_impact,
        "dex_fee_ada": int(estimate.total_dex_fee) / ADA_LOVELACE,
        "aggregator_fee_ada": int(estimate.aggregator_fee) / ADA_LOVELACE,
        "routes": [
            {
                "protocol": hop.get("protocol"),
                "amount_in": hop.get("amount_in"),
                "amount_out": hop.get("amount_out"),
                "impact": hop.get("price_impact"),
            }
            for path in estimate.paths
            for hop in path
        ],
    }
