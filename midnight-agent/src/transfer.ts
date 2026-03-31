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
import type {
  UnshieldedTokenTransfer,
  ShieldedTokenTransfer,
  CombinedTokenTransfer,
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
    const txId = await wallet.facade.submitTransaction(finalized);

    return { success: true, txHash: txId };
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
    const txId = await wallet.facade.submitTransaction(finalized);

    return { success: true, txHash: txId };
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
    const txId = await wallet.facade.submitTransaction(finalized);

    return { success: true, txHash: txId };
  } catch (e) {
    return {
      success: false,
      error: e instanceof Error ? e.message : String(e),
    };
  }
}

// ─── DUST Management ────────────────────────────────────────────────────

export async function registerNightForDust(
  wallet: InitializedWallet,
): Promise<TransferResult> {
  try {
    const state = await wallet.facade.waitForSyncedState();
    const availableCoins = state.unshielded.availableCoins;

    if (!availableCoins.length) {
      return { success: false, error: 'No unshielded NIGHT coins available to register' };
    }

    const recipe = await wallet.facade.registerNightUtxosForDustGeneration(
      availableCoins,
      wallet.keystore.getPublicKey(),
      (payload) => wallet.keystore.signData(payload),
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
