/**
 * Midnight Wallet — HD key derivation, address encoding, and wallet lifecycle.
 *
 * Uses the official Midnight Wallet SDK with BIP-32/BIP-44/CIP-1852 key derivation.
 * Derivation path: m / 44' / 2400' / account' / role / index
 *
 * Roles:
 *   0 (NightExternal) — Unshielded operations (NIGHT transfers)
 *   3 (Zswap)         — Shielded operations (ZK-proven transfers)
 *   4 (Dust)          — DUST token operations (fee payment)
 *
 * Wallet types:
 *   UnshieldedWallet — Manages NIGHT and unshielded tokens (UTxO model)
 *   ShieldedWallet   — Manages shielded tokens with ZK proofs
 *   DustWallet       — Manages DUST for transaction fees
 */

import { WebSocket } from 'ws';
if (!globalThis.WebSocket) {
  (globalThis as any).WebSocket = WebSocket;
}

import { randomBytes } from 'crypto';
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'fs';
import { join } from 'path';
import { homedir } from 'os';
import * as ledger from '@midnight-ntwrk/ledger-v8';
import { type AccountKey, HDWallet, type Role, Roles } from '@midnight-ntwrk/wallet-sdk-hd';
import {
  MidnightBech32m,
  UnshieldedAddress,
  ShieldedAddress,
  DustAddress,
  ShieldedCoinPublicKey,
  ShieldedEncryptionPublicKey,
} from '@midnight-ntwrk/wallet-sdk-address-format';
import {
  WalletFacade,
  type DefaultConfiguration,
  type TermsAndConditions,
} from '@midnight-ntwrk/wallet-sdk-facade';
import { ShieldedWallet } from '@midnight-ntwrk/wallet-sdk-shielded';
import {
  createKeystore,
  PublicKey,
  UnshieldedWallet,
  type UnshieldedKeystore,
} from '@midnight-ntwrk/wallet-sdk-unshielded-wallet';
import {
  InMemoryTransactionHistoryStorage,
  TransactionHistoryStorage as TxHistoryStorage,
} from '@midnight-ntwrk/wallet-sdk-abstractions';
import { DustWallet } from '@midnight-ntwrk/wallet-sdk-dust-wallet';
import { setNetworkId } from '@midnight-ntwrk/midnight-js-network-id';
import { getConfig, type NetworkId } from './config.js';

// ─── Key Derivation ─────────────────────────────────────────────────────

export interface DerivedKeys {
  shielded: {
    seed: Buffer;
    keys: ReturnType<typeof ledger.ZswapSecretKeys.fromSeed>;
  };
  dust: {
    seed: Buffer;
    key: ReturnType<typeof ledger.DustSecretKey.fromSeed>;
  };
  unshielded: Buffer;
}

function deriveRoleKey(
  accountKey: AccountKey,
  role: Role,
  addressIndex = 0,
): Buffer {
  const result = accountKey.selectRole(role).deriveKeyAt(addressIndex);
  if (result.type === 'keyDerived') {
    return Buffer.from(result.key);
  }
  // Small chance of derivation failure — retry with next index per BIP-44
  return deriveRoleKey(accountKey, role, addressIndex + 1);
}

export function deriveAllKeys(seed: Uint8Array): DerivedKeys {
  const hdWallet = HDWallet.fromSeed(seed);
  if (hdWallet.type !== 'seedOk') {
    throw new Error(`Failed to derive HD wallet from seed: ${hdWallet.type}`);
  }

  const account = hdWallet.hdWallet.selectAccount(0);
  const shieldedSeed = deriveRoleKey(account, Roles.Zswap);
  const dustSeed = deriveRoleKey(account, Roles.Dust);
  const unshieldedKey = deriveRoleKey(account, Roles.NightExternal);

  hdWallet.hdWallet.clear();

  return {
    shielded: {
      seed: shieldedSeed,
      keys: ledger.ZswapSecretKeys.fromSeed(shieldedSeed),
    },
    dust: {
      seed: dustSeed,
      key: ledger.DustSecretKey.fromSeed(dustSeed),
    },
    unshielded: unshieldedKey,
  };
}

// ─── Address Encoding ───────────────────────────────────────────────────

export interface WalletAddresses {
  unshielded: string;
  shielded: string;
  dust: string;
}

