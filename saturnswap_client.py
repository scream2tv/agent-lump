"""
SaturnSwap DEX Client — Python wrapper for the SaturnSwap GraphQL API.

SaturnSwap is an order-book DEX on Cardano. Unlike AMM-based DEXes,
prices are set by limit orders (bids/asks). Every pool pairs ADA with
another token. Prices and amounts are in display units (not lovelace).

API endpoint: https://api.saturnswap.io/v1/graphql/
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

logger = logging.getLogger(__name__)

SATURN_GRAPHQL_URL = "https://api.saturnswap.io/v1/graphql/"
REQUEST_TIMEOUT = 15
MAX_RETRIES = 2
RETRY_BACKOFF = 1.5


def _gql(query: str, variables: Optional[dict] = None) -> dict:
    """Execute a GraphQL query against SaturnSwap."""
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(
                SATURN_GRAPHQL_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code in (429, 500, 502, 503, 520, 521, 522, 524):
                wait = RETRY_BACKOFF * (attempt + 1)
                logger.warning(
                    "SaturnSwap %d, retrying in %.0fs (%d/%d)",
                    resp.status_code, wait, attempt + 1, MAX_RETRIES,
                )
                time.sleep(wait)
                continue

            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                logger.warning("SaturnSwap GraphQL errors: %s", data["errors"])
            return data.get("data", {})

        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (attempt + 1))
                continue
            raise

    return {}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SaturnPool:
    """A SaturnSwap pool (always ADA paired with a token)."""
    pool_id: str
    ticker: str
    policy_id: str
    asset_name: str
    price_ada: float
    best_bid_ada: float
    best_ask_ada: float
    tvl: float
    volume_24h: float
    volume_7d: float

    @property
    def spread_ada(self) -> float:
        if self.best_ask_ada > 0 and self.best_bid_ada > 0:
            return self.best_ask_ada - self.best_bid_ada
        return 0.0

    @property
    def spread_pct(self) -> float:
        if self.best_ask_ada > 0:
            return (self.spread_ada / self.best_ask_ada) * 100
        return 0.0


@dataclass
class SaturnOrderBookEntry:
    price: float
    token_amount_sell: float
    token_amount_buy: float


@dataclass
class SaturnOrderBook:
    pool_id: str
    bids: list[SaturnOrderBookEntry] = field(default_factory=list)
    asks: list[SaturnOrderBookEntry] = field(default_factory=list)

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 0.0

    @property
    def spread_pct(self) -> float:
        if self.best_ask > 0 and self.best_bid > 0:
            return ((self.best_ask - self.best_bid) / self.best_ask) * 100
        return 0.0

    @property
    def bid_depth_ada(self) -> float:
        return sum(e.token_amount_buy for e in self.bids)

    @property
    def ask_depth_tokens(self) -> float:
        return sum(e.token_amount_sell for e in self.asks)


@dataclass
class SaturnSwapEstimate:
    """Estimated output for a market swap on SaturnSwap."""
    token_in_ticker: str
    token_out_ticker: str
    amount_in_display: float
    estimated_out_display: float
    price_ada: float
    source: str = "saturnswap"


# ---------------------------------------------------------------------------
# Pool queries
# ---------------------------------------------------------------------------

_POOL_FIELDS = """
    id
    token_project_one { ticker decimals }
    token_project_two {
        ticker
        decimals
        price
        highest_bid_price
        lowest_ask_price
        policy_id
        asset_name
    }
    pool_stats {
        tvl
        volume_1d
        volume_7d
        volume_30d
    }
