/**
 * Token transfers on Midnight — shielded, unshielded, and combined.
 *
 * Transfer types:
 *   Unshielded — NIGHT and unshielded tokens, visible on-chain, Schnorr-signed
 *   Shielded   — Privacy-preserving with ZK proofs, amounts/participants hidden
 *   Combined   — Atomic transactions mixing both types
 *
 * All transfers require an initialized WalletFacade with synced state.
 */

import * as ledger from '@midnight-ntwrk/ledger-v8';
import {
  MidnightBech32m,
  UnshieldedAddress,
  ShieldedAddress,
} from '@midnight-ntwrk/wallet-sdk-address-format';
import {
  BalancingRecipe,
  type UnshieldedTokenTransfer,
  type ShieldedTokenTransfer,
  type CombinedTokenTransfer,
} from '@midnight-ntwrk/wallet-sdk-facade';
import type { InitializedWallet } from './wallet.js';
import { getConfig } from './config.js';

export interface TransferOutput {
  amount: bigint;
  receiverAddress: string;
  tokenType?: string;
}

export interface TransferResult {
  success: boolean;
  txHash?: string;
  error?: string;
  /** Fee paid by the submitted transaction in DUST specks (excludes balancing tx). */
  fee?: bigint;
}

export interface FeeEstimate {
  /** Fee for the transfer transaction only, in DUST specks. */
  transactionFee: bigint;
  /** Total fee including the balancing transaction, in DUST specks. */
  totalFee: bigint;
}

function parseUnshieldedAddress(bech32: string): UnshieldedAddress {
  const parsed = MidnightBech32m.parse(bech32);
  const networkId = getConfig().networkId;
  return parsed.decode(UnshieldedAddress, networkId);
}

function parseShieldedAddress(bech32: string): ShieldedAddress {
  const parsed = MidnightBech32m.parse(bech32);
  const networkId = getConfig().networkId;
  return parsed.decode(ShieldedAddress, networkId);
}

// ─── Unshielded Transfers ───────────────────────────────────────────────

export async function transferUnshielded(
  wallet: InitializedWallet,
  outputs: TransferOutput[],
  ttlMinutes = 30,
): Promise<TransferResult> {
  try {
    const ttl = new Date(Date.now() + ttlMinutes * 60 * 1000);

    const transfer: UnshieldedTokenTransfer = {
      type: 'unshielded',
      outputs: outputs.map((o) => ({
        amount: o.amount,
        receiverAddress: parseUnshieldedAddress(o.receiverAddress),
        type: o.tokenType ?? ledger.nativeToken().raw,
      })),
    };

    const recipe = await wallet.facade.transferTransaction(
      [transfer],
      {
        shieldedSecretKeys: wallet.keys.shielded.keys,
        dustSecretKey: wallet.keys.dust.key,
      },
      { ttl },
    );

    const signed = await wallet.facade.signRecipe(recipe, (payload) =>
      wallet.keystore.signData(payload),
    );
    const finalized = await wallet.facade.finalizeRecipe(signed);
    const fee = await wallet.facade.calculateTransactionFee(finalized);
    const txId = await wallet.facade.submitTransaction(finalized);

    return { success: true, txHash: txId, fee };
  } catch (e) {
    return {
      success: false,
      error: e instanceof Error ? e.message : String(e),
    };
  }
}

// ─── Shielded Transfers ─────────────────────────────────────────────────

export async function transferShielded(
  wallet: InitializedWallet,
  outputs: TransferOutput[],
  ttlMinutes = 30,
): Promise<TransferResult> {
  try {
    const ttl = new Date(Date.now() + ttlMinutes * 60 * 1000);

    const transfer: ShieldedTokenTransfer = {
      type: 'shielded',
      outputs: outputs.map((o) => ({
        amount: o.amount,
        receiverAddress: parseShieldedAddress(o.receiverAddress),
        type: o.tokenType ?? ledger.nativeToken().raw,
      })),
    };

    const recipe = await wallet.facade.transferTransaction(
      [transfer],
      {
        shieldedSecretKeys: wallet.keys.shielded.keys,
        dustSecretKey: wallet.keys.dust.key,
      },
      { ttl },
    );

    const finalized = await wallet.facade.finalizeRecipe(recipe);
    const fee = await wallet.facade.calculateTransactionFee(finalized);
    const txId = await wallet.facade.submitTransaction(finalized);

    return { success: true, txHash: txId, fee };
  } catch (e) {
    return {
      success: false,
      error: e instanceof Error ? e.message : String(e),
    };
  }
}

