"""
Bodega Market Client (V3)

Prediction market on Cardano with LMSR pricing.
Buy/sell YES/NO shares on real-world event outcomes.

API base: https://v3.bodegamarket.io/api
Docs: https://docs.bodegacardano.org

Flow:
  1. Fetch markets via getMarketConfigs
  2. POST /api/buyPosition or /api/sellPosition -> unsigned CBOR
  3. Sign locally with pycardano
  4. Assemble via /api/wallet/assemble -> fully signed tx
  5. Submit via Blockfrost or /api/wallet/submit
"""

import logging
import os
import time
from dataclasses import dataclass, field
from hashlib import blake2b
from typing import Optional

import cbor2
import requests
from dotenv import load_dotenv
from pycardano import PaymentSigningKey, PaymentVerificationKey

from blockfrost_client import BlockfrostClient

load_dotenv()

logger = logging.getLogger(__name__)

BODEGA_BASE_URL = os.environ.get(
    "BODEGA_BASE_URL", "https://v3.bodegamarket.io"
)
BODEGA_API_URL = f"{BODEGA_BASE_URL}/api"

MAX_RETRIES = 3
RETRY_BACKOFF = 2.0
ADA_LOVELACE = 1_000_000


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MarketPrices:
    yes_price: float
    no_price: float
    yes_volume: int
    no_volume: int
    total_yes_shares: int
    total_no_shares: int
    liquidity: int
    total_volume: int
    total_fees: int


@dataclass
class MarketOption:
    title: str
    description: str
    yes_label: str
    no_label: str
    yes_token_name: str
    no_token_name: str
    b_param: int
    seed_out_ref: dict
    fee: float
    share_policy_id: str
    positions_address: str
    prediction_info: Optional[MarketPrices] = None


@dataclass
class Market:
    id: str
    name: str
    description: str
    status: str
    tags: list[str]
    deadline: int
    creator_address: str
    options: list[MarketOption]
    winning_side: Optional[str] = None
    payment_policy_id: str = ""
    payment_token_name: str = ""
    protocol_config_index: int = 0
    img: str = ""
    time_created: int = 0


@dataclass
class BuyResult:
    cbor: str
    transaction: dict


@dataclass
class TradeResult:
    tx_hash: str
    market_id: str
    side: str
    amount: int
    price: float


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post(path: str, payload: dict) -> dict:
    url = f"{BODEGA_API_URL}{path}"
    last_resp = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=60,
            )
            last_resp = resp
            if resp.status_code in (429, 500, 502, 503):
                wait = RETRY_BACKOFF * (attempt + 1)
                logger.warning(
                    "Bodega %d on %s (attempt %d/%d), retrying in %.1fs",
                    resp.status_code, path, attempt + 1, MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                try:
                    err = resp.json()
                    error_msg = err.get("error", resp.text[:500])
                except Exception:
                    error_msg = resp.text[:500]
                raise RuntimeError(
                    f"Bodega API {resp.status_code} on {path}: {error_msg}"
                )
            return resp.json()
        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (attempt + 1))
                continue
            raise

    body_preview = ""
    if last_resp is not None:
        try:
            body_preview = f" | body: {last_resp.text[:200]}"
        except Exception:
            pass
    raise RuntimeError(
        f"Bodega API exhausted retries on {path}{body_preview}"
    )


def _get(path: str, params: Optional[dict] = None) -> dict:
    url = f"{BODEGA_API_URL}{path}"
    last_resp = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=30)
            last_resp = resp
            if resp.status_code in (429, 500, 502, 503):
                wait = RETRY_BACKOFF * (attempt + 1)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (attempt + 1))
                continue
            raise

    if last_resp is not None:
        last_resp.raise_for_status()
    raise RuntimeError(f"Bodega API exhausted retries on {path}")


# ---------------------------------------------------------------------------
# Market discovery
# ---------------------------------------------------------------------------

