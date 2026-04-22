"""
Arbitrage Scanner

Cross-venue price comparison engine for Cardano DEXes. Fans out free estimate
calls to Minswap, DexHunter, and CardexScan in parallel, identifies profitable
spreads, and reports opportunities.

Usage:
    python arb_scanner.py scan --amount 50 --min-profit 2 --interval 60
    python arb_scanner.py scan-once --amount 50 --min-profit 2
"""

import argparse
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

import minswap_client
import dexhunter_client
import cardexscan_client
from minswap_data_client import AssetMetric, build_arb_universe

logger = logging.getLogger(__name__)

ADA_LOVELACE = 1_000_000

# ---------------------------------------------------------------------------
# Fee constants (conservative estimates, in ADA)
# ---------------------------------------------------------------------------

TX_FEE_PER_LEG = 0.5
BATCHER_FEE_PER_LEG = 2.0
ROUND_TRIP_OVERHEAD = (TX_FEE_PER_LEG + BATCHER_FEE_PER_LEG) * 2


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PriceQuote:
    venue: str
    direction: str
    token_id: str
    amount_in: int
    amount_out: int
    fee_estimate: int
    price_impact: float
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class ArbOpportunity:
    token_id: str
    ticker: str
    buy_venue: str
    sell_venue: str
    amount_in_ada: float
    tokens_received: int
    ada_out: float
    gross_profit_ada: float
    estimated_fees_ada: float
    net_profit_ada: float
    spread_pct: float
    timestamp: float
    decimals: int = 0


# ---------------------------------------------------------------------------
# Per-venue estimate wrappers (buy direction: ADA -> token)
# ---------------------------------------------------------------------------

def _quote_minswap_buy(token_id: str, amount_lovelace: int) -> Optional[PriceQuote]:
    try:
        est = minswap_client.estimate_swap("lovelace", token_id, amount_lovelace)
        amount_out = int(est.amount_out) if est.amount_out else 0
        if amount_out <= 0:
            return None
        agg_fee = int(est.aggregator_fee) if est.aggregator_fee else 0
        dex_fee = int(est.total_dex_fee) if est.total_dex_fee else 0
        return PriceQuote(
            venue="minswap",
            direction="buy",
            token_id=token_id,
            amount_in=amount_lovelace,
            amount_out=amount_out,
            fee_estimate=agg_fee + dex_fee,
            price_impact=est.avg_price_impact,
            raw=est.raw,
        )
    except Exception as e:
        logger.debug("Minswap buy estimate failed for %s: %s", token_id, e)
        return None


def _quote_dexhunter_buy(token_id: str, amount_ada: float, decimals: int = 0) -> Optional[PriceQuote]:
    try:
        est = dexhunter_client.estimate_swap("", token_id, amount_ada)
        display_out = float(est.total_output) if est.total_output else 0
        if display_out <= 0:
            return None
        amount_out = int(display_out * (10 ** decimals))
        fee = int(float(est.total_fee) * ADA_LOVELACE) if est.total_fee else 0
        impact = float(est.price_impact) if est.price_impact else 0.0
        return PriceQuote(
            venue="dexhunter",
            direction="buy",
            token_id=token_id,
            amount_in=int(amount_ada * ADA_LOVELACE),
            amount_out=amount_out,
            fee_estimate=fee,
            price_impact=impact,
            raw=est.raw,
        )
    except Exception as e:
        logger.debug("DexHunter buy estimate failed for %s: %s", token_id, e)
        return None


def _quote_cardexscan_buy(token_id: str, amount_lovelace: int) -> Optional[PriceQuote]:
    try:
        est = cardexscan_client.estimate_swap("lovelace", token_id, amount_lovelace)
        amount_out = int(est.output_amount) if est.output_amount else 0
        if amount_out <= 0:
            return None
        return PriceQuote(
            venue="cardexscan",
            direction="buy",
            token_id=token_id,
            amount_in=amount_lovelace,
            amount_out=amount_out,
            fee_estimate=0,
            price_impact=0.0,
            raw=est.raw,
        )
    except Exception as e:
        logger.debug("CardexScan buy estimate failed for %s: %s", token_id, e)
        return None



# ---------------------------------------------------------------------------
# Per-venue estimate wrappers (sell direction: token -> ADA)
# ---------------------------------------------------------------------------

