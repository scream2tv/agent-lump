"""
OpenClaw DexHunter CLI bridge
Usage: cli.py <command> [args...]

Commands:
  search_tokens <query> [verified_only]
  get_token <token_id>
  estimate_swap <token_in> <token_out> <amount_in> [slippage_percent]
  register_wallet <address>
  build_swap <buyer_address> <token_in> <token_out> <amount_in> [slippage_percent]
  average_price <base> <quote>
  sign_and_submit <unsigned_cbor_hex>
"""

import json
import os
import sys

# Add parent dir to path so we can import from agent-lump root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import dexhunter_client as dh


def cmd_search_tokens(args):
    query = args[0]
    verified = args[1].lower() == "true" if len(args) > 1 else False
    results = dh.search_tokens(query, verified=verified if verified else None)
    print(json.dumps(results, indent=2))


def cmd_get_token(args):
    token_id = args[0]
    result = dh.get_token(token_id)
    print(json.dumps(result, indent=2))


def cmd_estimate_swap(args):
    token_in = args[0] if args[0] != '""' and args[0] != "''" else ""
    token_out = args[1]
    amount_in = float(args[2])
    slippage = float(args[3]) if len(args) > 3 else 1.0

    est = dh.estimate_swap(token_in, token_out, amount_in, slippage)
    print(json.dumps({
        "total_output": est.total_output,
        "net_price": est.net_price,
        "average_price": est.average_price,
        "total_fee": est.total_fee,
        "price_impact": est.price_impact,
        "splits": est.splits,
    }, indent=2))


def cmd_register_wallet(args):
    address = args[0]
    result = dh.register_wallet(address)
    print(json.dumps(result, indent=2))


def cmd_build_swap(args):
    buyer_address = args[0]
    token_in = args[1] if args[1] != '""' and args[1] != "''" else ""
    token_out = args[2]
    amount_in = float(args[3])
    slippage = float(args[4]) if len(args) > 4 else 1.0

    build = dh.build_swap(buyer_address, token_in, token_out, amount_in, slippage)
    print(json.dumps({
        "cbor": build.cbor,
        "expected_output": build.expected_output,
        "dexes": build.dexes,
    }, indent=2))


def cmd_average_price(args):
    base = args[0]
    quote = args[1]
    result = dh.get_average_price(base, quote)
    print(json.dumps(result, indent=2))


def cmd_sign_and_submit(args):
    unsigned_cbor_hex = args[0]

    key_path = os.environ.get("CARDANO_PRIVATE_KEY_PATH") or os.environ.get("PAYMENT_SKEY_PATH")
    if not key_path:
        print(json.dumps({"error": "CARDANO_PRIVATE_KEY_PATH not set in .env"}))
        sys.exit(1)

    from pycardano import BlockFrostChainContext, Network, PaymentSigningKey

    signing_key = PaymentSigningKey.load(key_path)
    signed_cbor = dh.sign_transaction(unsigned_cbor_hex, signing_key)
    witnessed = dh.add_witness(unsigned_cbor_hex, signed_cbor)
    final_cbor = witnessed.get("cbor", "")

    if not final_cbor:
        print(json.dumps({"error": "Witness assembly returned no CBOR", "response": witnessed}))
        sys.exit(1)

    project_id = os.environ["BLOCKFROST_PROJECT_ID"]
    context = BlockFrostChainContext(project_id, network=Network.MAINNET)
    tx_hash = dh.submit_transaction(final_cbor, context)

    print(json.dumps({
        "tx_hash": tx_hash,
        "cardanoscan": f"https://cardanoscan.io/transaction/{tx_hash}",
    }, indent=2))


COMMANDS = {
    "search_tokens": cmd_search_tokens,
    "get_token": cmd_get_token,
    "estimate_swap": cmd_estimate_swap,
    "register_wallet": cmd_register_wallet,
    "build_swap": cmd_build_swap,
    "average_price": cmd_average_price,
    "sign_and_submit": cmd_sign_and_submit,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(f"Usage: cli.py <command> [args...]\nCommands: {', '.join(COMMANDS)}", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    try:
        COMMANDS[cmd](args)
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