// ─── Combined Transfers ─────────────────────────────────────────────────

export interface CombinedTransferParams {
  unshielded: TransferOutput[];
  shielded: TransferOutput[];
}

export async function transferCombined(
  wallet: InitializedWallet,
  params: CombinedTransferParams,
  ttlMinutes = 30,
): Promise<TransferResult> {
  try {
    const ttl = new Date(Date.now() + ttlMinutes * 60 * 1000);

    const transferParts: CombinedTokenTransfer[] = [];

    if (params.unshielded.length > 0) {
      transferParts.push({
        type: 'unshielded',
        outputs: params.unshielded.map((o) => ({
          amount: o.amount,
          receiverAddress: parseUnshieldedAddress(o.receiverAddress),
          type: o.tokenType ?? ledger.nativeToken().raw,
        })),
      } satisfies UnshieldedTokenTransfer);
    }

    if (params.shielded.length > 0) {
      transferParts.push({
        type: 'shielded',
        outputs: params.shielded.map((o) => ({
          amount: o.amount,
          receiverAddress: parseShieldedAddress(o.receiverAddress),
          type: o.tokenType ?? ledger.nativeToken().raw,
        })),
      } satisfies ShieldedTokenTransfer);
    }

    const recipe = await wallet.facade.transferTransaction(
      transferParts,
      {
        shieldedSecretKeys: wallet.keys.shielded.keys,
        dustSecretKey: wallet.keys.dust.key,
      },
      { ttl },
    );

    const signed = await wallet.facade.signRecipe(recipe, (payload) =>
      wallet.keystore.signData(payload),
    );
    const finalized = await wallet.facade.finalizeRecipe(signed);
    const fee = await wallet.facade.calculateTransactionFee(finalized);
    const txId = await wallet.facade.submitTransaction(finalized);

    return { success: true, txHash: txId, fee };
  } catch (e) {
    return {
      success: false,
      error: e instanceof Error ? e.message : String(e),
    };
  }
}

// ─── Fee Estimation ─────────────────────────────────────────────────────

/**
 * Estimate fees for a hypothetical transfer without submitting.
 *
 * Builds the transfer recipe and queries both:
 *   - calculateTransactionFee — the transfer transaction's fee in isolation
 *   - estimateTransactionFee  — total fee including the balancing transaction
 *
 * Use the total to budget; use the per-transaction figure to compare against the
 * fee actually paid (returned in TransferResult.fee after submission).
 */
export async function estimateTransferFee(
  wallet: InitializedWallet,
  params: CombinedTransferParams,
  ttlMinutes = 30,
): Promise<FeeEstimate> {
  const ttl = new Date(Date.now() + ttlMinutes * 60 * 1000);
  const transferParts: CombinedTokenTransfer[] = [];

  if (params.unshielded.length > 0) {
    transferParts.push({
      type: 'unshielded',
      outputs: params.unshielded.map((o) => ({
        amount: o.amount,
        receiverAddress: parseUnshieldedAddress(o.receiverAddress),
        type: o.tokenType ?? ledger.nativeToken().raw,
      })),
    } satisfies UnshieldedTokenTransfer);
  }

  if (params.shielded.length > 0) {
    transferParts.push({
      type: 'shielded',
      outputs: params.shielded.map((o) => ({
        amount: o.amount,
        receiverAddress: parseShieldedAddress(o.receiverAddress),
        type: o.tokenType ?? ledger.nativeToken().raw,
      })),
    } satisfies ShieldedTokenTransfer);
  }

  const recipe = await wallet.facade.transferTransaction(
    transferParts,
    {
      shieldedSecretKeys: wallet.keys.shielded.keys,
      dustSecretKey: wallet.keys.dust.key,
    },
    { ttl },
  );

  const [tx] = BalancingRecipe.getTransactions(recipe);
  const transactionFee = await wallet.facade.calculateTransactionFee(tx);
  const totalFee = await wallet.facade.estimateTransactionFee(
    tx,
    wallet.keys.dust.key,
    { ttl },
  );

  return { transactionFee, totalFee };
}

