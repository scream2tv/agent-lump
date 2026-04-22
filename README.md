# Agent Lump

Cardano trading automation: DEX swaps, arbitrage scanning, copy trading, and
bonding-curve launches. Each tool below is written as a self-contained skill
an agent can pick up, aim, and run.

---

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env         # fill in BLOCKFROST_PROJECT_ID + wallet paths
python3 setup_wallet.py --update-env    # optional: generate a fresh wallet
```

Required env:

| Var                         | Purpose                                    |
|-----------------------------|--------------------------------------------|
| `BLOCKFROST_PROJECT_ID`     | Cardano mainnet access (https://blockfrost.io) |
| `CARDANO_PAYMENT_ADDRESS`   | Agent's bech32 payment address             |
| `CARDANO_PRIVATE_KEY_PATH`  | Path to agent's `.skey` signing key        |

---

## skill: copy_trader

**Use when** you want to mirror another Cardano wallet's Snek.fun buys and sells
with a configurable sizing strategy.

**What it does.** Polls Blockfrost for new txs on the target address, filters
to Snek.fun script involvement only (pool validator or order validator —
every other tx type is ignored), classifies the trade (`BUY` = target
received a token, `SELL` = target sent a token to a snek.fun script), and
copies it from the configured wallet via the Snek.fun builder API. Dedupes
by tx hash in `~/.agent-lump/copy_trader_state.json`; no backfill on cold
start.

**Sizing strategies (agent-selectable — required).** Pick one buy mode and
one sell mode per run. No strategy is assumed; the daemon refuses to start
until both are configured (via CLI flags or `COPY_TRADER_*` env vars).

| Mode          | Buy meaning                               | Sell meaning                 |
|---------------|-------------------------------------------|------------------------------|
| `fixed`       | buy exactly N ADA                         | —                            |
| `pct-target`  | buy N% of the ADA target committed        | —                            |
| `pct-wallet`  | buy N% of our wallet's free ADA balance   | —                            |
| `all`         | —                                         | sell 100% of our holding     |
| `pct-holding` | —                                         | sell N% of our holding       |

Every buy is clamped by `--min-buy-ada` / `--max-buy-ada` safety caps
(defaults: 1 / 100 ADA) regardless of mode.

**Run — explicit flags.**

```bash
# fixed ADA buy, sell everything
python3 copy_trader.py --target <addr> \
    --buy-mode fixed --buy-value 10 \
    --sell-mode all

# buy a percentage of what the target committed, sell partial
python3 copy_trader.py --target <addr> \
    --buy-mode pct-target --buy-value 20 \
    --sell-mode pct-holding --sell-value 50

# buy a percentage of our wallet balance
python3 copy_trader.py --target <addr> \
    --buy-mode pct-wallet --buy-value 5 \
    --sell-mode all

# detect only, no submit
python3 copy_trader.py --target <addr> \
    --buy-mode fixed --buy-value 10 --sell-mode all --dry-run
```

**Run — env-driven.** Set `COPY_TRADER_*` in `.env` (see `.env.example`)
and call without sizing flags:

```bash
python3 copy_trader.py --target <addr>
```

**Limitations.** Bonding-curve tokens only (CPMM / graduated tokens not yet
supported). Does not follow the target's in-app "splash wallet" — pass the
address that actually appears on-chain as the tx input/output.

---

## skill: swap_ada_to_token

**Use when** you want to swap ADA for any Cardano token via the best-priced
route across 15+ DEXes.

**What it does.** Uses DexHunter v3 (`search → estimate → build → local-sign
→ submit`) with a confirmation prompt, high-impact warnings, and a 2%
price-impact safety ceiling.

**Run.**

```bash
python3 swap_ada_to_token.py --token NIGHT --amount 2
python3 swap_ada_to_token.py --token-id <policyId+hex> --amount 5 --slippage 2
python3 swap_ada_to_token.py --token SNEK --amount 10 --yes    # skip prompt
```

---

## skill: arb_scanner / arb_executor

**Use when** you want to discover or execute cross-DEX arbitrage opportunities
on Cardano token pairs.

**What it does.** `arb_scanner.py` scans a token universe across multiple
DEXes and surfaces price discrepancies; `arb_executor.py` takes a detected
opportunity through to an executed cycle using DexHunter routing.

Configure via `ARB_*` env vars (trade amount, min profit, scan interval,
impact caps, dry-run). Defaults are dry-run.

---

## skill: snekfun_client

**Use when** you want to buy, sell, launch, or cancel orders on Snek.fun's
bonding-curve tokens directly (no copy-trading).

**What it does.** Wraps Snek.fun's builder API (`/trade`, `/cpmm-trade`,
`/launch`, `/cancel`, `/sign-and-submit`) with local CBOR signing so keys
never leave the machine. Also used as the execution layer of `copy_trader`.

**CLI.**

```bash
python3 snekfun_client.py --help
```

Convenience scripts:

- `snekfun_buy_lump.py` — one-shot buy of LUMP
- `snekfun_swap.py`     — direct buy/sell against the bonding curve
- `launch_woof.py`      — launch the WOOF token (reads `woof_launch.json`)
- `monitor_woof.py`     — watch WOOF pool state after launch

---

## skill: blockfrost_client

Shared Cardano chain access (UTxOs, protocol params, tx submit). Imported by
every other module; not a CLI.

---

## skill: midnight-agent

**Use when** you want to deploy tokens to the Midnight Network, interact with
the NYX deploy UI, or experiment with Compact smart contracts via the 1AM
wallet.

See `midnight-agent/README.md` for setup and commands.

---

## Files

```
copy_trader.py            # snek.fun copy-trading daemon
swap_ada_to_token.py      # one-shot DexHunter swap
arb_scanner.py            # cross-DEX arbitrage discovery
arb_executor.py           # arb execution
snekfun_client.py         # snek.fun builder API + CLI
dexhunter_client.py       # DexHunter v3 aggregator client
blockfrost_client.py      # shared Cardano chain client
minswap_*.py              # Minswap v1 / v2 / aggregator / data clients
sundaeswap_client.py      # SundaeSwap client
bodega_client.py          # Bodega prediction-market client
cardexscan_client.py      # CardexScan aggregator client
setup_wallet.py           # generate a fresh payment wallet
midnight-agent/           # Midnight Network tooling
```

---

## License

See `LICENSE`.