def get_market_configs() -> list[Market]:
    """Fetch all market configurations."""
    data = _post("/getMarketConfigs", {})
    markets = []
    for cfg in data.get("marketConfigs", []):
        options = []
        for opt in cfg.get("options", []):
            pi = opt.get("predictionInfo", {})
            prices = pi.get("prices", {})
            volumes = pi.get("volumes", {})
            shares = pi.get("totalShares", {})
            prediction_info = MarketPrices(
                yes_price=prices.get("yesPrice", 0) / ADA_LOVELACE,
                no_price=prices.get("noPrice", 0) / ADA_LOVELACE,
                yes_volume=volumes.get("yesVolume", 0),
                no_volume=volumes.get("noVolume", 0),
                total_yes_shares=shares.get("totalYes", 0),
                total_no_shares=shares.get("totalNo", 0),
                liquidity=pi.get("liquidity", 0),
                total_volume=pi.get("totalVolume", 0),
                total_fees=pi.get("totalFees", 0),
            )
            options.append(MarketOption(
                title=opt.get("title", ""),
                description=opt.get("description", ""),
                yes_label=opt.get("yesLabel", "Yes"),
                no_label=opt.get("noLabel", "No"),
                yes_token_name=opt.get("yesTokenName", ""),
                no_token_name=opt.get("noTokenName", ""),
                b_param=opt.get("bParam", 0),
                seed_out_ref=opt.get("seedOutRef", {}),
                fee=opt.get("fee", 0.02),
                share_policy_id=opt.get("sharePolicyId", ""),
                positions_address=opt.get("positionsAddress", ""),
                prediction_info=prediction_info,
            ))
        markets.append(Market(
            id=cfg["id"],
            name=cfg.get("name", ""),
            description=cfg.get("description", ""),
            status=cfg.get("status", ""),
            tags=cfg.get("tags", []),
            deadline=cfg.get("deadline", 0),
            creator_address=cfg.get("creatorAddress", ""),
            options=options,
            winning_side=cfg.get("winningSide"),
            payment_policy_id=cfg.get("paymentPolicyId", ""),
            payment_token_name=cfg.get("paymentTokenName", ""),
            protocol_config_index=cfg.get("protocolConfigIndex", 0),
            img=cfg.get("img", ""),
            time_created=cfg.get("time", 0),
        ))
    return markets


def get_active_markets() -> list[Market]:
    """Fetch only active (tradeable) markets."""
    return [m for m in get_market_configs() if m.status == "Active"]


def get_prediction_info(market_id: str) -> MarketPrices:
    """Fetch live prediction info (prices, volumes) for a market."""
    data = _get("/getPredictionInfo", {"id": market_id})
    pi = data.get("predictionInfo", {})
    prices = pi.get("prices", {})
    volumes = pi.get("volumes", {})
    shares = pi.get("totalShares", {})
    return MarketPrices(
        yes_price=prices.get("yesPrice", 0) / ADA_LOVELACE,
        no_price=prices.get("noPrice", 0) / ADA_LOVELACE,
        yes_volume=volumes.get("yesVolume", 0),
        no_volume=volumes.get("noVolume", 0),
        total_yes_shares=shares.get("totalYes", 0),
        total_no_shares=shares.get("totalNo", 0),
        liquidity=pi.get("liquidity", 0),
        total_volume=pi.get("totalVolume", 0),
        total_fees=pi.get("totalFees", 0),
    )


def get_predictions_history(market_id: str) -> list[dict]:
    """Fetch price history for charting."""
    data = _get("/getPredictionsHistory", {"id": market_id})
    return data.get("predictionsHistory", [])


def get_wallet_balance(address: str) -> dict:
    """Fetch token balances for an address via Bodega's API."""
    return _get("/wallet/balance", {"address": address})


def get_recent_activity() -> list[dict]:
    """Fetch recent trading activity across all markets."""
    data = _get("/stats/getRecentActivity")
    return data.get("data", [])


def get_global_stats() -> dict:
    """Fetch platform-wide statistics."""
    return _get("/stats/getGlobalStats")


# ---------------------------------------------------------------------------
# Transaction building (server-side)
# ---------------------------------------------------------------------------

