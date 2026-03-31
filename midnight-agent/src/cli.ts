#!/usr/bin/env node

/**
 * Midnight Agent CLI
 *
 * Usage:
 *   midnight-agent <domain> <command> [options]
 *
 * Domains:
 *   chain    — Query blocks, transactions, contracts, node status
 *   wallet   — Create wallets, derive keys, check balances
 *   transfer — Send tokens (shielded, unshielded, combined)
 *   token    — Deploy and interact with FungibleToken contracts
 *   dex      — Pool queries, swap estimation, DEX ecosystem info
 */

import { getConfig } from './config.js';
import {
  getChainInfo,
  verifyNode,
  getLatestBlock,
  getRecentBlocks,
  getRecentTransactions,
  getTransaction,
  getContractState,
  getContractStateRpc,
  getRpcMethods,
  getFinalizedHead,
  getHeader,
  getSidechainStatus,
  getSidechainParams,
  rpcCall,
  graphql,
} from './chain.js';
import {
  createWallet,
  getWalletInfo,
  initWallet,
  initWalletKeysOnly,
  stopWallet,
  getBalances,
} from './wallet.js';
import {
  transferUnshielded,
  transferShielded,
  registerNightForDust,
} from './transfer.js';
import { getTokenGuide, deployToken, connectToken } from './token.js';
import {
  getPoolState,
  estimateSwap,
  discoverPools,
  getKnownDexProjects,
  poolFeePct,
  poolExplorerLink,
} from './dex.js';

const [domain, command, ...rest] = process.argv.slice(2);

function arg(name: string): string | undefined {
  const idx = rest.indexOf(`--${name}`);
  if (idx === -1) return undefined;
  return rest[idx + 1];
}

function flag(name: string): boolean {
  return rest.includes(`--${name}`);
}

function usage(): void {
  console.log(`
  midnight-agent — Midnight Network CLI

  Usage: midnight-agent <domain> <command> [options]

  Domains:
    chain     info                          Chain info (RPC + indexer)
              health                        Verify node connectivity
              latest-block                  Latest block (indexer)
              blocks [--limit N]            Recent blocks
              finalized                     Finalized head (RPC)
              tx <hash>                     Transaction by hash
              txs [--limit N]              Recent transactions
              contract <address>            Contract state (indexer)
              contract-rpc <address>        Contract state (RPC)
              bridge                        Ariadne bridge status
              methods                       List all RPC methods
              rpc <method> [params_json]    Raw JSON-RPC call
              query <graphql_string>        Raw GraphQL query

    wallet    create                        Generate new wallet
              status                        Show wallet addresses
              balances                      Show token balances (requires sync)

    transfer  unshielded --to <addr> --amount <n>    Send NIGHT
              shielded --to <addr> --amount <n>      Send shielded tokens
              register-dust                          Register NIGHT for DUST generation

    token     guide                         Show token creation guide
              deploy --name <n> --ticker <t> --supply <n> [--contract <dir>]
              connect <address>              Connect to deployed token

    dex       status                        DEX ecosystem info
              pool <address> [--rpc]        Pool state
              estimate --pool <addr> --amount <n> [--direction a_to_b|b_to_a]
              discover                      Discover pools (placeholder)
  `);
}

