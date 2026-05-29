# Agent Lump

On-chain automation across two networks. The **Midnight Network agent** (TypeScript)
is the primary toolkit; a **Cardano toolkit** (Python) follows as secondary.

| Toolkit | Stack | What it does |
|---|---|---|
| [**midnight-agent**](#midnight-agent) | TypeScript | Midnight mainnet — HD wallets, chain/indexer queries, shielded + unshielded transfers, DUST, Compact token deploys, DEX & keyless contract reads |
| [Cardano toolkit](#cardano-toolkit) | Python | Cardano mainnet — DEX swaps, cross-DEX arbitrage, Snek.fun copy-trading & bonding-curve launches |

---

## midnight-agent

> Primary toolkit. Lives in [`midnight-agent/`](midnight-agent/) — see
> [`midnight-agent/README.md`](midnight-agent/README.md) for the full reference.

A TypeScript agent for **Midnight** — a privacy-preserving, Substrate-based sidechain
bridged to Cardano via the Ariadne protocol. Midnight's SDK is TypeScript-native (ZK
proof generation, wallet state, and contract calls all run through the official
`@midnight-ntwrk` packages), so this side of the repo is TS where the Cardano side is
Python.

**Modules**

| Module | Purpose |
|---|---|
| `src/config.ts`   | Network config — mainnet/preprod/preview endpoints (v4 indexer, no API key) |
| `src/chain.ts`    | Chain queries — Substrate JSON-RPC + GraphQL indexer; blocks, txs, contract state + **circuit decoding** |
| `src/wallet.ts`   | HD wallet creation, key derivation (unshielded/shielded/DUST), address encoding, balances |
| `src/transfer.ts` | Shielded / unshielded / combined transfers, DUST registration, atomic swaps, fee estimation |
| `src/token.ts`    | Compact FungibleToken deployment and interaction |
| `src/dex.ts`      | DEX pool state, swap estimation, execution scaffold |

### Setup

```bash
cd midnight-agent
npm install
cp .env.example .env      # defaults to mainnet + remote proof server (no Docker)
npm run build
node dist/cli.js chain info
```

### Read any mainnet contract — no API key, no source needed

Midnight has no Etherscan-style source verification, but the indexer exposes full
contract state over GraphQL. `chain contract` reads it and decodes the contract's
exported Compact circuits straight from the on-chain state blob:

```bash
npm run dev -- chain contract <CONTRACT_ADDRESS>
```

```
Contract: 8382dda611573d1ce9c8d969e50a7c5d001de9e31b1a8f5721ee4964bcce9921
Latest action: ContractDeploy
Fees paid: 1050320000001 Specks (0.001050320 DUST)
State size: 23140 bytes
Circuits / entry points (10):
  - registerAccount   - createOrder         - cancelOrder    - executeTrade
  - pruneOrder        - refreshEscrowMirror  - syncMarket     - setPausedTrading …
```

### Common commands

Full command reference (chain, wallet, transfer, DUST, token, DEX) is in
[`midnight-agent/README.md`](midnight-agent/README.md).

```bash
npm run dev -- wallet create            # HD wallet from a fresh seed
npm run dev -- wallet balances          # shielded + unshielded + DUST
npm run dev -- transfer unshielded --to mn_addr1... --amount 1000000
npm run dev -- dust register            # register NIGHT so DUST accrues
npm run dev -- token deploy --name "My Token" --ticker MTK --supply 1000000
npm run dev -- dex pool <POOL_ADDRESS>
```

### Mainnet

| | |
|---|---|
| Chain            | Midnight Mainnet (genesis Mar 30 2026) |
| Indexer (GraphQL)| `https://indexer.mainnet.midnight.network/api/v4/graphql` — no key required |
| RPC (Substrate)  | `https://rpc.mainnet.midnight.network/` |
| Fee token        | DUST — non-transferable, accrues from holding NIGHT |
| Contracts        | Compact (ZK circuits) |

Token deploys and transfers need a funded wallet with DUST and a proof server (remote
for mainnet, or local Docker for preprod). Compact contract authoring, the DUST
registration flow, and the TypeScript API are documented in the sub-README.

---

## Cardano toolkit

> Secondary toolkit (Python). Cardano mainnet trading automation: DEX swaps, arbitrage
> scanning, copy trading, and bonding-curve launches. Each tool below is written as a
> self-contained skill an agent can pick up, aim, and run.

**Skills**

| Skill | What it does |
|-------|--------------|
| [`copy_trader`](#skill-copy_trader)       | Mirror another wallet's Snek.fun buys / sells                 |
| [`swap_ada_to_token`](#skill-swap_ada_to_token) | One-shot ADA→token swap via DexHunter aggregation       |
| [`arb_scanner` / `arb_executor`](#skill-arb_scanner--arb_executor) | Cross-DEX arbitrage discovery & execution     |
| [`snekfun_client`](#skill-snekfun_client) | Snek.fun builder API: buy / sell / cancel / launch / vesting  |
| [`snekfun_launch`](#skill-snekfun_launch) | Launch a new bonding-curve token on Snek.fun                  |
| [`blockfrost_client`](#skill-blockfrost_client) | Shared Cardano chain access (imported by every module)  |

### Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env         # fill in BLOCKFROST_PROJECT_ID + wallet paths
python3 setup_wallet.py --update-env    # optional: generate a fresh wallet
```

Required env:

| Var                         | Purpose                                    |
|-----------------------------|--------------------------------------------|
| `BLOCKFROST_PROJECT_ID`     | Cardano mainnet access (https://blockfrost.io) |
| `CARDANO_PAYMENT_ADDRESS`   | Agent's bech32 payment address             |
| `CARDANO_PRIVATE_KEY_PATH`  | Path to agent's `.skey` signing key        |

### skill: copy_trader

**Use when** you want to mirror another Cardano wallet's Snek.fun buys and sells
with a configurable sizing strategy.

**What it does.** Polls Blockfrost for new txs on the target address, filters
to Snek.fun script involvement only (pool validator or order validator —
every other tx type is ignored), classifies the trade (`BUY` = target
received a token, `SELL` = target sent a token to a snek.fun script), and
copies it from the configured wallet via the Snek.fun builder API. Dedupes
by tx hash in `~/.agent-lump/copy_trader_state.json`; no backfill on cold
start.

**Sizing strategies (agent-selectable — required).** Pick one buy mode and
one sell mode per run. No strategy is assumed; the daemon refuses to start
until both are configured (via CLI flags or `COPY_TRADER_*` env vars).

| Mode          | Buy meaning                               | Sell meaning                 |
|---------------|-------------------------------------------|------------------------------|
| `fixed`       | buy exactly N ADA                         | —                            |
| `pct-target`  | buy N% of the ADA target committed        | —                            |
| `pct-wallet`  | buy N% of our wallet's free ADA balance   | —                            |
| `all`         | —                                         | sell 100% of our holding     |
| `pct-holding` | —                                         | sell N% of our holding       |

Every buy is clamped by `--min-buy-ada` / `--max-buy-ada` safety caps
(defaults: 1 / 100 ADA) regardless of mode.

**Run — explicit flags.**

```bash
# fixed ADA buy, sell everything
python3 copy_trader.py --target <addr> \
    --buy-mode fixed --buy-value 10 \
    --sell-mode all

# buy a percentage of what the target committed, sell partial
python3 copy_trader.py --target <addr> \
    --buy-mode pct-target --buy-value 20 \
    --sell-mode pct-holding --sell-value 50

# buy a percentage of our wallet balance
python3 copy_trader.py --target <addr> \
    --buy-mode pct-wallet --buy-value 5 \
    --sell-mode all

# detect only, no submit
python3 copy_trader.py --target <addr> \
    --buy-mode fixed --buy-value 10 --sell-mode all --dry-run
```

**Run — env-driven.** Set `COPY_TRADER_*` in `.env` (see `.env.example`)
and call without sizing flags:

```bash
python3 copy_trader.py --target <addr>
```

**Limitations.** Bonding-curve tokens only (CPMM / graduated tokens not yet
supported). Does not follow the target's in-app "splash wallet" — pass the
address that actually appears on-chain as the tx input/output.

### skill: swap_ada_to_token

**Use when** you want to swap ADA for any Cardano token via the best-priced
route across 15+ DEXes.

**What it does.** Uses DexHunter v3 (`search → estimate → build → local-sign
→ submit`) with a confirmation prompt, high-impact warnings, and a 2%
price-impact safety ceiling.

**Run.**

```bash
python3 swap_ada_to_token.py --token NIGHT --amount 2
python3 swap_ada_to_token.py --token-id <policyId+hex> --amount 5 --slippage 2
python3 swap_ada_to_token.py --token SNEK --amount 10 --yes    # skip prompt
```

### skill: arb_scanner / arb_executor

**Use when** you want to discover or execute cross-DEX arbitrage opportunities
on Cardano token pairs.

**What it does.** `arb_scanner.py` scans a token universe across multiple
DEXes and surfaces price discrepancies; `arb_executor.py` takes a detected
opportunity through to an executed cycle using DexHunter routing.

Configure via `ARB_*` env vars (trade amount, min profit, scan interval,
impact caps, dry-run). Defaults are dry-run.

**Run.**

```bash
python3 arb_scanner.py                  # passive scan, prints opportunities
python3 arb_executor.py                 # dry-run by default; set ARB_DRY_RUN=0 to fire
```

### skill: snekfun_client

**Use when** you want to buy, sell, or cancel orders on Snek.fun's
bonding-curve tokens directly (no copy-trading).

**What it does.** Covers the Snek.fun API surface with local CBOR signing
so keys never leave the machine. Also used as the execution layer of
`copy_trader` and `snekfun_launch`.

| API area         | Endpoint                                                | Function                                                               |
|------------------|---------------------------------------------------------|------------------------------------------------------------------------|
| Trade            | `POST {builder}/order`                                  | `buy_via_builder`, `sell_via_builder`, `buy_with_output_via_builder`   |
| Trade (CPMM)     | `POST {builder}/order` (auto-routed)                    | `buy_cpmm_via_builder`, `sell_cpmm_via_builder`                        |
| Cancel order     | `POST {builder}/cancel`                                 | `cancel_via_builder`                                                   |
| Sign & submit    | `POST {builder}/sign-and-submit`, `/sign`, `/submit`    | `sign_and_submit_via_builder`, `sign_via_builder`, `submit_via_builder`|
| Launch           | `POST {builder}/launch`                                 | `launch_token` (see `snekfun_launch.py`)                               |
| Transfer         | `POST {builder}/transfer`                               | `transfer_via_builder`                                                 |
| Parameters       | `GET {builder}/parameters`                              | `get_parameters`                                                       |
| Pool data        | `GET {analytics}/v1/pools-feed/...`                     | `get_pool_state`, `get_token_state`, `get_curve_progress`              |
| Balances         | `GET {balances}/v1/pool/holders`, `POST /v1/user/pnl-card`, `POST /v1/asset/asset-balance` | `get_pool_holders`, `get_pnl_card`, `get_asset_balance`   |
| Vesting (build)  | `POST {vesting}/create-lock`, `/withdraw`               | `create_vesting_lock`, `withdraw_vesting`                              |
| Vesting (query)  | `POST {vesting}/v1/vesting/get-by-redeemer`, `/get-by-asset/{asset}` | `get_vestings_by_redeemer`, `get_vestings_by_asset`         |
| UTXO Monitor     | `POST {utxo-monitor}/getUtxos`                          | `get_utxos_by_pkh`                                                     |
| Charts           | `GET {charts}/v1/charts/{history,initial-state,mcap/history,mcap/initial-state}` | `get_chart_history`, `get_chart_initial_state`, `get_mcap_history`, `get_mcap_initial_state` |

**Docs** — official Snek.fun API reference:

- Getting started: https://docs.snek.fun/getting-started/introduction
- API overview:    https://docs.snek.fun/api-reference/overview

**Host overrides** (for staging/testing):
`SNEKFUN_BUILDER_URL`, `SNEKFUN_ANALYTICS_URL`, `SNEKFUN_BALANCES_URL`,
`SNEKFUN_VESTING_URL`, `SNEKFUN_CHARTS_URL`, `SNEKFUN_UTXO_MONITOR_URL`.

**CLI.**

```bash
python3 snekfun_client.py --help
```

Convenience scripts:

- `snekfun_swap.py`    — direct buy / sell against the bonding curve
- `snekfun_launch.py`  — launch a new bonding-curve token (see next skill)

### skill: snekfun_launch

**Use when** you want to launch a new bonding-curve token on Snek.fun with
an optional creator buy.

**What it does.** Calls the Snek.fun builder `POST /launch` endpoint with
the provided metadata (name, ticker, description, logo, optional socials)
and optional initial-deposit ADA. Locally signs the returned CBOR, submits
to the network, and writes the launch metadata (assetId, policyId, tx hash,
logo CID, etc.) to a JSON file for downstream scripts.

**Docs** — launch endpoint spec:
https://docs.snek.fun/api-reference/overview

**Run.**

```bash
# Explicit flags
python3 snekfun_launch.py \
    --name "Example Token" --ticker EXMPL \
    --description "A demo token" \
    --image ./logo.png --initial-buy 25

# From a JSON config
python3 snekfun_launch.py --config launch.json

# Dry-run (calls /launch but does not sign or submit)
python3 snekfun_launch.py --config launch.json --dry-run
```

**Config file shape.**

```json
{
  "name": "Example Token",
  "ticker": "EXMPL",
  "description": "A demo token on the bonding curve.",
  "image": "./logo.png",
  "asset_type": "Meme",
  "launch_type": "DEFAULT",
  "initial_buy": 25,
  "twitter": "https://twitter.com/...",
  "website": "https://..."
}
```

**Constraints** (enforced client-side to match builder validation):
`name ≤ 16`, `ticker ≤ 6 alphanumeric`, `description ≤ 500`,
`asset_type ∈ {Meme, AI}`, `launch_type ∈ {DEFAULT, HYPED}`.

### skill: blockfrost_client

Shared Cardano chain access (UTxOs, protocol params, tx submit). Imported by
every other module; not a CLI.

---

## Repository layout

```
midnight-agent/           # Midnight Network agent (TypeScript) — primary toolkit
copy_trader.py            # snek.fun copy-trading daemon
snekfun_launch.py         # snek.fun token launcher
snekfun_swap.py           # snek.fun direct buy / sell
swap_ada_to_token.py      # one-shot DexHunter swap
arb_scanner.py            # cross-DEX arbitrage discovery
arb_executor.py           # arb execution
snekfun_client.py         # snek.fun builder API + CLI
dexhunter_client.py       # DexHunter v3 aggregator client
blockfrost_client.py      # shared Cardano chain client
minswap_*.py              # Minswap v1 / v2 / aggregator / data clients
sundaeswap_client.py      # SundaeSwap client
bodega_client.py          # Bodega prediction-market client
cardexscan_client.py      # CardexScan aggregator client
setup_wallet.py           # generate a fresh Cardano payment wallet
```

## License

See `LICENSE`.
