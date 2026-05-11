# Snek.fun Sniper — Cross-Device Handoff

Captured 2026-05-10 on the macOS agent-lump checkout. Pairs with the Snek.fun
sniper bot at `/home/scream2/snekfun-sniper/` on the WSL device. Investigation
context: snek's order/executor model means "buy tx confirmed" ≠ "tokens
received"; an executor batch can scoop an order without filling it (recent loss:
~7.78 ADA on tx `c74359a1…`, scoop tx `11b2497e…`). This document captures the
verified protocol details needed to fix that.

## Verified: `encode_order_datum` matches snek.fun's builder byte-for-byte

Captured `POST https://builder.snek.fun/order` for a 5 ADA buy of DUCKSNEK
(`772de9735ee1c5149b70b3da10059218b1bcb99e58c84601aa805930.4455434b534e454b`)
at slippage `"15"`. Parsed the order-output's inline datum, then re-encoded it
with `snekfun_client.encode_order_datum(...)` using the parsed values.

**Result: 219 bytes, identical hex.** The 10-field Constr0 spec is correct:

```
Constr0[
  [0] direction          : bytes (single byte: 0x01 BUY, 0x00 SELL)
  [1] order_info         : Constr0[
                              Constr0[ bytes<owner_payment_pkh (28)> ],
                              Constr0[ Constr0[ Constr0[ bytes<owner_stake_pkh (28)> ] ] ]
                           ]
  [2] asset_x            : Constr0[ bytes<empty>, bytes<empty> ]               ; ADA
  [3] asset_y            : Constr0[ bytes<policy (28)>, bytes<asset_name> ]    ; token
  [4] amounts            : Constr0[ uint<a0>, uint<a1> ]                       ; see slippage table
  [5] executor_fee       : uint   ; constant 1_100_000
  [6] deposit_ada        : uint   ; constant 1_500_000
  [7] permitted_executor : bytes  ; constant e865941988edcca559268b57b7ee939974fd42fd26c7e1acd7a50678
  [8] deadline           : uint   ; POSIX ms; builder uses now + 20 min
  [9] owner_pkh          : bytes  ; same as field [1] inner payment pkh
]
```

### Stake credential gotcha (likely root cause of the no-fill bug)

The `agent-lump` Python codec's docstring calls field [1]'s second slot
`return_pkh`, implying a second payment hash. **It is not.** The triple
`Constr0[Constr0[Constr0[bytes]]]` wrapper is the Plutus encoding of
`StakingHash(KeyHash)` — i.e. **field [1] carries `(payment_credential,
staking_credential)`**, not two payment hashes.

For a base address `addr1q<payment_pkh><stake_pkh>`, that inner triple-wrapped
bytes is the **stake key hash**. The Python call site happens to work for base
addresses because it passes the stake hash into the slot named `return_pkh`.
A naive TS port using Lucid's `paymentCredentialOf(addr)` for both slots will
produce a datum that:

- passes basic validator checks (no signature mismatch)
- but the executor uses the staking credential to reconstruct a return
  address; with the wrong stake part it can't materialize the user's address
- result: order eligible for the executor's batch but the fill output goes
  nowhere matching the user's wallet → "scooped without fill"

Loss math (`EXECUTOR_FEE 1.1 ADA + tx fee + pool skim ≈ 7-9 ADA`) matches
exactly what happened in the production incident. **This is the lead suspect.**

**Lucid extraction:**

```ts
import { getAddressDetails } from "@lucid-evolution/lucid";
const d = getAddressDetails(bech32);
const ownerPaymentPkh = d.paymentCredential!.hash;
const ownerStakePkh   = d.stakeCredential!.hash;  // required — splash address always has one
```

If you must support enterprise addresses (no stake), the codec needs a
different Constr variant in slot [1][1] (likely `Constr1` for `StakingPtr` or
`None`). The Python codec only handles the base-address case; we have no live
data for the enterprise variant.

## Verified: the `amounts` field uses two different validator codepaths

Captured the builder's response at every slippage level for the same 5 ADA buy
(`output_amount = 1_891_394` tokens quoted):

| slippage | amounts[0] | amounts[1] | a0 / quote | a1 / input |
|---:|---:|---:|---:|---:|
| `"15"`       | 32,153,698 | 100,000,000 | 17.0000× | 20.0000× |
| `"30"`       | 26,479,516 | 100,000,000 | 14.0000× | 20.0000× |
| `"50"`       |  1,891,394 |  10,000,000 |  1.0000× |  2.0000× |
| `"75"`       |    945,697 |  10,000,000 |  0.5000× |  2.0000× |
| `"infinity"` |          1 |   5,000,000 |  0.0000× |  1.0000× |

These are **not** a simple `(min_tokens_out, max_lovelace_in)` per the codec
docstring. Two distinct codepaths:

