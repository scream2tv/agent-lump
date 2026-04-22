"""
Launch a new bonding-curve token on Snek.fun.

Thin CLI wrapper around ``snekfun_client.launch_token`` covering the
`POST /launch` builder endpoint from the Snek.fun API.

Docs:
  - Getting started : https://docs.snek.fun/getting-started/introduction
  - Trading API     : https://docs.snek.fun/api-reference/overview
  - Launch endpoint : https://docs.snek.fun/api-reference/trading/launch

Each launch persists its on-chain metadata (assetId, policyId, tx hash,
etc.) to a JSON file so follow-up scripts can reference it without
hard-coding.

Usage:
    python3 snekfun_launch.py \
        --name "My Token" --ticker MYTK --description "..." \
        --image logo.png --initial-buy 25
    python3 snekfun_launch.py --config launch.json
    python3 snekfun_launch.py --config launch.json --dry-run
"""

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from pycardano import PaymentSigningKey

from blockfrost_client import BlockfrostClient
from snekfun_client import (
    launch_token,
    sign_transaction,
    submit_via_builder,
)


ASSET_TYPES = ("Meme", "AI")
LAUNCH_TYPES = ("DEFAULT", "HYPED")


def _merge(args: argparse.Namespace, cfg: dict) -> dict:
    """Merge CLI args on top of a config-file dict (CLI wins)."""
    out = dict(cfg)
    for k in (
        "name", "ticker", "description", "image",
        "asset_type", "launch_type", "initial_buy",
        "twitter", "discord", "telegram", "website",
    ):
        v = getattr(args, k, None)
        if v is not None:
            out[k] = v
    return out


def _require(cfg: dict, key: str) -> Any:
    if cfg.get(key) in (None, ""):
        print(f"ERROR: missing required field: {key}", file=sys.stderr)
        sys.exit(2)
    return cfg[key]


def main() -> None:
    p = argparse.ArgumentParser(
        description="Launch a token on Snek.fun (https://docs.snek.fun).",
    )
    p.add_argument("--config", type=Path,
                   help="Path to a JSON file with launch parameters. CLI flags override file values.")
    p.add_argument("--name", help="Token name, ≤16 chars.")
    p.add_argument("--ticker", help="Token ticker, ≤6 alphanumeric chars.")
    p.add_argument("--description", help="Token description, ≤500 chars.")
    p.add_argument("--image", help="Path to the token logo (PNG/JPG).")
    p.add_argument("--asset-type", choices=ASSET_TYPES, default=None,
                   help="Snek.fun asset category (default from config or 'Meme').")
    p.add_argument("--launch-type", choices=LAUNCH_TYPES, default=None,
                   help="Snek.fun launch type (default from config or 'DEFAULT').")
    p.add_argument("--initial-buy", type=float, default=None,
                   help="Creator's initial buy in ADA (default 0).")
    p.add_argument("--twitter", default=None)
    p.add_argument("--discord", default=None)
    p.add_argument("--telegram", default=None)
    p.add_argument("--website", default=None)
    p.add_argument("--out", type=Path, default=Path("launch_result.json"),
                   help="Where to write on-chain launch metadata (default: launch_result.json).")
    p.add_argument("--dry-run", action="store_true",
                   help="Call /launch but do not sign or submit.")
    args = p.parse_args()

    cfg: dict = {}
    if args.config:
        cfg = json.loads(args.config.read_text())
    cfg = _merge(args, cfg)

    name = _require(cfg, "name")
    ticker = _require(cfg, "ticker")
    description = _require(cfg, "description")
    image = _require(cfg, "image")
    asset_type = cfg.get("asset_type", "Meme")
    launch_type = cfg.get("launch_type", "DEFAULT")
    initial_buy_ada = float(cfg.get("initial_buy", 0) or 0)
    initial_deposit_lovelace = int(initial_buy_ada * 1_000_000)

    address = os.environ["CARDANO_PAYMENT_ADDRESS"]
    key_path = os.environ["CARDANO_PRIVATE_KEY_PATH"]

    print("=" * 56)
    print("  snek.fun launch")
    print("=" * 56)
    print(f"  Creator:      {address[:20]}...{address[-10:]}")
    print(f"  Name:         {name}")
    print(f"  Ticker:       {ticker}")
    print(f"  Asset type:   {asset_type}")
    print(f"  Launch type:  {launch_type}")
    print(f"  Initial buy:  {initial_buy_ada} ADA")
    print(f"  Logo:         {image}")
    print()

    bf = BlockfrostClient()

    result = launch_token(
        image_path=image,
        name=name,
        ticker=ticker,
        description=description,
        asset_type=asset_type,
        launch_type=launch_type,
        initial_deposit_lovelace=initial_deposit_lovelace,
        change_address=address,
        twitter=cfg.get("twitter"),
        discord=cfg.get("discord"),
        telegram=cfg.get("telegram"),
        website=cfg.get("website"),
        blockfrost=bf,
    )

    print("Builder /launch response:")
    print(json.dumps({k: v for k, v in result.items() if k != "cbor"}, indent=2))
    cbor = result.get("cbor", "")
    print(f"  cbor bytes: {len(cbor) // 2}\n")
    if not cbor:
        raise RuntimeError("No cbor in /launch response")

    if args.dry_run:
        print("[dry-run] skipping sign + submit")
        return

    skey = PaymentSigningKey.load(key_path)
    signed = sign_transaction(cbor, skey)

    partial = bool(result.get("partial", True))
    try:
        if partial:
            tx_hash = bf.submit_tx(signed)
        else:
            try:
                tx_hash = submit_via_builder(signed)
            except Exception:
                tx_hash = bf.submit_tx(signed)
    except Exception as e:
        print(f"Submission error: {e}")
        raise

    print(f"Tx submitted: {tx_hash}")
    print(f"https://cardanoscan.io/transaction/{tx_hash}")

    meta = {
        "name": name,
        "ticker": ticker,
        "assetId": result.get("assetId"),
        "policyId": result.get("policyId"),
        "assetSubject": result.get("assetSubject"),
        "logoCID": result.get("logoCID"),
        "userOutputTokens": result.get("userOutputTokens"),
        "txHash": tx_hash,
    }
    args.out.write_text(json.dumps(meta, indent=2))
    print(f"\nLaunch metadata written to {args.out}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
