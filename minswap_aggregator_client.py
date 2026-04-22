"""
Minswap Aggregator Client

Uses Minswap's aggregator API to estimate swaps and build transactions.
The aggregator finds the best route across all Cardano DEXes (Minswap V1/V2,
CswapV1, etc.) and returns a fully constructed unsigned transaction.

Flow: estimate → build-tx → sign locally → submit via Blockfrost

This is the same API the Minswap frontend uses, so transactions are
guaranteed to be correctly formatted and recognized by the batcher.
"""

import logging
import os
import time
from dataclasses import dataclass
from hashlib import blake2b
from typing import Optional

import cbor2
import requests
from pycardano import PaymentSigningKey, PaymentVerificationKey

from blockfrost_client import BlockfrostClient

logger = logging.getLogger(__name__)

AGGREGATOR_BASE_URL = "https://aggr-monorepo-mainnet-prod.minswap.org/aggregator"
DEFAULT_SLIPPAGE = 0.5
DEFAULT_TIMEOUT = 30


@dataclass
class SwapEstimate:
    """Result from the Minswap aggregator estimate endpoint."""
    token_in: str
    token_out: str
    amount_in: int
    amount_out: int
    min_amount_out: int
    total_lp_fee: int
    total_dex_fee: int
    deposits: int
    aggregator_fee: int
    avg_price_impact: float
    paths: list
    raw: dict