def _quote_minswap_sell(token_id: str, token_amount: int) -> Optional[PriceQuote]:
    try:
        est = minswap_client.estimate_swap(token_id, "lovelace", token_amount)
        amount_out = int(est.amount_out) if est.amount_out else 0
        if amount_out <= 0:
            return None
        agg_fee = int(est.aggregator_fee) if est.aggregator_fee else 0
        dex_fee = int(est.total_dex_fee) if est.total_dex_fee else 0
        return PriceQuote(
            venue="minswap",
            direction="sell",
            token_id=token_id,
            amount_in=token_amount,
            amount_out=amount_out,
            fee_estimate=agg_fee + dex_fee,
            price_impact=est.avg_price_impact,
            raw=est.raw,
        )
    except Exception as e:
        logger.debug("Minswap sell estimate failed for %s: %s", token_id, e)
        return None


def _quote_dexhunter_sell(token_id: str, token_amount: int, decimals: int = 0) -> Optional[PriceQuote]:
    try:
        amount_display = token_amount / (10 ** decimals)
        est = dexhunter_client.estimate_swap(token_id, "", amount_display)
        amount_out_ada = float(est.total_output) if est.total_output else 0
        amount_out = int(amount_out_ada * ADA_LOVELACE)
        if amount_out <= 0:
            return None
        fee = int(float(est.total_fee) * ADA_LOVELACE) if est.total_fee else 0
        impact = float(est.price_impact) if est.price_impact else 0.0
        return PriceQuote(
            venue="dexhunter",
            direction="sell",
            token_id=token_id,
            amount_in=token_amount,
            amount_out=amount_out,
            fee_estimate=fee,
            price_impact=impact,
            raw=est.raw,
        )
    except Exception as e:
        logger.debug("DexHunter sell estimate failed for %s: %s", token_id, e)
        return None


def _quote_cardexscan_sell(token_id: str, token_amount: int) -> Optional[PriceQuote]:
    try:
        est = cardexscan_client.estimate_swap(token_id, "lovelace", token_amount)
        amount_out = int(est.output_amount) if est.output_amount else 0
        if amount_out <= 0:
            return None
        return PriceQuote(
            venue="cardexscan",
            direction="sell",
            token_id=token_id,
            amount_in=token_amount,
            amount_out=amount_out,
            fee_estimate=0,
            price_impact=0.0,
            raw=est.raw,
        )
    except Exception as e:
        logger.debug("CardexScan sell estimate failed for %s: %s", token_id, e)
        return None



# ---------------------------------------------------------------------------
# Aggregated quote fetchers
# ---------------------------------------------------------------------------

_cardexscan_available = None


def _is_cardexscan_available() -> bool:
    global _cardexscan_available
    if _cardexscan_available is None:
        _cardexscan_available = bool(os.environ.get("CARDEXSCAN_API_KEY"))
    return _cardexscan_available


def get_buy_quotes(
    token_id: str,
    amount_ada: float,
    decimals: int = 0,
    max_workers: int = 3,
) -> list[PriceQuote]:
    """Get buy estimates from all venues in parallel.

    Returns quotes sorted by tokens received (best first).
    """
    amount_lovelace = int(amount_ada * ADA_LOVELACE)
    futures = {}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures["minswap"] = pool.submit(_quote_minswap_buy, token_id, amount_lovelace)
        futures["dexhunter"] = pool.submit(_quote_dexhunter_buy, token_id, amount_ada, decimals)
        if _is_cardexscan_available():
            futures["cardexscan"] = pool.submit(_quote_cardexscan_buy, token_id, amount_lovelace)

        quotes = []
        for name, fut in futures.items():
            try:
                q = fut.result(timeout=30)
                if q:
                    quotes.append(q)
            except Exception as e:
                logger.debug("Quote %s timed out or failed: %s", name, e)

    quotes.sort(key=lambda q: q.amount_out, reverse=True)
    return quotes


def get_sell_quotes(
    token_id: str,
    token_amount: int,
    decimals: int = 0,
    max_workers: int = 3,
) -> list[PriceQuote]:
    """Get sell estimates from all venues in parallel.

    Returns quotes sorted by ADA received (best first).
    """
    futures = {}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures["minswap"] = pool.submit(_quote_minswap_sell, token_id, token_amount)
        futures["dexhunter"] = pool.submit(_quote_dexhunter_sell, token_id, token_amount, decimals)
        if _is_cardexscan_available():
            futures["cardexscan"] = pool.submit(_quote_cardexscan_sell, token_id, token_amount)

        quotes = []
        for name, fut in futures.items():
            try:
                q = fut.result(timeout=30)
                if q:
                    quotes.append(q)
            except Exception as e:
                logger.debug("Quote %s timed out or failed: %s", name, e)

    quotes.sort(key=lambda q: q.amount_out, reverse=True)
    return quotes