- **Tight tier (`"15"`, `"30"`):** `amounts[1] = 100 ADA` (round-number absolute
  cap), `amounts[0]` larger than quoted output. Meaningless as "min tokens." These
  are almost certainly **pool-state invariants** the validator checks at fill
  time — e.g. `pool_reserve_token ≥ amounts[0]` and `pool_reserve_ada ≤ amounts[1]`
  to enforce "price has not moved much from quote-time state."
- **Loose tier (`"50"`, `"75"`):** `amounts[1] = 2× input lovelace`, `amounts[0]`
  acts as conventional per-trade min-tokens-out. At `"50"`, `amounts[0]` equals
  the quote exactly (zero tolerance — strict). At `"75"`, half the quote.
- **`"infinity"`:** sentinel `(1, input_lovelace)`.

**TS guidance:** never compute `amounts` client-side. Echo the builder's CBOR.
The cliff between `"30"` and `"50"` (a1: 100M → 10M, a0: 26.5M → 1.9M) is not
derivable from a slippage percentage.

**Recommended sniper slippage:** `"infinity"` — the only tier with predictable
encoding and the only one where you'll fill against unpredictable pool state at
submit time. For exit sells, `"75"` gives a meaningful price floor
(0.5× quote).

## Verified: WS protocol — docs file was incomplete

`wss://analytics.snek.fun/websocket/ws` and `wss://charts.snek.fun/websocket/ws`.
No subprotocol negotiation. The published docs envelope
(`{topic, ...params}`) is **wrong** — the server silently drops those messages.

### Wire format (captured from snek.fun frontend)

```jsonc
// Client → Server
{"requestId":"0","messageType":"connect","clientId":"<22-char base64ish>"}

{"requestId":"<n>","messageType":"subscribe","clientId":"<same>","topic":"<TopicName>","data":<topic-specific object>}

{"requestId":"<n>","messageType":"pong","clientId":"<same>"}

// Server → Client
{"requestId":"0","event":"ConnectionSucceeded","messageType":"Response","data":{"timestamp":1778466917627}}

{"requestId":null,"event":"Ping","messageType":"Request","data":{"timestamp":...}}   // ~every 5s; you MUST pong or get dropped

{"requestId":"<n>","event":"<EventName>","messageType":"Response","data":<event-specific>}   // topic data
```

### Topics (live frontend traffic, May 2026)

| Topic | Subscribe `data` | Purpose |
|---|---|---|
| `KOTH` | `{}` | Crown-of-the-hill changes |
| `Latest` | `{}` | Latest token activity (global) |
| `New` | `{}` | New launches |
| `Trending` | `{}` | Trending tokens |
| `PoolFeed` | `{asset}` | Per-token live updates |
| `UserHistoryByAddress` | `{address}` | All user activity (bech32) |
| `UserHistoryByAddressAndAsset` | `{asset, address}` | Per-(user, token), narrower noise |
| `LiveOrdersFeed` | `{asset, pkhs: []}` | Open orders at a pool, filterable by pkh list |
| `LastBar` | `{pair: {base, quote}, interval: "min5"}` | Chart bar deltas |

`asset` is `policyId.assetName` (dot-separated). `quote: "."` is ADA.
Statuses to look for inside `data` for user events: `MempoolEvaluated`,
`MempoolRefunded` (per the partial published docs; payload shape not yet
captured live).

### Recommended topic set for the sniper

- **`LiveOrdersFeed`** with `{asset, pkhs:[OUR_PAYMENT_PKH]}` per token you've
  placed an order in. When the executor scoops your order (filled OR refunded),
  the order's UTxO leaves the pool's "live orders" set and you get a delta.
  This is the lowest-noise, most actionable signal — pinned to a specific
  (token, wallet) tuple.
- **`UserHistoryByAddress`** with `{address: OUR_BECH32}` as a backstop. Catches
  events `LiveOrdersFeed` may not (e.g. cross-token activity).
- Pong on every `Ping`. Drop = silent dis-fill detection.

The WS is a trigger only — for "did I actually get tokens" use Blockfrost/Kupo
to fetch the executor's batch tx and run `classify_snekfun_trade(tx, OUR_ADDR)`.

## Fill-detection algorithm (canonical, from `copy_trader.py`)

This is independent of any snek API — runs against a raw tx and the user's
bech32 address. Port directly to TS.

