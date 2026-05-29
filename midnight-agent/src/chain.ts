/**
 * Chain query client for Midnight — JSON-RPC + GraphQL Indexer.
 *
 * Two independent data paths:
 *   1. JSON-RPC — Direct Substrate node (blocks, state, tx submission)
 *   2. GraphQL Indexer — Indexed chain data (blocks, txs, contracts)
 */

import { getConfig, GENESIS_HASH, type MidnightConfig } from './config.js';

// ─── JSON-RPC Transport ─────────────────────────────────────────────────

let rpcIdCounter = 0;

export class RpcError extends Error {
  constructor(
    public code: number,
    public rpcMessage: string,
    public data?: unknown,
  ) {
    super(`RPC error ${code}: ${rpcMessage}`);
    this.name = 'RpcError';
  }
}

export async function rpcCall(
  method: string,
  params: unknown[] = [],
  config?: MidnightConfig,
): Promise<unknown> {
  const { rpcUrl } = config ?? getConfig();
  const payload = {
    jsonrpc: '2.0',
    id: ++rpcIdCounter,
    method,
    params,
  };

  const resp = await fetch(rpcUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

  if (!resp.ok) {
    throw new Error(`RPC HTTP ${resp.status}: ${resp.statusText}`);
  }

  const data = (await resp.json()) as {
    result?: unknown;
    error?: { code: number; message: string; data?: unknown };
  };

  if (data.error) {
    throw new RpcError(
      data.error.code,
      data.error.message,
      data.error.data,
    );
  }

  return data.result;
}

// ─── GraphQL Indexer Transport ──────────────────────────────────────────

export class IndexerError extends Error {
  constructor(
    public errors: Array<{ message: string }>,
    public query: string,
  ) {
    const msgs = errors.map((e) => e.message).join('; ');
    super(`Indexer errors: ${msgs}`);
    this.name = 'IndexerError';
  }
}

export async function graphql(
  query: string,
  variables?: Record<string, unknown>,
  config?: MidnightConfig,
): Promise<Record<string, unknown>> {
  const { indexerUrl } = config ?? getConfig();
  const payload: Record<string, unknown> = { query };
  if (variables) payload.variables = variables;

  const resp = await fetch(indexerUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

  if (!resp.ok) {
    throw new Error(`Indexer HTTP ${resp.status}: ${resp.statusText}`);
  }

  const data = (await resp.json()) as {
    data?: Record<string, unknown>;
    errors?: Array<{ message: string }>;
  };

  if (data.errors?.length) {
    throw new IndexerError(data.errors, query);
  }

  return data.data ?? {};
}

// ─── System / Node Info (RPC) ───────────────────────────────────────────

export interface NodeHealth {
  peers: number;
  isSyncing: boolean;
  shouldHavePeers: boolean;
}

export async function getNodeHealth(): Promise<NodeHealth> {
  const r = (await rpcCall('system_health')) as Record<string, unknown>;
  return {
    peers: (r.peers as number) ?? 0,
    isSyncing: (r.isSyncing as boolean) ?? true,
    shouldHavePeers: (r.shouldHavePeers as boolean) ?? true,
  };
}

export async function getChainName(): Promise<string> {
  return (await rpcCall('system_chain')) as string;
}

export async function getNodeVersion(): Promise<string> {
  return (await rpcCall('system_version')) as string;
}

export async function getGenesisHash(): Promise<string> {
  return (await rpcCall('chain_getBlockHash', [0])) as string;
}

export async function getRpcMethods(): Promise<string[]> {
  const r = (await rpcCall('rpc_methods')) as { methods: string[] };
  return r.methods ?? [];
}

export async function getSystemProperties(): Promise<Record<string, unknown>> {
  return (await rpcCall('system_properties')) as Record<string, unknown>;
}

export async function getSyncState(): Promise<Record<string, unknown>> {
  return (await rpcCall('system_syncState')) as Record<string, unknown>;
}

// ─── Midnight-Specific RPC ──────────────────────────────────────────────

export async function getContractStateRpc(
  contractAddress: string,
): Promise<unknown> {
  return rpcCall('midnight_contractState', [contractAddress]);
}

export async function getLedgerStateRoot(): Promise<string> {
  return (await rpcCall('midnight_ledgerStateRoot')) as string;
}

export async function getLedgerVersion(): Promise<unknown> {
  return rpcCall('midnight_ledgerVersion');
}

export async function getZswapStateRoot(
  blockHash?: string,
): Promise<string> {
  const params = blockHash ? [blockHash] : [];
  return (await rpcCall('midnight_zswapStateRoot', params)) as string;
}

// ─── Sidechain / Ariadne Bridge (RPC) ──────────────────────────────────

export interface SidechainStatus {
  numPermissionedCandidates: number;
  numRegisteredCandidates: number;
}

export async function getSidechainStatus(): Promise<SidechainStatus> {
  const r = (await rpcCall('sidechain_getStatus')) as Record<string, unknown>;
  return {
    numPermissionedCandidates:
      (r.numPermissionedCandidates as number) ?? 0,
    numRegisteredCandidates:
      (r.numRegisteredCandidates as number) ?? 0,
  };
}

export async function getSidechainParams(): Promise<Record<string, unknown>> {
  return (await rpcCall('sidechain_getParams')) as Record<string, unknown>;
}

// ─── Block Queries (RPC) ────────────────────────────────────────────────

export async function getFinalizedHead(): Promise<string> {
  return (await rpcCall('chain_getFinalizedHead')) as string;
}

export async function getBlockHash(number: number): Promise<string> {
  return (await rpcCall('chain_getBlockHash', [number])) as string;
}

export async function getBlockRpc(
  blockHash?: string,
): Promise<Record<string, unknown>> {
  const params = blockHash ? [blockHash] : [];
  return (await rpcCall('chain_getBlock', params)) as Record<string, unknown>;
}

export async function getHeader(
  blockHash?: string,
): Promise<Record<string, unknown>> {
  const params = blockHash ? [blockHash] : [];
  return (await rpcCall('chain_getHeader', params)) as Record<string, unknown>;
}

// ─── Transaction Submission (RPC) ───────────────────────────────────────

export async function submitExtrinsic(
  extrinsicHex: string,
): Promise<string> {
  return (await rpcCall('author_submitExtrinsic', [extrinsicHex])) as string;
}

export async function getPendingExtrinsics(): Promise<unknown[]> {
  return (await rpcCall('author_pendingExtrinsics')) as unknown[];
}

// ─── Block Queries (Indexer) ────────────────────────────────────────────

export interface MidnightBlock {
  hash: string;
  height: number;
  timestamp: string;
  txCount: number;
  parentHash: string;
}

function parseBlock(b: Record<string, unknown>): MidnightBlock {
  const parent = b.parent as Record<string, unknown> | null;
  const txs = b.transactions as unknown[] | null;
  return {
    hash: b.hash as string,
    height: b.height as number,
    timestamp: String(b.timestamp ?? ''),
    txCount: txs?.length ?? 0,
    parentHash: parent?.hash ? String(parent.hash) : '',
  };
}

export async function getLatestBlock(): Promise<MidnightBlock> {
  const data = await graphql(`
    query {
      block {
        hash height timestamp
        parent { hash }
        transactions { hash }
      }
    }
  `);

  const b = data.block as Record<string, unknown> | null;
  if (!b) throw new Error('No block returned from indexer');
  return parseBlock(b);
}

export async function getRecentBlocks(
  count = 10,
): Promise<MidnightBlock[]> {
  // v4 indexer returns one block at a time; walk backwards from latest
  const latest = await getLatestBlock();
  const blocks: MidnightBlock[] = [latest];

  for (let h = latest.height - 1; h > latest.height - count && h >= 0; h--) {
    try {
      blocks.push(await getBlockByHeight(h));
    } catch {
      break;
    }
  }

  return blocks;
}

export async function getBlockByHeight(
  height: number,
): Promise<MidnightBlock> {
  const data = await graphql(
    `
      query ($height: Int!) {
        block(offset: { height: $height }) {
          hash height timestamp
          parent { hash }
          transactions { hash }
        }
      }
    `,
    { height },
  );

  const b = data.block as Record<string, unknown> | null;
  if (!b) throw new Error(`Block at height ${height} not found`);
  return parseBlock(b);
}

// ─── Transaction Queries (Indexer) ──────────────────────────────────────

export interface MidnightTransaction {
  hash: string;
  id: string;
  blockHash: string;
  blockHeight: number;
  protocolVersion: number;
}

export async function getTransaction(
  txHash: string,
): Promise<MidnightTransaction> {
  const data = await graphql(
    `
      query ($hash: HexEncoded!) {
        transactions(offset: { hash: $hash }) {
          hash id protocolVersion
          block { hash height }
        }
      }
    `,
    { hash: txHash },
  );

  const txs = (data.transactions as Array<Record<string, unknown>>) ?? [];
  if (!txs.length) throw new Error(`Transaction ${txHash} not found`);

  const t = txs[0];
  const block = (t.block as Record<string, unknown>) ?? {};
  return {
    hash: t.hash as string,
    id: (t.id as string) ?? '',
    blockHash: (block.hash as string) ?? '',
    blockHeight: (block.height as number) ?? 0,
    protocolVersion: (t.protocolVersion as number) ?? 0,
  };
}

export async function getRecentTransactions(
  _limit = 20,
): Promise<MidnightTransaction[]> {
  // v4 indexer requires a hash/identifier offset for transactions query.
  // To get recent txs, fetch the latest block and return its transactions.
  const latest = await getLatestBlock();
  const data = await graphql(
    `
      query ($height: Int!) {
        block(offset: { height: $height }) {
          transactions {
            hash id protocolVersion
            block { hash height }
          }
        }
      }
    `,
    { height: latest.height },
  );

  const block = data.block as Record<string, unknown> | null;
  if (!block) return [];

  const txs = (block.transactions as Array<Record<string, unknown>>) ?? [];
  return txs.map((t) => {
    const b = (t.block as Record<string, unknown>) ?? {};
    return {
      hash: t.hash as string,
      id: (t.id as string) ?? '',
      blockHash: (b.hash as string) ?? '',
      blockHeight: (b.height as number) ?? 0,
      protocolVersion: (t.protocolVersion as number) ?? 0,
    };
  });
}

// ─── Contract Queries (Indexer) ─────────────────────────────────────────

export interface ContractBalance {
  /** Token type (hex-encoded). */
  tokenType: string;
  /** Balance in base units, as a string (may exceed Number.MAX_SAFE_INTEGER). */
  amount: string;
}

export interface ContractActionInfo {
  address: string;
  /** GraphQL __typename: ContractDeploy | ContractCall | ContractUpdate. */
  actionType: string;
  /** Raw contract state blob (hex). Circuit names are embedded in here. */
  state: string;
  /** Raw Zswap state blob (hex). */
  zswapState: string;
  /** Unshielded token balances held by the contract. */
  unshieldedBalances: ContractBalance[];
  /** For ContractCall actions: the circuit/entry point that was invoked. */
  entryPoint: string | null;
  /** Exported circuit / entry-point names decoded from `state`. */
  circuits: string[];
  /** The transaction that produced this action. */
  tx: {
    hash: string;
    protocolVersion: number;
    blockHeight: number;
    blockTimestamp: string;
    /** Fees actually paid, in DUST base units (Specks). Null for system txs. */
    paidFees: string | null;
    estimatedFees: string | null;
  };
}

/**
 * Decode the exported circuit / entry-point names from a contract `state` blob.
 *
 * Midnight has no on-chain source verification, but Compact entry-point names
 * are stored as ASCII inside the serialized `state[v6]` operation map. Each name
 * is terminated by the byte sequence `00 b9` and prefixed by a 2-byte tag (whose
 * second byte equals the first minus 4); this strips that framing. Assumes the
 * conventional lowercase-first camelCase naming used by Compact circuits.
 * Verified against live mainnet contracts (recovers full circuit sets exactly).
 */
export function decodeContractCircuits(stateHex: string): string[] {
  if (!stateHex) return [];
  const hex = stateHex.startsWith('0x') ? stateHex.slice(2) : stateHex;
  const b = Buffer.from(hex, 'hex');
  const isId = (c: number) =>
    (c >= 0x30 && c <= 0x39) || // 0-9
    (c >= 0x41 && c <= 0x5a) || // A-Z
    (c >= 0x61 && c <= 0x7a) || // a-z
    c === 0x5f; // _

  const seen = new Set<string>();
  const out: string[] = [];
  for (let i = 0; i + 1 < b.length; i++) {
    if (b[i] !== 0x00 || b[i + 1] !== 0xb9) continue; // entry-point terminator
    let s = i;
    while (s > 0 && isId(b[s - 1])) s--; // walk back over identifier bytes
    let name = b.subarray(s, i).toString('ascii');
    if (name.length >= 3 && name.charCodeAt(1) === name.charCodeAt(0) - 4) {
      name = name.slice(2); // strip 2-byte framing tag (b1 === b0 - 4)
    }
    name = name.replace(/^[0-9]+/, '').replace(/^[A-Z]+(?=[a-z])/, ''); // residual tag bytes
    if (/^[a-z][A-Za-z0-9_]{3,63}$/.test(name) && !seen.has(name)) {
      seen.add(name);
      out.push(name);
    }
  }
  return out;
}

/**
 * Read a contract's latest on-chain action from the indexer (v4 schema):
 * full state, Zswap state, unshielded balances, the originating transaction
 * (with DUST fees), and the decoded circuit set. No API key required.
 */
export async function getContractState(
  contractAddress: string,
): Promise<ContractActionInfo> {
  const data = await graphql(
    `
      query ($address: HexEncoded!) {
        contractAction(address: $address) {
          __typename
          address
          state
          zswapState
          unshieldedBalances { tokenType amount }
          ... on ContractCall { entryPoint }
          transaction {
            hash
            protocolVersion
            block { height timestamp }
            ... on RegularTransaction {
              fees { paidFees estimatedFees }
            }
          }
        }
      }
    `,
    { address: contractAddress },
  );

  const ca = data.contractAction as Record<string, unknown> | null;
  if (!ca) throw new Error(`Contract ${contractAddress} not found`);

  const tx = (ca.transaction as Record<string, unknown>) ?? {};
  const block = (tx.block as Record<string, unknown>) ?? {};
  const fees = (tx.fees as Record<string, unknown>) ?? {};
  const balances =
    (ca.unshieldedBalances as Array<Record<string, unknown>>) ?? [];
  const state = (ca.state as string) ?? '';

  return {
    address: ca.address as string,
    actionType: (ca.__typename as string) ?? 'unknown',
    state,
    zswapState: (ca.zswapState as string) ?? '',
    unshieldedBalances: balances.map((bal) => ({
      tokenType: String(bal.tokenType ?? ''),
      amount: String(bal.amount ?? '0'),
    })),
    entryPoint: (ca.entryPoint as string) ?? null,
    circuits: decodeContractCircuits(state),
    tx: {
      hash: (tx.hash as string) ?? '',
      protocolVersion: (tx.protocolVersion as number) ?? 0,
      blockHeight: (block.height as number) ?? 0,
      blockTimestamp: String(block.timestamp ?? ''),
      paidFees: (fees.paidFees as string) ?? null,
      estimatedFees: (fees.estimatedFees as string) ?? null,
    },
  };
}

// ─── Combined / High-Level ──────────────────────────────────────────────

export interface ChainInfo {
  networkId: string;
  rpcUrl: string;
  indexerUrl: string;
  proverUrl: string;
  explorerUrl: string;
  genesisHash: string;
  chainName?: string;
  nodeVersion?: string;
  peers?: number;
  isSyncing?: boolean;
  finalizedHead?: string;
  bridgePermissioned?: number;
  bridgeRegistered?: number;
  rpcStatus: string;
}

export async function getChainInfo(): Promise<ChainInfo> {
  const config = getConfig();
  const info: ChainInfo = {
    networkId: config.networkId,
    rpcUrl: config.rpcUrl,
    indexerUrl: config.indexerUrl,
    proverUrl: config.proverUrl,
    explorerUrl: config.explorerUrl,
    genesisHash: GENESIS_HASH,
    rpcStatus: 'unknown',
  };

  try {
    const [health, version, chain] = await Promise.all([
      getNodeHealth(),
      getNodeVersion(),
      getChainName(),
    ]);
    info.peers = health.peers;
    info.isSyncing = health.isSyncing;
    info.nodeVersion = version;
    info.chainName = chain;
    info.rpcStatus = 'connected';
  } catch (e) {
    info.rpcStatus = `error: ${e instanceof Error ? e.message : String(e)}`;
  }

  try {
    info.finalizedHead = await getFinalizedHead();
  } catch {
    // non-critical
  }

  try {
    const sc = await getSidechainStatus();
    info.bridgePermissioned = sc.numPermissionedCandidates;
    info.bridgeRegistered = sc.numRegisteredCandidates;
  } catch {
    // non-critical
  }

  return info;
}

export async function verifyNode(): Promise<{
  valid: boolean;
  chainName: string;
  nodeVersion: string;
  genesisMatch: boolean;
  peers: number;
  isSyncing: boolean;
}> {
  const [chain, version, genesis, health] = await Promise.all([
    getChainName(),
    getNodeVersion(),
    getGenesisHash(),
    getNodeHealth(),
  ]);

  return {
    valid: genesis === GENESIS_HASH && !health.isSyncing,
    chainName: chain,
    nodeVersion: version,
    genesisMatch: genesis === GENESIS_HASH,
    peers: health.peers,
    isSyncing: health.isSyncing,
  };
}