// ─── DUST Management ────────────────────────────────────────────────────

export interface DustStatus {
  nightBalance: bigint;
  dustBalance: bigint;
  dustCoinCount: number;
  registeredCoinCount: number;
  unregisteredCoinCount: number;
  hasNight: boolean;
  hasDust: boolean;
  isRegistered: boolean;
}

const SPECK_PER_DUST = 1_000_000_000_000_000n;
const STAR_PER_NIGHT = 1_000_000n;

export function formatNight(raw: bigint): string {
  const whole = raw / STAR_PER_NIGHT;
  const frac = (raw % STAR_PER_NIGHT).toString().padStart(6, '0');
  return `${whole.toLocaleString()}.${frac}`;
}

export function formatDust(raw: bigint): string {
  const whole = raw / SPECK_PER_DUST;
  const frac = (raw % SPECK_PER_DUST).toString().padStart(15, '0').slice(0, 6);
  return `${whole.toLocaleString()}.${frac}`;
}

export async function getDustStatus(
  wallet: InitializedWallet,
): Promise<DustStatus> {
  const state = await wallet.facade.waitForSyncedState();
  const nightBalance = state.unshielded.balances[ledger.nativeToken().raw] ?? 0n;
  const dustBalance = state.dust.balance(new Date());
  const dustCoinCount = state.dust.totalCoins.length;

  const allCoins = state.unshielded.availableCoins;
  const unregistered = allCoins.filter(
    (coin: any) => coin.meta?.registeredForDustGeneration !== true,
  );

  return {
    nightBalance,
    dustBalance,
    dustCoinCount,
    registeredCoinCount: allCoins.length - unregistered.length,
    unregisteredCoinCount: unregistered.length,
    hasNight: nightBalance > 0n,
    hasDust: dustBalance > 0n,
    isRegistered: unregistered.length === 0 && allCoins.length > 0,
  };
}

export async function registerNightForDust(
  wallet: InitializedWallet,
  options?: { waitForDust?: boolean; pollIntervalMs?: number; timeoutMs?: number },
): Promise<TransferResult & { dustStatus?: DustStatus }> {
  try {
    const state = await wallet.facade.waitForSyncedState();

    // Check if DUST is already available
    const dustBalance = state.dust.balance(new Date());
    if (state.dust.totalCoins.length > 0 && dustBalance > 0n) {
      const status = await getDustStatus(wallet);
      return {
        success: true,
        txHash: '(already registered)',
        dustStatus: status,
      };
    }

    // Find unregistered NIGHT UTXOs
    const unregistered = state.unshielded.availableCoins.filter(
      (coin: any) => coin.meta?.registeredForDustGeneration !== true,
    );

    if (!unregistered.length && !state.unshielded.availableCoins.length) {
      return { success: false, error: 'No unshielded NIGHT coins available. Fund your wallet first.' };
    }

    if (!unregistered.length) {
      // All coins registered but no DUST yet — just need to wait
      console.log('  All NIGHT already registered. Waiting for DUST to accrue...');
    } else {
      // Register unregistered coins
      const recipe = await wallet.facade.registerNightUtxosForDustGeneration(
        unregistered,
        wallet.keystore.getPublicKey(),
        (payload) => wallet.keystore.signData(payload),
      );

      const finalized = await wallet.facade.finalizeRecipe(recipe);
      const txId = await wallet.facade.submitTransaction(finalized);
      console.log(`  Registration tx submitted: ${txId}`);
    }

    // Optionally wait for DUST to start accruing
    if (options?.waitForDust !== false) {
      const pollInterval = options?.pollIntervalMs ?? 5_000;
      const timeout = options?.timeoutMs ?? 180_000;
      const deadline = Date.now() + timeout;

      console.log('  Waiting for DUST to accrue (may take 1-2 minutes)...');
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, pollInterval));
        const current = await wallet.facade.waitForSyncedState();
        const bal = current.dust.balance(new Date());
        if (bal > 0n) {
          const status = await getDustStatus(wallet);
          console.log(`  DUST available: ${formatDust(bal)}`);
          return { success: true, dustStatus: status };
        }
      }
      return { success: true, error: 'Registration submitted but DUST has not accrued yet. Check again later.' };
    }

    const status = await getDustStatus(wallet);
    return { success: true, dustStatus: status };
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    // Error 138 means already registered
    if (msg.includes('138') || msg.includes('already registered')) {
      const status = await getDustStatus(wallet);
      return { success: true, txHash: '(already registered)', dustStatus: status };
    }
    return { success: false, error: msg };
  }
}