export function encodeAddresses(
  keys: DerivedKeys,
  networkId: NetworkId,
): WalletAddresses {
  // Unshielded
  const verifyingKey = ledger.signatureVerifyingKey(
    keys.unshielded.toString('hex'),
  );
  const unshieldedAddr = new UnshieldedAddress(
    Buffer.from(ledger.addressFromKey(verifyingKey), 'hex'),
  );
  const unshielded = MidnightBech32m.encode(networkId, unshieldedAddr).toString();

  // Shielded
  const shieldedAddr = new ShieldedAddress(
    new ShieldedCoinPublicKey(
      Buffer.from(keys.shielded.keys.coinPublicKey, 'hex'),
    ),
    new ShieldedEncryptionPublicKey(
      Buffer.from(keys.shielded.keys.encryptionPublicKey, 'hex'),
    ),
  );
  const shielded = MidnightBech32m.encode(networkId, shieldedAddr).toString();

  // DUST
  const dustAddr = new DustAddress(keys.dust.key.publicKey);
  const dust = MidnightBech32m.encode(networkId, dustAddr).toString();

  return { unshielded, shielded, dust };
}

// ─── Seed Management ────────────────────────────────────────────────────

const DEFAULT_WALLET_DIR = join(homedir(), '.agent-lump', 'midnight');

function getWalletDir(): string {
  const dir = process.env.MIDNIGHT_WALLET_DIR ?? DEFAULT_WALLET_DIR;
  if (!existsSync(dir)) {
    mkdirSync(dir, { recursive: true, mode: 0o700 });
  }
  return dir;
}

export function generateSeed(): Buffer {
  return randomBytes(32);
}

export function saveSeed(seed: Buffer, walletDir?: string): string {
  const dir = walletDir ?? getWalletDir();
  const seedPath = join(dir, 'seed.hex');
  writeFileSync(seedPath, seed.toString('hex'), { mode: 0o600 });
  return seedPath;
}

export function loadSeed(walletDir?: string): Buffer {
  const envSeed = process.env.MIDNIGHT_WALLET_SEED;
  if (envSeed) {
    return Buffer.from(envSeed, 'hex');
  }

  const dir = walletDir ?? getWalletDir();
  const seedPath = join(dir, 'seed.hex');
  if (!existsSync(seedPath)) {
    throw new Error(
      `No wallet seed found. Either:\n` +
        `  1. Set MIDNIGHT_WALLET_SEED in .env\n` +
        `  2. Run: midnight-agent wallet create\n` +
        `  3. Place seed hex in ${seedPath}`,
    );
  }
  return Buffer.from(readFileSync(seedPath, 'utf-8').trim(), 'hex');
}

// ─── Wallet Creation ────────────────────────────────────────────────────

export interface WalletInfo {
  networkId: NetworkId;
  addresses: WalletAddresses;
  seedPath: string;
}

export function createWallet(walletDir?: string): WalletInfo {
  const config = getConfig();
  const seed = generateSeed();
  const seedPath = saveSeed(seed, walletDir);
  const keys = deriveAllKeys(seed);
  const addresses = encodeAddresses(keys, config.networkId);

  // Clear seed from memory
  seed.fill(0);

  return {
    networkId: config.networkId,
    addresses,
    seedPath,
  };
}

export function getWalletInfo(walletDir?: string): WalletInfo {
  const config = getConfig();
  const seed = loadSeed(walletDir);
  const keys = deriveAllKeys(seed);
  const addresses = encodeAddresses(keys, config.networkId);

  seed.fill(0);

  const dir = walletDir ?? getWalletDir();
  return {
    networkId: config.networkId,
    addresses,
    seedPath: join(dir, 'seed.hex'),
  };
}

// ─── Wallet Facade (Full SDK Wallet) ────────────────────────────────────

export interface InitializedWallet {
  facade: WalletFacade;
  keys: DerivedKeys;
  keystore: UnshieldedKeystore;
  addresses: WalletAddresses;
}

export async function initWallet(
  walletDir?: string,
  options?: { waitForSync?: boolean; syncTimeoutMs?: number },
): Promise<InitializedWallet> {
  const config = getConfig();
  setNetworkId(config.networkId);

  const seed = loadSeed(walletDir);
  const keys = deriveAllKeys(seed);
  seed.fill(0);

  const addresses = encodeAddresses(keys, config.networkId);
  const unshieldedKeystore = createKeystore(
    keys.unshielded,
    config.networkId,
  );

  const configuration: DefaultConfiguration = {
    networkId: config.networkId,
    costParameters: {
      additionalFeeOverhead: 300_000_000_000_000n,
      feeBlocksMargin: 5,
    },
    relayURL: new URL(config.rpcWssUrl),
    provingServerUrl: new URL(config.proverUrl),
    indexerClientConnection: {
      indexerHttpUrl: config.indexerUrl,
      indexerWsUrl: config.indexerWsUrl,
    },
    txHistoryStorage: new InMemoryTransactionHistoryStorage(
      TxHistoryStorage.TransactionHistoryCommonSchema,
    ),
  };

  const facade = await WalletFacade.init({
    configuration,
    shielded: (cfg) =>
      ShieldedWallet(cfg).startWithSecretKeys(keys.shielded.keys),
    unshielded: (cfg) =>
      UnshieldedWallet(cfg).startWithPublicKey(
        PublicKey.fromKeyStore(unshieldedKeystore),
      ),
    dust: (cfg) =>
      DustWallet(cfg).startWithSecretKey(
        keys.dust.key,
        ledger.LedgerParameters.initialParameters().dust,
      ),
  });

  await facade.start(keys.shielded.keys, keys.dust.key);

  if (options?.waitForSync !== false) {
    const envMs = parseInt(process.env.MIDNIGHT_WALLET_SYNC_TIMEOUT_MS ?? '', 10);
    const syncTimeoutMs =
      options?.syncTimeoutMs ??
      (Number.isFinite(envMs) && envMs > 0 ? envMs : 600_000);
    console.log(`  Waiting for wallet sync (timeout: ${syncTimeoutMs / 1000}s)...`);

    const syncResult = await Promise.race([
      facade.waitForSyncedState().then(() => 'synced' as const),
      waitForSyncWithProgress(facade, syncTimeoutMs),
    ]);
    if (syncResult === 'timeout') {
      console.log('  Wallet sync timed out — proceeding with partial state.');
    } else {
      console.log('  Wallet synced.');
    }
  }

  return { facade, keys, keystore: unshieldedKeystore, addresses };
}

