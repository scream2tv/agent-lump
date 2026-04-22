"""
Copy Trader Daemon (Snek.fun only)

Watches a target Cardano wallet and mirrors its Snek.fun ADA<->token trades
from the configured agent wallet using the Snek.fun builder API.

Filter:
  - Only transactions involving a Snek.fun on-chain script (pool validator
    or order validator) are considered. Every other tx type (transfers,
    Minswap/Sundae/DexHunter trades, airdrops, NFT mints, etc.) is ignored.

Classification (within snek.fun txs only):
  - BUY  — target received exactly one non-ADA token (fulfillment of a
           buy order).
  - SELL — target sent exactly one non-ADA token to a snek.fun script
           (sell order placement).

Sizing strategies (required; pick one of each via CLI or env):

  Buy modes (--buy-mode / COPY_TRADER_BUY_MODE):
    fixed        buy exactly --buy-value ADA per copy.
    pct-target   buy (buy-value %) of the ADA the target committed.
    pct-wallet   buy (buy-value %) of our wallet's free ADA balance.

  Sell modes (--sell-mode / COPY_TRADER_SELL_MODE):
    all          sell 100% of our holding (ignores --sell-value).
    pct-holding  sell (sell-value %) of our holding.

  Safety caps: --min-buy-ada / --max-buy-ada bound every buy regardless of
  mode.

Usage:
    python3 copy_trader.py --target <addr> --buy-mode fixed --buy-value 25 --sell-mode all
    python3 copy_trader.py --target <addr> --buy-mode pct-target --buy-value 20 --sell-mode pct-holding --sell-value 50
    python3 copy_trader.py --target <addr> --buy-mode pct-wallet --buy-value 5 --sell-mode all
    python3 copy_trader.py --target <addr> ... --dry-run    # log only, no submit
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from pycardano import Address, PaymentSigningKey

from blockfrost_client import BlockfrostClient
import snekfun_client as snekfun


LOG_FORMAT = "%(asctime)s %(levelname)-5s %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt="%H:%M:%S")
logger = logging.getLogger("copy_trader")

STATE_DIR = Path.home() / ".agent-lump"
STATE_PATH = STATE_DIR / "copy_trader_state.json"
MAX_PROCESSED_RETAIN = 500

BUY_MODES = ("fixed", "pct-target", "pct-wallet")
SELL_MODES = ("all", "pct-holding")

# Snek.fun order UTxOs carry a ~2 ADA min-utxo that is returned to the user
# in the fulfillment tx. Subtracting it makes "target ADA committed" more
# accurate.
SNEKFUN_ORDER_MIN_UTXO_LOVELACE = 2_000_000


# ---------------------------------------------------------------------------
# Snek.fun on-chain identifiers
# ---------------------------------------------------------------------------

SNEKFUN_SCRIPT_HASHES: set[str] = {
    "905ab869961b094f1b8197278cfe15b45cbe49fa8f32c6b014f85a2d",  # pool validator
    snekfun.ORDER_SCRIPT_BASE_HASH,                                # order validator
}
SNEKFUN_LITERAL_ADDRESSES: set[str] = {snekfun.POOL_ADDRESS}


def _payment_hash(addr: str) -> Optional[str]:
    try:
        a = Address.from_primitive(addr)
    except Exception:
        return None
    pp = a.payment_part
    if pp is None:
        return None
    try:
        return pp.payload.hex()
    except AttributeError:
        return None


def is_snekfun_address(addr: str) -> bool:
    if not addr:
        return False
    if addr in SNEKFUN_LITERAL_ADDRESSES:
        return True
    h = _payment_hash(addr)
    return h is not None and h in SNEKFUN_SCRIPT_HASHES


def is_snekfun_order_address(addr: str) -> bool:
    return _payment_hash(addr) == snekfun.ORDER_SCRIPT_BASE_HASH


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


@dataclass
class Strategy:
    buy_mode: str         # fixed | pct-target | pct-wallet
    buy_value: float      # ADA for fixed; percent for others
    sell_mode: str        # all | pct-holding
    sell_value: float     # percent (ignored if mode=all)
    min_buy_ada: float
    max_buy_ada: float
    slippage: str
    dry_run: bool

    def compute_buy_ada(
        self,
        target_committed_lovelace: int,
        our_ada_lovelace: int,
    ) -> float:
        if self.buy_mode == "fixed":
            raw = self.buy_value
        elif self.buy_mode == "pct-target":
            raw = (target_committed_lovelace / 1_000_000) * (self.buy_value / 100)
        elif self.buy_mode == "pct-wallet":
            raw = (our_ada_lovelace / 1_000_000) * (self.buy_value / 100)
        else:
            raise ValueError(f"unknown buy_mode: {self.buy_mode}")
        return max(self.min_buy_ada, min(self.max_buy_ada, round(raw, 2)))

    def compute_sell_qty(self, our_holding_base_units: int) -> int:
        if self.sell_mode == "all" or self.sell_value >= 100:
            return our_holding_base_units
        if self.sell_mode == "pct-holding":
            return int(our_holding_base_units * (self.sell_value / 100))
        raise ValueError(f"unknown sell_mode: {self.sell_mode}")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"processed_tx_hashes": []}
    try:
        return json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        logger.warning("state file corrupt, starting fresh")
        return {"processed_tx_hashes": []}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    processed = state.get("processed_tx_hashes", [])
    if len(processed) > MAX_PROCESSED_RETAIN:
        state["processed_tx_hashes"] = processed[-MAX_PROCESSED_RETAIN:]
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def tx_involves_snekfun(tx_utxos: dict) -> bool:
    for side in ("inputs", "outputs"):
        for io in tx_utxos.get(side, []):
            if is_snekfun_address(io.get("address", "")):
                return True
    return False


def compute_net_delta(tx_utxos: dict, address: str) -> tuple[int, dict[str, int]]:
    ada_in = ada_out = 0
    tokens_in: dict[str, int] = defaultdict(int)
    tokens_out: dict[str, int] = defaultdict(int)

    for inp in tx_utxos.get("inputs", []):
        if inp.get("address") != address:
            continue
        for amt in inp.get("amount", []):
            if amt["unit"] == "lovelace":
                ada_in += int(amt["quantity"])
            else:
                tokens_in[amt["unit"]] += int(amt["quantity"])

    for outp in tx_utxos.get("outputs", []):
        if outp.get("address") != address:
            continue
        for amt in outp.get("amount", []):
            if amt["unit"] == "lovelace":
                ada_out += int(amt["quantity"])
            else:
                tokens_out[amt["unit"]] += int(amt["quantity"])

    net_ada = ada_out - ada_in
    all_units = set(tokens_in) | set(tokens_out)
    net_tokens = {u: tokens_out[u] - tokens_in[u] for u in all_units}
    net_tokens = {u: q for u, q in net_tokens.items() if q != 0}
    return net_ada, net_tokens


def classify_snekfun_trade(
    tx_utxos: dict, target: str
) -> Optional[tuple[str, str, int]]:
    """Return (action, unit, qty) or None.

    BUY  — target received exactly one non-ADA token in a snek.fun tx.
    SELL — target sent exactly one non-ADA token to a snek.fun script addr.
    """
    net_ada, net_tokens = compute_net_delta(tx_utxos, target)
    if len(net_tokens) != 1:
        return None
    unit, qty = next(iter(net_tokens.items()))

    if qty > 0:
        return ("BUY", unit, qty)

    if qty < 0:
        lost_to_snekfun = 0
        for outp in tx_utxos.get("outputs", []):
            if not is_snekfun_address(outp.get("address", "")):
                continue
            for amt in outp.get("amount", []):
                if amt["unit"] == unit:
                    lost_to_snekfun += int(amt["quantity"])
        if lost_to_snekfun <= 0:
            return None
        return ("SELL", unit, -qty)

    return None


def estimate_target_buy_lovelace(tx_utxos: dict) -> int:
    """Estimate lovelace the target committed to this buy.

    We sum lovelace from snek.fun ORDER-script inputs in the fulfillment tx
    (skipping the pool UTxO which has unrelated balances), minus the
    standard ~2 ADA min-utxo deposit per order input (returned to target).
    """
    total = 0
    n_order_inputs = 0
    for inp in tx_utxos.get("inputs", []):
        if is_snekfun_order_address(inp.get("address", "")):
            for amt in inp.get("amount", []):
                if amt["unit"] == "lovelace":
                    total += int(amt["quantity"])
            n_order_inputs += 1
    return max(0, total - n_order_inputs * SNEKFUN_ORDER_MIN_UTXO_LOVELACE)


# ---------------------------------------------------------------------------
# Wallet helpers
# ---------------------------------------------------------------------------


def get_our_ada_lovelace(bf: BlockfrostClient, address: str) -> int:
    try:
        utxos = bf.get_utxos(address)
    except Exception as e:
        logger.warning("could not read our ADA balance: %s", e)
        return 0
    total = 0
    for utxo in utxos:
        for amt in utxo.get("amount", []):
            if amt["unit"] == "lovelace":
                total += int(amt["quantity"])
    return total


def get_our_token_balance(bf: BlockfrostClient, address: str, unit: str) -> int:
    try:
        utxos = bf.get_utxos(address)
    except Exception as e:
        logger.warning("could not read our token balance: %s", e)
        return 0
    total = 0
    for utxo in utxos:
        for amt in utxo.get("amount", []):
            if amt["unit"] == unit:
                total += int(amt["quantity"])
    return total


def unit_to_asset_id(unit: str) -> str:
    if len(unit) < 56:
        raise ValueError(f"invalid token unit: {unit}")
    return f"{unit[:56]}.{unit[56:]}"


def _short(s: str, head: int = 12, tail: int = 6) -> str:
    if not s or len(s) <= head + tail + 3:
        return s
    return f"{s[:head]}..{s[-tail:]}"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def execute_buy(
    bf: BlockfrostClient,
    our_address: str,
    signing_key: PaymentSigningKey,
    unit: str,
    strategy: Strategy,
    target_committed_lovelace: int,
) -> None:
    asset_id = unit_to_asset_id(unit)

    our_ada = get_our_ada_lovelace(bf, our_address) if strategy.buy_mode == "pct-wallet" else 0
    ada_amount = strategy.compute_buy_ada(target_committed_lovelace, our_ada)

    logger.info(
        "BUY  copy: %.2f ADA -> %s (mode=%s target_committed=%.2f ADA)",
        ada_amount, _short(asset_id), strategy.buy_mode,
        target_committed_lovelace / 1_000_000,
    )

    try:
        result = snekfun.execute_buy(
            asset_id=asset_id,
            ada_amount=ada_amount,
            sender_address=our_address,
            signing_key=signing_key,
            slippage=strategy.slippage,
            blockfrost=bf,
            dry_run=strategy.dry_run,
        )
    except Exception as e:
        logger.exception("  buy execution failed: %s", e)
        return

    tx_hash = result.get("tx_hash")
    tag = "[dry-run] would buy" if strategy.dry_run else "submitted"
    logger.info("  %s tx=%s (input=%s output=%s)",
                tag, tx_hash, result.get("input_amount"), result.get("output_amount"))


def execute_sell(
    bf: BlockfrostClient,
    our_address: str,
    signing_key: PaymentSigningKey,
    unit: str,
    strategy: Strategy,
) -> None:
    asset_id = unit_to_asset_id(unit)
    held = get_our_token_balance(bf, our_address, unit)
    if held <= 0:
        logger.info("SELL copy: we hold 0 of %s -- skipping", _short(asset_id))
        return

    sell_qty = strategy.compute_sell_qty(held)
    if sell_qty <= 0:
        logger.info("SELL copy: sizing resolved to 0 for %s -- skipping", _short(asset_id))
        return

    logger.info(
        "SELL copy: %d / %d base units of %s -> ADA (mode=%s)",
        sell_qty, held, _short(asset_id), strategy.sell_mode,
    )

    try:
        result = snekfun.execute_sell(
            asset_id=asset_id,
            token_amount=sell_qty,
            sender_address=our_address,
            signing_key=signing_key,
            slippage=strategy.slippage,
            blockfrost=bf,
            dry_run=strategy.dry_run,
        )
    except Exception as e:
        logger.exception("  sell execution failed: %s", e)
        return

    tx_hash = result.get("tx_hash")
    tag = "[dry-run] would sell" if strategy.dry_run else "submitted"
    logger.info("  %s tx=%s (input=%s output=%s)",
                tag, tx_hash, result.get("input_amount"), result.get("output_amount"))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

_stop = False


def _handle_signal(*_):
    global _stop
    logger.info("signal received, shutting down after current tick")
    _stop = True


def _sleep(sec: float) -> None:
    end = time.time() + sec
    while time.time() < end and not _stop:
        time.sleep(min(1.0, max(0.0, end - time.time())))


def run(args, strategy: Strategy) -> None:
    our_address = os.environ["CARDANO_PAYMENT_ADDRESS"]
    key_path = os.environ["CARDANO_PRIVATE_KEY_PATH"]
    project_id = os.environ["BLOCKFROST_PROJECT_ID"]

    signing_key = PaymentSigningKey.load(key_path)
    bf = BlockfrostClient(project_id)

    state = load_state()
    processed: set[str] = set(state.get("processed_tx_hashes", []))

    if not processed:
        try:
            recent = bf.get_address_transactions(args.target, count=20)
        except Exception as e:
            logger.error("initial fetch failed: %s", e)
            sys.exit(1)
        for tx in recent:
            processed.add(tx["tx_hash"])
        state["processed_tx_hashes"] = list(processed)
        save_state(state)
        logger.info("cold start: marked %d existing txs as seen (no backfill)", len(processed))

    logger.info(
        "copy trader online (snek.fun only)  target=%s  buy=%s:%s (min=%.2f max=%.2f ADA)  "
        "sell=%s:%s  poll=%ds  slippage=%s  dry_run=%s",
        _short(args.target),
        strategy.buy_mode, strategy.buy_value,
        strategy.min_buy_ada, strategy.max_buy_ada,
        strategy.sell_mode, strategy.sell_value,
        args.poll_sec, strategy.slippage, strategy.dry_run,
    )

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while not _stop:
        try:
            recent = bf.get_address_transactions(args.target, count=20)
        except Exception as e:
            logger.warning("poll failed: %s", e)
            _sleep(args.poll_sec)
            continue

        new_txs = [tx for tx in reversed(recent) if tx["tx_hash"] not in processed]
        if new_txs:
            logger.info("found %d new tx(s) on target", len(new_txs))

        for tx in new_txs:
            tx_hash = tx["tx_hash"]
            try:
                utxos = bf.get_tx_utxos(tx_hash)
            except Exception as e:
                logger.warning("tx_utxos fetch failed for %s: %s", _short(tx_hash), e)
                continue

            processed.add(tx_hash)
            state["processed_tx_hashes"] = list(processed)
            save_state(state)

            if not tx_involves_snekfun(utxos):
                logger.info("skip  %s  non-snekfun tx", _short(tx_hash))
                continue

            classification = classify_snekfun_trade(utxos, args.target)
            if classification is None:
                logger.info("skip  %s  snekfun tx but not a single-token trade for target",
                            _short(tx_hash))
                continue

            action, unit, qty = classification
            logger.info("trade %s  %s %s qty=%d", _short(tx_hash), action, _short(unit), qty)

            try:
                if action == "BUY":
                    target_committed = estimate_target_buy_lovelace(utxos)
                    execute_buy(bf, our_address, signing_key, unit,
                                strategy, target_committed)
                elif action == "SELL":
                    execute_sell(bf, our_address, signing_key, unit, strategy)
            except Exception as e:
                logger.exception("execution error for %s: %s", action, e)

        _sleep(args.poll_sec)

    logger.info("stopped.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v else default


def _env_float_opt(name: str) -> Optional[float]:
    v = os.environ.get(name)
    return float(v) if v else None


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes"}


def main() -> None:
    p = argparse.ArgumentParser(
        description="Copy trade a target Cardano wallet on Snek.fun with configurable sizing.",
    )
    p.add_argument(
        "--target",
        default=os.environ.get("COPY_TRADER_TARGET"),
        help="Target bech32 address to copy. Defaults to $COPY_TRADER_TARGET.",
    )
    p.add_argument(
        "--poll-sec", type=int,
        default=int(os.environ.get("COPY_TRADER_POLL_SEC", 30)),
        help="Poll interval in seconds (default: 30).",
    )

    # --- Buy sizing (required; pick a strategy) ---
    p.add_argument(
        "--buy-mode", choices=BUY_MODES,
        default=os.environ.get("COPY_TRADER_BUY_MODE"),
        help=f"Buy sizing strategy (required). One of: {', '.join(BUY_MODES)}. "
             f"Env: COPY_TRADER_BUY_MODE.",
    )
    p.add_argument(
        "--buy-value", type=float,
        default=_env_float_opt("COPY_TRADER_BUY_VALUE"),
        help="Buy sizing value (required): ADA amount if mode=fixed; "
             "percent otherwise. Env: COPY_TRADER_BUY_VALUE.",
    )
    # Shortcut: --budget-ada N == --buy-mode fixed --buy-value N.
    p.add_argument(
        "--budget-ada", type=float, default=None,
        help="Shortcut for --buy-mode fixed --buy-value N.",
    )
    p.add_argument(
        "--min-buy-ada", type=float,
        default=_env_float("COPY_TRADER_MIN_BUY_ADA", 1.0),
        help="Safety cap: lower bound on any buy in ADA (default: 1). "
             "Env: COPY_TRADER_MIN_BUY_ADA.",
    )
    p.add_argument(
        "--max-buy-ada", type=float,
        default=_env_float("COPY_TRADER_MAX_BUY_ADA", 100.0),
        help="Safety cap: upper bound on any buy in ADA (default: 100). "
             "Env: COPY_TRADER_MAX_BUY_ADA.",
    )

    # --- Sell sizing (required; pick a strategy) ---
    p.add_argument(
        "--sell-mode", choices=SELL_MODES,
        default=os.environ.get("COPY_TRADER_SELL_MODE"),
        help=f"Sell sizing strategy (required). One of: {', '.join(SELL_MODES)}. "
             f"Env: COPY_TRADER_SELL_MODE.",
    )
    p.add_argument(
        "--sell-value", type=float,
        default=_env_float_opt("COPY_TRADER_SELL_VALUE"),
        help="Sell sizing percent 0-100 (required unless --sell-mode=all). "
             "Env: COPY_TRADER_SELL_VALUE.",
    )

    # --- Execution knobs ---
    p.add_argument(
        "--slippage",
        default=os.environ.get("COPY_TRADER_SLIPPAGE", "15"),
        help='Snek.fun slippage option (default: "15"). Use "infinity" to accept any.',
    )
    p.add_argument(
        "--dry-run", action="store_true",
        default=_env_bool("COPY_TRADER_DRY_RUN"),
        help="Detect and log trades but do not submit.",
    )

    args = p.parse_args()

    if not args.target:
        print("ERROR: --target is required (or set COPY_TRADER_TARGET).", file=sys.stderr)
        sys.exit(2)

    if args.budget_ada is not None:
        args.buy_mode = "fixed"
        args.buy_value = args.budget_ada

    missing = []
    if not args.buy_mode:
        missing.append("--buy-mode / COPY_TRADER_BUY_MODE")
    if args.buy_value is None:
        missing.append("--buy-value / COPY_TRADER_BUY_VALUE")
    if not args.sell_mode:
        missing.append("--sell-mode / COPY_TRADER_SELL_MODE")
    if args.sell_mode and args.sell_mode != "all" and args.sell_value is None:
        missing.append("--sell-value / COPY_TRADER_SELL_VALUE")
    if missing:
        print(
            "ERROR: missing required sizing config:\n  "
            + "\n  ".join(missing)
            + f"\n\nBuy modes:  {', '.join(BUY_MODES)}"
            + f"\nSell modes: {', '.join(SELL_MODES)}",
            file=sys.stderr,
        )
        sys.exit(2)

    # sell_value is only used when mode != all; default to 0 to keep the
    # dataclass well-formed.
    if args.sell_value is None:
        args.sell_value = 0.0

    strategy = Strategy(
        buy_mode=args.buy_mode,
        buy_value=args.buy_value,
        sell_mode=args.sell_mode,
        sell_value=args.sell_value,
        min_buy_ada=args.min_buy_ada,
        max_buy_ada=args.max_buy_ada,
        slippage=args.slippage,
        dry_run=args.dry_run,
    )

    run(args, strategy)


if __name__ == "__main__":
    main()
