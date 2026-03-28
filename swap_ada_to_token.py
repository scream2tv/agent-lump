"""
Swap ADA → Token via DexHunter v3

Step-by-step execution with confirmation gate.
Uses raw CBOR signing — keys never leave your machine.

Usage:
    python3 swap_ada_to_token.py --token NIGHT --amount 2
    python3 swap_ada_to_token.py --token-id 0691b2fe...4e49474854 --amount 5 --slippage 2
"""

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from pycardano import BlockFrostChainContext, Network, PaymentSigningKey

from dexhunter_client import (
    build_swap,
    estimate_swap,
    search_tokens,
    sign_transaction,
    add_witness,
    submit_transaction,
)


def resolve_token_id(ticker: str) -> str:
    """Search DexHunter for a token by ticker and return the verified match."""
    results = search_tokens(ticker, verified=True)
    if not results:
        results = search_tokens(ticker)
    if not results:
        print(f"No token found for '{ticker}'")
        sys.exit(1)

    if len(results) == 1:
        token = results[0]
    else:
        exact = [t for t in results if (t.get("ticker") or "").upper() == ticker.upper()]
        token = exact[0] if exact else results[0]

    token_id = token.get("token_id", "")
    print(f"Resolved: {token.get('ticker', '?')} → {token_id[:20]}...{token_id[-8:]}")
    return token_id


def main():
    parser = argparse.ArgumentParser(description="Swap ADA for a Cardano token via DexHunter")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--token", help="Token ticker to search (e.g. NIGHT, SNEK, MIN)")
    group.add_argument("--token-id", help="Full token ID (policyId + assetNameHex)")
    parser.add_argument("--amount", type=float, required=True, help="ADA amount to swap (display units)")
    parser.add_argument("--slippage", type=float, default=1.0, help="Slippage tolerance in percent (default: 1)")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    address = os.environ["CARDANO_PAYMENT_ADDRESS"]
    key_path = os.environ["CARDANO_PRIVATE_KEY_PATH"]
    project_id = os.environ["BLOCKFROST_PROJECT_ID"]

    if args.token:
        token_out = resolve_token_id(args.token)
    else:
        token_out = args.token_id

    # --- Step 1: Estimate ---
    print(f"\n{'='*50}")
    print(f"  Estimating {args.amount} ADA → token swap")
    print(f"{'='*50}")

    est = estimate_swap(
        token_in="",
        token_out=token_out,
        amount_in=args.amount,
        slippage=args.slippage,
    )

    print(f"\n  Expected output:  {est.total_output}")
    print(f"  Net price:        {est.net_price} ADA/token")
    print(f"  DexHunter fee:    {est.total_fee} ADA")

    if est.splits:
        print(f"\n  Routing ({len(est.splits)} split{'s' if len(est.splits) > 1 else ''}):")
        for s in est.splits:
            impact = s.get("price_impact", "N/A")
            print(f"    {s.get('dex', '?'):15s}  in={s.get('amount_in')}  out={s.get('expected_output')}  impact={impact}")

    max_impact = 0.0
    for s in est.splits:
        try:
            max_impact = max(max_impact, float(s.get("price_impact", 0)))
        except (ValueError, TypeError):
            pass

    if max_impact > 5.0:
        print(f"\n  WARNING: High price impact ({max_impact:.2f}%). Consider reducing amount.")
    elif max_impact > 2.0:
        print(f"\n  CAUTION: Price impact {max_impact:.2f}% is elevated.")

    # --- Step 2: Confirm ---
    if not args.yes:
        confirm = input(f"\n  Swap {args.amount} ADA? (yes/no): ").strip().lower()
        if confirm != "yes":
            print("  Cancelled.")
            sys.exit(0)

    # --- Step 3: Build ---
    print("\n  Building transaction...")
    build = build_swap(
        buyer_address=address,
        token_in="",
        token_out=token_out,
        amount_in=args.amount,
        slippage=args.slippage,
    )

    if not build.cbor:
        print("  ERROR: DexHunter returned empty CBOR.")
        sys.exit(1)

    print(f"  CBOR length: {len(build.cbor)} chars")
    if build.dexes:
        print(f"  DEXes: {', '.join(build.dexes)}")

    # --- Step 4: Sign locally ---
    print("  Signing locally...")
    signing_key = PaymentSigningKey.load(key_path)
    signed_cbor = sign_transaction(build.cbor, signing_key)

    # --- Step 5: Witness assembly via DexHunter ---
    print("  Assembling witnesses...")
    witnessed = add_witness(build.cbor, signed_cbor)
    final_cbor = witnessed.get("cbor", "")

    if not final_cbor:
        print("  ERROR: Witness assembly returned no CBOR.")
        print(f"  Response: {witnessed}")
        sys.exit(1)

    # --- Step 6: Submit ---
    print("  Submitting to Cardano mainnet...")
    context = BlockFrostChainContext(project_id, network=Network.MAINNET)
    tx_hash = submit_transaction(final_cbor, context)

    print(f"\n{'='*50}")
    print(f"  Transaction submitted!")
    print(f"  TX Hash: {tx_hash}")
    print(f"  https://cardanoscan.io/transaction/{tx_hash}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()