# ---------------------------------------------------------------------------
# Opportunity detection
# ---------------------------------------------------------------------------

def find_opportunities(
    universe: list[AssetMetric],
    amount_ada: float,
    min_profit_ada: float = 2.0,
    max_price_impact: float = 2.0,
) -> list[ArbOpportunity]:
    """Scan the token universe for cross-venue arbitrage opportunities.

    For each token: get buy quotes from all venues, take the best buy,
    then get sell quotes using that output amount. If selling on a
    different venue yields more ADA than we spent (after fees), it's
    an opportunity.
    """
    opportunities = []
    stats = {
        "no_buy_quotes": 0,
        "buy_impact_too_high": 0,
        "no_sell_quotes": 0,
        "sell_impact_too_high": 0,
        "same_venue_only": 0,
        "unprofitable": 0,
        "best_net": -999.0,
        "best_net_ticker": "",
        "best_gross": -999.0,
        "best_gross_ticker": "",
        "spreads": [],
    }

    for i, asset in enumerate(universe):
        token_id = asset.token_id
        ticker = asset.ticker
        decimals = asset.metadata.decimals if asset.metadata else 0

        logger.debug("[%d/%d] %s (%s)", i + 1, len(universe), ticker, token_id[:20] + "...")

        buy_quotes = get_buy_quotes(token_id, amount_ada, decimals)
        if not buy_quotes:
            stats["no_buy_quotes"] += 1
            logger.debug("  %s: no buy quotes from any venue", ticker)
            continue

        best_buy = buy_quotes[0]
        buy_venues = ", ".join(f"{q.venue}={q.amount_out}" for q in buy_quotes)
        logger.debug("  %s buy quotes: %s", ticker, buy_venues)

        if best_buy.price_impact > max_price_impact:
            stats["buy_impact_too_high"] += 1
            logger.debug(
                "  %s: best buy impact %.2f%% exceeds %.2f%%, skipping",
                ticker, best_buy.price_impact, max_price_impact,
            )
            continue

        sell_quotes = get_sell_quotes(token_id, best_buy.amount_out, decimals)
        if not sell_quotes:
            stats["no_sell_quotes"] += 1
            logger.debug("  %s: no sell quotes from any venue", ticker)
            continue

        sell_venues = ", ".join(f"{q.venue}={q.amount_out}" for q in sell_quotes)
        logger.debug("  %s sell quotes: %s", ticker, sell_venues)

        found_cross_venue = False
        for sell_q in sell_quotes:
            if sell_q.venue == best_buy.venue:
                continue
            found_cross_venue = True
            if sell_q.price_impact > max_price_impact:
                stats["sell_impact_too_high"] += 1
                continue

            ada_out = sell_q.amount_out / ADA_LOVELACE
            gross = ada_out - amount_ada
            net = gross - ROUND_TRIP_OVERHEAD
            spread = (gross / amount_ada) * 100 if amount_ada > 0 else 0

            stats["spreads"].append((ticker, best_buy.venue, sell_q.venue, gross, net, spread))
            if net > stats["best_net"]:
                stats["best_net"] = net
                stats["best_net_ticker"] = ticker
            if gross > stats["best_gross"]:
                stats["best_gross"] = gross
                stats["best_gross_ticker"] = ticker

            logger.debug(
                "  %s: buy %s -> sell %s | %.2f ADA out | gross %+.2f | net %+.2f (%.2f%%)",
                ticker, best_buy.venue, sell_q.venue, ada_out, gross, net, spread,
            )

            if net >= min_profit_ada:
                opp = ArbOpportunity(
                    token_id=token_id,
                    ticker=ticker,
                    buy_venue=best_buy.venue,
                    sell_venue=sell_q.venue,
                    amount_in_ada=amount_ada,
                    tokens_received=best_buy.amount_out,
                    ada_out=ada_out,
                    gross_profit_ada=gross,
                    estimated_fees_ada=ROUND_TRIP_OVERHEAD,
                    net_profit_ada=net,
                    spread_pct=spread,
                    timestamp=time.time(),
                    decimals=decimals,
                )
                opportunities.append(opp)
                logger.info(
                    "OPPORTUNITY: %s | buy %s sell %s | %.2f ADA in -> %.2f ADA out | net +%.2f ADA (%.2f%%)",
                    ticker, best_buy.venue, sell_q.venue,
                    amount_ada, ada_out, net, spread,
                )
                break
            else:
                stats["unprofitable"] += 1

        if not found_cross_venue:
            stats["same_venue_only"] += 1

    _log_scan_summary(stats, len(universe), amount_ada, min_profit_ada)

    opportunities.sort(key=lambda o: o.net_profit_ada, reverse=True)
    return opportunities


