#!/usr/bin/env python3
"""
Snek.fun Swap Script - Buy/Sell tokens on bonding curve or CPMM pools

Usage:
  python snekfun_swap.py buy <asset_id> <ada_amount> [--slippage=15] [--dry-run]
  python snekfun_swap.py sell <asset_id> <token_amount> [--slippage=15] [--dry-run]
  
Examples:
  # Buy 10 ADA worth of LUMP
  python snekfun_swap.py buy "73797786382c0832b5787a5b306f5308488f14571b7061f79396ad2c.4c756d70" 10.0
  
  # Sell 1000000 LUMP tokens
  python snekfun_swap.py sell "73797786382c0832b5787a5b306f5308488f14571b7061f79396ad2c.4c756d70" 1000000
"""

import os
import sys
import argparse
from dotenv import load_dotenv
load_dotenv()

from pycardano import PaymentSigningKey
from blockfrost_client import BlockfrostClient
from snekfun_client import (
    execute_buy, execute_sell, buy_cpmm_via_builder, sell_cpmm_via_builder,
    get_token_state, sign_transaction
)

def main():
    parser = argparse.ArgumentParser(description='Snek.fun swap utility')
    parser.add_argument('action', choices=['buy', 'sell'], help='Buy or sell tokens')
    parser.add_argument('asset_id', help='Token asset ID (policyId.assetName)')
    parser.add_argument('amount', type=float, help='Amount (ADA for buy, tokens for sell)')
    parser.add_argument('--slippage', default='15', choices=['15', '30', '50', '75', 'infinity'],
                       help='Slippage tolerance (default: 15)')
    parser.add_argument('--dry-run', action='store_true', help='Build transaction but don\'t submit')
    parser.add_argument('--cpmm', action='store_true', help='Use CPMM endpoint for graduated tokens')
    
    args = parser.parse_args()
    
    # Environment setup
    try:
        address = os.environ["CARDANO_PAYMENT_ADDRESS"]
        key_path = os.environ.get("CARDANO_PRIVATE_KEY_PATH", "/Users/scream2/.cardano-agent/agent_payment.skey")
        project_id = os.environ["BLOCKFROST_PROJECT_ID"]
    except KeyError as e:
        print(f"Missing environment variable: {e}")
        print("Required: CARDANO_PAYMENT_ADDRESS, BLOCKFROST_PROJECT_ID")
        print("Optional: CARDANO_PRIVATE_KEY_PATH (defaults to /Users/scream2/.cardano-agent/agent_payment.skey)")
        sys.exit(1)
    
    print(f"{'='*60}")
    print(f"Snek.fun {args.action.upper()} Order")
    print(f"{'='*60}")
    print(f"Asset ID: {args.asset_id}")
    print(f"Amount: {args.amount} {'ADA' if args.action == 'buy' else 'tokens'}")
    print(f"Address: {address}")
    print(f"Slippage: {args.slippage}%")
    print(f"Mode: {'CPMM (graduated)' if args.cpmm else 'Bonding curve'}")
    if args.dry_run:
        print("DRY RUN: Will not submit transaction")
    print(f"{'='*60}")
    
    # Check token state
    print("\nFetching token state...")
    token_state = get_token_state(args.asset_id)
    if token_state:
        print(f"Token found on snek.fun")
        if 'poolState' in token_state and token_state['poolState']:
            pool = token_state['poolState']
            print(f"Reserve ADA: {int(pool.get('reserveA', 0)) / 1e6:.2f}")
            print(f"Reserve Token: {int(pool.get('reserveB', 0)):,}")
            if 'bondingProgress' in pool:
                print(f"Bonding progress: {pool['bondingProgress']:.1f}%")
        else:
            print("Token might be graduated (completed bonding curve)")
            if not args.cpmm:
                print("Consider using --cpmm flag for graduated tokens")
    else:
        print("Warning: Could not fetch token state from snek.fun API")
    
    # Load signing key
    print(f"\nLoading signing key from {key_path}...")
    try:
        signing_key = PaymentSigningKey.load(key_path)
        print("Signing key loaded successfully")
    except Exception as e:
        print(f"Error loading signing key: {e}")
        sys.exit(1)
    
    # Create blockfrost client
    bf = BlockfrostClient()
    
    try:
        if args.action == 'buy':
            if args.cpmm:
                print(f"\nBuilding CPMM buy order via snek.fun builder...")
                trade = buy_cpmm_via_builder(
                    asset_id=args.asset_id,
                    ada_amount=args.amount,
                    sender_address=address,
                    slippage=args.slippage,
                    blockfrost=bf
                )
                if not trade.cbor:
                    raise RuntimeError("Builder API returned empty CBOR")
                
                result = {
                    "trade_id": trade.trade_id,
                    "input_amount": trade.input_amount, 
                    "output_amount": trade.output_amount,
                    "cbor_size": len(trade.cbor) // 2,
                    "tx_hash": None
                }
                
                if not args.dry_run:
                    print("Signing and submitting transaction...")
                    signed_cbor = sign_transaction(trade.cbor, signing_key)
                    tx_hash = bf.submit_tx(signed_cbor)
                    result["tx_hash"] = tx_hash
            else:
                print(f"\nExecuting bonding curve buy order...")
                result = execute_buy(
                    asset_id=args.asset_id,
                    ada_amount=args.amount,
                    sender_address=address,
                    signing_key=signing_key,
                    slippage=args.slippage,
                    blockfrost=bf,
                    dry_run=args.dry_run
                )
        else:  # sell
            token_amount = int(args.amount)  # Sell amount should be integer tokens
            if args.cpmm:
                print(f"\nBuilding CPMM sell order via snek.fun builder...")
                trade = sell_cpmm_via_builder(
                    asset_id=args.asset_id,
                    token_amount=token_amount,
                    sender_address=address,
                    slippage=args.slippage,
                    blockfrost=bf
                )
                if not trade.cbor:
                    raise RuntimeError("Builder API returned empty CBOR")
                
                result = {
                    "trade_id": trade.trade_id,
                    "input_amount": trade.input_amount,
                    "output_amount": trade.output_amount, 
                    "cbor_size": len(trade.cbor) // 2,
                    "tx_hash": None
                }
                
                if not args.dry_run:
                    print("Signing and submitting transaction...")
                    signed_cbor = sign_transaction(trade.cbor, signing_key)
                    tx_hash = bf.submit_tx(signed_cbor)
                    result["tx_hash"] = tx_hash
            else:
                print(f"\nExecuting bonding curve sell order...")
                result = execute_sell(
                    asset_id=args.asset_id,
                    token_amount=token_amount,
                    sender_address=address,
                    signing_key=signing_key,
                    slippage=args.slippage,
                    blockfrost=bf,
                    dry_run=args.dry_run
                )
        
        # Display results
        print(f"\n{'='*60}")
        print(f"Transaction {'Built' if args.dry_run else 'Completed'}")
        print(f"{'='*60}")
        if result.get("trade_id"):
            print(f"Trade ID: {result['trade_id']}")
        print(f"Input: {result.get('input_amount', 'N/A')}")
        print(f"Output: {result.get('output_amount', 'N/A')}")
        print(f"CBOR size: {result.get('cbor_size', 'N/A')} bytes")
        
        if result.get('tx_hash') and not args.dry_run:
            print(f"TX Hash: {result['tx_hash']}")
            print(f"CardanoScan: https://cardanoscan.io/transaction/{result['tx_hash']}")
        elif args.dry_run:
            print("Transaction built successfully (dry run)")
        
        print(f"{'='*60}")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()