def estimate(
    *,
    token_in: str,
    token_out: str,
    amount: int,
    slippage: float = DEFAULT_SLIPPAGE,
    exclude_protocols: Optional[list] = None,
) -> SwapEstimate:
    """Get a swap estimate from the Minswap aggregator.

    Args:
        token_in: Input token ID ("lovelace" for ADA, or policyId+assetNameHex).
        token_out: Output token ID.
        amount: Amount of token_in in smallest units (lovelace for ADA).
        slippage: Slippage tolerance as percentage (0.5 = 0.5%).
        exclude_protocols: Protocols to exclude (e.g. ["MuesliSwap"]).

    Returns:
        SwapEstimate with routing and fee details.
    """
    payload = {
        "amount": str(amount),
        "token_in": token_in,
        "token_out": token_out,
        "slippage": slippage,
        "allow_multi_hops": True,
        "allow_non_atomic_multi_hops": True,
    }
    if exclude_protocols:
        payload["exclude_protocols"] = exclude_protocols

    url = f"{AGGREGATOR_BASE_URL}/estimate"
    logger.debug("POST %s %s", url, payload)

    resp = requests.post(url, json=payload, timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    return SwapEstimate(
        token_in=data["token_in"],
        token_out=data["token_out"],
        amount_in=int(data["amount_in"]),
        amount_out=int(data["amount_out"]),
        min_amount_out=int(data["min_amount_out"]),
        total_lp_fee=int(data.get("total_lp_fee", 0)),
        total_dex_fee=int(data.get("total_dex_fee", 0)),
        deposits=int(data.get("deposits", 0)),
        aggregator_fee=int(data.get("aggregator_fee", 0)),
        avg_price_impact=float(data.get("avg_price_impact", 0)),
        paths=data.get("paths", []),
        raw=data,
    )


def build_tx(
    *,
    sender_address: str,
    token_in: str,
    token_out: str,
    amount: int,
    min_amount_out: int,
    slippage: float = DEFAULT_SLIPPAGE,
    exclude_protocols: Optional[list] = None,
) -> str:
    """Build an unsigned swap transaction via the Minswap aggregator.

    Args:
        sender_address: Bech32 Cardano address.
        token_in: Input token ID.
        token_out: Output token ID.
        amount: Amount of token_in in smallest units.
        min_amount_out: Minimum acceptable output (from estimate).
        slippage: Slippage tolerance as percentage.
        exclude_protocols: Protocols to exclude.

    Returns:
        Unsigned transaction CBOR hex string.
    """
    estimate_payload = {
        "amount": str(amount),
        "token_in": token_in,
        "token_out": token_out,
        "slippage": slippage,
        "allow_multi_hops": True,
        "allow_non_atomic_multi_hops": True,
        "min_amount_out": str(min_amount_out),
    }
    if exclude_protocols:
        estimate_payload["exclude_protocols"] = exclude_protocols

    payload = {
        "sender": sender_address,
        "estimate": estimate_payload,
        "min_amount_out": str(min_amount_out),
    }

    url = f"{AGGREGATOR_BASE_URL}/build-tx"
    logger.debug("POST %s sender=%s...", url, sender_address[:30])

    resp = requests.post(url, json=payload, timeout=DEFAULT_TIMEOUT)
    if resp.status_code >= 400:
        logger.error("build-tx %d: %s", resp.status_code, resp.text[:500])
    resp.raise_for_status()
    data = resp.json()

    cbor_hex = data["cbor"]
    logger.info("build-tx returned %d bytes of CBOR", len(cbor_hex) // 2)
    return cbor_hex


def sign_transaction(unsigned_cbor_hex: str, signing_key: PaymentSigningKey) -> str:
    """Sign an unsigned CBOR transaction and return the fully-signed hex."""
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


def get_tx_hash(cbor_hex: str) -> str:
    """Compute the transaction hash from CBOR hex."""
    tx_bytes = bytes.fromhex(cbor_hex)
    tx_array = cbor2.loads(tx_bytes)
    body_bytes = cbor2.dumps(tx_array[0])
    return blake2b(body_bytes, digest_size=32).hexdigest()


def execute_swap(
    *,
    token_in: str,
    token_out: str,
    amount: int,
    sender_address: str,
    signing_key: PaymentSigningKey,
    slippage: float = DEFAULT_SLIPPAGE,
    exclude_protocols: Optional[list] = None,
    blockfrost: Optional[BlockfrostClient] = None,
    dry_run: bool = False,
) -> dict:
    """End-to-end swap: estimate → build-tx → sign → submit.

    Args:
        token_in: Input token ID ("lovelace" for ADA).
        token_out: Output token ID (policyId+assetNameHex).
        amount: Amount of token_in in smallest units.
        sender_address: Bech32 Cardano address.
        signing_key: pycardano PaymentSigningKey.
        slippage: Slippage tolerance percentage.
        exclude_protocols: Protocols to exclude.
        blockfrost: Optional BlockfrostClient for submission.
        dry_run: If True, estimate and build but don't submit.

    Returns:
        Dict with estimate details and tx_hash.
    """
    if blockfrost is None:
        blockfrost = BlockfrostClient()

    logger.info(
        "Estimating swap: %s %s → %s (slippage %.1f%%)",
        amount, token_in[:16], token_out[:16], slippage,
    )

    est = estimate(
        token_in=token_in,
        token_out=token_out,
        amount=amount,
        slippage=slippage,
        exclude_protocols=exclude_protocols,
    )

    route_desc = " → ".join(
        p.get("protocol", "?") for path in est.paths for p in path
    )

    result = {
        "token_in": est.token_in,
        "token_out": est.token_out,
        "amount_in": est.amount_in,
        "amount_out": est.amount_out,
        "min_amount_out": est.min_amount_out,
        "total_lp_fee": est.total_lp_fee,
        "total_dex_fee": est.total_dex_fee,
        "deposits": est.deposits,
        "aggregator_fee": est.aggregator_fee,
        "price_impact_pct": est.avg_price_impact,
        "route": route_desc,
        "tx_hash": None,
    }

    logger.info(
        "Estimate: %s → %s (min %s), route: %s, impact: %.2f%%",
        est.amount_in, est.amount_out, est.min_amount_out,
        route_desc, est.avg_price_impact,
    )

    if dry_run:
        return result

    unsigned_cbor = build_tx(
        sender_address=sender_address,
        token_in=token_in,
        token_out=token_out,
        amount=amount,
        min_amount_out=est.min_amount_out,
        slippage=slippage,
        exclude_protocols=exclude_protocols,
    )

    signed_cbor = sign_transaction(unsigned_cbor, signing_key)
    tx_hash = get_tx_hash(unsigned_cbor)
    logger.info("Signed tx %s, submitting...", tx_hash)

    submitted = blockfrost.submit_tx(signed_cbor)
    logger.info("Submitted: %s", submitted)

    result["tx_hash"] = tx_hash
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    import argparse
    from dotenv import load_dotenv

    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Minswap Aggregator Client")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    est_p = sub.add_parser("estimate", help="Get a swap estimate")
    est_p.add_argument("token_out", help="Output token ID (policyId+assetNameHex)")
    est_p.add_argument("--token-in", default="lovelace", help="Input token ID")
    est_p.add_argument("--amount", type=int, required=True, help="Amount in smallest units")
    est_p.add_argument("--slippage", type=float, default=0.5, help="Slippage %%")

    buy_p = sub.add_parser("buy", help="Buy tokens with ADA")
    buy_p.add_argument("token_out", help="Output token ID")
    buy_p.add_argument("--ada", type=float, required=True, help="ADA to spend")
    buy_p.add_argument("--slippage", type=float, default=0.5, help="Slippage %%")
    buy_p.add_argument("--dry-run", action="store_true")

    sell_p = sub.add_parser("sell", help="Sell tokens for ADA")
    sell_p.add_argument("token_in", help="Input token ID")
    sell_p.add_argument("--amount", type=int, required=True, help="Token amount (smallest units)")
    sell_p.add_argument("--slippage", type=float, default=0.5, help="Slippage %%")
    sell_p.add_argument("--dry-run", action="store_true")

    swap_p = sub.add_parser("swap", help="Swap any token pair")
    swap_p.add_argument("token_in", help="Input token ID")
    swap_p.add_argument("token_out", help="Output token ID")
    swap_p.add_argument("--amount", type=int, required=True, help="Amount in smallest units")
    swap_p.add_argument("--slippage", type=float, default=0.5, help="Slippage %%")
    swap_p.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.command:
        parser.print_help()
        return

    if args.command == "estimate":
        est = estimate(
            token_in=args.token_in,
            token_out=args.token_out,
            amount=args.amount,
            slippage=args.slippage,
        )
        print(f"{'='*55}")
        print(f"  Minswap Aggregator Estimate")
        print(f"{'='*55}")
        print(f"  In:  {est.amount_in:>15,} {est.token_in[:20]}")
        print(f"  Out: {est.amount_out:>15,} {est.token_out[:20]}")
        print(f"  Min: {est.min_amount_out:>15,}")
        print(f"  LP fee:     {est.total_lp_fee:>10,} lovelace")
        print(f"  DEX fee:    {est.total_dex_fee:>10,} lovelace")
        print(f"  Aggr fee:   {est.aggregator_fee:>10,} lovelace")
        print(f"  Deposits:   {est.deposits:>10,} lovelace (returned)")
        print(f"  Impact:     {est.avg_price_impact:>9.2f}%")
        for i, path in enumerate(est.paths):
            hops = " → ".join(p.get("protocol", "?") for p in path)
            print(f"  Path {i}: {hops}")

    elif args.command in ("buy", "sell", "swap"):
        address = os.environ["CARDANO_PAYMENT_ADDRESS"]
        skey = PaymentSigningKey.load(os.environ["CARDANO_PRIVATE_KEY_PATH"])

        if args.command == "buy":
            token_in = "lovelace"
            token_out = args.token_out
            amount = int(args.ada * 1_000_000)
            dry_run = args.dry_run
        elif args.command == "sell":
            token_in = args.token_in
            token_out = "lovelace"
            amount = args.amount
            dry_run = args.dry_run
        else:
            token_in = args.token_in
            token_out = args.token_out
            amount = args.amount
            dry_run = args.dry_run

        mode = "DRY RUN" if dry_run else "LIVE"
        print(f"{'='*55}")
        print(f"  Minswap Aggregator Swap — {mode}")
        print(f"{'='*55}")
        print(f"  Address: {address[:20]}...{address[-10:]}")
        print(f"  Slippage: {args.slippage}%")
        print()

        result = execute_swap(
            token_in=token_in,
            token_out=token_out,
            amount=amount,
            sender_address=address,
            signing_key=skey,
            slippage=args.slippage,
            dry_run=dry_run,
        )

        in_label = "ADA" if result["token_in"] == "lovelace" else result["token_in"][:16] + "..."
        out_label = "ADA" if result["token_out"] == "lovelace" else result["token_out"][:16] + "..."

        print(f"  {result['amount_in']:,} {in_label} → {result['amount_out']:,} {out_label}")
        print(f"  Minimum receive: {result['min_amount_out']:,}")
        print(f"  Route: {result['route']}")
        print(f"  Price impact: {result['price_impact_pct']:.2f}%")
        print(f"  LP fee: {result['total_lp_fee']:,} | DEX fee: {result['total_dex_fee']:,} | Aggr fee: {result['aggregator_fee']:,}")
        print(f"  Deposits: {result['deposits']:,} lovelace (returned)")

        if result["tx_hash"]:
            print(f"\n  Transaction submitted!")
            print(f"  Tx hash: {result['tx_hash']}")
            print(f"  https://cardanoscan.io/transaction/{result['tx_hash']}")
        elif dry_run:
            print(f"\n  DRY RUN — not submitted.")


if __name__ == "__main__":
    _cli()
