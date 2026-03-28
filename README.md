# Agent Lump

AI-native trading infrastructure for Cardano. Estimate, build, sign, and submit swaps autonomously across every major DEX.

## What It Does

Agent Lump is a Python trading agent that connects to five Cardano swap protocols through a unified interface. Designed to be driven by LLMs, MCP tools, or standalone scripts — it handles routing, transaction construction, local key signing, and on-chain submission without human intervention.

| Client | Protocol | DEXes | API Key |
|---|---|---|---|
| `minswap_client.py` | Minswap Aggregator | 14 AMM DEXes | None |
| `dexhunter_client.py` | DexHunter Aggregator | 15+ DEXes | Optional |
| `cardexscan_client.py` | CardexScan/Hydra | Independent routing | Required |
| `saturnswap_client.py` | SaturnSwap | Order-book DEX | None |
| `snekfun_client.py` | Snek.fun | Bonding curve pools | None |

Shared infrastructure:

| Module | Purpose |
|---|---|
| `blockfrost_client.py` | Cardano chain queries and tx submission via Blockfrost |
| `setup_wallet.py` | Generate payment keys and derive addresses |
| `swap_ada_to_token.py` | CLI for DexHunter swaps with confirmation gate |

Each client handles the full pipeline: **estimate → build → sign → submit**.

Private keys never leave your machine. Transactions are signed locally with pycardano, then submitted to the network.

## Setup

### 1. Install dependencies

```bash
git clone https://github.com/scream2tv/agent-lump.git
cd agent-lump
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Create a wallet

Generate a new Cardano payment key pair and derive your address:

```bash
python3 setup_wallet.py --update-env
```

This will:
- Generate a signing key (`agent_payment.skey`) and verification key (`agent_payment.vkey`)
- Derive your bech32 payment address
- Save everything to `~/.agent-lump/`
- Write the address and key path into your `.env` file
- Set file permissions to `600` (owner-only read/write) on the signing key

To use a custom directory or testnet:

```bash
python3 setup_wallet.py --output-dir /path/to/keys --network testnet --update-env
```

**Back up your signing key.** If you lose it, you lose access to this wallet. The `.skey` file is already in `.gitignore` — never commit it.

### 3. Get a Blockfrost project ID

Sign up at [blockfrost.io](https://blockfrost.io) (free tier works) and create a **mainnet** project. Add the project ID to your `.env`:

```bash
cp .env.example .env   # if you didn't use --update-env above
```

Then edit `.env`:

```
BLOCKFROST_PROJECT_ID=mainnetYourProjectIdHere
```

### 4. Fund your wallet

Send ADA to the address printed by `setup_wallet.py` (also saved in `~/.agent-lump/agent_payment.addr`). You can send from any Cardano wallet — Eternl, Lace, Nami, etc.

### 5. Swap

```bash
# DexHunter aggregator swap
python3 swap_ada_to_token.py --token NIGHT --amount 5
python3 swap_ada_to_token.py --token MIN --amount 10 --slippage 2

# Snek.fun bonding curve buy
python3 snekfun_client.py buy --asset-id 73797786382c0832b5787a5b306f5308488f14571b7061f79396ad2c.4c756d70 --ada 5
python3 snekfun_client.py buy --asset-id <policyId.assetName> --ada 10 --slippage 30 --dry-run

# Snek.fun bonding curve sell
python3 snekfun_client.py sell --asset-id <policyId.assetName> --tokens 500000 --dry-run
python3 snekfun_client.py sell --asset-id <policyId.assetName> --tokens 1000000 --slippage 30

# Snek.fun pool state and price estimates
python3 snekfun_client.py pool-state --policy-id <hex> --asset-name <hex> --pool-nft <hex>
python3 snekfun_client.py estimate --policy-id <hex> --asset-name <hex> --pool-nft <hex> --ada 25 50 100
python3 snekfun_client.py estimate --policy-id <hex> --asset-name <hex> --pool-nft <hex> --tokens 500000 1000000
```

## Clients

### Minswap (`minswap_client.py`)

Routes across 14 AMM DEXes. No API key needed. Amounts in base units (lovelace).

```python
from minswap_client import estimate_swap, execute_swap

est = estimate_swap(token_in="lovelace", token_out=TOKEN_ID, amount_lovelace=5_000_000)
print(f"Output: {est.amount_out}, Impact: {est.avg_price_impact}%")
```

### DexHunter (`dexhunter_client.py`)

Independent aggregator with 15+ DEXes. Amounts in display units (5 = 5 ADA).

```python
from dexhunter_client import estimate_swap, execute_swap