```pseudo
compute_net_delta(tx_utxos, address):
  ada_in, ada_out = 0, 0
  tokens_in, tokens_out = {}, {}
  for inp in tx_utxos.inputs where inp.address == address:
    add ada and per-unit token quantities to *_in
  for outp in tx_utxos.outputs where outp.address == address:
    add ada and per-unit token quantities to *_out
  net_ada = ada_out - ada_in
  net_tokens = {unit: tokens_out[u] - tokens_in[u]}
  drop zero entries
  return net_ada, net_tokens

classify_snekfun_trade(tx_utxos, target) -> ("BUY"|"SELL", unit, qty) | None:
  net_ada, net_tokens = compute_net_delta(tx_utxos, target)
  if len(net_tokens) != 1: return None
  unit, qty = single entry
  if qty > 0:
    return ("BUY", unit, qty)
  if qty < 0:
    # verify the tokens went to a snek address (pool or order script)
    lost = sum qty of `unit` in outputs whose payment-key hash is in SNEKFUN_PKHS
    if lost <= 0: return None
    return ("SELL", unit, -qty)
  return None
```

`SNEKFUN_PKHS = {ORDER_SCRIPT_BASE_HASH, POOL_SCRIPT_HASH}` (see constants
below). The single-token-positive heuristic correctly handles
`MempoolEvaluated` vs `MempoolRefunded` cases: a refund has `qty == 0` (or all
ADA back with no token delta), which produces `None` → "not a fill."

## Protocol constants (verified on-chain)

```
POOL_SCRIPT_HASH          = 63f947b8d9535bc4e4ce6919e3dc056547e8d30ada12f29aa5f826b8
ORDER_SCRIPT_BASE_HASH    = d9143ac63473b17a215d1b7484dfb6ac6b4a0005beb0e26a6ca02c96
PERMITTED_EXECUTOR        = e865941988edcca559268b57b7ee939974fd42fd26c7e1acd7a50678
PROTOCOL_FEE_PKH_1        = 8807fbe6e36b1c35ad6f36f0993e2fc67ab6f2db06041cfa3a53c04a
PROTOCOL_FEE_PKH_2        = 30c1003aa7dec834e0d0a78db547ba8840e58060725dbfae352f0d64
POOL_ADDRESS              = addr1xxg94wrfjcdsjncmsxtj0r87zk69e0jfl28n934sznu95tdj764lvrxdayh2ux30fl0ktuh27csgmpevdu89jlxppvrs2993lw

EXECUTOR_FEE              = 1_100_000  (lovelace)
DEPOSIT_ADA               = 1_500_000  (lovelace)
MIN_UTXO_LOVELACE         = 1_500_000  (lovelace)

Reference scripts (for spend-via-reference):
  pool_validator    : tx c4a540ac2e06c217dd4fb3f39ca3863da394ba134677dafa9b98830ca71d584d#3
                      hash 905ab869961b094f1b8197278cfe15b45cbe49fa8f32c6b014f85a2d
  order_validator   : tx e2ed9e953ebf98ca701fc93588d73cb9769f87b9d13712474f566a0743963e8b#0
                      hash d9143ac63473b17a215d1b7484dfb6ac6b4a0005beb0e26a6ca02c96
  minting_policy    : tx e2ed9e953ebf98ca701fc93588d73cb9769f87b9d13712474f566a0743963e8b#1
                      hash a5643b4a22a192d7691d05baf4a9bbb8acdbb5daa60be1f333e128f1
```

## Pool datum (9 fields, for parsing pool state)

```
Constr0[
  [0] pool NFT             : Constr0[ bytes<policy>, bytes<name> ]
  [1] asset X              : Constr0[ bytes, bytes ]                     ; ADA: empty/empty
  [2] asset Y              : Constr0[ bytes<policy>, bytes<name> ]       ; token
  [3] aNum                 : uint                                        ; curve param
  [4] bNum                 : uint                                        ; curve param
  [5] permitted_executor   : bytes (28)
  [6] ada_cap_threshold    : uint (lovelace)                             ; bonding curve graduation cap
  [7] protocol_fee_pkh_1   : bytes (28)
  [8] protocol_fee_pkh_2   : bytes (28)
]
```

Constant-product math (verified):

```
estimate_buy(x_reserve_ada, y_reserve_token, dx_lovelace):
  dy = y - (x * y) // (x + dx)   // tokens out

estimate_sell(x_reserve_ada, y_reserve_token, dy_tokens):
  dx = x - (x * y) // (y + dy)   // lovelace out
```

## HTTP endpoints worth knowing (verified live)

```
GET  https://analytics.snek.fun/v1/pools-main-page/initial/state?filter=Latest
     → array of {pool, metrics, info} — bootstrap for Latest topic; shape

GET  https://analytics.snek.fun/v1/pools-main-page/koth
     → KOTH bootstrap

GET  https://analytics.snek.fun/v1/utility/server/time
GET  https://analytics.snek.fun/v1/utility/ada-usd/rate

POST https://builder.snek.fun/order
     body {assetId, amount, side, changeAddress, slippage, utxos}
     → {cbor, tradeId?, inputAmount, outputAmount, ...}

POST https://builder.snek.fun/sign-and-submit
     body {cbor, witness, changeAddress}  → {txHash}

POST https://utxo-monitor.snek.fun/getUtxos
     body {pkh, offset, limit, query: "unspent"}
     → [{txHash, index, address, value:[{unit, amount}]}]
     This is the post-submit polling backstop snek's own frontend uses.

POST https://balance.snek.fun/balance
     body {address}
     → {balance:[{policyId, base16Name, amount}]}
```

