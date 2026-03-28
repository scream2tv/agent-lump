# Snek.fun API Endpoints - Comprehensive Analysis

## Base URLs (Service Hosts)

| Host Variable | Actual URL | Purpose |
|---|---|---|
| `hosts.api.http` | `https://analytics.snek.fun` | Main analytics/data API |
| `hosts.api.ws` | `wss://analytics.snek.fun/websocket/ws` | Main WebSocket (pool data, trades) |
| `hosts.api.serverTime` | `https://analytics.snek.fun/v1/utility/server/time` | Server time sync |
| `hosts.chart.ws` | `wss://charts.snek.fun/websocket/ws` | Chart data WebSocket |
| `hosts.builderUrl` | `https://builder.snek.fun` | Transaction builder service |
| `hosts.balanceUrl` | `https://balance.snek.fun` | Balance query service |
| `hosts.walletUrl` | `https://wallet.snek.fun` | In-app wallet iframe service |
| `hosts.submitUrl` | `https://builder.snek.fun` | Tx submission (same as builder) |
| `hosts.vestingUrl` | `https://token-vesting.snek.fun` | Token vesting/locking service |
| `hosts.avatarUploadUrl` | `https://avatar.snek.fun` | Avatar/image upload |
| `hosts.ipfsUrl` | `https://snekdotfun.mypinata.cloud/ipfs` | IPFS gateway for images |
| `hosts.uTxOMonitorUrl` | `https://utxo-monitor.snek.fun/getUtxos` | UTxO monitoring |
| `hosts.configs.ui` | `https://analytics.snek.fun/snekfun-front/ui-state-v11.json` | UI config |
| `hosts.explorer.address` | (explorer URL) | Block explorer |
| `hosts.utils.nsfwValidation` | `https://nsfw.snek.fun` | NSFW image validation |
| `hosts.utils.referral` | `https://in-app-wallet.snek.fun/referral-api/v1/referral-link` | Referral system |
| `hosts.utils.points` | `https://points-api.snek.fun/v1` | Points/rewards system |
| `hosts.utils.claim` | `https://lottery.snek.fun/v1/claim` | Lottery/airdrop claims |
| `hosts.utils.registryMetadata` | `https://spectrum.fi/cardano/metadata/` | Token metadata registry |

Additional URLs:
- `https://chat-api.snek.fun/v1/snekfun-chat` - Chat API
- `wss://chat-api.snek.fun/websocket/ws` - Chat WebSocket
- `https://in-app-wallet.snek.fun/v1/user-account` - User account management
- `https://analytics.snek.fun/snekfun-front/announcements.json` - Announcements
- `https://analytics.snek.fun/v1/utility/ada-usd/rate` - ADA/USD price

---

## Trading Endpoints (Builder Service)

### POST `{builderUrl}/trade`
**Purpose:** Execute a bonding curve trade (buy/sell tokens)
**Host:** `https://builder.snek.fun`
**Auth:** `withCredentials: false` (no cookies, but uses trading wallet)

```json
{
  "utxos": "splash-wallet",
  "side": "BUY" | "SELL" | "BUY_WITH_OUTPUT",
  "slippage": "infinity" | <number>,
  "assetId": "<policyId.assetName>",
  "changeAddress": "<bech32 address>",
  "wsClientId": "<websocket client id>",
  "amount": "<lovelace amount as string>"
}
```

**Response:** `{ cbor: "<tx cbor>", ... }` (unsigned transaction)

**Trade sides:**
- `BUY` - Buy tokens with ADA (input is ADA amount)
- `BUY_WITH_OUTPUT` - Buy tokens specifying desired output token amount
- `SELL` - Sell tokens for ADA (input is token amount)

---

### POST `{builderUrl}/cpmm-trade`
**Purpose:** Execute a CPMM (Constant Product Market Maker) trade (for graduated/completed tokens)
**Host:** `https://builder.snek.fun`

```json
{
  "utxos": "splash-wallet",
  "side": "<BUY|SELL|BUY_WITH_OUTPUT>",
  "slippage": "<number>",
  "assetId": "<policyId.assetName>",
  "changeAddress": "<bech32 address>",
  "amount": "<amount as string>"
}
```