function waitForSyncWithProgress(
  facade: WalletFacade,
  timeoutMs: number,
): Promise<'timeout'> {
  return new Promise((resolve) => {
    let lastLog = 0;
    const interval = setInterval(() => {
      lastLog++;
    }, 1000);

    const sub = facade.state().subscribe({
      next: (state) => {
        if (lastLog < 10) return;
        lastLog = 0;
        const sh = state.shielded.progress;
        const du = state.dust.progress;
        const shPct = sh && sh.highestRelevantWalletIndex
          ? ((Number(sh.appliedIndex) / Number(sh.highestRelevantWalletIndex)) * 100).toFixed(1)
          : '?';
        const duPct = du && du.highestRelevantWalletIndex
          ? ((Number(du.appliedIndex) / Number(du.highestRelevantWalletIndex)) * 100).toFixed(1)
          : '?';
        console.log(
          `  Sync progress: shielded ${shPct}% (${sh?.appliedIndex}/${sh?.highestRelevantWalletIndex})` +
          ` | dust ${duPct}% (${du?.appliedIndex}/${du?.highestRelevantWalletIndex})`,
        );
      },
    });

    setTimeout(() => {
      clearInterval(interval);
      sub.unsubscribe();
      resolve('timeout');
    }, timeoutMs);
  });
}

/**
 * Lightweight wallet init that only derives keys — no chain sync.
 * Use with gas-sponsored flows where WalletFacade isn't needed.
 */
export function initWalletKeysOnly(walletDir?: string): InitializedWallet {
  const config = getConfig();
  setNetworkId(config.networkId);
  const seed = loadSeed(walletDir);
  const keys = deriveAllKeys(seed);
  seed.fill(0);

  const addresses = encodeAddresses(keys, config.networkId);
  const unshieldedKeystore = createKeystore(keys.unshielded, config.networkId);

  // Return a stub facade — callers using gas sponsorship won't call facade methods
  return {
    facade: null as unknown as WalletFacade,
    keys,
    keystore: unshieldedKeystore,
    addresses,
  };
}

export async function stopWallet(wallet: InitializedWallet): Promise<void> {
  await wallet.facade.stop();
}

// ─── Balance Queries ────────────────────────────────────────────────────

export interface WalletBalances {
  shielded: Record<string, bigint>;
  unshielded: Record<string, bigint>;
  dustCoinCount: number;
  dustBalance: bigint;
}

export async function getBalances(
  wallet: InitializedWallet,
): Promise<WalletBalances> {
  const state = await wallet.facade.waitForSyncedState();
  return {
    shielded: state.shielded.balances as Record<string, bigint>,
    unshielded: state.unshielded.balances as Record<string, bigint>,
    dustCoinCount: state.dust.totalCoins.length,
    dustBalance: state.dust.balance(new Date()),
  };
}

// ─── Terms & Conditions ─────────────────────────────────────────────────

/**
 * Fetch the current Midnight Terms and Conditions from the network indexer.
 *
 * Static SDK call — does not require an initialized wallet. Returns the document
 * URL plus a SHA-256 hash for content verification. Wallet builders should display
 * the T&C to end users before initialization and persist the acknowledged hash.
 */
export async function getTermsAndConditions(): Promise<TermsAndConditions> {
  const config = getConfig();
  return WalletFacade.fetchTermsAndConditions({
    indexerClientConnection: {
      indexerHttpUrl: config.indexerUrl,
      indexerWsUrl: config.indexerWsUrl,
    },
  });
}
