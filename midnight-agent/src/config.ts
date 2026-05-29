/**
 * Network configuration for Midnight mainnet/preprod/preview.
 *
 * Mainnet (verified May 2026):
 *   Chain:        Midnight Mainnet (genesis Mar 30 2026)
 *   Node:         ~0.22.x
 *   Ledger:       v8.0.3
 *   Indexer API:  v4 (GraphQL) — https://indexer.mainnet.midnight.network/api/v4/graphql
 *   Genesis hash: 0x1941ca8e2bb88146c14dea084d3be7eb6e96ca7135429c543848b628124f2854
 *
 * Public indexer/RPC endpoints require no API key. The chain.ts queries target
 * the v4 indexer schema (contractAction/block(offset)/transactions(offset)).
 */

import 'dotenv/config';

export type NetworkId = 'mainnet' | 'preprod' | 'preview';

export interface MidnightConfig {
  networkId: NetworkId;
  rpcUrl: string;
  rpcWssUrl: string;
  indexerUrl: string;
  indexerWsUrl: string;
  proverUrl: string;
  explorerUrl: string;
}

const DEFAULTS: Record<NetworkId, MidnightConfig> = {
  mainnet: {
    networkId: 'mainnet',
    rpcUrl: 'https://rpc.mainnet.midnight.network/',
    rpcWssUrl: 'wss://rpc.mainnet.midnight.network',
    indexerUrl: 'https://indexer.mainnet.midnight.network/api/v4/graphql',
    indexerWsUrl: 'wss://indexer.mainnet.midnight.network/api/v4/graphql/ws',
    proverUrl: 'http://localhost:6300',
    explorerUrl: 'https://explorer.mainnet.midnight.network',
  },
  preprod: {
    networkId: 'preprod',
    rpcUrl: 'https://rpc.preprod.midnight.network/',
    rpcWssUrl: 'wss://rpc.preprod.midnight.network',
    indexerUrl: 'https://indexer.preprod.midnight.network/api/v4/graphql',
    indexerWsUrl: 'wss://indexer.preprod.midnight.network/api/v4/graphql/ws',
    proverUrl: 'http://localhost:6300',
    explorerUrl: 'https://explorer.preprod.midnight.network',
  },
  preview: {
    networkId: 'preview',
    rpcUrl: 'https://rpc.preview.midnight.network/',
    rpcWssUrl: 'wss://rpc.preview.midnight.network',
    indexerUrl: 'https://indexer.preview.midnight.network/api/v4/graphql',
    indexerWsUrl: 'wss://indexer.preview.midnight.network/api/v4/graphql/ws',
    proverUrl: 'http://localhost:6300',
    explorerUrl: 'https://explorer.preview.midnight.network',
  },
};

export const GENESIS_HASH =
  '0x1941ca8e2bb88146c14dea084d3be7eb6e96ca7135429c543848b628124f2854';

export function getConfig(): MidnightConfig {
  const network = (process.env.MIDNIGHT_NETWORK ?? 'mainnet') as NetworkId;
  const defaults = DEFAULTS[network] ?? DEFAULTS.mainnet;

  return {
    networkId: network,
    rpcUrl: process.env.MIDNIGHT_RPC_URL ?? defaults.rpcUrl,
    rpcWssUrl: process.env.MIDNIGHT_RPC_WSS_URL ?? defaults.rpcWssUrl,
    indexerUrl: process.env.MIDNIGHT_INDEXER_URL ?? defaults.indexerUrl,
    indexerWsUrl: process.env.MIDNIGHT_INDEXER_WS_URL ?? defaults.indexerWsUrl,
    proverUrl: process.env.MIDNIGHT_PROVER_URL ?? defaults.proverUrl,
    explorerUrl: process.env.MIDNIGHT_EXPLORER_URL ?? defaults.explorerUrl,
  };
}

export function explorerLink(path: string): string {
  const config = getConfig();
  return `${config.explorerUrl}${path}`;
}
