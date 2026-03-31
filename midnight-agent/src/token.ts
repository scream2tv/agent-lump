/**
 * Token operations on Midnight — deploy, mint, and manage FungibleToken contracts.
 *
 * Midnight tokens are Compact smart contracts (not native assets like Cardano).
 * The OpenZeppelin FungibleToken module provides an ERC-20-like standard:
 *   https://github.com/OpenZeppelin/compact-contracts
 *
 * Token lifecycle:
 *   1. Write Compact contract importing FungibleToken
 *   2. Compile with `compact compile` (generates ZK circuits + TS API)
 *   3. Deploy contract to Midnight via proof server + wallet
 *   4. Interact via the generated TypeScript contract API
 *
 * Current limitations (Compact toolchain 0.30.0, ledger v8):
 *   - Contract-to-contract calls not yet supported
 *   - Events not yet supported (planned)
 *   - Value type is Uint<128> (256-bit not available in Compact)
 */

import * as path from 'path';
import { pathToFileURL } from 'url';
import { existsSync } from 'fs';
import type { InitializedWallet } from './wallet.js';
import { getConfig, explorerLink } from './config.js';

// ─── Types ──────────────────────────────────────────────────────────────

export interface TokenMetadata {
  name: string;
  ticker: string;
  description: string;
  decimals: number;
  initialSupply: bigint;
}

export interface DeployedToken {
  contractAddress: string;
  metadata: TokenMetadata;
  explorerUrl: string;
  txHash: string;
}

export interface TokenDeployParams {
  /** Path to the compiled contract directory (output of `compact compile`). Empty string uses default. */
  contractDir: string;
  metadata: TokenMetadata;
  /** ZswapCoinPublicKey of the recipient for the initial supply */
  recipientPublicKey?: string;
}

// ─── Contract Loading ───────────────────────────────────────────────────

const DEFAULT_CONTRACT_DIR = path.resolve(
  import.meta.dirname ?? '.',
  '..',
  'contracts',
  'managed',
  'my_token',
);

export async function loadCompiledContract(contractDir?: string) {
  const dir = (contractDir && contractDir.length > 0) ? contractDir : DEFAULT_CONTRACT_DIR;
  const contractJsPath = path.join(dir, 'contract', 'index.js');

  if (!existsSync(contractJsPath)) {
    throw new Error(
      `Compiled contract not found at ${contractJsPath}.\n` +
        `Run: npm run compact:compile`,
    );
  }

  const contractModule = await import(pathToFileURL(contractJsPath).href);
  return { contractModule, dir };
}

// ─── Deployment ─────────────────────────────────────────────────────────

export async function deployToken(
  wallet: InitializedWallet,
  params: TokenDeployParams,
): Promise<DeployedToken> {
  const { deployContract } = await import('@midnight-ntwrk/midnight-js-contracts');
  const { CompiledContract } = await import('@midnight-ntwrk/compact-js');

  const { contractModule, dir } = await loadCompiledContract(params.contractDir);

  const compiledContract = (CompiledContract as any).make(
    'my_token',
    contractModule.Contract,
  ).pipe(
    (CompiledContract as any).withVacantWitnesses,
    (CompiledContract as any).withCompiledFileAssets(dir),
  );

  const providers = await createContractProviders(wallet, dir);

  // Build the recipient as Either<ZswapCoinPublicKey, ContractAddress> (left = ZswapCoinPublicKey)
  const recipientPubKey = params.recipientPublicKey ?? wallet.keys.shielded.keys.coinPublicKey;
  const recipient = {
    is_left: true,
    left: { bytes: Buffer.from(recipientPubKey, 'hex') },
    right: { bytes: new Uint8Array(32) }, // unused side of Either
  };

  const deployed = await (deployContract as any)(providers, {
    compiledContract,
    privateStateId: 'tokenState',
    initialPrivateState: {},
    args: [
      params.metadata.name,
      params.metadata.ticker,
      BigInt(params.metadata.decimals),
      recipient,
      params.metadata.initialSupply,
    ],
  });

  const contractAddress = deployed.deployTxData.public.contractAddress;

  return {
    contractAddress,
    metadata: params.metadata,
    explorerUrl: explorerLink(`/contract/${contractAddress}`),
    txHash: deployed.deployTxData.public.txId,
  };
}