est = estimate_swap(token_in="", token_out=TOKEN_ID, amount_in=5.0)
print(f"Output: {est.total_output}, Fee: {est.total_fee}")
```

### CardexScan (`cardexscan_client.py`)

Hydra aggregator with independent routing. Requires `CARDEXSCAN_API_KEY`. Amounts in base units.

```python
from cardexscan_client import estimate_swap

est = estimate_swap(token_in="lovelace", token_out_id=TOKEN_ID, amount_in_lovelace=5_000_000)
print(f"Output: {est.output_amount}")
```

### SaturnSwap (`saturnswap_client.py`)

Order-book DEX (ADA-paired pools only). Amounts in display units.

```python
from saturnswap_client import get_pool_by_tokens, estimate_buy

pool = get_pool_by_tokens(policy_id="0691b2fe...", asset_name="4e49474854")
est = estimate_buy(pool.pool_id, ada_amount_display=10.0)
print(f"Output: {est.estimated_out_display} {est.token_out_ticker}")
```

### Snek.fun (`snekfun_client.py`)

Direct bonding curve trading on snek.fun. No API key needed. Supports both bonding curve and CPMM (graduated) pools.

```python
from snekfun_client import get_pool_state, estimate_buy, estimate_sell, execute_buy, execute_sell
from blockfrost_client import BlockfrostClient

bf = BlockfrostClient()
pool = get_pool_state(bf, policy_id, asset_name, pool_nft_name)

# Buy estimate
est = estimate_buy(pool, ada_lovelace=10_000_000)
print(f"Buy output: ~{est.token_output:,} tokens")

# Sell estimate
sell_est = estimate_sell(pool, token_amount=500_000)
print(f"Sell output: ~{sell_est.ada_output / 1e6:.4f} ADA")

# Full buy pipeline
result = execute_buy(
    asset_id="<policyId>.<assetName>",
    ada_amount=10.0,
    sender_address=address,
    signing_key=skey,
)
print(f"Tx: {result['tx_hash']}")

# Full sell pipeline
result = execute_sell(
    asset_id="<policyId>.<assetName>",
    token_amount=500_000,
    sender_address=address,
    signing_key=skey,
)
print(f"Tx: {result['tx_hash']}")
```

### Blockfrost (`blockfrost_client.py`)

Shared Cardano chain client. Used internally by `snekfun_client` and available for any module that needs UTXOs, protocol parameters, or raw tx submission.

```python
from blockfrost_client import BlockfrostClient

bf = BlockfrostClient()  # reads BLOCKFROST_PROJECT_ID from env
utxos = bf.get_utxos("addr1q...")
params = bf.get_protocol_parameters()
tx_hash = bf.submit_tx(signed_cbor_hex)
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `BLOCKFROST_PROJECT_ID` | Yes | Blockfrost mainnet project ID |
| `CARDANO_PAYMENT_ADDRESS` | Yes | Your bech32 wallet address |
| `CARDANO_PRIVATE_KEY_PATH` | Yes | Path to your `.skey` file |
| `DEXHUNTER_PARTNER_ID` | No | DexHunter partner ID for better routing |
| `DEXHUNTER_API_KEY` | No | DexHunter API key |
| `CARDEXSCAN_API_KEY` | No | Required for CardexScan aggregator |
| `SNEKFUN_BUILDER_URL` | No | Override snek.fun builder (default: https://builder.snek.fun) |
| `SNEKFUN_ANALYTICS_URL` | No | Override snek.fun analytics (default: https://analytics.snek.fun) |

## How Signing Works

All five clients use the same local signing approach:

1. The API returns an unsigned CBOR transaction
2. pycardano hashes the transaction body and signs with your key
3. The signed witness is assembled with the original transaction
4. The final transaction is submitted to the Cardano network

Your private key is loaded from disk, used in memory, and never sent over the network.

## Security

- **Signing keys** are stored outside the repo (default: `~/.agent-lump/`) with `600` permissions
- **`.skey` files** are in `.gitignore` — they will never be committed
- **`.env`** is in `.gitignore` — your API keys and addresses stay local
- **No key material** is ever sent to any API. Aggregators receive only your public address and unsigned transactions

## Requirements

- Python 3.10+
- A [Blockfrost](https://blockfrost.io) mainnet project ID (free tier works)

## License

MIT
