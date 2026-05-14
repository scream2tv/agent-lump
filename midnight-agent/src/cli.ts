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
  getTermsAndConditions,
} from './wallet.js';
import {
  transferUnshielded,
  transferShielded,
  estimateTransferFee,
  registerNightForDust,
  getDustStatus,
  formatNight,
  formatDust,
} from './transfer.js';
import { getTokenGuide, deployToken, connectToken, preflightCheck } from './token.js';
import {
  getPoolState,
  estimateSwap,
  discoverPools,
  getKnownDexProjects,
  poolFeePct,
  poolExplorerLink,
} from './dex.js';
import {
  getAscendConfig,
  getMarkets,
  getMarket,
  getEvents,
  getOrderbook,
  getTicker,
  getLeaderboard,
  getAccounts,
  getPositions,
  getMarketSummary,
  createOrder,
  waitForOrder,
  closePosition,
  waitForClose,
  cancelOrder,
  type OrderSide,
  type OrderType,
  type ChainType,
  type EventRange,
  type LeaderboardRange,
} from './ascend.js';

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
              terms                         Show network Terms & Conditions (no wallet sync)

    transfer  unshielded --to <addr> --amount <n>    Send NIGHT
              shielded --to <addr> --amount <n>      Send shielded tokens
              estimate [--unshielded-to <addr> --unshielded-amount <n>]
                       [--shielded-to <addr> --shielded-amount <n>]
                                                     Preview transfer fees (no submit)
              register-dust                          Register NIGHT for DUST generation

    dust      status                        Show DUST balance and registration state
              register [--no-wait]           Register NIGHT UTXOs for DUST generation

    token     guide                         Show token creation guide
              preflight [--contract <dir>]   Check deploy readiness (contract, prover, DUST)
              deploy --name <n> --ticker <t> --supply <n> [--contract <dir>] [--local-prove] [--no-sponsor]
              connect <address>              Connect to deployed token

    dex       status                        DEX ecosystem info
              pool <address> [--rpc]        Pool state
              estimate --pool <addr> --amount <n> [--direction a_to_b|b_to_a]
              discover                      Discover pools (placeholder)

    ascend    markets                       List all active markets
              market <slug>                 Market detail + orderbook summary
              events <slug> [--range 1D|1W|1M|ALL]  Price history
              orderbook <slug>              Live orderbook
              ticker <id>                   Spot price for a ticker
              leaderboard [--limit N] [--range today|weekly|monthly|all]
              accounts                      Linked addresses and balances (auth)
              positions <address>           Open positions (auth)
              order --market <slug> --side YES|NO --margin <n> --leverage <n>
                    [--type MARKET|LIMIT] [--trigger-price <n>]
                    [--address <addr>] [--chain MIDNIGHT]
              close --order-id <n> --address <addr> [--chain MIDNIGHT]
              cancel --order-id <n> --address <addr> [--chain MIDNIGHT]
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
    } else if (command === 'terms') {
      try {
        const tnc = await getTermsAndConditions();
        console.log(`\n  Midnight Network Terms & Conditions`);
        console.log(`    URL:  ${tnc.url}`);
        console.log(`    Hash: ${tnc.hash}`);
      } catch (e) {
        console.error(
          `\n  Failed to fetch Terms & Conditions: ${e instanceof Error ? e.message : String(e)}`,
        );
        process.exit(1);
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
          if (result.fee !== undefined) {
            console.log(`  Fee paid: ${formatDust(result.fee)} DUST`);
          }
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
          if (result.fee !== undefined) {
            console.log(`  Fee paid: ${formatDust(result.fee)} DUST`);
          }
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
    } else if (command === 'estimate') {
      const unshieldedTo = arg('unshielded-to');
      const unshieldedAmount = arg('unshielded-amount');
      const shieldedTo = arg('shielded-to');
      const shieldedAmount = arg('shielded-amount');
      if (
        (!unshieldedTo || !unshieldedAmount) &&
        (!shieldedTo || !shieldedAmount)
      ) {
        console.error(
          'Usage: midnight-agent transfer estimate [--unshielded-to <a> --unshielded-amount <n>] [--shielded-to <a> --shielded-amount <n>]',
        );
        process.exit(1);
      }
      console.log('\n  Initializing wallet...');
      const wallet = await initWallet();
      try {
        const est = await estimateTransferFee(wallet, {
          unshielded:
            unshieldedTo && unshieldedAmount
              ? [{ amount: BigInt(unshieldedAmount), receiverAddress: unshieldedTo }]
              : [],
          shielded:
            shieldedTo && shieldedAmount
              ? [{ amount: BigInt(shieldedAmount), receiverAddress: shieldedTo }]
              : [],
        });
        console.log(`  Transaction fee:  ${formatDust(est.transactionFee)} DUST`);
        console.log(`  Total fee (incl. balancing): ${formatDust(est.totalFee)} DUST`);
      } finally {
        await stopWallet(wallet);
      }
    } else {
      console.error(`Unknown transfer command: ${command}`);
      usage();
      process.exit(1);
    }
  }

  // ─── DUST Commands ─────────────────────────────────────────────────

  else if (domain === 'dust') {
    if (command === 'status') {
      console.log('\n  Initializing wallet and syncing with chain...');
      const wallet = await initWallet();
      try {
        const status = await getDustStatus(wallet);
        console.log(`\n  DUST Status`);
        console.log(`  ─────────────────────────────`);
        console.log(`  NIGHT balance:      ${formatNight(status.nightBalance)} tNight`);
        console.log(`  DUST balance:       ${formatDust(status.dustBalance)} DUST`);
        console.log(`  DUST coins:         ${status.dustCoinCount}`);
        console.log(`  Registered UTXOs:   ${status.registeredCoinCount}`);
        console.log(`  Unregistered UTXOs: ${status.unregisteredCoinCount}`);
        console.log(`  Status:             ${status.isRegistered ? 'Registered' : status.hasNight ? 'NIGHT available — run "dust register"' : 'No NIGHT — fund wallet first'}`);
        if (status.hasDust) {
          console.log(`\n  Ready to deploy contracts and call circuits.`);
        } else if (status.isRegistered) {
          console.log(`\n  Registered but DUST still accruing. Rate: 5 DUST per NIGHT, ~1 week to cap.`);
        }
      } finally {
        await stopWallet(wallet);
      }
    } else if (command === 'register') {
      console.log('\n  Initializing wallet and syncing with chain...');
      const wallet = await initWallet();
      try {
        const noWait = flag('no-wait');
        const result = await registerNightForDust(wallet, { waitForDust: !noWait });
        if (result.success) {
          if (result.txHash === '(already registered)') {
            console.log(`\n  NIGHT already registered for DUST generation.`);
          } else if (result.txHash) {
            console.log(`\n  NIGHT registered for DUST generation: ${result.txHash}`);
          }
          if (result.dustStatus) {
            console.log(`  NIGHT balance: ${formatNight(result.dustStatus.nightBalance)}`);
            console.log(`  DUST balance:  ${formatDust(result.dustStatus.dustBalance)}`);
            if (result.dustStatus.hasDust) {
              console.log(`\n  Ready to deploy contracts.`);
            }
          }
          if (result.error) {
            console.log(`\n  Note: ${result.error}`);
          }
        } else {
          console.error(`\n  Registration failed: ${result.error}`);
        }
      } finally {
        await stopWallet(wallet);
      }
    } else {
      console.error(`Unknown dust command: ${command}`);
      usage();
      process.exit(1);
    }
  }

  // ─── Token Commands ─────────────────────────────────────────────────

  else if (domain === 'token') {
    if (command === 'guide') {
      console.log(getTokenGuide());
    } else if (command === 'preflight') {
      const useSponsorship = flag('sponsored') || !flag('no-sponsor');
      let wallet = null;
      if (!useSponsorship) {
        console.log('\n  Initializing wallet for balance checks...');
        wallet = await initWallet();
      }
      try {
        const result = await preflightCheck(wallet, arg('contract') ?? '', { useGasSponsorship: useSponsorship });
        console.log(`\n  Deploy Readiness Check`);
        console.log(`  ──────────────────────────────`);
        console.log(`  Contract compiled:  ${result.contractCompiled ? 'YES' : 'NO'}`);
        console.log(`  Proof server:       ${result.proverReachable ? 'YES' : 'NO'}`);
        console.log(`  DUST available:     ${result.walletHasDust ? 'YES' : 'NO'}${result.dustBalance ? ` (${result.dustBalance})` : useSponsorship ? ' (gas sponsored)' : ''}`);
        if (result.nightBalance) {
          console.log(`  NIGHT balance:      ${result.nightBalance}`);
        }
        console.log(`\n  Status: ${result.ready ? 'READY TO DEPLOY' : 'NOT READY'}`);
        for (const err of result.errors) {
          console.log(`  ERROR: ${err}`);
        }
        for (const warn of result.warnings) {
          console.log(`  WARNING: ${warn}`);
        }
      } finally {
        if (wallet) await stopWallet(wallet);
      }
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
      const useSponsorship = flag('sponsored') || !flag('no-sponsor');
      const useLocalProve = flag('local-prove');
      const deployParams = {
        contractDir: arg('contract') ?? '',
        metadata: {
          name,
          ticker,
          description: arg('description') ?? '',
          decimals: Number(arg('decimals') ?? 6),
          initialSupply: BigInt(supply),
        },
        useGasSponsorship: useSponsorship,
        localProve: useLocalProve,
        localProverUrl: arg('local-prover-url'),
        remoteSubmitUrl: arg('remote-submit-url'),
      };

      console.log('\n  Running pre-deploy checks...');
      const preflight = await preflightCheck(null, deployParams.contractDir, { useGasSponsorship: useSponsorship });
      if (!preflight.ready) {
        console.error('\n  Deploy readiness check FAILED:');
        for (const err of preflight.errors) {
          console.error(`    - ${err}`);
        }
        process.exit(1);
      }
      console.log('  Pre-deploy checks passed.');
      for (const warn of preflight.warnings) {
        console.log(`  WARNING: ${warn}`);
      }

      if (useLocalProve) {
        console.log(`\n  Initializing wallet (keys only — local prove + remote submit)...`);
        const wallet = initWalletKeysOnly();
        const result = await deployToken(wallet, deployParams);
        console.log(`\n  Token Deployed!`);
        console.log(`  Contract: ${result.contractAddress}`);
        console.log(`  Explorer: ${result.explorerUrl}`);
        console.log(`  Tx: ${result.txHash}`);
      } else {
        console.log('\n  Initializing wallet (full sync — this may take several minutes on first run)...');
        const wallet = await initWallet();
        try {
          const result = await deployToken(wallet, deployParams);
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
  }

  // ─── Ascend Commands ──────────────────────────────────────────────

  else if (domain === 'ascend') {
    if (command === 'markets') {
      const markets = await getMarkets();
      console.log(`\n  Ascend — ${markets.length} active markets:\n`);
      for (const m of markets) {
        const price = m.mark_price !== null ? `${(m.mark_price * 100).toFixed(1)}%` : '—';
        const status = m.closed ? '[CLOSED]' : '';
        console.log(`  ${m.slug.padEnd(32)} ${price.padStart(7)}  ${m.category ?? ''}  ${status}`);
      }
    } else if (command === 'market') {
      const slug = rest[0];
      if (!slug) {
        console.error('Usage: midnight-agent ascend market <slug>');
        process.exit(1);
      }
      const summary = await getMarketSummary(slug);
      console.log(`\n  ${summary.title}`);
      console.log(`  ─────────────────────────────`);
      console.log(`  Slug:       ${summary.slug}`);
      console.log(`  Mark price: ${summary.mark_price !== null ? (summary.mark_price * 100).toFixed(2) + '%' : '—'}`);
      console.log(`  Category:   ${summary.category ?? '—'}`);
      console.log(`  Closed:     ${summary.closed}`);
      console.log(`  Best bid:   ${summary.bestBid !== null ? (summary.bestBid * 100).toFixed(2) + '%' : '—'}`);
      console.log(`  Best ask:   ${summary.bestAsk !== null ? (summary.bestAsk * 100).toFixed(2) + '%' : '—'}`);
      console.log(`  Bid depth:  $${summary.bidDepth.toFixed(2)}`);
      console.log(`  Ask depth:  $${summary.askDepth.toFixed(2)}`);
    } else if (command === 'events') {
      const slug = rest[0];
      if (!slug) {
        console.error('Usage: midnight-agent ascend events <slug> [--range 1D|1W|1M|ALL]');
        process.exit(1);
      }
      const range = (arg('range') ?? '1M') as EventRange;
      const events = await getEvents(slug, range);
      console.log(`\n  Ascend — ${slug} price history (${range}, ${events.length} points):\n`);
      for (const e of events.data.slice(-20)) {
        const bar = '█'.repeat(Math.round(e.yes_percent / 2));
        console.log(`  ${e.timestamp.slice(0, 16)}  YES ${String(e.yes_percent).padStart(3)}%  ${bar}`);
      }
    } else if (command === 'orderbook') {
      const slug = rest[0];
      if (!slug) {
        console.error('Usage: midnight-agent ascend orderbook <slug>');
        process.exit(1);
      }
      const book = await getOrderbook(slug);
      console.log(`\n  Ascend Orderbook — ${slug}\n`);
      console.log(`  BIDS (YES)                     ASKS (NO)`);
      console.log(`  ${'─'.repeat(28)}   ${'─'.repeat(28)}`);
      const maxRows = Math.max(book.bids.length, book.asks.length, 1);
      for (let i = 0; i < Math.min(maxRows, 15); i++) {
        const bid = book.bids[i];
        const ask = book.asks[i];
        const bidStr = bid ? `${(bid.price * 100).toFixed(1).padStart(6)}%  $${bid.size.toFixed(2).padStart(8)}` : ''.padEnd(18);
        const askStr = ask ? `${(ask.price * 100).toFixed(1).padStart(6)}%  $${ask.size.toFixed(2).padStart(8)}` : '';
        console.log(`  ${bidStr}           ${askStr}`);
      }
    } else if (command === 'ticker') {
      const id = rest[0];
      if (!id) {
        console.error('Usage: midnight-agent ascend ticker <id>');
        process.exit(1);
      }
      const ticker = await getTicker(id);
      console.log(`\n  Ticker: ${ticker.id}`);
      console.log(`  Price:  $${ticker.price}`);
      console.log(`  Source: ${ticker.source}`);
    } else if (command === 'leaderboard') {
      const limit = Number(arg('limit') ?? 20);
      const range = (arg('range') ?? 'all') as LeaderboardRange;
      const entries = await getLeaderboard(limit, range);
      console.log(`\n  Ascend Leaderboard (${range}, top ${entries.length}):\n`);
      console.log(`  ${'#'.padStart(4)}  ${'Address'.padEnd(16)}  ${'PnL'.padStart(10)}  ${'Win%'.padStart(6)}  Trades`);
      console.log(`  ${'─'.repeat(60)}`);
      entries.forEach((e, i) => {
        const addr = (e.username ?? e.address).slice(0, 14).padEnd(16);
        const pnl = `$${e.total_pnl.toFixed(2)}`.padStart(10);
        const wr = `${(e.win_rate * 100).toFixed(0)}%`.padStart(6);
        console.log(`  ${String(i + 1).padStart(4)}  ${addr}  ${pnl}  ${wr}  ${e.trade_count}`);
      });
    } else if (command === 'accounts') {
      const accounts = await getAccounts();
      console.log(`\n  Ascend Accounts:\n`);
      for (const a of accounts) {
        console.log(`  ID ${a.id}  ${a.chain_type ?? '?'}  ${a.address ?? '—'}  Balance: $${a.balance?.toFixed(2) ?? '0.00'}`);
      }
    } else if (command === 'positions') {
      const address = rest[0];
      if (!address) {
        console.error('Usage: midnight-agent ascend positions <address>');
        process.exit(1);
      }
      const positions = await getPositions(address);
      if (!positions.length) {
        console.log(`\n  No open positions for ${address}`);
      } else {
        console.log(`\n  Ascend Positions — ${address.slice(0, 20)}...\n`);
        for (const p of positions) {
          const pnlStr = p.pnl !== null ? `$${p.pnl.toFixed(2)}` : '—';
          const lev = p.leverage !== null ? `${p.leverage}x` : '—';
          console.log(`  #${p.id}  ${(p.slug ?? '?').padEnd(24)}  ${(p.side ?? '?').padEnd(4)}  ${lev.padEnd(5)}  PnL: ${pnlStr}  [${p.status ?? '?'}]`);
        }
      }
    } else if (command === 'order') {
      const market = arg('market');
      const side = arg('side') as OrderSide | undefined;
      const margin = arg('margin');
      const leverage = arg('leverage');
      if (!market || !side || !margin || !leverage) {
        console.error('Usage: midnight-agent ascend order --market <slug> --side YES|NO --margin <n> --leverage <n>');
        process.exit(1);
      }
      const orderType = (arg('type') ?? 'MARKET') as OrderType;
      const chainType = (arg('chain') ?? 'MIDNIGHT') as ChainType;
      const triggerPrice = arg('trigger-price');

      let address = arg('address');
      if (!address) {
        const accounts = await getAccounts();
        const midnightAcct = accounts.find((a) => a.chain_type === chainType);
        if (!midnightAcct?.address) {
          console.error(`  No ${chainType} address linked to your API key. Link one at testnet.ascend.market`);
          process.exit(1);
        }
        address = midnightAcct.address;
      }

      const summary = await getMarketSummary(market);
      console.log(`\n  Pre-trade Summary`);
      console.log(`  ─────────────────────────────`);
      console.log(`  Market:     ${summary.title}`);
      console.log(`  Mark price: ${summary.mark_price !== null ? (summary.mark_price * 100).toFixed(2) + '%' : '—'}`);
      console.log(`  Best bid:   ${summary.bestBid !== null ? (summary.bestBid * 100).toFixed(2) + '%' : '—'}`);
      console.log(`  Best ask:   ${summary.bestAsk !== null ? (summary.bestAsk * 100).toFixed(2) + '%' : '—'}`);
      console.log(`  Side:       ${side}`);
      console.log(`  Type:       ${orderType}`);
      console.log(`  Margin:     $${margin}`);
      console.log(`  Leverage:   ${leverage}x`);
      console.log(`  Chain:      ${chainType}`);
      console.log(`  Address:    ${address}`);
      if (triggerPrice) console.log(`  Trigger:    ${triggerPrice}`);

      const params: Parameters<typeof createOrder>[0] = {
        chain_type: chainType,
        address,
        market,
        side,
        margin: Number(margin),
        leverage: Number(leverage),
        type: orderType,
      };
      if (triggerPrice) params.trigger_price = Number(triggerPrice);

      console.log(`\n  Placing order...`);
      const result = await createOrder(params);
      console.log(`  Success: ${result.success}`);
      if (result.client_order_id) {
        console.log(`  Order ID: ${result.client_order_id}`);
        console.log(`  Liquidation: ${result.liquidation_price ?? '—'}`);
        console.log(`  Profit:      ${result.profit_price ?? '—'}`);
        console.log(`\n  Polling for acceptance...`);
        const poll = await waitForOrder(result.client_order_id, orderType);
        if (poll.data) {
          console.log(`  Order accepted: ${poll.data.event_type}`);
        } else if (poll.error) {
          console.error(`  Order error: ${poll.error.message}`);
        }
      }
    } else if (command === 'close') {
      const orderId = arg('order-id');
      const address = arg('address');
      if (!orderId || !address) {
        console.error('Usage: midnight-agent ascend close --order-id <n> --address <addr>');
        process.exit(1);
      }
      const chainType = (arg('chain') ?? 'MIDNIGHT') as ChainType;
      console.log(`\n  Closing position #${orderId}...`);
      const result = await closePosition({
        chain_type: chainType,
        order_id: Number(orderId),
        user_address: address,
      });
      console.log(`  Success: ${result.success}`);
      if (result.client_order_id) {
        console.log(`  Polling for settlement...`);
        const poll = await waitForClose(result.client_order_id);
        console.log(`  Settled: ${JSON.stringify(poll.data ?? poll.error)}`);
      }
    } else if (command === 'cancel') {
      const orderId = arg('order-id');
      const address = arg('address');
      if (!orderId || !address) {
        console.error('Usage: midnight-agent ascend cancel --order-id <n> --address <addr>');
        process.exit(1);
      }
      const chainType = (arg('chain') ?? 'MIDNIGHT') as ChainType;
      console.log(`\n  Cancelling order #${orderId}...`);
      const result = await cancelOrder({
        chain_type: chainType,
        order_id: Number(orderId),
        user_address: address,
      });
      console.log(`  Success: ${result.success}`);
      console.log(`  Client order ID: ${result.client_order_id}`);
    } else {
      console.error(`Unknown ascend command: ${command}`);
      usage();
      process.exit(1);
    }
  }

  else {
    console.error(`Unknown domain: ${domain}`);
    usage();
    process.exit(1);
  }
}

main().catch((e) => {
  console.error(`\n  Error: ${e instanceof Error ? e.message : String(e)}`);
  process.exit(1);
});
