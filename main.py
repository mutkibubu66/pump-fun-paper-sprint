"""Pump.fun paper-trading bot for make.fun (agent-fun/replay/v2).

Strategy: early momentum with a hard exit discipline. Every new coin is a
candidate the moment its CreateEvent arrives. A coin is bought only once it
has already run up at least 10% from its creation price (fresh curves are
mostly bundled launches that get dumped, so the run-up is the filter) and its
size cap lets us buy a meaningful amount; the stake is a fixed 0.05 SOL
(never more than five open positions at once, never more than the free cash
allows). Each position is then watched every batch and sold in full on the
first of three exits:

  * take profit: price up 30% from our entry price,
  * stop loss:   price down 20% from our entry price,
  * timeout:     held longer than 4,000 slots (~27 minutes) with neither
                 exit hit, so the slot is freed for a fresher coin.

Prices come from the bonding curve's virtual reserves (SOL per token), the
entry price from the position's cost over its token amount; the ratio is
compared with integer cross-multiplication, so no floats are involved anywhere
and the two graded executions agree byte for byte. The bot reads no clock, no
environment and no unseeded random.

The guardrails shape the sizing: a round trip must be at least 0.01 SOL on at
least 20 distinct coins, and the 0.05 SOL stake clears that floor while the
take-profit/stop-loss/timeout exits keep the book turning over fast enough to
reach 20 coins inside the 6-hour window. At most 0.25 SOL is ever at risk at
once, so the 50% max-drawdown guardrail has a wide margin.

Run by the grader as ``python3 -P -s /bot/main.py`` with PYTHONHASHSEED=0.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script directory off sys.path)

STAKE_LAMPORTS = 50_000_000            # 0.05 SOL per coin
MIN_BUY_LAMPORTS = 10_000_000          # 0.01 SOL: the guardrail's round-trip floor
MAX_SLIPPAGE_BPS = 10_000
TAKE_PROFIT_NUM = 13                   # sell when price >= 1.3x entry
TAKE_PROFIT_DEN = 10
STOP_LOSS_NUM = 80                     # sell when price <= 0.80x entry
STOP_LOSS_DEN = 100
HOLD_TIMEOUT_SLOTS = 4000              # ~27 minutes at ~0.4 s per slot
MOMENTUM_NUM = 11                      # buy only once price >= 1.1x creation price
MOMENTUM_DEN = 10

#: Coins created and not yet decided (only tested for membership, never iterated).
candidates = set()
#: Creation price (virtual_quote_reserves, virtual_token_reserves) per mint.
creation_price = {}
#: Slot each open position was first seen at, by mint.
entry_slot = {}


def _exit_reason(state, mint, holding):
    """None, or "take-profit" / "stop-loss" / "timeout" for this position."""
    curve = state.curves.get(mint)
    if curve is None or curve.complete:
        return None
    if curve.virtual_token_reserves <= 0 or holding.cost_lamports <= 0:
        return None
    # ratio = current_price / entry_price
    #       = (vq / vt) / (cost / tokens) = vq * tokens / (vt * cost)
    num = curve.virtual_quote_reserves * holding.token_amount
    den = curve.virtual_token_reserves * holding.cost_lamports
    if num * TAKE_PROFIT_DEN >= den * TAKE_PROFIT_NUM:
        return "take-profit"
    if num * STOP_LOSS_DEN <= den * STOP_LOSS_NUM:
        return "stop-loss"
    opened = entry_slot.get(mint)
    if opened is not None and state.slot - opened > HOLD_TIMEOUT_SLOTS:
        return "timeout"
    return None


def on_batch(state, batch):
    caps = state.hello["caps"]
    tx = state.tx_fee_lamports()
    free = state.free_cash_lamports()
    slots = caps["max_open_positions"] - state.committed_position_count()
    orders = []
    max_orders = caps["max_orders_per_batch"]

    # Remember when each open position was first seen.
    for mint in state.positions:
        entry_slot.setdefault(mint, state.slot)
    for mint in list(entry_slot):
        if mint not in state.positions:
            del entry_slot[mint]

    # 1. Exits first: sell every position whose exit has triggered, in full.
    selling = set()
    for p in state.pending.values():
        if p.order["side"] == "sell":
            selling.add(p.order["mint"])
    for mint, holding in state.positions.items():
        if mint in selling or len(orders) >= max_orders:
            continue
        if _exit_reason(state, mint, holding) is None:
            continue
        orders.append(
            {
                "side": "sell",
                "mint": mint,
                "token_amount": holding.token_amount,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            }
        )
        selling.add(mint)

    # 2. Entries: one fixed stake per fresh coin, decided once.
    for e in batch["events"]:
        mint = e["mint"]
        curve = state.curves.get(mint)
        if e["event"] == "CreateEvent":
            candidates.add(mint)
            if curve is not None and curve.virtual_token_reserves > 0:
                creation_price[mint] = (
                    curve.virtual_quote_reserves,
                    curve.virtual_token_reserves,
                )
        if mint not in candidates:
            continue
        if curve is None or curve.complete:
            candidates.discard(mint)
            continue
        cap = state.max_buy_lamports(mint)
        if cap < MIN_BUY_LAMPORTS:
            continue
        # Momentum filter: only buy coins already up from their creation price.
        cp = creation_price.get(mint)
        if cp is not None and curve.virtual_token_reserves > 0:
            if (
                curve.virtual_quote_reserves * cp[1] * MOMENTUM_DEN
                < cp[0] * curve.virtual_token_reserves * MOMENTUM_NUM
            ):
                continue
        candidates.discard(mint)
        amount = min(cap, STAKE_LAMPORTS)
        if len(orders) >= max_orders or slots <= 0 or free < amount + tx:
            continue
        orders.append(
            {
                "side": "buy",
                "mint": mint,
                "quote_lamports": amount,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            }
        )
        free -= amount + tx
        slots -= 1
    return orders


def on_done(state, done):
    cash = int(state.account["cash_lamports"])
    start = int(state.hello["capital"]["lamports"])
    print(
        f"[pump-fun-paper] done after {state.seq} batches: "
        f"cash {cash} lamports vs {start} start "
        f"({(cash - start) / 1e9:+.4f} SOL), "
        f"{len(state.positions)} positions open, "
        f"{len(state.curves)} curves seen",
        file=sys.stderr,
    )


if __name__ == "__main__":
    makefun_bot.run(on_batch, on_done=on_done)
