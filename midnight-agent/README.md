# Midnight Agent

TypeScript agent for the Midnight Network — wallet management, token operations, transfers, and DEX interaction.

## Architecture

Midnight is a privacy-preserving Substrate-based sidechain bridged to Cardano via the Ariadne protocol. Unlike Cardano where Python handles the full pipeline, Midnight's SDK ecosystem is TypeScript-native — ZK proof generation, wallet state management, and contract interaction all require the official `@midnight-ntwrk` packages.

| Aspect | Cardano (Python) | Midnight (TypeScript) |
|---|---|---|
| Smart contracts | Plutus (Haskell) | Compact (ZK circuits) |
| Native token | ADA (transferable) | DUST (non-transferable, accrues from NIGHT) |
| Privacy | Transparent | Shielded + unshielded modes |
| Chain API | Blockfrost REST | Substrate JSON-RPC + GraphQL Indexer |
| Tx signing | Local Ed25519 | Wallet-mediated + ZK proof generation |
| Wallet | pycardano | Midnight Wallet SDK (HD keys, 3-wallet architecture) |
| Bridge | N/A | Ariadne (cNIGHT on Cardano ↔ NIGHT on Midnight) |

## Modules

| Module | Purpose |
|---|---|
| `src/config.ts` | Network configuration (mainnet/preprod/preview endpoints) |
| `src/chain.ts` | Chain queries — JSON-RPC (138 Substrate methods) + GraphQL Indexer |
| `src/wallet.ts` | HD wallet creation, key derivation, address encoding, balance queries |
| `src/transfer.ts` | Shielded, unshielded, and combined token transfers; DUST management; atomic swaps |
| `src/token.ts` | FungibleToken contract deployment and interaction |
| `src/dex.ts` | DEX pool discovery, swap estimation, and execution scaffold |
| `src/cli.ts` | CLI entry point for all operations |

## Setup

### 1. Install dependencies

```bash
cd midnight-agent
npm install
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your settings. Defaults point to mainnet with a remote proof server (no local Docker needed).

### 3. Create a wallet

```bash
npm run dev -- wallet create
```

This generates an HD wallet from a random seed, derives all three key types (unshielded/shielded/DUST), and saves the seed to `~/.agent-lump/midnight/seed.hex` with `600` permissions.

**Back up your seed file.** If you lose it, you lose this wallet.

### 4. Build (optional, for production)

```bash
npm run build
node dist/cli.js wallet status
```

## CLI Reference

### Chain Queries

```bash
# Chain info (RPC + indexer combined)
npm run dev -- chain info

# Verify node connectivity and chain identity
npm run dev -- chain health

# Latest block (indexer)
npm run dev -- chain latest-block

# Recent blocks
npm run dev -- chain blocks --limit 10

# Finalized head (RPC)
npm run dev -- chain finalized

# Look up a transaction
npm run dev -- chain tx <TX_HASH>

# Recent transactions
npm run dev -- chain txs --limit 20

# Contract state (indexer or RPC)
npm run dev -- chain contract <CONTRACT_ADDRESS>
npm run dev -- chain contract-rpc <CONTRACT_ADDRESS>

# Ariadne bridge status
npm run dev -- chain bridge

# List all RPC methods
npm run dev -- chain methods

# Raw JSON-RPC call
npm run dev -- chain rpc system_health '[]'

# Raw GraphQL query
npm run dev -- chain query '{ blocks(limit: 1) { height hash } }'
```

### Wallet Management

```bash
# Create a new wallet
npm run dev -- wallet create

# Show wallet addresses
npm run dev -- wallet status

# Check balances (connects to chain, syncs wallet state)
npm run dev -- wallet balances
```

### Transfers

```bash
# Send NIGHT (unshielded)
npm run dev -- transfer unshielded --to mn_addr1... --amount 1000000

# Send shielded tokens
npm run dev -- transfer shielded --to mn_shield-addr1... --amount 1000000

# Register NIGHT for DUST generation
npm run dev -- transfer register-dust
```

### Token Operations

```bash
# Show token creation guide
npm run dev -- token guide

# Compile the included FungibleToken contract
npm run compact:compile

# Deploy the compiled token
npm run dev -- token deploy --name "My Token" --ticker MTK --supply 1000000

# Deploy with a custom compiled contract
npm run dev -- token deploy --contract ./path/to/compiled --name "My Token" --ticker MTK --supply 1000000

# Connect to a deployed token and read its state
npm run dev -- token connect <CONTRACT_ADDRESS>
```

### DEX

```bash
# DEX ecosystem status and known projects
npm run dev -- dex status

# Query a pool contract
npm run dev -- dex pool <POOL_CONTRACT_ADDRESS>
npm run dev -- dex pool <POOL_CONTRACT_ADDRESS> --rpc

# Estimate a swap
npm run dev -- dex estimate --pool <ADDR> --amount 1000000 --direction a_to_b

