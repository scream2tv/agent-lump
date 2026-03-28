"""
CardexScan / Hydra DEX Aggregator Client

Alternative aggregator to DexHunter with independent routing.
Supports swap estimation, transaction building, and trending tokens.

IMPORTANT: CardexScan uses base units (lovelace for ADA), not display units.
1 ADA = 1_000_000 lovelace.

API base: https://cardexscan.com
Requires: CARDEXSCAN_API_KEY environment variable.
"""

import os
import time
from dataclasses import dataclass
from hashlib import blake2b
from typing import Optional, Union

import cbor2
import requests
from pycardano import PaymentSigningKey, PaymentVerificationKey

CARDEXSCAN_BASE_URL = os.environ.get(
    "CARDEXSCAN_API_URL", "https://cardexscan.com"
)
CARDEXSCAN_API_KEY = os.environ.get("CARDEXSCAN_API_KEY", "")

ADA_LOVELACE = 1_000_000

MAX_RETRIES = 3
RETRY_BACKOFF = 2.0


def _headers() -> dict:
    if not CARDEXSCAN_API_KEY:
        raise RuntimeError("CARDEXSCAN_API_KEY environment variable is required")
    return {
        "Content-Type": "application/json",
        "x-api-key": CARDEXSCAN_API_KEY,
    }


def _request(
    method: str,
    path: str,
    payload: Optional[dict] = None,
    params: Optional[dict] = None,
) -> dict:
    url = f"{CARDEXSCAN_BASE_URL}{path}"
    timeout = 60 if method == "POST" else 30

    for attempt in range(MAX_RETRIES):
        try:
            if method == "GET":
                resp = requests.get(url, params=params, headers=_headers(), timeout=timeout)
            else:
                resp = requests.post(url, json=payload, headers=_headers(), timeout=timeout)

            if resp.status_code in (429, 500, 502, 503, 520, 521, 522, 524):
                wait = RETRY_BACKOFF * (attempt + 1)
                print(f"  CardexScan {resp.status_code} on {path}, retrying in {wait:.0f}s... ({attempt + 1}/{MAX_RETRIES})")
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
# Token ID Helpers
# ---------------------------------------------------------------------------


def parse_token_id(token_id: str) -> dict:
    """Split a DexHunter-style token ID into CardexScan's {policyId, assetName} format.

    Cardano native tokens have a 56-char policy ID followed by the asset name hex.

    Args:
        token_id: Full token ID (policyId + assetNameHex), e.g.
                  "0691b2fecca1ac4f53cb6dfb00b7013e561d1f34403b957cbb5af1fa4e49474854"

    Returns:
        {"policyId": "0691b2fe...", "assetName": "4e49474854"}
    """
    if not token_id or token_id.lower() == "lovelace":
        return "lovelace"
    policy_id = token_id[:56]
    asset_name = token_id[56:]
    return {"policyId": policy_id, "assetName": asset_name}


def ada_to_lovelace(ada: float) -> int:
    """Convert display ADA to lovelace (base units)."""
    return int(ada * ADA_LOVELACE)


def lovelace_to_ada(lovelace: int) -> float:
    """Convert lovelace to display ADA."""
    return lovelace / ADA_LOVELACE


# ---------------------------------------------------------------------------
# Swap Estimation
# ---------------------------------------------------------------------------


@dataclass
class CardexEstimate:
    """Parsed CardexScan estimate response."""

    output_amount: str
    price: Optional[str]
    splits: list[dict]
    raw: dict