export async function deregisterNightFromDust(
  wallet: InitializedWallet,
  coinIndex = 0,
): Promise<TransferResult> {
  try {
    const state = await wallet.facade.waitForSyncedState();
    const coin = state.unshielded.availableCoins[coinIndex];

    if (!coin) {
      return { success: false, error: `No coin at index ${coinIndex}` };
    }

    const recipe = await wallet.facade.deregisterFromDustGeneration(
      [coin],
      wallet.keystore.getPublicKey(),
      (payload) => wallet.keystore.signData(payload),
    );

    const balanced = await wallet.facade.balanceUnprovenTransaction(
      recipe.transaction,
      {
        shieldedSecretKeys: wallet.keys.shielded.keys,
        dustSecretKey: wallet.keys.dust.key,
      },
      {
        ttl: new Date(Date.now() + 30 * 60 * 1000),
        tokenKindsToBalance: ['dust'],
      },
    );

    const finalized = await wallet.facade.finalizeRecipe(balanced);
    const txId = await wallet.facade.submitTransaction(finalized);

    return { success: true, txHash: txId };
  } catch (e) {
    return {
      success: false,
      error: e instanceof Error ? e.message : String(e),
    };
  }
}

// ─── Atomic Swaps ───────────────────────────────────────────────────────

export interface SwapOffer {
  offerTokenType: string;
  offerAmount: bigint;
  requestTokenType: string;
  requestAmount: bigint;
  receiverAddress: string;
}

export async function createSwapOffer(
  wallet: InitializedWallet,
  offer: SwapOffer,
  ttlMinutes = 30,
): Promise<ledger.FinalizedTransaction> {
  const ttl = new Date(Date.now() + ttlMinutes * 60 * 1000);

  const recipe = await wallet.facade.initSwap(
    { shielded: { [offer.offerTokenType]: offer.offerAmount } },
    [
      {
        type: 'shielded' as const,
        outputs: [
          {
            type: offer.requestTokenType,
            amount: offer.requestAmount,
            receiverAddress: parseShieldedAddress(offer.receiverAddress),
          },
        ],
      },
    ],
    {
      shieldedSecretKeys: wallet.keys.shielded.keys,
      dustSecretKey: wallet.keys.dust.key,
    },
    { ttl },
  );

  return wallet.facade.finalizeRecipe(recipe);
}

export async function completeSwap(
  wallet: InitializedWallet,
  partialTx: ledger.FinalizedTransaction,
  ttlMinutes = 30,
): Promise<TransferResult> {
  try {
    const ttl = new Date(Date.now() + ttlMinutes * 60 * 1000);

    const recipe = await wallet.facade.balanceFinalizedTransaction(
      partialTx,
      {
        shieldedSecretKeys: wallet.keys.shielded.keys,
        dustSecretKey: wallet.keys.dust.key,
      },
      { ttl },
    );

    const finalized = await wallet.facade.finalizeRecipe(recipe);
    const txId = await wallet.facade.submitTransaction(finalized);

    return { success: true, txHash: txId };
  } catch (e) {
    return {
      success: false,
      error: e instanceof Error ? e.message : String(e),
    };
  }
}