"""


def get_pool_by_tokens(policy_id: str, asset_name: str = "") -> Optional[SaturnPool]:
    """Look up a SaturnSwap pool by token policy ID and asset name."""
    query = f"""
    query {{
        poolByTokens(input: {{
            policyIdOne: ""
            assetNameOne: ""
            policyIdTwo: "{policy_id}"
            assetNameTwo: "{asset_name}"
        }}) {{
            {_POOL_FIELDS}
        }}
    }}
    """
    data = _gql(query)
    pool = data.get("poolByTokens")
    if not pool:
        return None
    return _parse_pool(pool)


def get_pool_by_id(pool_id: str) -> Optional[SaturnPool]:
    """Look up a SaturnSwap pool by UUID."""
    query = f"""
    query {{
        pool(id: "{pool_id}") {{
            {_POOL_FIELDS}
        }}
    }}
    """
    data = _gql(query)
    pool = data.get("pool")
    if not pool:
        return None
    return _parse_pool(pool)


def list_pools(first: int = 20, order_by: str = "volume_1d") -> list[SaturnPool]:
    """List top pools sorted by volume or TVL."""
    query = f"""
    query {{
        pools(first: {first}, order: {{ pool_stats: {{ {order_by}: DESC }} }}) {{
            edges {{
                node {{
                    {_POOL_FIELDS}
                }}
            }}
        }}
    }}
    """
    data = _gql(query)
    edges = data.get("pools", {}).get("edges", [])
    return [_parse_pool(e["node"]) for e in edges if e.get("node")]


def _parse_pool(p: dict) -> SaturnPool:
    t2 = p.get("token_project_two") or {}
    stats = p.get("pool_stats") or {}
    return SaturnPool(
        pool_id=p.get("id", ""),
        ticker=t2.get("ticker", "???"),
        policy_id=t2.get("policy_id", ""),
        asset_name=t2.get("asset_name", ""),
        price_ada=float(t2.get("price") or 0),
        best_bid_ada=float(t2.get("highest_bid_price") or 0),
        best_ask_ada=float(t2.get("lowest_ask_price") or 0),
        tvl=float(stats.get("tvl") or 0),
        volume_24h=float(stats.get("volume_1d") or 0),
        volume_7d=float(stats.get("volume_7d") or 0),
    )


# ---------------------------------------------------------------------------
# Order book
# ---------------------------------------------------------------------------

_PLACEHOLDER_ADDRESS = (
    "addr1qx2fxv2umyhttkxyxp8x0dlpdt3k6cwng5pxj3jhsydzer3"
    "jcu5d8ps7zex2k2xt3uqxgjqnnj83ws8lhrn648jjxtwq2ytjqp"
)


def get_order_book(pool_id: str, depth: int = 10, address: str = "") -> SaturnOrderBook:
    """Fetch bids and asks for a pool.

    ``address`` excludes the caller's own orders from the book.
    Pass the user's wallet address when available; defaults to a
    zero-utxo placeholder so the full book is returned.
    """
    addr = address or _PLACEHOLDER_ADDRESS
    ask_query = f"""
    query {{
        orderBookSellPoolUtxos(address: "{addr}", first: {depth}, where: {{ pool_id: {{ eq: "{pool_id}" }} }}, order: {{ price: ASC }}) {{
            edges {{
                node {{ price token_amount_sell token_amount_buy }}
            }}
        }}
    }}
    """
    bid_query = f"""
    query {{
        orderBookBuyPoolUtxos(address: "{addr}", first: {depth}, where: {{ pool_id: {{ eq: "{pool_id}" }} }}, order: {{ price: DESC }}) {{
            edges {{
                node {{ price token_amount_sell token_amount_buy }}
            }}
        }}
    }}
    """
    ask_data = _gql(ask_query)
    bid_data = _gql(bid_query)

    asks = [
        SaturnOrderBookEntry(
            price=float(e["node"]["price"]),
            token_amount_sell=float(e["node"]["token_amount_sell"]),
            token_amount_buy=float(e["node"]["token_amount_buy"]),
        )
        for e in ask_data.get("orderBookSellPoolUtxos", {}).get("edges", [])
        if e.get("node")
    ]

    bids = [
        SaturnOrderBookEntry(
            price=float(e["node"]["price"]),
            token_amount_sell=float(e["node"]["token_amount_sell"]),
            token_amount_buy=float(e["node"]["token_amount_buy"]),
        )
        for e in bid_data.get("orderBookBuyPoolUtxos", {}).get("edges", [])
        if e.get("node")
    ]

    return SaturnOrderBook(pool_id=pool_id, bids=bids, asks=asks)


# ---------------------------------------------------------------------------
# Swap estimation (walk the order book)
# ---------------------------------------------------------------------------

def estimate_buy(pool_id: str, ada_amount_display: float) -> Optional[SaturnSwapEstimate]:
    """Estimate how many tokens you get for `ada_amount_display` ADA (market buy).

    Walks the ask side of the order book to simulate filling.
    """
    ob = get_order_book(pool_id, depth=50)
    if not ob.asks:
        return None

    pool = get_pool_by_id(pool_id)
    ticker = pool.ticker if pool else "?"

    remaining_ada = ada_amount_display
    tokens_received = 0.0

    for ask in ob.asks:
        if remaining_ada <= 0:
            break
        order_ada_cost = ask.token_amount_sell * ask.price
        if order_ada_cost <= 0:
            continue
        if remaining_ada >= order_ada_cost:
            tokens_received += ask.token_amount_sell
            remaining_ada -= order_ada_cost
        else:
            fraction = remaining_ada / order_ada_cost
            tokens_received += ask.token_amount_sell * fraction
            remaining_ada = 0

    if tokens_received <= 0:
        return None

    effective_price = ada_amount_display / tokens_received if tokens_received > 0 else 0

    return SaturnSwapEstimate(
        token_in_ticker="ADA",
        token_out_ticker=ticker,
        amount_in_display=ada_amount_display,
        estimated_out_display=tokens_received,
        price_ada=effective_price,
    )


def estimate_sell(pool_id: str, token_amount_display: float) -> Optional[SaturnSwapEstimate]:
    """Estimate how much ADA you get for selling `token_amount_display` tokens (market sell).

    Walks the bid side of the order book to simulate filling.
    """
    ob = get_order_book(pool_id, depth=50)
    if not ob.bids:
        return None

    pool = get_pool_by_id(pool_id)
    ticker = pool.ticker if pool else "?"

    remaining_tokens = token_amount_display
    ada_received = 0.0

    for bid in ob.bids:
        if remaining_tokens <= 0:
            break
        order_token_size = bid.token_amount_buy
        if order_token_size <= 0:
            continue
        if remaining_tokens >= order_token_size:
            ada_received += order_token_size * bid.price
            remaining_tokens -= order_token_size
        else:
            ada_received += remaining_tokens * bid.price
            remaining_tokens = 0

    if ada_received <= 0:
        return None

    effective_price = ada_received / token_amount_display if token_amount_display > 0 else 0

    return SaturnSwapEstimate(
        token_in_ticker=ticker,
        token_out_ticker="ADA",
        amount_in_display=token_amount_display,
        estimated_out_display=ada_received,
        price_ada=effective_price,
    )


# ---------------------------------------------------------------------------
# Transaction execution: Create → Sign → Submit
# ---------------------------------------------------------------------------

@dataclass
class SaturnSwapTx:
    """Unsigned transaction returned by createOrderTransaction."""
    transaction_id: str
    hex_transaction: str


def create_market_swap(
    payment_address: str,
    pool_id: str,
    token_amount_sell: float,
    token_amount_buy: float = 0,
    market_order_type: str = "MARKET_BUY_ORDER",
    slippage: float = 3.0,
    version: int = 2,
) -> SaturnSwapTx:
    """Create an unsigned market swap transaction via SaturnSwap GraphQL.

    All amounts are in DISPLAY units (e.g. 10 = 10 ADA, not lovelace).

    market_order_type:
        MARKET_BUY_ORDER  — selling ADA to buy tokens
        MARKET_SELL_ORDER — selling tokens for ADA
    """
    query = f"""
    mutation {{
        createOrderTransaction(input: {{
            paymentAddress: "{payment_address}"
            marketOrderComponents: [{{
                poolId: "{pool_id}"
                tokenAmountSell: {token_amount_sell}
                tokenAmountBuy: {token_amount_buy}
                marketOrderType: {market_order_type}
                slippage: {slippage}
                version: {version}
            }}]
        }}) {{
            successTransactions {{ transactionId hexTransaction }}
            error {{ message }}
        }}
    }}
    """
    data = _gql(query)
    result = data.get("createOrderTransaction", {})

    error = result.get("error")
    if error and error.get("message"):
        raise RuntimeError(f"SaturnSwap create swap error: {error['message']}")

    txs = result.get("successTransactions", [])
    if not txs:
        raise RuntimeError("SaturnSwap returned no transaction — check amount and pool availability")

    tx = txs[0]
    return SaturnSwapTx(
        transaction_id=tx["transactionId"],
        hex_transaction=tx["hexTransaction"],
    )


def sign_transaction(unsigned_hex: str, signing_key) -> str:
    """Sign a SaturnSwap unsigned transaction using pycardano.

    Preserves any existing witnesses from the API-built transaction
    (e.g. script witnesses) and appends our vkey witness.

    Returns fully-signed CBOR hex ready for submission.
    """
    import cbor2
    from hashlib import blake2b
    from pycardano import PaymentVerificationKey

    tx_bytes = bytes.fromhex(unsigned_hex)
    tx_array = cbor2.loads(tx_bytes)
    body_bytes = cbor2.dumps(tx_array[0])
    tx_hash = blake2b(body_bytes, digest_size=32).digest()

    vk = PaymentVerificationKey.from_signing_key(signing_key)
    signature = signing_key.sign(tx_hash)

    vkey_witness = [vk.payload, signature]
    existing_witnesses = tx_array[1] if isinstance(tx_array[1], dict) else {}
    existing_vkeys = existing_witnesses.get(0, [])
    existing_vkeys.append(vkey_witness)
    existing_witnesses[0] = existing_vkeys

    tx_array[1] = existing_witnesses
    return cbor2.dumps(tx_array).hex()


def submit_order_transaction(
    payment_address: str,
    transaction_id: str,
    signed_hex: str,
) -> str:
    """Submit a signed transaction back to SaturnSwap for broadcast.

    Returns the confirmed transaction ID.
    """
    query = f"""
    mutation {{
        submitOrderTransaction(input: {{
            paymentAddress: "{payment_address}"
            successTransactions: [{{
                transactionId: "{transaction_id}"
                hexTransaction: "{signed_hex}"
            }}]
        }}) {{
            successTransactions {{ transactionId }}
            error {{ message }}
        }}
    }}
    """
    data = _gql(query)
    result = data.get("submitOrderTransaction", {})

    error = result.get("error")
    if error and error.get("message"):
        raise RuntimeError(f"SaturnSwap submit error: {error['message']}")

    txs = result.get("successTransactions", [])
    if not txs:
        raise RuntimeError("SaturnSwap submission returned no confirmation")

    return txs[0]["transactionId"]
