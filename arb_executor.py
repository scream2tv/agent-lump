"""
Arbitrage Executor

Two-leg ADA-to-ADA execution engine for Cardano DEX arbitrage.
Buy tokens on the cheap venue, wait for the batcher to fill, sell on the
expensive venue. Wallet holds only ADA.

Usage:
    python arb_executor.py run --amount 50 --min-profit 2 --dry-run
    python arb_executor.py execute --token TOKEN_ID --buy-venue minswap --sell-venue dexhunter --amount 50
"""

import argparse
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv
from pycardano import BlockFrostChainContext, PaymentSigningKey

import minswap_client
import dexhunter_client
import cardexscan_client
from blockfrost_client import BlockfrostClient
from arb_scanner import (
    ArbOpportunity,
    PriceQuote,
    get_buy_quotes,
    get_sell_quotes,
    scan_once,
    ROUND_TRIP_OVERHEAD,
    ADA_LOVELACE,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class ArbConfig:
    trade_amount_ada: float = 50.0
    min_profit_ada: float = 2.0
    max_price_impact: float = 2.0
    max_trade_ada: float = 100.0
    fill_timeout_sec: int = 180
    fill_poll_interval_sec: int = 10
    cooldown_sec: int = 120
    scan_interval_sec: int = 60
    universe_refresh_min: int = 10
    min_volume_ada: float = 500.0
    min_liquidity_ada: float = 5000.0
    max_tokens: int = 200
    dry_run: bool = True

    @classmethod
    def from_env(cls) -> "ArbConfig":
        return cls(
            trade_amount_ada=float(os.environ.get("ARB_TRADE_AMOUNT_ADA", "50")),
            min_profit_ada=float(os.environ.get("ARB_MIN_PROFIT_ADA", "2")),
            max_price_impact=float(os.environ.get("ARB_MAX_PRICE_IMPACT", "2.0")),
            max_trade_ada=float(os.environ.get("ARB_MAX_TRADE_ADA", "100")),
            scan_interval_sec=int(os.environ.get("ARB_SCAN_INTERVAL_SEC", "60")),
            universe_refresh_min=int(os.environ.get("ARB_UNIVERSE_REFRESH_MIN", "10")),
            min_volume_ada=float(os.environ.get("ARB_MIN_VOLUME_ADA", "500")),
            min_liquidity_ada=float(os.environ.get("ARB_MIN_LIQUIDITY_ADA", "5000")),
            dry_run=os.environ.get("ARB_DRY_RUN", "true").lower() == "true",
        )


# ---------------------------------------------------------------------------
# UTXO polling
# ---------------------------------------------------------------------------

def wait_for_token(
    blockfrost: BlockfrostClient,
    address: str,
    token_id: str,
    timeout: int = 180,
    poll_interval: int = 10,
) -> Optional[int]:
    """Poll UTXOs until a UTXO containing token_id appears.

    Returns the token amount found, or None on timeout.
    """
    deadline = time.time() + timeout
    logger.info("Waiting for token %s at %s (timeout %ds)...", token_id[:20], address[:20], timeout)

    while time.time() < deadline:
        try:
            utxos = blockfrost.get_utxos(address)
            for utxo in utxos:
                for asset in utxo.get("amount", []):
                    if asset.get("unit") == token_id:
                        amount = int(asset["quantity"])
                        logger.info("Token arrived: %d units of %s", amount, token_id[:20])
                        return amount
        except Exception as e:
            logger.warning("UTXO poll error: %s", e)

        time.sleep(poll_interval)

    logger.warning("Timeout waiting for token %s", token_id[:20])
    return None


def wait_for_ada_change(
    blockfrost: BlockfrostClient,
    address: str,
    baseline_lovelace: int,
    timeout: int = 180,
    poll_interval: int = 10,
) -> Optional[int]:
    """Poll UTXOs until total ADA increases above baseline.

    Returns the new total lovelace, or None on timeout.
    """
    deadline = time.time() + timeout
    logger.info("Waiting for ADA increase above %d lovelace...", baseline_lovelace)

    while time.time() < deadline:
        try:
            utxos = blockfrost.get_utxos(address)
            total = sum(
                int(a["quantity"])
                for u in utxos
                for a in u.get("amount", [])
                if a.get("unit") == "lovelace"
            )
            if total > baseline_lovelace:
                logger.info("ADA increased: %d -> %d lovelace", baseline_lovelace, total)
                return total
        except Exception as e:
            logger.warning("UTXO poll error: %s", e)

        time.sleep(poll_interval)

    logger.warning("Timeout waiting for ADA increase")
    return None


def get_current_ada(blockfrost: BlockfrostClient, address: str) -> int:
    """Get total lovelace across all UTXOs at an address."""
    utxos = blockfrost.get_utxos(address)
    return sum(
        int(a["quantity"])
        for u in utxos
        for a in u.get("amount", [])
        if a.get("unit") == "lovelace"
    )


# ---------------------------------------------------------------------------
# Per-venue execution
# ---------------------------------------------------------------------------

def _execute_buy_minswap(
    token_id: str, amount_lovelace: int, sender: str, skey: PaymentSigningKey,
    max_impact: float,
) -> dict:
    result = minswap_client.execute_swap(
        sender=sender,
        token_in="lovelace",
        token_out=token_id,
        amount_lovelace=amount_lovelace,
        signing_key=skey,
        max_price_impact=max_impact,
    )
    return {"tx_hash": result.get("tx_id", ""), "venue": "minswap", "raw": result}


def _execute_sell_minswap(
    token_id: str, token_amount: int, sender: str, skey: PaymentSigningKey,
    max_impact: float,
) -> dict:
    result = minswap_client.execute_swap(
        sender=sender,
        token_in=token_id,
        token_out="lovelace",
        amount_lovelace=token_amount,
        signing_key=skey,
        max_price_impact=max_impact,
    )
    return {"tx_hash": result.get("tx_id", ""), "venue": "minswap", "raw": result}


def _execute_buy_dexhunter(
    token_id: str, amount_ada: float, sender: str, skey: PaymentSigningKey,
    context,
) -> dict:
    result = dexhunter_client.execute_swap(
        buyer_address=sender,
        token_in="",
        token_out=token_id,
        amount_in=amount_ada,
        signing_key=skey,
        context=context,
    )
    return {"tx_hash": result.get("tx_hash", ""), "venue": "dexhunter", "raw": result}


def _execute_sell_dexhunter(
    token_id: str, token_amount: int, sender: str, skey: PaymentSigningKey,
    context,
) -> dict:
    result = dexhunter_client.execute_swap(
        buyer_address=sender,
        token_in=token_id,
        token_out="",
        amount_in=float(token_amount),
        signing_key=skey,
        context=context,
    )
    return {"tx_hash": result.get("tx_hash", ""), "venue": "dexhunter", "raw": result}


def _execute_buy_cardexscan(
    token_id: str, amount_lovelace: int, sender: str, skey: PaymentSigningKey,
    context,
) -> dict:
    build = cardexscan_client.build_swap(sender, "lovelace", token_id, amount_lovelace)
    if not build.cbor:
        raise RuntimeError("CardexScan returned empty CBOR")
    signed = cardexscan_client.sign_transaction(build.cbor, skey)
    tx_hash = cardexscan_client.submit_transaction(signed, context)
    return {"tx_hash": tx_hash, "venue": "cardexscan", "raw": build.raw}


def _execute_sell_cardexscan(
    token_id: str, token_amount: int, sender: str, skey: PaymentSigningKey,
    context,
) -> dict:
    build = cardexscan_client.build_swap(sender, token_id, "lovelace", token_amount)
    if not build.cbor:
        raise RuntimeError("CardexScan returned empty CBOR")
    signed = cardexscan_client.sign_transaction(build.cbor, skey)
    tx_hash = cardexscan_client.submit_transaction(signed, context)
    return {"tx_hash": tx_hash, "venue": "cardexscan", "raw": build.raw}



# ---------------------------------------------------------------------------
# Main execution pipeline
# ---------------------------------------------------------------------------

@dataclass
class ArbResult:
    status: str
    token_id: str
    buy_venue: str
    sell_venue: str
    amount_in_ada: float
    buy_tx_hash: str = ""
    sell_tx_hash: str = ""
    tokens_received: int = 0
    ada_returned: float = 0.0
    net_profit_ada: float = 0.0
    error: str = ""
    elapsed_sec: float = 0.0


def execute_arb(
    opportunity: ArbOpportunity,
    signing_key: PaymentSigningKey,
    sender_address: str,
    config: ArbConfig,
    blockfrost: Optional[BlockfrostClient] = None,
    context=None,
) -> ArbResult:
    """Execute a two-leg arbitrage trade.

    1. Re-estimate both legs (pre-flight)
    2. Execute buy leg
    3. Wait for token UTXO to appear
    4. Re-estimate sell leg with actual amount
    5. Execute sell leg
    6. Wait for ADA return
    """
    start = time.time()
    token_id = opportunity.token_id

    result = ArbResult(
        status="pending",
        token_id=token_id,
        buy_venue=opportunity.buy_venue,
        sell_venue=opportunity.sell_venue,
        amount_in_ada=opportunity.amount_in_ada,
    )

    if blockfrost is None:
        blockfrost = BlockfrostClient()

    amount_ada = opportunity.amount_in_ada
    amount_lovelace = int(amount_ada * ADA_LOVELACE)

    if amount_ada > config.max_trade_ada:
        result.status = "rejected"
        result.error = f"Trade {amount_ada} ADA exceeds max {config.max_trade_ada} ADA"
        return result

    # --- Pre-flight re-estimation ---
    decimals = getattr(opportunity, "decimals", 0)
    logger.info("Pre-flight: re-estimating %s...", opportunity.ticker)
    buy_quotes = get_buy_quotes(token_id, amount_ada, decimals)
    best_buy = next((q for q in buy_quotes if q.venue == opportunity.buy_venue), None)
    if not best_buy:
        result.status = "aborted"
        result.error = f"Buy venue {opportunity.buy_venue} no longer available"
        return result

    sell_quotes = get_sell_quotes(token_id, best_buy.amount_out, decimals)
    best_sell = next((q for q in sell_quotes if q.venue == opportunity.sell_venue), None)
    if not best_sell:
        result.status = "aborted"
        result.error = f"Sell venue {opportunity.sell_venue} no longer available"
        return result

    ada_out = best_sell.amount_out / ADA_LOVELACE
    net = ada_out - amount_ada - ROUND_TRIP_OVERHEAD
    if net < config.min_profit_ada:
        result.status = "aborted"
        result.error = f"Profit dropped to {net:.2f} ADA (below {config.min_profit_ada} threshold)"
        return result

    logger.info(
        "Pre-flight passed: buy %s on %s, sell on %s, expected net +%.2f ADA",
        opportunity.ticker, opportunity.buy_venue, opportunity.sell_venue, net,
    )

    if config.dry_run:
        result.status = "dry_run"
        result.tokens_received = best_buy.amount_out
        result.ada_returned = ada_out
        result.net_profit_ada = net
        result.elapsed_sec = time.time() - start
        return result

    # --- Leg 1: Buy ---
    logger.info("LEG 1: Buying %s on %s with %.0f ADA...", opportunity.ticker, opportunity.buy_venue, amount_ada)
    try:
        buy_result = _dispatch_buy(
            venue=opportunity.buy_venue,
            token_id=token_id,
            amount_ada=amount_ada,
            amount_lovelace=amount_lovelace,
            sender=sender_address,
            skey=signing_key,
            context=context,
            max_impact=config.max_price_impact,
        )
        result.buy_tx_hash = buy_result["tx_hash"]
        logger.info("Buy tx submitted: %s", result.buy_tx_hash)
    except Exception as e:
        result.status = "buy_failed"
        result.error = str(e)
        result.elapsed_sec = time.time() - start
        return result

    # --- Wait for fill ---
    tokens = wait_for_token(
        blockfrost, sender_address, token_id,
        timeout=config.fill_timeout_sec,
        poll_interval=config.fill_poll_interval_sec,
    )
    if tokens is None:
        result.status = "buy_timeout"
        result.error = "Batcher did not fill buy order within timeout"
        result.elapsed_sec = time.time() - start
        return result

    result.tokens_received = tokens

    # --- Re-estimate sell with actual amount ---
    logger.info("Received %d tokens. Re-estimating sell on %s...", tokens, opportunity.sell_venue)
    sell_quotes_now = get_sell_quotes(token_id, tokens, decimals)
    sell_now = next((q for q in sell_quotes_now if q.venue == opportunity.sell_venue), None)
    if not sell_now:
        result.status = "sell_no_quote"
        result.error = f"Sell venue {opportunity.sell_venue} unavailable after buy fill"
        result.elapsed_sec = time.time() - start
        return result

    ada_out_now = sell_now.amount_out / ADA_LOVELACE
    net_now = ada_out_now - amount_ada - ROUND_TRIP_OVERHEAD
    if net_now < 0:
        logger.warning(
            "Price moved against us: expected +%.2f, now %.2f ADA. Proceeding to recover.",
            net, net_now,
        )

    # --- Leg 2: Sell ---
    logger.info("LEG 2: Selling %d tokens on %s...", tokens, opportunity.sell_venue)
    try:
        sell_result = _dispatch_sell(
            venue=opportunity.sell_venue,
            token_id=token_id,
            token_amount=tokens,
            sender=sender_address,
            skey=signing_key,
            context=context,
            max_impact=config.max_price_impact,
        )
        result.sell_tx_hash = sell_result["tx_hash"]
        logger.info("Sell tx submitted: %s", result.sell_tx_hash)
    except Exception as e:
        result.status = "sell_failed"
        result.error = str(e)
        result.elapsed_sec = time.time() - start
        return result

    # --- Wait for ADA return ---
    baseline = get_current_ada(blockfrost, sender_address)
    final_ada = wait_for_ada_change(
        blockfrost, sender_address, baseline,
        timeout=config.fill_timeout_sec,
        poll_interval=config.fill_poll_interval_sec,
    )

    result.ada_returned = (final_ada / ADA_LOVELACE) if final_ada else 0
    result.net_profit_ada = result.ada_returned - amount_ada - ROUND_TRIP_OVERHEAD if final_ada else 0
    result.status = "complete"
    result.elapsed_sec = time.time() - start
    return result


def _dispatch_buy(
    venue: str, token_id: str, amount_ada: float, amount_lovelace: int,
    sender: str, skey: PaymentSigningKey, context, max_impact: float,
) -> dict:
    if venue == "minswap":
        return _execute_buy_minswap(token_id, amount_lovelace, sender, skey, max_impact)
    elif venue == "dexhunter":
        if context is None:
            raise RuntimeError("DexHunter requires a BlockFrostChainContext")
        return _execute_buy_dexhunter(token_id, amount_ada, sender, skey, context)
    elif venue == "cardexscan":
        if context is None:
            raise RuntimeError("CardexScan requires a BlockFrostChainContext")
        return _execute_buy_cardexscan(token_id, amount_lovelace, sender, skey, context)
    else:
        raise ValueError(f"Unknown buy venue: {venue}")


def _dispatch_sell(
    venue: str, token_id: str, token_amount: int,
    sender: str, skey: PaymentSigningKey, context, max_impact: float,
) -> dict:
    if venue == "minswap":
        return _execute_sell_minswap(token_id, token_amount, sender, skey, max_impact)
    elif venue == "dexhunter":
        if context is None:
            raise RuntimeError("DexHunter requires a BlockFrostChainContext")
        return _execute_sell_dexhunter(token_id, token_amount, sender, skey, context)
    elif venue == "cardexscan":
        if context is None:
            raise RuntimeError("CardexScan requires a BlockFrostChainContext")
        return _execute_sell_cardexscan(token_id, token_amount, sender, skey, context)
    else:
        raise ValueError(f"Unknown sell venue: {venue}")


# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------

def run_loop(
    signing_key: PaymentSigningKey,
    sender_address: str,
    config: ArbConfig,
):
    """Continuous scan-and-execute loop."""
    blockfrost = BlockfrostClient()
    context = None
    project_id = os.environ.get("BLOCKFROST_PROJECT_ID", "")
    if project_id:
        try:
            context = BlockFrostChainContext(project_id)
        except Exception:
            logger.warning("Could not create BlockFrostChainContext — DexHunter/CardexScan execution disabled")

    cooldowns: dict[str, float] = {}

    print(f"{'='*60}")
    print(f"  Arbitrage Executor")
    print(f"  Trade: {config.trade_amount_ada} ADA | Min profit: {config.min_profit_ada} ADA")
    print(f"  Mode: {'DRY RUN' if config.dry_run else 'LIVE'}")
    print(f"  Scan interval: {config.scan_interval_sec}s")
    print(f"{'='*60}\n")

    while True:
        opportunities = scan_once(
            amount_ada=config.trade_amount_ada,
            min_profit_ada=config.min_profit_ada,
            min_volume_ada=config.min_volume_ada,
            min_liquidity_ada=config.min_liquidity_ada,
            max_tokens=config.max_tokens,
            max_price_impact=config.max_price_impact,
        )

        now = time.time()
        for opp in opportunities:
            if opp.token_id in cooldowns and now < cooldowns[opp.token_id]:
                remaining = cooldowns[opp.token_id] - now
                logger.info("Skipping %s — cooldown %.0fs remaining", opp.ticker, remaining)
                continue

            print(f"\n[{time.strftime('%H:%M:%S')}] Executing arb: {opp.ticker}")
            print(f"  Buy {opp.buy_venue} -> Sell {opp.sell_venue}")
            print(f"  {opp.amount_in_ada:.0f} ADA -> {opp.tokens_received:,} tokens -> ~{opp.ada_out:.2f} ADA")
            print(f"  Expected net: +{opp.net_profit_ada:.2f} ADA")

            result = execute_arb(opp, signing_key, sender_address, config, blockfrost, context)

            print(f"  Status: {result.status}")
            if result.buy_tx_hash:
                print(f"  Buy tx:  {result.buy_tx_hash}")
            if result.sell_tx_hash:
                print(f"  Sell tx: {result.sell_tx_hash}")
            if result.net_profit_ada:
                print(f"  Net: {'+' if result.net_profit_ada > 0 else ''}{result.net_profit_ada:.2f} ADA")
            if result.error:
                print(f"  Error: {result.error}")
            print(f"  Elapsed: {result.elapsed_sec:.1f}s\n")

            cooldowns[opp.token_id] = now + config.cooldown_sec
            break

        if not opportunities:
            print(f"[{time.strftime('%H:%M:%S')}] No opportunities — sleeping {config.scan_interval_sec}s")

        time.sleep(config.scan_interval_sec)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Cardano arbitrage executor")
    sub = parser.add_subparsers(dest="command")

    # --- run ---
    run_p = sub.add_parser("run", help="Continuous scan + execute loop")
    run_p.add_argument("--amount", type=float, default=None)
    run_p.add_argument("--min-profit", type=float, default=None)
    run_p.add_argument("--interval", type=int, default=None)
    run_p.add_argument("--dry-run", action="store_true", default=None)
    run_p.add_argument("--live", action="store_true")

    # --- execute ---
    exec_p = sub.add_parser("execute", help="Execute a single arb manually")
    exec_p.add_argument("--token", required=True, help="Full token ID (policyId + assetNameHex)")
    exec_p.add_argument("--buy-venue", required=True, choices=["minswap", "dexhunter", "cardexscan"])
    exec_p.add_argument("--sell-venue", required=True, choices=["minswap", "dexhunter", "cardexscan"])
    exec_p.add_argument("--amount", type=float, required=True, help="ADA amount")
    exec_p.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    address = os.environ.get("CARDANO_PAYMENT_ADDRESS", "")
    key_path = os.environ.get("CARDANO_PRIVATE_KEY_PATH", "")
    if not address or not key_path:
        print("Error: CARDANO_PAYMENT_ADDRESS and CARDANO_PRIVATE_KEY_PATH must be set in .env")
        return

    skey = PaymentSigningKey.load(key_path)

    if args.command == "run":
        config = ArbConfig.from_env()
        if args.amount is not None:
            config.trade_amount_ada = args.amount
        if args.min_profit is not None:
            config.min_profit_ada = args.min_profit
        if args.interval is not None:
            config.scan_interval_sec = args.interval
        if args.live:
            config.dry_run = False
        elif args.dry_run:
            config.dry_run = True

        run_loop(skey, address, config)

    elif args.command == "execute":
        config = ArbConfig.from_env()
        config.dry_run = args.dry_run

        token_id = args.token

        opp = ArbOpportunity(
            token_id=token_id,
            ticker=token_id[56:64] or "???",
            buy_venue=args.buy_venue,
            sell_venue=args.sell_venue,
            amount_in_ada=args.amount,
            tokens_received=0,
            ada_out=0,
            gross_profit_ada=0,
            estimated_fees_ada=ROUND_TRIP_OVERHEAD,
            net_profit_ada=0,
            spread_pct=0,
            timestamp=time.time(),
        )

        blockfrost = BlockfrostClient()
        context = None
        project_id = os.environ.get("BLOCKFROST_PROJECT_ID", "")
        if project_id:
            try:
                context = BlockFrostChainContext(project_id)
            except Exception:
                pass

        print(f"{'='*50}")
        print(f"  Manual Arb Execution")
        print(f"  Token: {token_id[:20]}...{token_id[-10:]}")
        print(f"  Buy: {args.buy_venue} | Sell: {args.sell_venue}")
        print(f"  Amount: {args.amount} ADA")
        print(f"  Mode: {'DRY RUN' if config.dry_run else 'LIVE'}")
        print(f"{'='*50}\n")

        result = execute_arb(opp, skey, address, config, blockfrost, context)

        print(f"Status: {result.status}")
        if result.buy_tx_hash:
            print(f"Buy tx:  https://cardanoscan.io/transaction/{result.buy_tx_hash}")
        if result.sell_tx_hash:
            print(f"Sell tx: https://cardanoscan.io/transaction/{result.sell_tx_hash}")
        if result.tokens_received:
            print(f"Tokens received: {result.tokens_received:,}")
        if result.ada_returned:
            print(f"ADA returned: {result.ada_returned:.2f}")
        if result.net_profit_ada:
            print(f"Net profit: {'+' if result.net_profit_ada > 0 else ''}{result.net_profit_ada:.2f} ADA")
        if result.error:
            print(f"Error: {result.error}")
        print(f"Elapsed: {result.elapsed_sec:.1f}s")


if __name__ == "__main__":
    _cli()