// ─── Token Interaction ──────────────────────────────────────────────────

export interface TokenContractApi {
  transfer(to: { is_left: boolean; left: { bytes: Uint8Array }; right: { bytes: Uint8Array } }, amount: bigint): Promise<string>;
  balanceOf(account: { is_left: boolean; left: { bytes: Uint8Array }; right: { bytes: Uint8Array } }): Promise<bigint>;
  name(): Promise<string>;
  symbol(): Promise<string>;
  decimals(): Promise<bigint>;
  totalSupply(): Promise<bigint>;
}

export async function connectToken(
  wallet: InitializedWallet,
  contractAddress: string,
  contractDir?: string,
): Promise<TokenContractApi> {
  const { findDeployedContract } = await import('@midnight-ntwrk/midnight-js-contracts');
  const { CompiledContract } = await import('@midnight-ntwrk/compact-js');

  const { contractModule, dir } = await loadCompiledContract(contractDir);

  const compiledContract = (CompiledContract as any).make(
    'my_token',
    contractModule.Contract,
  ).pipe(
    (CompiledContract as any).withVacantWitnesses,
    (CompiledContract as any).withCompiledFileAssets(dir),
  );

  const providers = await createContractProviders(wallet, dir);

  const contract: any = await (findDeployedContract as any)(providers, {
    contractAddress,
    compiledContract,
    privateStateId: 'tokenState',
    initialPrivateState: {},
  });

  return {
    async transfer(to, amount) {
      const tx = await contract.callTx.transfer(to, amount);
      return tx.public.txId;
    },
    async balanceOf(account) {
      const tx = await contract.callTx.balanceOf(account);
      return tx.public.contractState;
    },
    async name() {
      const tx = await contract.callTx.name();
      return tx.public.contractState;
    },
    async symbol() {
      const tx = await contract.callTx.symbol();
      return tx.public.contractState;
    },
    async decimals() {
      const tx = await contract.callTx.decimals();
      return tx.public.contractState;
    },
    async totalSupply() {
      const tx = await contract.callTx.totalSupply();
      return tx.public.contractState;
    },
  };
}

// ─── Contract Providers ─────────────────────────────────────────────────