def build_buy_position(
    *,
    market_id: str,
    side: str,
    amount: int,
    address: str,
    price: int,
    slippage: float = 0.05,
    option: int = 0,
    canonical: bool = False,
) -> BuyResult:
    """Request the Bodega server to build a buy transaction.

    Args:
        market_id: Market ID (e.g. "F6D0_NBA_RAPTORS_25_").
        side: "Yes" or "No".
        amount: Number of shares to buy.
        address: Buyer's bech32 address.
        price: Current price in lovelace (from predictionInfo).
        slippage: Slippage tolerance (0.05 = 5%).
        option: Option index (usually 0 for binary markets).
        canonical: Whether to use canonical CBOR encoding.

    Returns:
        BuyResult with unsigned CBOR and transaction metadata.
    """
    if side not in ("Yes", "No"):
        raise ValueError(f"side must be 'Yes' or 'No', got '{side}'")

    request_body = {
        "id": market_id,
        "option": option,
        "side": side,
        "amount": amount,
        "slippage": slippage,
        "address": address,
        "price": price,
        "canonical": canonical,
    }
    data = _post("/buyPosition", {"request": request_body})
    return BuyResult(
        cbor=data["cbor"],
        transaction=data.get("transaction", {}),
    )


def build_sell_position(
    *,
    market_id: str,
    side: str,
    amount: int,
    address: str,
    price: int,
    slippage: float = 0.05,
    option: int = 0,
    canonical: bool = False,
) -> BuyResult:
    """Request the Bodega server to build a sell transaction.

    Args:
        market_id: Market ID.
        side: "Yes" or "No".
        amount: Number of shares to sell.
        address: Seller's bech32 address.
        price: Current price in lovelace.
        slippage: Slippage tolerance.
        option: Option index.
        canonical: Whether to use canonical CBOR encoding.

    Returns:
        BuyResult with unsigned CBOR and transaction metadata.
    """
    if side not in ("Yes", "No"):
        raise ValueError(f"side must be 'Yes' or 'No', got '{side}'")

    request_body = {
        "id": market_id,
        "option": option,
        "side": side,
        "amount": amount,
        "slippage": slippage,
        "address": address,
        "price": price,
        "canonical": canonical,
    }
    data = _post("/sellPosition", {"request": request_body})
    return BuyResult(
        cbor=data["cbor"],
        transaction=data.get("transaction", {}),
    )


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

def sign_transaction(unsigned_cbor_hex: str, signing_key: PaymentSigningKey) -> str:
    """Produce a CIP-30 style witness hex from an unsigned transaction.

    The Bodega assemble endpoint expects a witness set (not a fully signed tx),
    matching what a CIP-30 wallet's signTx() returns.

    Returns hex-encoded CBOR witness set.
    """
    tx_bytes = bytes.fromhex(unsigned_cbor_hex)
    tx_array = cbor2.loads(tx_bytes)
    body_bytes = cbor2.dumps(tx_array[0])
    tx_hash = blake2b(body_bytes, digest_size=32).digest()

    vk = PaymentVerificationKey.from_signing_key(signing_key)
    signature = signing_key.sign(tx_hash)

    witness_set = {0: [[vk.payload, signature]]}
    return cbor2.dumps(witness_set).hex()


def sign_transaction_full(unsigned_cbor_hex: str, signing_key: PaymentSigningKey) -> str:
    """Sign and return the fully signed transaction CBOR hex.

    Use this for direct Blockfrost submission (bypassing Bodega assemble).
    """
    tx_bytes = bytes.fromhex(unsigned_cbor_hex)
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


def get_tx_hash(unsigned_cbor_hex: str) -> str:
    """Compute the transaction hash from unsigned CBOR."""
    tx_bytes = bytes.fromhex(unsigned_cbor_hex)
    tx_array = cbor2.loads(tx_bytes)
    body_bytes = cbor2.dumps(tx_array[0])
    return blake2b(body_bytes, digest_size=32).hexdigest()


# ---------------------------------------------------------------------------
# Assembly + Submission
# ---------------------------------------------------------------------------