---

### POST `{builderUrl}/sign`
**Purpose:** Sign a transaction (server-side co-signing)
**Host:** `https://builder.snek.fun`

```json
{
  "cbor": "<unsigned tx cbor>",
  "witness": "<witness cbor>",
  "changeAddress": "<bech32 address>"
}
```

**Response:** `{ cbor: "<signed tx cbor>" }`

---

### POST `{builderUrl}/sign-and-submit`
**Purpose:** Sign and submit a transaction in one call
**Host:** `https://builder.snek.fun`

```json
{
  "cbor": "<unsigned tx cbor>",
  "witness": "<witness cbor>",
  "changeAddress": "<bech32 address>"
}
```

**Response:** `{ txHash: "<tx hash>" }`

---

### POST `{builderUrl}/submit`
**Purpose:** Submit a fully signed transaction
**Host:** `https://builder.snek.fun`

```json
{
  "cbor": "<signed tx cbor>",
  "changeAddress": "<bech32 address>"
}
```

**Response:** `{ txHash: "<tx hash>" }`

---

### POST `{builderUrl}/cancel`
**Purpose:** Cancel a pending order
**Host:** `https://builder.snek.fun`

```json
{
  "txHash": "<original tx hash>",
  "index": <output index>,
  "changeAddress": "<bech32 address>",
  "utxos": "splash-wallet",
  "collaterals": null
}
```

---

### POST `{builderUrl}/launch`
**Purpose:** Launch a new token on the bonding curve
**Host:** `https://builder.snek.fun`

```json
{
  "collaterals": null,
  "utxos": "splash-wallet",
  "initialDeposit": "<lovelace amount as string>",
  "ticker": "<token ticker>",
  "description": "<token description>",
  "changeAddress": "<bech32 address>",
  "assetType": "<asset type>",
  "launchType": "<fair | ...>",
  "twitter": "<optional>",
  "discord": "<optional>",
  "telegram": "<optional>",
  "website": "<optional>",
  "name": "<token name>"
}
```

**Response:** `{ cbor: "<tx cbor>", ... }` (requires sign-and-submit)

---

### POST `{builderUrl}/transfer`
**Purpose:** Transfer ADA/tokens between wallets
**Host:** `https://builder.snek.fun`

**Variant 1 (from funding wallet):**
```json
{
  "utxos": ["<utxo cbor array>"],
  "changeAddress": "<source address>",
  "distAddress": "<destination address>",
  "transferAssets": {
    "lovelace": "<amount as string>" | "max"
  }
}
```

**Variant 2 (from splash/trading wallet):**
```json
{
  "utxos": "splash-wallet",
  "changeAddress": "<source address>",
  "distAddress": "<destination address>",
  "transferAssets": {
    "lovelace": "<amount as string>" | "max",
    "assets": [...]
  }
}
```

---

## Balance Endpoint

### POST `{balanceUrl}/balance`
**Host:** `https://balance.snek.fun`

```json
{
  "address": "<bech32 address>"
}
```

**Response:**
```json
{
  "balance": [
    { "policyId": "", "base16Name": "", "amount": "1000000" },
    { "policyId": "<hex>", "base16Name": "<hex>", "amount": "500" }
  ]
}
```

---

## UTxO Monitor

### POST `https://utxo-monitor.snek.fun/getUtxos`
**Purpose:** Poll for UTxO availability (used after tx submission)
**Headers:** `Accept: application/json`, `Content-Type: application/json`

---

## Vesting Endpoints

### POST `{vestingUrl}/create-lock`
**Host:** `https://token-vesting.snek.fun`

```json
{
  "address": "<bech32 address>",
  "assetId": "<policyId.assetName>",
  "amount": "<amount as string>",
  "lockEnd": <unix timestamp>,
  "stagesCount": <number>
}
```

### POST `{vestingUrl}/withdraw`
**Host:** `https://token-vesting.snek.fun`

```json
{
  "id": "<lock id>",
  "address": "<bech32 address>"
}
```

---

