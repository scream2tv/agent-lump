/**
 * Midnight DEX Client — pool discovery, swap estimation, and execution.
 *
 * Known DEX projects on Midnight:
 *   - Pulse Finance: Batcher-free atomic AMM (Compact, open-source)
 *     https://github.com/pulse-finance/midnight-dex-contract
 *   - GalaxySwap: Major DeFi protocol (waitlist stage)
 *     https://galaxyswap.io
 *   - Sevryn Labs: ZK bonding curve AMM
 *     https://github.com/sevryn-labs/midnight-bonding-curve
 *   - Arcane Finance: DEX proof of concept
 *     https://github.com/arcane-finance-defi/midnight-dex-poc
 *
 * Unlike Cardano DEXes, Midnight swaps are atomic (no batcher) and
 * privacy-preserving (ZK-proven execution). The flow:
 *   1. Query pool state via indexer or RPC
 *   2. Estimate output with constant-product math
 *   3. Build transaction with circuit inputs
 *   4. Generate ZK proof via proof server
 *   5. Balance (add DUST fees) and submit
 */

import { graphql, getContractStateRpc, getContractState } from './chain.js';
import { explorerLink } from './config.js';

// ─── Types ──────────────────────────────────────────────────────────────

export interface MidnightPool {
  contractAddress: string;
  tokenA: string;
  tokenB: string;
  reserveA: bigint;
  reserveB: bigint;
  feeNumerator: number;
  feeDenominator: number;
  poolType: string;
}

export interface SwapEstimate {
  poolAddress: string;
  direction: 'a_to_b' | 'b_to_a';
  amountIn: bigint;
  amountOut: bigint;
  minReceive: bigint;
  priceImpactPct: number;
  feePaid: bigint;
}

// ─── Pool Queries ───────────────────────────────────────────────────────

export async function getPoolState(
  contractAddress: string,
  useRpc = false,
): Promise<MidnightPool> {
  if (useRpc) {
    const rpcState = await getContractStateRpc(contractAddress);
    const state =
      typeof rpcState === 'object' && rpcState !== null
        ? (rpcState as Record<string, unknown>)
        : {};

    return {
      contractAddress,
      tokenA: (state.token_a as string) ?? 'DUST',
      tokenB: (state.token_b as string) ?? '',
      reserveA: BigInt((state.reserve_a as string | number) ?? 0),
      reserveB: BigInt((state.reserve_b as string | number) ?? 0),
      feeNumerator: Number(state.fee_numerator ?? 30),
      feeDenominator: Number(state.fee_denominator ?? 10_000),
      poolType: (state.pool_type as string) ?? 'constant_product',
    };
  }

  const cs = await getContractState(contractAddress);
  const state =
    typeof cs.state === 'object' && cs.state !== null
      ? (cs.state as Record<string, unknown>)
      : {};

  return {
    contractAddress: cs.address,
    tokenA: (state.token_a as string) ?? 'DUST',
    tokenB: (state.token_b as string) ?? '',
    reserveA: BigInt((state.reserve_a as string | number) ?? 0),
    reserveB: BigInt((state.reserve_b as string | number) ?? 0),
    feeNumerator: Number(state.fee_numerator ?? 30),
    feeDenominator: Number(state.fee_denominator ?? 10_000),
    poolType: (state.pool_type as string) ?? 'constant_product',
  };
}

export function poolFeePct(pool: MidnightPool): number {
  return (pool.feeNumerator / pool.feeDenominator) * 100;
}

export function poolExplorerLink(pool: MidnightPool): string {
  return explorerLink(`/contract/${pool.contractAddress}`);
}

// ─── Pool Discovery ─────────────────────────────────────────────────────

export async function discoverPools(
  _tokenId?: string,
): Promise<MidnightPool[]> {
  // Pool discovery depends on each DEX's indexing strategy.
  // Some maintain a registry contract; others require scanning for known patterns.
  // This will be populated once DEX mainnet deployments go live.
  console.warn(
    'Pool discovery is a placeholder — Midnight DEX mainnet deployments ' +
      'are still emerging. Check GalaxySwap and Pulse Finance for updates.',
  );
  return [];
}