def _log_scan_summary(
    stats: dict,
    total_tokens: int,
    amount_ada: float,
    min_profit_ada: float,
):
    """Print a diagnostic summary after each scan pass."""
    scanned = total_tokens
    no_buy = stats["no_buy_quotes"]
    buy_impact = stats["buy_impact_too_high"]
    no_sell = stats["no_sell_quotes"]
    sell_impact = stats["sell_impact_too_high"]
    same_venue = stats["same_venue_only"]
    unprofitable = stats["unprofitable"]
    spreads = stats["spreads"]

    print(f"\n{'─'*60}")
    print(f"  Scan summary ({scanned} tokens, {amount_ada:.0f} ADA trade size)")
    print(f"{'─'*60}")
    print(f"  No buy quotes:        {no_buy:>4}")
    print(f"  Buy impact too high:  {buy_impact:>4}")
    print(f"  No sell quotes:       {no_sell:>4}")
    print(f"  Sell impact too high: {sell_impact:>4}")
    print(f"  Same venue only:      {same_venue:>4}")
    print(f"  Unprofitable pairs:   {unprofitable:>4}")
    print(f"  Fee overhead/trip:    {ROUND_TRIP_OVERHEAD:.1f} ADA")
    print(f"  Min profit threshold: {min_profit_ada:.1f} ADA")

    if spreads:
        spreads.sort(key=lambda s: s[4], reverse=True)
        print(f"\n  Top cross-venue spreads (best {min(5, len(spreads))}):")
        for ticker, buy_v, sell_v, gross, net, spread_pct in spreads[:5]:
            marker = " <<<" if net >= min_profit_ada else ""
            print(
                f"    {ticker:>10s}  buy {buy_v:<12s} sell {sell_v:<12s}"
                f"  gross {gross:>+7.2f}  net {net:>+7.2f} ADA ({spread_pct:>+.2f}%){marker}"
            )

        best_net = stats["best_net"]
        best_ticker = stats["best_net_ticker"]
        gap = min_profit_ada - best_net
        if best_net < min_profit_ada:
            print(f"\n  Closest to profitable: {best_ticker} at net {best_net:+.2f} ADA ({gap:.2f} ADA short)")
    else:
        print("\n  No cross-venue quote pairs obtained.")

    print(f"{'─'*60}\n")


def scan_once(
    amount_ada: float = 50.0,
    min_profit_ada: float = 2.0,
    min_volume_ada: float = 500.0,
    min_liquidity_ada: float = 5000.0,
    max_tokens: int = 200,
    max_price_impact: float = 2.0,
) -> list[ArbOpportunity]:
    """Single scan pass: build universe, find opportunities."""
    universe = build_arb_universe(min_volume_ada, min_liquidity_ada, max_tokens)
    if not universe:
        logger.warning("Empty universe — no tokens met the criteria")
        return []

    logger.info("Scanning %d tokens for arb at %.0f ADA...", len(universe), amount_ada)
    return find_opportunities(universe, amount_ada, min_profit_ada, max_price_impact)