async function createContractProviders(
  wallet: InitializedWallet,
  zkConfigPath: string,
  options?: { useGasSponsorship?: boolean },
) {
  const { httpClientProofProvider } = await import(
    '@midnight-ntwrk/midnight-js-http-client-proof-provider'
  );
  const { indexerPublicDataProvider } = await import(
    '@midnight-ntwrk/midnight-js-indexer-public-data-provider'
  );
  const { NodeZkConfigProvider } = await import(
    '@midnight-ntwrk/midnight-js-node-zk-config-provider'
  );

  const config = getConfig();
  const zkConfigProvider = new NodeZkConfigProvider(zkConfigPath);
  const useSponsorship = options?.useGasSponsorship ?? true;

  let walletProvider: any;

  if (useSponsorship) {
    // Use remote proof server gas sponsorship — the server handles DUST balancing
    console.log('  Using remote proof server for gas sponsorship...');
    // Create standard proof provider to prove via /prove (sends ZK circuit data)
    const proofProv = httpClientProofProvider(config.proverUrl, zkConfigProvider);

    walletProvider = {
      getCoinPublicKey: () => wallet.keys.shielded.keys.coinPublicKey,
      getEncryptionPublicKey: () => wallet.keys.shielded.keys.encryptionPublicKey,
      async balanceTx(tx: unknown, _ttl?: Date) {
        // Step 1: Prove the tx via /prove (includes ZK circuit data)
        console.log('  Proving transaction via remote prover...');
        const proven = await proofProv.proveTx(tx as any);
        console.log('  Transaction proved successfully.');
        return proven;
      },
      async submitTx(tx: unknown) {
        // Step 2: Send proven tx to /balance-and-submit for DUST sponsorship
        const serialized = serializeTransaction(tx);
        // Debug: check if tx is actually proven
        const txStr = tx && typeof (tx as any).toString === 'function' ? (tx as any).toString() : '';
        const isProven = txStr.includes('proof,') && !txStr.includes('proof-preimage');
        console.log(`  Submitting for DUST balancing (${serialized.length} bytes, proven=${isProven})...`);
        console.log(`  TX header: ${serialized.slice(0, 100).toString('utf8').replace(/[^\x20-\x7E]/g, '?')}`);

        const resp = await fetch(`${config.proverUrl}/balance-and-submit`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/octet-stream' },
          body: serialized,
        });

        if (!resp.ok) {
          const err = await resp.text();
          throw new Error(`Prove-and-submit failed (${resp.status}): ${err}`);
        }

        const result = await resp.json() as { txHash: string; submitted: boolean };
        console.log(`  Proof server submitted tx: ${result.txHash}`);
        return result.txHash;
      },
    };
  } else {
    // Standard SDK flow — requires local DUST balance
    walletProvider = {
      getCoinPublicKey: () => wallet.keys.shielded.keys.coinPublicKey,
      getEncryptionPublicKey: () => wallet.keys.shielded.keys.encryptionPublicKey,
      async balanceTx(tx: unknown, ttl?: Date) {
        const recipe = await wallet.facade.balanceUnboundTransaction(
          tx as never,
          {
            shieldedSecretKeys: wallet.keys.shielded.keys,
            dustSecretKey: wallet.keys.dust.key,
          },
          { ttl: ttl ?? new Date(Date.now() + 30 * 60 * 1000) },
        );

        const signFn = (payload: Uint8Array) =>
          wallet.keystore.signData(payload);

        signTransactionIntents(
          recipe.baseTransaction as TransactionWithIntents,
          signFn,
        );
        if (recipe.balancingTransaction) {
          signTransactionIntents(
            recipe.balancingTransaction as TransactionWithIntents,
            signFn,
          );
        }

        return wallet.facade.finalizeRecipe(recipe);
      },
      submitTx: (tx: unknown) => wallet.facade.submitTransaction(tx as never),
    };
  }

  const privateStateProvider = createInMemoryPrivateStateProvider();

  return {
    privateStateProvider,
    publicDataProvider: indexerPublicDataProvider(
      config.indexerUrl,
      config.indexerWsUrl,
    ),
    zkConfigProvider,
    proofProvider: useSponsorship
      ? createPassthroughProofProvider()
      : httpClientProofProvider(config.proverUrl, zkConfigProvider),
    walletProvider,
    midnightProvider: walletProvider,
  };
}

// ─── Passthrough Proof Provider (for gas-sponsored flow) ───────────────

function createPassthroughProofProvider() {
  // Returns the transaction as-is without proving — the remote /prove-and-submit
  // endpoint handles proving server-side. The SDK expects a proof provider
  // that takes an unproven tx and returns a proven tx, but for the sponsored
  // flow we pass the raw unproven tx all the way to submitTx.
  return {
    async proveTx(tx: unknown): Promise<unknown> {
      return tx; // pass through unproven
    },
  };
}

// ─── Transaction Serialization ─────────────────────────────────────────

function serializeTransaction(tx: unknown): Buffer {
  // The Midnight SDK Transaction objects have a serialize() method
  // that returns a tagged binary representation
  if (tx && typeof (tx as any).serialize === 'function') {
    const bytes = (tx as any).serialize();
    return Buffer.from(bytes);
  }
  // Fallback: try hex encoding
  if (tx && typeof (tx as any).toHex === 'function') {
    return Buffer.from((tx as any).toHex(), 'hex');
  }
  // Fallback: try to get the raw bytes
  if (tx && typeof (tx as any).bytes === 'function') {
    return Buffer.from((tx as any).bytes());
  }
  if (tx instanceof Uint8Array || Buffer.isBuffer(tx)) {
    return Buffer.from(tx);
  }
  throw new Error(`Cannot serialize transaction: unknown type ${typeof tx}`);
}

// ─── In-Memory Private State Provider ───────────────────────────────────