## Authentication Endpoints (API Service)

All auth endpoints use `hosts.apiUrl` (`https://in-app-wallet.snek.fun/v1/user-account` or similar).

### POST `{apiUrl}/auth/random-bytes/create`
**Purpose:** Get random bytes for signing (nonce generation)
**Auth:** No credentials
**Response:** `{ uuid: "<uuid>", bytes: "<hex>" }`

### POST `{apiUrl}/auth/login`
**Purpose:** Authenticate with wallet signature
**Auth:** `withCredentials: true` (sets HTTP-only cookies)

```json
{
  "signature": "<hex signature>",
  "fundingWalletPublicKey": "<hex public key>",
  "postfix": "<hex>",
  "prefix": "<hex>",
  "uuid": "<uuid from random-bytes>",
  "deviceData": {
    "hash": "<device fingerprint hash>",
    "name": "<device name>"
  }
}
```

**Response:** `{ uuid: "<session uuid>", ... }` (may require 2FA)

### POST `{apiUrl}/auth/logout`
**Auth:** `withCredentials: true`

### POST `{apiUrl}/auth/revoke-access`
**Auth:** `withCredentials: true`

```json
{
  "fundingWalletPrefix": "<hex>",
  "fundingWalletPostfix": "<hex>",
  "fundingWalletSignature": "<hex>",
  "uuid": "<uuid>"
}
```

---

## Profile Endpoints

### GET `{apiUrl}/profile/status`
**Purpose:** Check if session is still valid
**Auth:** `withCredentials: true`

### POST `{apiUrl}/profile/get/by/token`
**Purpose:** Get user profile
**Auth:** `withCredentials: true`
**Response:** `{ profile: { ... } }`

### POST `{apiUrl}/profile/edit`
**Auth:** `withCredentials: true`

```json
{
  "image": "<optional base64>",
  "<other profile fields>": "..."
}
```

### POST `{apiUrl}/profile/seed/get`
**Purpose:** Get encrypted seed container
**Auth:** `withCredentials: true`
**Response:** `{ body: "<encrypted seed>" }`

### POST `{apiUrl}/profile/seed/link`
**Purpose:** Link a new trading wallet seed
**Auth:** `withCredentials: true`

```json
{
  "tradingWalletPublicKey": "<hex>",
  "tradingWalletSignature": "<hex>",
  "fundingWalletPostfix": "<hex>",
  "fundingWalletSignature": "<hex>",
  "fundingWalletPrefix": "<hex>",
  "<encrypted seed fields>": "..."
}
```

### POST `{apiUrl}/profile/seed/reset`
**Auth:** `withCredentials: true`

---

## Session Endpoints

### POST `{apiUrl}/session/create`
**Auth:** `withCredentials: true`

```json
{
  "tradingWalletSignature": "<hex>",
  "<encrypted session fields>": "..."
}
```

### POST `{apiUrl}/session/get`
**Auth:** `withCredentials: true`

---

## Two-Factor Auth Endpoints

### POST `{apiUrl}/two-factor-auth/init`
**Auth:** `withCredentials: true`
**Response:** `{ uuid: "<uuid>", message: "<message to sign>" }`

### POST `{apiUrl}/two-factor-auth/complete`
**Auth:** `withCredentials: true`

```json
{
  "uuid": "<uuid>",
  "totp": "<6-digit code>",
  "tradingWalletSignature": "<hex>",
  "fundingWalletPrefix": "<hex>",
  "fundingWalletPostfix": "<hex>",
  "fundingWalletSignature": "<hex>"
}
```

### POST `{apiUrl}/two-factor-auth/login`
**Auth:** `withCredentials: true`

```json
{
  "uuid": "<uuid>",
  "totp": "<6-digit code>"
}
```

### POST `{apiUrl}/two-factor-auth/recover`
**Auth:** `withCredentials: true`

---

## Passkey (WebAuthn) Endpoints

### POST `{apiUrl}/passkey/register/start`
**Auth:** `withCredentials: true`
**Response:** `{ publicKey: <WebAuthn creation options> }`

### POST `{apiUrl}/passkey/register/complete`
**Auth:** `withCredentials: true`