def assemble_transaction(
    *,
    address: str,
    tx_cbor: str,
    witnesses: list[str],
    canonical: bool = False,
) -> str:
    """Assemble a signed transaction via Bodega's wallet API.

    Combines the unsigned tx with witness set(s) server-side.

    Returns the fully signed transaction CBOR hex.
    """
    data = _post("/wallet/assemble", {
        "address": address,
        "tx": tx_cbor,
        "witnesses": witnesses,
        "canonical": canonical,
    })
    return data["signedTx"]


def submit_via_bodega(*, address: str, signed_tx: str) -> str:
    """Submit a signed transaction via Bodega's wallet API.

    Returns the transaction hash.
    """
    data = _post("/wallet/submit", {
        "address": address,
        "tx": signed_tx,
    })
    return data["txHash"]


# ---------------------------------------------------------------------------
# High-level: execute buy
# ---------------------------------------------------------------------------

def execute_buy(
    *,
    market_id: str,
    side: str,
    amount: int,
    address: str,
    signing_key: PaymentSigningKey,
    slippage: float = 0.05,
    submit_method: str = "bodega",
    blockfrost: Optional[BlockfrostClient] = None,
    dry_run: bool = False,
) -> TradeResult:
    """Buy shares on a Bodega prediction market.

    Args:
        market_id: Market ID string.
        side: "Yes" or "No".
        amount: Number of shares to buy.
        address: Your bech32 Cardano address.
        signing_key: pycardano PaymentSigningKey.
        slippage: Slippage tolerance (default 5%).
        submit_method: "bodega" (via their API) or "blockfrost" (direct).
        blockfrost: BlockfrostClient instance (required if submit_method="blockfrost").
        dry_run: If True, build and sign but don't submit.

    Returns:
        TradeResult with tx hash and trade details.
    """
    info = get_prediction_info(market_id)
    price = int(info.yes_price * ADA_LOVELACE) if side == "Yes" else int(info.no_price * ADA_LOVELACE)

    logger.info(
        "Building BUY %s x%d on %s (price: %.4f ADA, slippage: %.1f%%)",
        side, amount, market_id, price / ADA_LOVELACE, slippage * 100,
    )

    result = build_buy_position(
        market_id=market_id,
        side=side,
        amount=amount,
        address=address,
        price=price,
        slippage=slippage,
    )

    tx_hash_computed = get_tx_hash(result.cbor)
    logger.info("Unsigned tx hash: %s", tx_hash_computed)

    if dry_run:
        logger.info("DRY RUN - not submitting")
        return TradeResult(
            tx_hash=tx_hash_computed,
            market_id=market_id,
            side=side,
            amount=amount,
            price=price / ADA_LOVELACE,
        )

    if submit_method == "bodega":
        witness_hex = sign_transaction(result.cbor, signing_key)
        signed_tx = assemble_transaction(
            address=address,
            tx_cbor=result.cbor,
            witnesses=[witness_hex],
        )
        tx_hash = submit_via_bodega(address=address, signed_tx=signed_tx)
    elif submit_method == "blockfrost":
        if blockfrost is None:
            raise ValueError("blockfrost client required for blockfrost submission")
        signed_cbor = sign_transaction_full(result.cbor, signing_key)
        tx_hash = blockfrost.submit_tx(signed_cbor)
    else:
        raise ValueError(f"Unknown submit_method: {submit_method}")

    logger.info("Submitted tx: %s", tx_hash)
    return TradeResult(
        tx_hash=tx_hash,
        market_id=market_id,
        side=side,
        amount=amount,
        price=price / ADA_LOVELACE,
    )