function createInMemoryPrivateStateProvider() {
  const store = new Map<string, unknown>();
  const signingKeys = new Map<string, unknown>();
  let contractAddress: string | null = null;

  return {
    setContractAddress(addr: string) {
      contractAddress = addr;
    },
    async get(key: string) {
      return store.get(`${contractAddress}:${key}`) ?? null;
    },
    async set(key: string, value: unknown) {
      store.set(`${contractAddress}:${key}`, value);
    },
    async remove(key: string) {
      store.delete(`${contractAddress}:${key}`);
    },
    async clear() {
      for (const k of store.keys()) {
        if (k.startsWith(`${contractAddress}:`)) store.delete(k);
      }
    },
    async getSigningKey(key: string) {
      return signingKeys.get(key) ?? null;
    },
    async setSigningKey(key: string, value: unknown) {
      signingKeys.set(key, value);
    },
    async removeSigningKey(key: string) {
      signingKeys.delete(key);
    },
    async clearSigningKeys() {
      signingKeys.clear();
    },
  };
}

// ─── Transaction Signing ────────────────────────────────────────────────

interface TransactionWithIntents {
  intents?: Map<number, IntentLike>;
}

interface IntentLike {
  signatureData(segment: number): Uint8Array;
  fallibleUnshieldedOffer?: OfferLike;
  guaranteedUnshieldedOffer?: OfferLike;
}

interface OfferLike {
  inputs: unknown[];
  signatures: { at(i: number): unknown };
  addSignatures(sigs: unknown[]): OfferLike;
}

function signTransactionIntents(
  tx: TransactionWithIntents,
  signFn: (payload: Uint8Array) => unknown,
): void {
  if (!tx.intents || tx.intents.size === 0) return;

  for (const segment of tx.intents.keys()) {
    const intent = tx.intents.get(segment);
    if (!intent) continue;

    const sigData = intent.signatureData(segment);
    const signature = signFn(sigData);

    if (intent.fallibleUnshieldedOffer) {
      const offer = intent.fallibleUnshieldedOffer;
      const sigs = offer.inputs.map(
        (_: unknown, i: number) => offer.signatures.at(i) ?? signature,
      );
      intent.fallibleUnshieldedOffer = offer.addSignatures(sigs);
    }

    if (intent.guaranteedUnshieldedOffer) {
      const offer = intent.guaranteedUnshieldedOffer;
      const sigs = offer.inputs.map(
        (_: unknown, i: number) => offer.signatures.at(i) ?? signature,
      );
      intent.guaranteedUnshieldedOffer = offer.addSignatures(sigs);
    }
  }
}

// ─── Token Info ─────────────────────────────────────────────────────────

export function getTokenGuide(): string {
  return `
Midnight Token Creation Guide
══════════════════════════════

Prerequisites:
  - Compact toolchain installed (compact compile --version)
  - If not installed:
    curl --proto '=https' --tlsv1.2 -LsSf \\
      https://github.com/midnightntwrk/compact/releases/latest/download/compact-installer.sh | sh
    compact update

This project includes a ready-to-use FungibleToken contract:
  contracts/my_token.compact

It uses OpenZeppelin's FungibleToken module (ERC-20 equivalent) with:
  - Fixed supply minted to a recipient on deployment
  - transfer, transferFrom, approve, allowance circuits
  - balanceOf, totalSupply, name, symbol, decimals queries

1. Compile the contract:
   npm run compact:compile

   This generates ZK circuits, proving/verifying keys, and a TypeScript API
   in contracts/managed/my_token/

2. Deploy via this agent:
   npm run dev -- token deploy --name "My Token" --ticker MTK --supply 1000000

3. Interact with a deployed token:
   npm run dev -- token connect <contract_address>

Contract source: contracts/my_token.compact
OpenZeppelin modules: contracts/compact-contracts/contracts/src/

Resources:
  - OpenZeppelin Compact Contracts: https://github.com/OpenZeppelin/compact-contracts
  - Compact Language Docs: https://docs.midnight.network/compact
  - Compact Toolchain: https://docs.midnight.network/getting-started/installation
`.trim();
}