```json
{
  "uuid": "<uuid>",
  "body": "<PublicKeyCredential.toJSON()>",
  "signature": "<hex>"
}
```

### POST `{apiUrl}/passkey/auth/start`
**Auth:** `withCredentials: true`

```json
{
  "credentialId": "<credential id>"
}
```

**Response:** `{ body: { publicKey: <WebAuthn request options> } }`

### POST `{apiUrl}/passkey/auth/complete`
**Auth:** `withCredentials: true`
**Body:** `<PublicKeyCredential.toJSON()>`

### POST `{apiUrl}/passkey/reset`
**Auth:** `withCredentials: true`

---

## Mobile Device Auth Endpoints

### POST `{apiUrl}/device-auth/init`
**Auth:** `withCredentials: true`
**Response:** `{ uuid: "<uuid>" }`

### POST `{apiUrl}/device-auth/register`
**Auth:** `withCredentials: true`

```json
{
  "uuid": "<uuid>",
  "nonce": "<6-digit code>",
  "name": "<device name>",
  "deviceHash": "<fingerprint hash>",
  "saltPK": "<hex>",
  "ivPK": "<hex>",
  "ciphertextPK": "<hex>"
}
```

### POST `{apiUrl}/device-auth/device/poll`
**Purpose:** Poll for mobile device registration completion
**Auth:** `withCredentials: true`

```json
{
  "uuid": "<uuid>"
}
```

**Response:** `{ status: "pending" | "registered", data: { ... } }`

### POST `{apiUrl}/device-auth/desktop/poll`
**Purpose:** Poll from desktop side for mobile auth
**Auth:** `withCredentials: true`

### POST `{apiUrl}/device-auth/complete`
**Auth:** `withCredentials: true`

```json
{
  "uuid": "<uuid>",
  "signature": "<hex>",
  "prefix": "<hex>",
  "postfix": "<hex>",
  "ivSeed": "<hex>",
  "ciphertextSeed": "<hex>",
  "ephemeralPublicKey": "<hex>"
}
```

---

## Utility/Analytics Endpoints

### GET `{hosts.api.http}/v1/user-history-feed/initial/state/open?limit=100&offset=0`
**Purpose:** Get user's open order history

### POST `{hosts.utils.points}/aggregation/points`
**Purpose:** Get aggregated points for users

```json
{
  "users": ["<list of user identifiers>"]
}
```

### GET `{hosts.utils.referral}/get?tradingWalletPublicKeyHash={hash}`
**Purpose:** Get referral link info

### POST `{hosts.utils.referral}/validate`
**Purpose:** Validate a referral link

```json
{
  "link": "<referral link>"
}
```

### POST `{hosts.utils.referral}/link`
**Auth:** `withCredentials: true`

```json
{
  "link": "<referral code>",
  "prefix": "",
  "postfix": "",
  "signature": "<hex>",
  "tradingWalletPublicKeyHash": "<hex>"
}
```

### POST `{hosts.utils.claim}/execute`
**Purpose:** Execute a lottery/airdrop claim

```json
{
  "tradingWalletPublicKey": "<hex>",
  "skh": "<stake key hash>",
  "signature": "<hex>"
}
```

### POST `{hosts.utils.claim}/eligibility`
**Purpose:** Check claim eligibility

### POST `{hosts.utils.nsfwValidation}/validate/by/file`
**Purpose:** Validate image for NSFW content (FormData upload)

### POST `{avatarUploadUrl}/upload`
**Purpose:** Upload avatar image (FormData)

### GET `{hosts.utils.registryMetadata}{assetId}.json`
**Purpose:** Fetch token metadata from Spectrum registry
**Example:** `https://spectrum.fi/cardano/metadata/<policyId.assetName>.json`

### GET `{hosts.api.http}/v1/utility/ada-usd/rate`
**Purpose:** Get current ADA/USD exchange rate

### GET `{hosts.api.http}/v1/utility/server/time`
**Purpose:** Get server time for sync