def estimate_swap(
    token_in: str,
    token_out_id: str,
    amount_in_lovelace: int,
    slippage: float = 1.0,
    blacklisted_dexes: Optional[list[str]] = None,
) -> CardexEstimate:
    """Estimate a swap via CardexScan's Hydra aggregator.

    Args:
        token_in: "lovelace" for ADA, or full token ID for other tokens.
        token_out_id: Full token ID (policyId + assetNameHex) for the output token.
        amount_in_lovelace: Amount in base units (lovelace for ADA).
        slippage: Slippage tolerance in percent (default 1%).
        blacklisted_dexes: DEX names to exclude from routing.

    Returns:
        CardexEstimate with routing and pricing.
    """
    token_in_parsed = parse_token_id(token_in) if token_in and token_in != "lovelace" else "lovelace"
    token_out_parsed = parse_token_id(token_out_id)

    payload = {
        "tokenInAmount": amount_in_lovelace,
        "slippage": slippage,
        "tokenIn": token_in_parsed,
        "tokenOut": token_out_parsed,
        "blacklisted_dexes": blacklisted_dexes or [],
    }
    data = _post("/api/cds/swap/aggregate", payload)
    return CardexEstimate(
        output_amount=str(data.get("outputAmount", data.get("tokenOutAmount", ""))),
        price=str(data.get("price", "")),
        splits=data.get("splits", data.get("routes", [])),
        raw=data,
    )


# ---------------------------------------------------------------------------
# Transaction Building
# ---------------------------------------------------------------------------


@dataclass
class CardexBuild:
    """Parsed CardexScan build response with unsigned CBOR."""

    cbor: str
    raw: dict


def build_swap(
    user_address: str,
    token_in: str,
    token_out_id: str,
    amount_in_lovelace: int,
    slippage: float = 1.0,
    blacklisted_dexes: Optional[list[str]] = None,
) -> CardexBuild:
    """Build an unsigned swap transaction via CardexScan.

    Args:
        user_address: Bech32 Cardano address.
        token_in: "lovelace" for ADA, or full token ID.
        token_out_id: Full token ID for the output token.
        amount_in_lovelace: Amount in base units (lovelace for ADA).
        slippage: Slippage tolerance in percent.
        blacklisted_dexes: DEX names to exclude.

    Returns:
        CardexBuild with unsigned CBOR hex.
    """
    token_in_parsed = parse_token_id(token_in) if token_in and token_in != "lovelace" else "lovelace"
    token_out_parsed = parse_token_id(token_out_id)

    payload = {
        "tokenInAmount": amount_in_lovelace,
        "slippage": slippage,
        "tokenIn": token_in_parsed,
        "tokenOut": token_out_parsed,
        "userAddress": user_address,
        "blacklisted_dexes": blacklisted_dexes or [],
    }
    data = _post("/api/cds/swap/cbor/build", payload)
    cbor_hex = data.get("cbor", data.get("tx", data.get("transaction", "")))
    return CardexBuild(cbor=cbor_hex, raw=data)


# ---------------------------------------------------------------------------
# Signing (same raw-bytes approach as dexhunter_client)
# ---------------------------------------------------------------------------


def sign_transaction(unsigned_cbor_hex: str, signing_key: PaymentSigningKey) -> str:
    """Sign an unsigned CBOR transaction locally using raw bytes.

    Args:
        unsigned_cbor_hex: Hex-encoded unsigned transaction CBOR.
        signing_key: pycardano PaymentSigningKey.

    Returns:
        Hex-encoded signed transaction CBOR.
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


def submit_transaction(signed_cbor_hex: str, context) -> str:
    """Submit a signed transaction to Cardano mainnet via Blockfrost.

    Args:
        signed_cbor_hex: Fully-signed transaction CBOR hex.
        context: pycardano BlockFrostChainContext.

    Returns:
        Transaction hash string.
    """
    tx_bytes = bytes.fromhex(signed_cbor_hex)
    return str(context.submit_tx(tx_bytes))


# ---------------------------------------------------------------------------
# Trending Tokens
# ---------------------------------------------------------------------------


def get_trending_tokens(timeframe: str = "24h", count: int = 20) -> list[dict]:
    """Get trending tokens by volume.

    Args:
        timeframe: "1h", "4h", "24h", "7d".
        count: Number of tokens to return.
    """
    return _get(f"/api/tokens/trending?timeframe={timeframe}&count={count}")


def get_all_trades(timeframe: str = "24h", limit: int = 50, order: str = "desc") -> list[dict]:
    """Get recent token trades across all DEXes."""
    return _get(f"/api/token/trades/all?timeframe={timeframe}&limit={limit}&order={order}")


def get_order_history(address: str) -> list[dict]:
    """Get swap order history for a wallet address."""
    return _get(f"/api/orders?address={address}")