Endpoints that **404 as of May 2026** despite appearing in older docs:

```
GET  /v1/user-history-feed/initial/state/open
GET  /v1/user-history-feed/by-address
```

User history is WS-only now.

## What does NOT exist in `agent-lump` (don't copy these — they don't help)

- **No post-submit polling.** `execute_buy` / `execute_sell` end at
  `blockfrost.submit_tx()` and return. Same architectural blindspot as the TS
  sniper. The Python `get_utxos_by_pkh()` wrapper for `utxo-monitor.snek.fun`
  exists but no code in the trade pipeline calls it.
- **No WS subscription.** No fill verification.
- **No tests, no recorded successful order CBOR fixtures.** The canonical
  reference is to capture a fresh builder CBOR (see "Reproducing" below).

## Recommended TS architecture

1. **New module `src/snekOrderWatcher.ts`**:
   - Connect to `wss://analytics.snek.fun/websocket/ws`, send `connect`, store
     `clientId`.
   - Subscribe `LiveOrdersFeed` with `{asset, pkhs:[OUR_PKH]}` for each pending
     order's token. Subscribe `UserHistoryByAddress` with `{address: OUR_BECH32}`
     as a backstop.
   - Reply `pong` on every `Ping`.
   - On any topic delta involving our submitted order's tx, fire a settlement
     check: fetch the batch tx via Blockfrost/Kupo, run
     `classify_snekfun_trade(tx, OUR_ADDR)`.
   - Emit `OrderFilled(unit, qty)` on single-positive-token; `OrderRefunded()`
     on no-token-delta. Deadline-bound (~90s) → if no signal arrives, mark
     failed.
2. **`trader.ts`:** replace chainSync-based confirm with
   `snekOrderWatcher.waitForSettlement(submittedTxId, walletBech32)`.
   Sell only fires on `OrderFilled`. Panic timer becomes "no settlement signal
   → mark failed, no sell."
3. **Datum-build sanity check:** before submitting, fetch the builder's CBOR
   and verify the order output's inline datum has the right
   stake-credential-in-slot-[1][1] shape. This is one line of cbor decoding and
   catches the bug class up-front.

## Reproducing this investigation on the other device

```bash
cd /path/to/agent-lump
source .venv/bin/activate   # or: pip install -r requirements.txt

# Capture a fresh builder CBOR + diff against encode_order_datum (verifies codec):
python -c "
from dotenv import load_dotenv; load_dotenv()
import os, cbor2
from blockfrost_client import BlockfrostClient
from snekfun_client import buy_via_builder, encode_order_datum
from pycardano import Address
addr = os.environ['CARDANO_PAYMENT_ADDRESS']
trade = buy_via_builder(
    asset_id='<policyId>.<assetNameHex>',
    ada_amount=5.0, sender_address=addr, slippage='infinity',
    blockfrost=BlockfrostClient())
tx = cbor2.loads(bytes.fromhex(trade.cbor))
for out in tx[0][1]:
    if isinstance(out, dict) and 2 in out and out.get(0,b'').hex().startswith('11d9143ac6'):
        d = cbor2.loads(out[2][1].value if hasattr(out[2][1],'value') else out[2][1])
        print('datum 10 fields:', len(d.value))
        print('amounts:', d.value[4].value)
        print('owner pkh:', d.value[9].hex())
        break
"
```

To tap the WS (uses Chrome DevTools MCP): open `https://snek.fun` with a
`window.WebSocket` wrapper that pushes inbound frames into
`window.__capturedFrames`. Then `await fetch(...)` or sit on a token detail page
to capture real `event` payloads. The frontend itself emits the correct
envelope shape — copy it verbatim.

## Open items (not solved yet)

- **`MempoolEvaluated` / `MempoolRefunded` exact `data` payload shape** — known
  to exist, not captured live in this session (LUMP was quiet during the
  observation window). The envelope `{requestId, event, messageType, data}`
  is confirmed; the inner `data` shape would have to be reverse-engineered by
  letting the sniper run live or by triggering a small intentional buy/scoop
  with the WS tap recording. Practical workaround: ignore `data` payload
  detail; treat any event with `topic === UserHistoryByAddress` or
  `topic === LiveOrdersFeed` as a "go look at the chain" trigger and rely on
  `classify_snekfun_trade` for ground truth.
- **Stake-credential hypothesis not yet proven** — strong circumstantial fit
  with the production loss, but only a fresh small-stakes buy with corrected
  datum encoding will confirm.