// ─── Swap Estimation ────────────────────────────────────────────────────

export function estimateSwap(
  pool: MidnightPool,
  amountIn: bigint,
  direction: 'a_to_b' | 'b_to_a' = 'a_to_b',
  slippagePct = 0.5,
): SwapEstimate {
  const reserveIn =
    direction === 'a_to_b' ? pool.reserveA : pool.reserveB;
  const reserveOut =
    direction === 'a_to_b' ? pool.reserveB : pool.reserveA;

  const effectiveIn =
    (amountIn * BigInt(pool.feeDenominator - pool.feeNumerator)) /
    BigInt(pool.feeDenominator);
  const feePaid = amountIn - effectiveIn;

  if (reserveIn + effectiveIn === 0n) {
    return {
      poolAddress: pool.contractAddress,
      direction,
      amountIn,
      amountOut: 0n,
      minReceive: 0n,
      priceImpactPct: 100,
      feePaid,
    };
  }

  const amountOut =
    (reserveOut * effectiveIn) / (reserveIn + effectiveIn);

  const midPrice =
    reserveIn > 0n
      ? Number(reserveOut) / Number(reserveIn)
      : 0;
  const execPrice =
    amountIn > 0n ? Number(amountOut) / Number(amountIn) : 0;
  const priceImpact =
    midPrice > 0 ? Math.abs(1 - execPrice / midPrice) * 100 : 0;

  const slippageMultiplier = 1 - slippagePct / 100;
  const minReceive = BigInt(
    Math.floor(Number(amountOut) * slippageMultiplier),
  );

  return {
    poolAddress: pool.contractAddress,
    direction,
    amountIn,
    amountOut,
    minReceive,
    priceImpactPct: priceImpact,
    feePaid,
  };
}

// ─── Swap Execution ─────────────────────────────────────────────────────

/**
 * Execute a swap on a Midnight DEX.
 *
 * Full execution requires:
 *   1. The DEX's compiled Compact contract artifacts
 *   2. A running proof server for ZK proof generation
 *   3. An initialized wallet with sufficient balances
 *
 * This will be wired once DEX contracts are deployed on mainnet.
 * For now, use estimateSwap() for price discovery.
 */
export async function executeSwap(
  _poolAddress: string,
  _amountIn: bigint,
  _minReceive: bigint,
  _direction: 'a_to_b' | 'b_to_a' = 'a_to_b',
): Promise<{ txHash: string }> {
  throw new Error(
    'Swap execution requires a deployed DEX contract on Midnight mainnet. ' +
      'Use estimateSwap() for price estimation. ' +
      'Check GalaxySwap (galaxyswap.io) and Pulse Finance for mainnet launch updates.',
  );
}

// ─── DEX Ecosystem Info ─────────────────────────────────────────────────

export interface DexProject {
  name: string;
  description: string;
  url: string;
  status: string;
}

export function getKnownDexProjects(): DexProject[] {
  return [
    {
      name: 'GalaxySwap',
      description: 'Major DeFi protocol on Midnight',
      url: 'https://galaxyswap.io',
      status: 'waitlist',
    },
    {
      name: 'Pulse Finance',
      description: 'Batcher-free atomic AMM (open-source Compact)',
      url: 'https://github.com/pulse-finance/midnight-dex-contract',
      status: 'development',
    },
    {
      name: 'Sevryn Labs',
      description: 'ZK bonding curve where balances stay private',
      url: 'https://github.com/sevryn-labs/midnight-bonding-curve',
      status: 'development',
    },
    {
      name: 'Arcane Finance',
      description: 'DEX proof of concept',
      url: 'https://github.com/arcane-finance-defi/midnight-dex-poc',
      status: 'poc',
    },
  ];
}