def scan_loop(
    amount_ada: float = 50.0,
    min_profit_ada: float = 2.0,
    scan_interval: int = 60,
    universe_refresh_min: int = 10,
    min_volume_ada: float = 500.0,
    min_liquidity_ada: float = 5000.0,
    max_tokens: int = 200,
    max_price_impact: float = 2.0,
):
    """Continuous scan loop. Rebuilds universe periodically, scans every interval."""
    universe = []
    last_universe_build = 0

    print(f"{'='*60}")
    print(f"  Arbitrage Scanner")
    print(f"  Trade size: {amount_ada} ADA | Min profit: {min_profit_ada} ADA")
    print(f"  Scan interval: {scan_interval}s | Universe refresh: {universe_refresh_min}min")
    print(f"{'='*60}\n")

    while True:
        now = time.time()

        if not universe or (now - last_universe_build) > universe_refresh_min * 60:
            print(f"[{time.strftime('%H:%M:%S')}] Rebuilding token universe...")
            universe = build_arb_universe(min_volume_ada, min_liquidity_ada, max_tokens)
            last_universe_build = now
            print(f"  Universe: {len(universe)} tokens\n")

        if not universe:
            print(f"[{time.strftime('%H:%M:%S')}] No tokens in universe, waiting...")
            time.sleep(scan_interval)
            continue

        print(f"[{time.strftime('%H:%M:%S')}] Scanning {len(universe)} tokens...")
        opportunities = find_opportunities(
            universe, amount_ada, min_profit_ada, max_price_impact,
        )

        if opportunities:
            print(f"\n  Found {len(opportunities)} opportunities:")
            for opp in opportunities:
                print(
                    f"    {opp.ticker:>8s} | buy {opp.buy_venue:<12s} sell {opp.sell_venue:<12s} "
                    f"| {opp.amount_in_ada:.0f} ADA -> {opp.ada_out:.2f} ADA "
                    f"| net +{opp.net_profit_ada:.2f} ADA ({opp.spread_pct:.2f}%)"
                )
            print()
        else:
            print(f"  No opportunities above {min_profit_ada} ADA threshold\n")

        time.sleep(scan_interval)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Cardano arbitrage scanner")
    sub = parser.add_subparsers(dest="command")

    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging (per-token quote details)")

    scan_p = sub.add_parser("scan", help="Continuous scan loop")
    scan_p.add_argument("--amount", type=float, default=float(os.environ.get("ARB_TRADE_AMOUNT_ADA", "50")))
    scan_p.add_argument("--min-profit", type=float, default=float(os.environ.get("ARB_MIN_PROFIT_ADA", "2")))
    scan_p.add_argument("--interval", type=int, default=int(os.environ.get("ARB_SCAN_INTERVAL_SEC", "60")))
    scan_p.add_argument("--universe-refresh", type=int, default=int(os.environ.get("ARB_UNIVERSE_REFRESH_MIN", "10")))
    scan_p.add_argument("--min-volume", type=float, default=float(os.environ.get("ARB_MIN_VOLUME_ADA", "500")))
    scan_p.add_argument("--min-liquidity", type=float, default=float(os.environ.get("ARB_MIN_LIQUIDITY_ADA", "5000")))
    scan_p.add_argument("--max-tokens", type=int, default=200)
    scan_p.add_argument("--max-impact", type=float, default=float(os.environ.get("ARB_MAX_PRICE_IMPACT", "2.0")))

    once_p = sub.add_parser("scan-once", help="Single scan pass")
    once_p.add_argument("--amount", type=float, default=float(os.environ.get("ARB_TRADE_AMOUNT_ADA", "50")))
    once_p.add_argument("--min-profit", type=float, default=float(os.environ.get("ARB_MIN_PROFIT_ADA", "2")))
    once_p.add_argument("--min-volume", type=float, default=float(os.environ.get("ARB_MIN_VOLUME_ADA", "500")))
    once_p.add_argument("--min-liquidity", type=float, default=float(os.environ.get("ARB_MIN_LIQUIDITY_ADA", "5000")))
    once_p.add_argument("--max-tokens", type=int, default=200)
    once_p.add_argument("--max-impact", type=float, default=float(os.environ.get("ARB_MAX_PRICE_IMPACT", "2.0")))

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger(__name__).setLevel(logging.DEBUG)

    if not args.command:
        parser.print_help()
        return

    if args.command == "scan":
        scan_loop(
            amount_ada=args.amount,
            min_profit_ada=args.min_profit,
            scan_interval=args.interval,
            universe_refresh_min=args.universe_refresh,
            min_volume_ada=args.min_volume,
            min_liquidity_ada=args.min_liquidity,
            max_tokens=args.max_tokens,
            max_price_impact=args.max_impact,
        )
    elif args.command == "scan-once":
        opportunities = scan_once(
            amount_ada=args.amount,
            min_profit_ada=args.min_profit,
            min_volume_ada=args.min_volume,
            min_liquidity_ada=args.min_liquidity,
            max_tokens=args.max_tokens,
            max_price_impact=args.max_impact,
        )
        if opportunities:
            print(f"\nFound {len(opportunities)} opportunities:\n")
            for opp in opportunities:
                print(f"  {opp.ticker}")
                print(f"    Buy on {opp.buy_venue}, sell on {opp.sell_venue}")
                print(f"    {opp.amount_in_ada:.0f} ADA -> {opp.tokens_received:,} tokens -> {opp.ada_out:.2f} ADA")
                print(f"    Gross: +{opp.gross_profit_ada:.2f} ADA | Fees: ~{opp.estimated_fees_ada:.2f} ADA | Net: +{opp.net_profit_ada:.2f} ADA")
                print(f"    Spread: {opp.spread_pct:.2f}%\n")
        else:
            print("\nNo arbitrage opportunities found.")


if __name__ == "__main__":
    _cli()