def execute_sell(
    *,
    market_id: str,
    side: str,
    amount: int,
    address: str,
    signing_key: PaymentSigningKey,
    slippage: float = 0.05,
    submit_method: str = "bodega",
    blockfrost: Optional[BlockfrostClient] = None,
    dry_run: bool = False,
) -> TradeResult:
    """Sell shares on a Bodega prediction market.

    Args:
        market_id: Market ID string.
        side: "Yes" or "No".
        amount: Number of shares to sell.
        address: Your bech32 Cardano address.
        signing_key: pycardano PaymentSigningKey.
        slippage: Slippage tolerance (default 5%).
        submit_method: "bodega" or "blockfrost".
        blockfrost: BlockfrostClient instance (required if submit_method="blockfrost").
        dry_run: If True, build and sign but don't submit.

    Returns:
        TradeResult with tx hash and trade details.
    """
    info = get_prediction_info(market_id)
    price = int(info.yes_price * ADA_LOVELACE) if side == "Yes" else int(info.no_price * ADA_LOVELACE)

    logger.info(
        "Building SELL %s x%d on %s (price: %.4f ADA, slippage: %.1f%%)",
        side, amount, market_id, price / ADA_LOVELACE, slippage * 100,
    )

    result = build_sell_position(
        market_id=market_id,
        side=side,
        amount=amount,
        address=address,
        price=price,
        slippage=slippage,
    )

    tx_hash_computed = get_tx_hash(result.cbor)
    logger.info("Unsigned tx hash: %s", tx_hash_computed)

    if dry_run:
        logger.info("DRY RUN - not submitting")
        return TradeResult(
            tx_hash=tx_hash_computed,
            market_id=market_id,
            side=side,
            amount=amount,
            price=price / ADA_LOVELACE,
        )

    if submit_method == "bodega":
        witness_hex = sign_transaction(result.cbor, signing_key)
        signed_tx = assemble_transaction(
            address=address,
            tx_cbor=result.cbor,
            witnesses=[witness_hex],
        )
        tx_hash = submit_via_bodega(address=address, signed_tx=signed_tx)
    elif submit_method == "blockfrost":
        if blockfrost is None:
            raise ValueError("blockfrost client required for blockfrost submission")
        signed_cbor = sign_transaction_full(result.cbor, signing_key)
        tx_hash = blockfrost.submit_tx(signed_cbor)
    else:
        raise ValueError(f"Unknown submit_method: {submit_method}")

    logger.info("Submitted tx: %s", tx_hash)
    return TradeResult(
        tx_hash=tx_hash,
        market_id=market_id,
        side=side,
        amount=amount,
        price=price / ADA_LOVELACE,
    )


# ---------------------------------------------------------------------------
# Portfolio helpers
# ---------------------------------------------------------------------------

def get_my_positions(address: str, markets: Optional[list[Market]] = None) -> list[dict]:
    """Get all Bodega share positions for an address.

    Returns a list of dicts with market info and share counts.
    """
    balance = get_wallet_balance(address)
    if markets is None:
        markets = get_active_markets()

    policy_to_market = {}
    for m in markets:
        for opt in m.options:
            policy_to_market[opt.share_policy_id] = (m, opt)

    positions = []
    for asset_key, qty in balance.items():
        if "." not in asset_key or asset_key == "lovelace":
            continue
        policy_id, token_hex = asset_key.split(".", 1)
        if policy_id not in policy_to_market:
            continue

        market, option = policy_to_market[policy_id]
        try:
            token_name = bytes.fromhex(token_hex).decode("utf-8")
        except Exception:
            token_name = token_hex

        side = None
        if token_name == option.yes_token_name:
            side = "Yes"
        elif token_name == option.no_token_name:
            side = "No"

        if side:
            positions.append({
                "market_id": market.id,
                "market_name": market.name,
                "side": side,
                "shares": int(qty),
                "token_name": token_name,
                "policy_id": policy_id,
                "status": market.status,
                "deadline": market.deadline,
            })

    return positions


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _format_ada(lovelace: int) -> str:
    return f"{lovelace / ADA_LOVELACE:,.2f}"