### GET `{hosts.configs.ui}`
**Purpose:** Get UI configuration (maintenance mode, etc.)
**URL:** `https://analytics.snek.fun/snekfun-front/ui-state-v11.json`

### GET `https://analytics.snek.fun/snekfun-front/announcements.json`
**Purpose:** Get platform announcements

---

## WebSocket Connections

### Main Analytics WebSocket
**URL:** `wss://analytics.snek.fun/websocket/ws`
**Protocol:** v2
**Topics:**
- `UserHistoryByAddress` - User's order history updates (params: `{ address: "<bech32>" }`)
- `KOTH` - King of the Hill updates
- `Latest` - Latest token activity
- `New` - New token launches
- `Trending` - Trending tokens
- `MCapDesc` - Market cap descending
- `CompletedMCap` - Completed tokens by market cap
- `CompletedCreateOn` - Completed tokens by creation date

**Message format:** `JSON.stringify({ topic: "<topic>", ...params })`

**UserHistory message statuses:**
- `MempoolEvaluated` - Transaction evaluated in mempool
- `MempoolRefunded` - Transaction refunded from mempool

### Chart WebSocket
**URL:** `wss://charts.snek.fun/websocket/ws`
**Protocol:** v2
**Purpose:** Real-time price chart data (TradingView integration)

### Chat WebSocket
**URL:** `wss://chat-api.snek.fun/websocket/ws`
**Purpose:** Real-time chat for token pages

---

## Order Submission Flow

The complete buy/sell flow works as follows:

1. **User initiates trade** via the `Token.buy()` or `Token.sell()` method
2. **Token class calls `operations.trade()`** with:
   - `amount` (lovelace for buy, token amount for sell)
   - `assetId` (policyId.assetName)
   - `slippage` ("infinity" for sequential ordering, or numeric)
   - `side` ("BUY", "SELL", or "BUY_WITH_OUTPUT")
   - `wsClientId` (for WebSocket correlation)
3. **Operations calls `POST {builderUrl}/trade`** which returns unsigned tx CBOR
4. **Transaction is signed** via either:
   - In-app wallet (iframe): `iFrameConnector.signTx(cbor)`
   - Browser wallet (CIP-30): `wallet.signTx(cbor)`
5. **Signed tx is submitted** via `POST {builderUrl}/sign-and-submit` with `{ cbor, witness, changeAddress }`
6. **Response contains `txHash`**
7. **UTxO monitor polls** `POST utxo-monitor.snek.fun/getUtxos` to confirm tx

For CPMM trades (graduated tokens), the flow is identical but uses `/cpmm-trade` instead of `/trade`.

---

## Authentication Flow

1. **Get nonce:** `POST /auth/random-bytes/create` -> `{ uuid, bytes }`
2. **Sign with funding wallet:** Sign the bytes + device info with CIP-30 `signData`
3. **Login:** `POST /auth/login` with signature, public key, device data
4. **2FA (if enabled):** `POST /two-factor-auth/login` with TOTP code
5. **Session cookies set** via HTTP-only cookies (`withCredentials: true`)
6. **Get/create seed:** `POST /profile/seed/get` or `/profile/seed/link`
7. **Create session:** `POST /session/create` with encrypted session data

The `securedFetch` wrapper (`tZ`) handles:
- Adding `X-Request-ID` header (UUID v4) for tracing
- `credentials: "include"` for cookie-based auth
- `Content-Type: application/json` for non-FormData bodies
- Error mapping by HTTP status code (401 -> SessionExpired, etc.)

---

## Key Architecture Notes

- **"splash-wallet"** is a special UTxO source identifier meaning "use the in-app trading wallet's UTxOs server-side"
- The **in-app wallet** runs in an iframe at `wallet.snek.fun` with ECDSA P-384 key exchange for secure communication
- **Bonding curve math** is computed client-side using `mathjs` (cubic formula for AMM)
- **Token metadata** is fetched from Spectrum's registry at `spectrum.fi/cardano/metadata/`
- **IPFS images** are served via Pinata gateway at `snekdotfun.mypinata.cloud`
- The platform uses **Sentry** for error tracking
- All WebSocket connections use a **v2 protocol** with JSON message framing