# Discover pools (placeholder — populates as DEXes launch on mainnet)
npm run dev -- dex discover
```

## TypeScript API

```typescript
import {
  getChainInfo,
  createWallet,
  initWallet,
  stopWallet,
  getBalances,
  transferUnshielded,
  transferShielded,
  estimateSwap,
  getPoolState,
} from 'midnight-agent';

// Chain queries (no wallet needed)
const info = await getChainInfo();
console.log(`${info.chainName} — ${info.nodeVersion}`);

// Create a wallet
const wallet = createWallet();
console.log(`Unshielded: ${wallet.addresses.unshielded}`);

// Initialize full wallet (connects to chain, syncs state)
const w = await initWallet();
const balances = await getBalances(w);
console.log(`DUST: ${balances.dustTotal}`);

// Transfer
const result = await transferUnshielded(w, [
  { amount: 1_000_000n, receiverAddress: 'mn_addr1...' },
]);

// DEX estimation
const pool = await getPoolState('contract_address');
const est = estimateSwap(pool, 1_000_000n, 'a_to_b');
console.log(`Output: ~${est.amountOut}, Impact: ${est.priceImpactPct}%`);

await stopWallet(w);
```

## Mainnet Config

| Property | Value |
|---|---|
| Chain name | Midnight Mainnet |
| Node version | 0.22.1-9ce45781 |
| Ledger version | 8.0.2 |
| Genesis hash | `0x1941ca8e2bb88146c14dea084d3be7eb6e96ca7135429c543848b628124f2854` |
| RPC (HTTP) | `https://rpc.mainnet.midnight.network/` |
| RPC (WSS) | `wss://rpc.mainnet.midnight.network` |
| Indexer (HTTP) | `https://indexer.mainnet.midnight.network/api/v4/graphql` |
| Indexer (WSS) | `wss://indexer.mainnet.midnight.network/api/v4/graphql/ws` |
| Prover | Configurable via `MIDNIGHT_PROVER_URL` (remote, no Docker) |
| Explorer | Configurable via `MIDNIGHT_EXPLORER_URL` |

## Known DEX Projects

- **[GalaxySwap](https://galaxyswap.io)** — Major DeFi protocol (waitlist stage)
- **[Pulse Finance](https://github.com/pulse-finance/midnight-dex-contract)** — Batcher-free atomic AMM (open-source Compact)
- **[Sevryn Labs](https://github.com/sevryn-labs/midnight-bonding-curve)** — ZK bonding curve AMM
- **[Arcane Finance](https://github.com/arcane-finance-defi/midnight-dex-poc)** — DEX proof of concept

## Token Creation

Midnight tokens are Compact smart contracts (not native assets like Cardano). This project includes a ready-to-use FungibleToken contract built on [OpenZeppelin's Compact Contracts](https://github.com/OpenZeppelin/compact-contracts).

### Prerequisites

Install the Compact toolchain:

```bash
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/midnightntwrk/compact/releases/latest/download/compact-installer.sh | sh
compact update
compact compile --version  # should show 0.30.0
```

### Contract

The contract is at `contracts/my_token.compact` — a fixed-supply ERC-20-like token using OpenZeppelin's FungibleToken module with these circuits:

- `name`, `symbol`, `decimals`, `totalSupply` — metadata queries
- `balanceOf` — check token balance for an account
- `transfer`, `transferFrom`, `approve`, `allowance` — standard ERC-20 operations

### Compile

```bash
npm run compact:compile
```

This generates ZK circuits, proving/verifying keys, and a TypeScript API in `contracts/managed/my_token/`.

### Deploy

```bash
npm run dev -- token deploy --name "My Token" --ticker MTK --supply 1000000
```

Deployment requires:
- A funded wallet with DUST (register NIGHT for DUST generation first)
- A proof server (remote for mainnet, or local Docker for preprod)

### Interact

```bash
npm run dev -- token connect <contract_address>
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `MIDNIGHT_NETWORK` | No | `mainnet`, `preprod`, or `preview` (default: mainnet) |
| `MIDNIGHT_RPC_URL` | No | Substrate JSON-RPC HTTP endpoint |
| `MIDNIGHT_RPC_WSS_URL` | No | Substrate JSON-RPC WebSocket endpoint |
| `MIDNIGHT_INDEXER_URL` | No | Indexer GraphQL endpoint |
| `MIDNIGHT_INDEXER_WS_URL` | No | Indexer WebSocket endpoint |
| `MIDNIGHT_PROVER_URL` | No | ZK proof server URL |
| `MIDNIGHT_EXPLORER_URL` | No | Block explorer URL |
| `MIDNIGHT_WALLET_SEED` | No | Wallet seed as hex (alternative to seed file) |
| `MIDNIGHT_WALLET_DIR` | No | Custom wallet directory (default: `~/.agent-lump/midnight`) |