def _cli():
    import argparse
    import json
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Bodega Market V3 - Prediction Market Client",
    )
    sub = parser.add_subparsers(dest="command")

    # --- markets ---
    p_markets = sub.add_parser("markets", help="List active markets")
    p_markets.add_argument("--tag", help="Filter by tag (e.g. sports, politics, crypto)")
    p_markets.add_argument("--search", help="Search market names")
    p_markets.add_argument("--json", action="store_true", help="Output raw JSON")

    # --- info ---
    p_info = sub.add_parser("info", help="Show market details")
    p_info.add_argument("market_id", help="Market ID")

    # --- buy ---
    p_buy = sub.add_parser("buy", help="Buy shares")
    p_buy.add_argument("market_id", help="Market ID")
    p_buy.add_argument("side", choices=["Yes", "No"], help="Side to buy")
    p_buy.add_argument("amount", type=int, help="Number of shares")
    p_buy.add_argument("--slippage", type=float, default=0.05, help="Slippage tolerance (default 0.05)")
    p_buy.add_argument("--submit", choices=["bodega", "blockfrost"], default="bodega")
    p_buy.add_argument("--dry-run", action="store_true", help="Build but don't submit")

    # --- sell ---
    p_sell = sub.add_parser("sell", help="Sell shares")
    p_sell.add_argument("market_id", help="Market ID")
    p_sell.add_argument("side", choices=["Yes", "No"], help="Side to sell")
    p_sell.add_argument("amount", type=int, help="Number of shares")
    p_sell.add_argument("--slippage", type=float, default=0.05, help="Slippage tolerance (default 0.05)")
    p_sell.add_argument("--submit", choices=["bodega", "blockfrost"], default="bodega")
    p_sell.add_argument("--dry-run", action="store_true", help="Build but don't submit")

    # --- positions ---
    p_pos = sub.add_parser("positions", help="Show your positions")

    # --- activity ---
    sub.add_parser("activity", help="Show recent activity")

    # --- stats ---
    sub.add_parser("stats", help="Show global stats")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "markets":
        markets = get_active_markets()
        if args.tag:
            markets = [m for m in markets if args.tag.lower() in [t.lower() for t in m.tags]]
        if args.search:
            q = args.search.lower()
            markets = [m for m in markets if q in m.name.lower()]

        if getattr(args, "json", False):
            import dataclasses
            print(json.dumps([dataclasses.asdict(m) for m in markets[:50]], indent=2))
            return

        if not markets:
            print("No markets found.")
            return

        print(f"\n{'ID':<30} {'Yes':>8} {'No':>8} {'Vol':>10} {'Tags':<20} Name")
        print("-" * 110)
        for m in sorted(markets, key=lambda x: x.options[0].prediction_info.total_volume if x.options and x.options[0].prediction_info else 0, reverse=True)[:50]:
            if not m.options:
                continue
            pi = m.options[0].prediction_info
            if pi is None:
                continue
            print(
                f"{m.id:<30} {pi.yes_price:>7.2f}  {pi.no_price:>7.2f}  "
                f"{_format_ada(pi.total_volume):>10} {','.join(m.tags):<20} {m.name[:50]}"
            )

    elif args.command == "info":
        markets = get_market_configs()
        market = next((m for m in markets if m.id == args.market_id), None)
        if not market:
            print(f"Market not found: {args.market_id}")
            sys.exit(1)

        info = get_prediction_info(args.market_id)
        print(f"\n  Market: {market.name}")
        print(f"  ID:     {market.id}")
        print(f"  Status: {market.status}")
        print(f"  Tags:   {', '.join(market.tags)}")
        print(f"  Description: {market.description[:200]}")
        print()
        print(f"  YES price: {info.yes_price:.4f} ADA  ({info.yes_price*100:.1f}%)")
        print(f"  NO  price: {info.no_price:.4f} ADA  ({info.no_price*100:.1f}%)")
        print(f"  YES shares: {info.total_yes_shares}")
        print(f"  NO  shares: {info.total_no_shares}")
        print(f"  Liquidity:  {_format_ada(info.liquidity)} ADA")
        print(f"  Volume:     {_format_ada(info.total_volume)} ADA")
        print(f"  Fees:       {_format_ada(info.total_fees)} ADA")
        if market.deadline:
            from datetime import datetime, timezone
            dl = datetime.fromtimestamp(market.deadline / 1000, tz=timezone.utc)
            print(f"  Deadline:   {dl.strftime('%Y-%m-%d %H:%M UTC')}")

    elif args.command in ("buy", "sell"):
        address = os.environ.get("CARDANO_PAYMENT_ADDRESS")
        key_path = os.environ.get("CARDANO_PRIVATE_KEY_PATH")
        if not address or not key_path:
            print("Error: CARDANO_PAYMENT_ADDRESS and CARDANO_PRIVATE_KEY_PATH must be set")
            sys.exit(1)

        signing_key = PaymentSigningKey.load(key_path)

        info = get_prediction_info(args.market_id)
        price = info.yes_price if args.side == "Yes" else info.no_price
        cost_estimate = price * args.amount
        print(f"\n  Market:   {args.market_id}")
        print(f"  Action:   {args.command.upper()} {args.side}")
        print(f"  Shares:   {args.amount}")
        print(f"  Price:    {price:.4f} ADA per share")
        print(f"  Est cost: {cost_estimate:.2f} ADA (+ 2% fee)")
        print(f"  Slippage: {args.slippage*100:.1f}%")
        print(f"  Submit:   {args.submit}")

        if not args.dry_run:
            confirm = input("\n  Confirm? [y/N] ").strip().lower()
            if confirm != "y":
                print("  Cancelled.")
                return

        bf = None
        if args.submit == "blockfrost":
            bf = BlockfrostClient()

        execute_fn = execute_buy if args.command == "buy" else execute_sell
        result = execute_fn(
            market_id=args.market_id,
            side=args.side,
            amount=args.amount,
            address=address,
            signing_key=signing_key,
            slippage=args.slippage,
            submit_method=args.submit,
            blockfrost=bf,
            dry_run=args.dry_run,
        )
        print(f"\n  Tx hash: {result.tx_hash}")
        if not args.dry_run:
            print(f"  View: https://cardanoscan.io/transaction/{result.tx_hash}")

    elif args.command == "positions":
        address = os.environ.get("CARDANO_PAYMENT_ADDRESS")
        if not address:
            print("Error: CARDANO_PAYMENT_ADDRESS must be set")
            sys.exit(1)

        positions = get_my_positions(address)
        if not positions:
            print("\nNo Bodega positions found.")
            return

        print(f"\n{'Market':<35} {'Side':<6} {'Shares':>8} {'Status':<10} Name")
        print("-" * 90)
        for p in positions:
            print(
                f"{p['market_id']:<35} {p['side']:<6} {p['shares']:>8} "
                f"{p['status']:<10} {p['market_name'][:40]}"
            )

    elif args.command == "activity":
        activity = get_recent_activity()
        print(f"\n{'Action':<20} {'Side':<6} {'Shares':>7} {'Cost (ADA)':>11} {'Market':<35} Time")
        print("-" * 105)
        for a in activity[:20]:
            from datetime import datetime, timezone
            t = datetime.fromtimestamp(a["time"] / 1000, tz=timezone.utc)
            print(
                f"{a['action']:<20} {a['side']:<6} "
                f"{a.get('amount', 0):>7} "
                f"{_format_ada(a.get('price', 0)):>11} "
                f"{a['id'][:33]:<35} {t.strftime('%H:%M UTC')}"
            )

    elif args.command == "stats":
        s = get_global_stats()
        print(f"\n  Bodega Market Global Stats")
        print(f"  {'='*40}")
        print(f"  Total Addresses: {s.get('totalAddresses', 0):,}")
        print(f"  Total Buys:      {s.get('totalBuys', 0):,}")
        print(f"  Total Sells:     {s.get('totalSells', 0):,}")
        print(f"  Total Rewards:   {s.get('totalRewards', 0):,}")
        print(f"  Trade Volume:    {s.get('totalTradeVolume', 0):,.0f} ADA")
        print(f"  Buy Volume:      {s.get('totalBuyVolume', 0):,.0f} ADA")
        print(f"  Sell Volume:     {s.get('totalSellVolume', 0):,.0f} ADA")
        print(f"  Reward Volume:   {s.get('totalRewardVolume', 0):,.0f} ADA")


if __name__ == "__main__":
    _cli()
