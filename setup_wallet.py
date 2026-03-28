"""
Generate a Cardano wallet for Agent Lump.

Creates a payment signing key (.skey), derives the bech32 address,
and writes both to disk. Optionally updates .env with the new values.

Usage:
    python3 setup_wallet.py
    python3 setup_wallet.py --output-dir ~/.cardano-agent
    python3 setup_wallet.py --output-dir ~/.cardano-agent --update-env
    python3 setup_wallet.py --network testnet
"""

import argparse
import os
import sys
from pathlib import Path

from pycardano import (
    Address,
    Network,
    PaymentSigningKey,
    PaymentVerificationKey,
)


def generate_wallet(output_dir: Path, network: Network) -> tuple[str, Path]:
    """Generate a new payment key pair and derive the address.

    Returns:
        (bech32_address, skey_path)
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    skey = PaymentSigningKey.generate()
    vkey = PaymentVerificationKey.from_signing_key(skey)
    address = Address(vkey.hash(), network=network)

    skey_path = output_dir / "agent_payment.skey"

    if skey_path.exists():
        print(f"ERROR: {skey_path} already exists. Remove it first or choose a different --output-dir.")
        sys.exit(1)

    skey.save(str(skey_path))
    os.chmod(skey_path, 0o600)

    vkey_path = output_dir / "agent_payment.vkey"
    vkey.save(str(vkey_path))

    addr_path = output_dir / "agent_payment.addr"
    addr_path.write_text(str(address))

    return str(address), skey_path


def update_env_file(env_path: Path, address: str, skey_path: Path):
    """Write or update CARDANO_PAYMENT_ADDRESS and CARDANO_PRIVATE_KEY_PATH in .env."""
    lines = []
    if env_path.exists():
        lines = env_path.read_text().splitlines()

    keys_written = {"CARDANO_PAYMENT_ADDRESS": False, "CARDANO_PRIVATE_KEY_PATH": False}
    new_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("CARDANO_PAYMENT_ADDRESS="):
            new_lines.append(f"CARDANO_PAYMENT_ADDRESS={address}")
            keys_written["CARDANO_PAYMENT_ADDRESS"] = True
        elif stripped.startswith("CARDANO_PRIVATE_KEY_PATH="):
            new_lines.append(f"CARDANO_PRIVATE_KEY_PATH={skey_path}")
            keys_written["CARDANO_PRIVATE_KEY_PATH"] = True
        else:
            new_lines.append(line)

    for key, written in keys_written.items():
        if not written:
            if key == "CARDANO_PAYMENT_ADDRESS":
                new_lines.append(f"CARDANO_PAYMENT_ADDRESS={address}")
            elif key == "CARDANO_PRIVATE_KEY_PATH":
                new_lines.append(f"CARDANO_PRIVATE_KEY_PATH={skey_path}")

    env_path.write_text("\n".join(new_lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Generate a Cardano wallet for Agent Lump")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.home() / ".agent-lump",
        help="Directory to store key files (default: ~/.agent-lump)",
    )
    parser.add_argument(
        "--network",
        choices=["mainnet", "testnet"],
        default="mainnet",
        help="Cardano network (default: mainnet)",
    )
    parser.add_argument(
        "--update-env",
        action="store_true",
        help="Write the address and key path into .env",
    )
    args = parser.parse_args()

    network = Network.TESTNET if args.network == "testnet" else Network.MAINNET

    print(f"Generating wallet ({args.network})...")
    address, skey_path = generate_wallet(args.output_dir, network)

    print()
    print(f"  Address:  {address}")
    print(f"  Key dir:  {args.output_dir}")
    print(f"  Files:")
    print(f"    {skey_path}          (signing key — KEEP SECRET)")
    print(f"    {args.output_dir / 'agent_payment.vkey'}          (verification key)")
    print(f"    {args.output_dir / 'agent_payment.addr'}          (address file)")
    print()

    if args.update_env:
        env_path = Path(__file__).parent / ".env"
        update_env_file(env_path, address, skey_path)
        print(f"  Updated {env_path}")
        print()

    print("Next steps:")
    print(f"  1. Fund this address with ADA:")
    print(f"     {address}")
    print(f"  2. Get a free Blockfrost project ID at https://blockfrost.io")
    if not args.update_env:
        print(f"  3. Add to your .env:")
        print(f"     CARDANO_PAYMENT_ADDRESS={address}")
        print(f"     CARDANO_PRIVATE_KEY_PATH={skey_path}")
        print(f"     BLOCKFROST_PROJECT_ID=<your project id>")
    else:
        print(f"  3. Add your Blockfrost project ID to .env:")
        print(f"     BLOCKFROST_PROJECT_ID=<your project id>")
    print()
    print("IMPORTANT: Back up your signing key. If you lose it, you lose access to this wallet.")
    print(f"           Never commit {skey_path.name} to git (already in .gitignore).")


if __name__ == "__main__":
    main()