async function main(): Promise<void> {
  if (!domain || !command) {
    usage();
    process.exit(0);
  }

  // ─── Chain Commands ─────────────────────────────────────────────────

  if (domain === 'chain') {
    if (command === 'info') {
      const info = await getChainInfo();
      console.log(`\n  Midnight Network — ${info.chainName ?? info.networkId}`);
      console.log(`  Node version: ${info.nodeVersion ?? '?'}`);
      console.log(`  RPC:        ${info.rpcUrl}  [${info.rpcStatus}]`);
      console.log(`  Indexer:    ${info.indexerUrl}`);
      console.log(`  Prover:     ${info.proverUrl}`);
      console.log(`  Explorer:   ${info.explorerUrl}`);
      console.log(`  Genesis:    ${info.genesisHash}`);
      if (info.peers !== undefined) {
        console.log(`  Peers: ${info.peers}  Syncing: ${info.isSyncing}`);
      }
      if (info.finalizedHead) {
        console.log(`  Finalized: ${info.finalizedHead}`);
      }
      if (info.bridgePermissioned !== undefined) {
        console.log(
          `  Bridge: ${info.bridgePermissioned} permissioned, ${info.bridgeRegistered} registered validators`,
        );
      }
    } else if (command === 'health') {
      const result = await verifyNode();
      console.log(`\n  ${result.chainName}`);
      console.log(`  Version: ${result.nodeVersion}`);
      console.log(`  Genesis match: ${result.genesisMatch ? 'OK' : 'MISMATCH!'}`);
      console.log(`  Peers: ${result.peers}`);
      console.log(`  Syncing: ${result.isSyncing}`);
      console.log(`  Status: ${result.valid ? 'Verified and synced' : 'Not ready'}`);
    } else if (command === 'latest-block') {
      const b = await getLatestBlock();
      console.log(`\n  Block #${b.height}`);
      console.log(`  Hash: ${b.hash}`);
      console.log(`  Txs: ${b.txCount}`);
      console.log(`  Time: ${b.timestamp}`);
    } else if (command === 'blocks') {
      const limit = Number(arg('limit') ?? 5);
      const blocks = await getRecentBlocks(limit);
      console.log(`\n  Midnight — last ${blocks.length} blocks:\n`);
      for (const b of blocks) {
        console.log(
          `  #${b.height}  ${b.hash.slice(0, 24)}...  txs=${b.txCount}  ${b.timestamp}`,
        );
      }
    } else if (command === 'finalized') {
      const h = await getFinalizedHead();
      console.log(`\n  Finalized head: ${h}`);
      const header = await getHeader(h);
      console.log(`  Number: ${parseInt(String(header.number ?? '0x0'), 16)}`);
      console.log(`  Parent: ${header.parentHash ?? ''}`);
    } else if (command === 'tx') {
      const hash = rest[0];
      if (!hash) {
        console.error('Usage: midnight-agent chain tx <hash>');
        process.exit(1);
      }
      const t = await getTransaction(hash);
      console.log(`\n  Transaction: ${t.hash}`);
      console.log(`  ID: ${t.id}`);
      console.log(`  Block: #${t.blockHeight} (${t.blockHash})`);
      console.log(`  Protocol version: ${t.protocolVersion}`);
    } else if (command === 'txs') {
      const limit = Number(arg('limit') ?? 10);
      const txs = await getRecentTransactions(limit);
      console.log(`\n  Midnight — last ${txs.length} transactions:\n`);
      for (const t of txs) {
        console.log(
          `  ${t.hash.slice(0, 24)}...  block=#${t.blockHeight}  id=${t.id.slice(0, 16)}...`,
        );
      }
    } else if (command === 'contract') {
      const address = rest[0];
      if (!address) {
        console.error('Usage: midnight-agent chain contract <address>');
        process.exit(1);
      }
      const cs = await getContractState(address);
      console.log(`\n  Contract (indexer): ${cs.address}`);
      console.log(
        `  State: ${cs.state ? JSON.stringify(cs.state, null, 2) : '(empty)'}`,
      );
    } else if (command === 'contract-rpc') {
      const address = rest[0];
      if (!address) {
        console.error('Usage: midnight-agent chain contract-rpc <address>');
        process.exit(1);
      }
      const result = await getContractStateRpc(address);
      console.log(`\n  Contract (RPC): ${address}`);
      console.log(`  State: ${JSON.stringify(result, null, 2)}`);
    } else if (command === 'bridge') {
      const sc = await getSidechainStatus();
      const params = await getSidechainParams();
      console.log(`\n  Ariadne Bridge Status`);
      console.log(`  Permissioned validators: ${sc.numPermissionedCandidates}`);
      console.log(`  Registered validators:   ${sc.numRegisteredCandidates}`);
      console.log(`  Params: ${JSON.stringify(params, null, 2)}`);
    } else if (command === 'methods') {
      const methods = await getRpcMethods();
      console.log(`\n  ${methods.length} RPC methods available:\n`);
      for (const m of methods.sort()) {
        console.log(`  ${m}`);
      }
    } else if (command === 'rpc') {
      const method = rest[0];
      const params = rest[1] ? JSON.parse(rest[1]) : [];
      const result = await rpcCall(method, params);
      console.log(JSON.stringify(result, null, 2));
    } else if (command === 'query') {
      const gql = rest[0];
      if (!gql) {
        console.error('Usage: midnight-agent chain query <graphql_string>');
        process.exit(1);
      }
      const result = await graphql(gql);
      console.log(JSON.stringify(result, null, 2));
    } else {
      console.error(`Unknown chain command: ${command}`);
      usage();
      process.exit(1);
    }
  }

  // ─── Wallet Commands ────────────────────────────────────────────────

  else if (domain === 'wallet') {
    if (command === 'create') {
      const info = createWallet();
      console.log(`\n  Midnight Wallet Created — ${info.networkId}`);
      console.log(`  Seed saved to: ${info.seedPath}`);
      console.log(`\n  Addresses:`);
      console.log(`    Unshielded: ${info.addresses.unshielded}`);
      console.log(`    Shielded:   ${info.addresses.shielded}`);
      console.log(`    DUST:       ${info.addresses.dust}`);
      console.log(`\n  IMPORTANT: Back up your seed file. If you lose it, you lose this wallet.`);
    } else if (command === 'status') {
      try {
        const info = getWalletInfo();
        console.log(`\n  Midnight Wallet — ${info.networkId}`);
        console.log(`  Seed: ${info.seedPath}`);
        console.log(`\n  Addresses:`);
        console.log(`    Unshielded: ${info.addresses.unshielded}`);
        console.log(`    Shielded:   ${info.addresses.shielded}`);
        console.log(`    DUST:       ${info.addresses.dust}`);
      } catch (e) {
        console.error(
          `\n  ${e instanceof Error ? e.message : String(e)}`,
        );
      }
    } else if (command === 'balances') {
      console.log('\n  Initializing wallet and syncing with chain...');
      const wallet = await initWallet();
      try {
        const balances = await getBalances(wallet);
        const bigintReplacer = (_k: string, v: unknown) => typeof v === 'bigint' ? v.toString() : v;
        console.log(`\n  Balances:`);
        console.log(`    Unshielded: ${JSON.stringify(balances.unshielded, bigintReplacer)}`);
        console.log(`    Shielded:   ${JSON.stringify(balances.shielded, bigintReplacer)}`);
        console.log(`    DUST:       ${balances.dustBalance.toString()} (${balances.dustCoinCount} coins)`);
      } finally {
        await stopWallet(wallet);
      }
    } else {
      console.error(`Unknown wallet command: ${command}`);
      usage();
      process.exit(1);
    }
  }

  // ─── Transfer Commands ──────────────────────────────────────────────

  else if (domain === 'transfer') {
    if (command === 'unshielded') {
      const to = arg('to');
      const amount = arg('amount');
      if (!to || !amount) {
        console.error(
          'Usage: midnight-agent transfer unshielded --to <address> --amount <n>',
        );
        process.exit(1);
      }
      console.log('\n  Initializing wallet...');
      const wallet = await initWallet();
      try {
        const result = await transferUnshielded(wallet, [
          { amount: BigInt(amount), receiverAddress: to },
        ]);
        if (result.success) {
          console.log(`  Transfer submitted: ${result.txHash}`);
        } else {
          console.error(`  Transfer failed: ${result.error}`);
        }
      } finally {
        await stopWallet(wallet);
      }
    } else if (command === 'shielded') {
      const to = arg('to');
      const amount = arg('amount');
      if (!to || !amount) {
        console.error(
          'Usage: midnight-agent transfer shielded --to <address> --amount <n>',
        );
        process.exit(1);
      }
      console.log('\n  Initializing wallet...');
      const wallet = await initWallet();
      try {
        const result = await transferShielded(wallet, [
          { amount: BigInt(amount), receiverAddress: to },
        ]);
        if (result.success) {
          console.log(`  Transfer submitted: ${result.txHash}`);
        } else {
          console.error(`  Transfer failed: ${result.error}`);
        }
      } finally {
        await stopWallet(wallet);
      }
    } else if (command === 'register-dust') {
      console.log('\n  Initializing wallet...');
      const wallet = await initWallet();
      try {
        const result = await registerNightForDust(wallet);
        if (result.success) {
          console.log(`  NIGHT registered for DUST generation: ${result.txHash}`);
        } else {
          console.error(`  Registration failed: ${result.error}`);
        }
      } finally {
        await stopWallet(wallet);
      }
    } else {
      console.error(`Unknown transfer command: ${command}`);
      usage();
      process.exit(1);
    }
  }

  // ─── Token Commands ─────────────────────────────────────────────────

  else if (domain === 'token') {
    if (command === 'guide') {
      console.log(getTokenGuide());
    } else if (command === 'deploy') {
      const name = arg('name');
      const ticker = arg('ticker');
      const supply = arg('supply');
      if (!name || !ticker || !supply) {
        console.error(
          'Usage: midnight-agent token deploy --name <n> --ticker <t> --supply <n> [--contract <dir>]',
        );
        process.exit(1);
      }
      const sponsored = flag('sponsored') || !flag('no-sponsor');
      if (sponsored) {
        console.log('\n  Initializing wallet (keys only — gas sponsored by remote prover)...');
        const wallet = initWalletKeysOnly();
        const result = await deployToken(wallet, {
          contractDir: arg('contract') ?? '',
          metadata: {
            name,
            ticker,
            description: arg('description') ?? '',
            decimals: Number(arg('decimals') ?? 6),
            initialSupply: BigInt(supply),
          },
        });
        console.log(`\n  Token Deployed!`);
        console.log(`  Contract: ${result.contractAddress}`);
        console.log(`  Explorer: ${result.explorerUrl}`);
        console.log(`  Tx: ${result.txHash}`);
      } else {
        console.log('\n  Initializing wallet...');
        const wallet = await initWallet();
        try {
          const result = await deployToken(wallet, {
            contractDir: arg('contract') ?? '',
            metadata: {
              name,
              ticker,
              description: arg('description') ?? '',
              decimals: Number(arg('decimals') ?? 6),
              initialSupply: BigInt(supply),
            },
          });
          console.log(`\n  Token Deployed!`);
          console.log(`  Contract: ${result.contractAddress}`);
          console.log(`  Explorer: ${result.explorerUrl}`);
          console.log(`  Tx: ${result.txHash}`);
        } finally {
          await stopWallet(wallet);
        }
      }
    } else if (command === 'connect') {
      const address = rest[0];
      if (!address) {
        console.error('Usage: midnight-agent token connect <address>');
        process.exit(1);
      }
      console.log('\n  Initializing wallet...');
      const wallet = await initWallet();
      try {
const token = await connectToken(wallet, address, arg('contract'));
        const [tokenName, tokenSymbol, tokenDecimals, tokenSupply] =
          await Promise.all([
            token.name(),
            token.symbol(),
            token.decimals(),
            token.totalSupply(),
          ]);
        console.log(`\n  Token: ${tokenName} (${tokenSymbol})`);
        console.log(`  Decimals: ${tokenDecimals}`);
        console.log(`  Total Supply: ${tokenSupply}`);
        console.log(`  Contract: ${address}`);
      } finally {
        await stopWallet(wallet);
      }
    } else {
      console.error(`Unknown token command: ${command}`);
      usage();
      process.exit(1);
    }
  }

  // ─── DEX Commands ───────────────────────────────────────────────────

  else if (domain === 'dex') {
    if (command === 'status') {
      const config = getConfig();
      console.log(`\n  Midnight DEX Ecosystem Status`);
      console.log(`  ─────────────────────────────`);
      console.log(`  Network:  ${config.networkId}`);
      console.log(`  Prover:   ${config.proverUrl}`);
      console.log(`  Explorer: ${config.explorerUrl}`);
      console.log();
      console.log(`  Known DEX Projects:`);
      for (const p of getKnownDexProjects()) {
        console.log(`    ${p.name.padEnd(18)} — ${p.description} [${p.status}]`);
        console.log(`    ${''.padEnd(18)}   ${p.url}`);
      }
      console.log();
      console.log(`  Notes:`);
      console.log(`    - DUST is non-transferable (accrues from NIGHT staking)`);
      console.log(`    - ZK proofs via remote prover — no local Docker needed`);
      console.log(`    - Compact contracts compile to ZK circuits`);
      console.log(`    - Pool interactions are atomic (no batcher needed)`);
    } else if (command === 'pool') {
      const address = rest[0];
      if (!address) {
        console.error('Usage: midnight-agent dex pool <address> [--rpc]');
        process.exit(1);
      }
      const pool = await getPoolState(address, flag('rpc'));
      const source = flag('rpc') ? 'RPC' : 'indexer';
      console.log(`\n  Midnight Pool (${source}): ${pool.contractAddress}`);
      console.log(`  Pair: ${pool.tokenA}/${pool.tokenB}`);
      console.log(`  Reserves: ${pool.reserveA} / ${pool.reserveB}`);
      console.log(`  Fee: ${poolFeePct(pool).toFixed(2)}%`);
      console.log(`  Type: ${pool.poolType}`);
      console.log(`  Explorer: ${poolExplorerLink(pool)}`);
    } else if (command === 'estimate') {
      const poolAddr = arg('pool');
      const amount = arg('amount');
      if (!poolAddr || !amount) {
        console.error(
          'Usage: midnight-agent dex estimate --pool <addr> --amount <n> [--direction a_to_b|b_to_a]',
        );
        process.exit(1);
      }
      const pool = await getPoolState(poolAddr);
      const direction = (arg('direction') ?? 'a_to_b') as 'a_to_b' | 'b_to_a';
      const slippage = Number(arg('slippage') ?? 0.5);
      const est = estimateSwap(pool, BigInt(amount), direction, slippage);
      console.log(`\n  Midnight DEX Estimate`);
      console.log(`  Pool: ${est.poolAddress}`);
      console.log(`  Direction: ${est.direction}`);
      console.log(`  Input: ${est.amountIn}`);
      console.log(`  Output: ~${est.amountOut}`);
      console.log(`  Min receive: ${est.minReceive}`);
      console.log(`  Impact: ${est.priceImpactPct.toFixed(4)}%`);
      console.log(`  Fee: ${est.feePaid}`);
    } else if (command === 'discover') {
      const pools = await discoverPools();
      if (!pools.length) {
        console.log('\n  No pools discovered yet.');
        console.log('  Midnight DEX ecosystem is emerging — check:');
        for (const p of getKnownDexProjects()) {
          console.log(`    - ${p.name}: ${p.url}`);
        }
      }
    } else {
      console.error(`Unknown dex command: ${command}`);
      usage();
      process.exit(1);
    }
  } else {
    console.error(`Unknown domain: ${domain}`);
    usage();
    process.exit(1);
  }
}

main().catch((e) => {
  console.error(`\n  Error: ${e instanceof Error ? e.message : String(e)}`);
  process.exit(1);
});
