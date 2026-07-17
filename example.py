"""
example.py — end-to-end demo of event_backtest.

Fabricates a small signal stream and synthetic candles, runs the harness
with the toy strategy in example_strategy.py, and prints stats. No network,
no real data — just proves the wiring works.
"""
from __future__ import annotations

import random

from event_backtest import BacktestConfig, Candle, Signal, run_backtest, sweep
from example_strategy import flat_take_profit_stop_loss


# ---------------------------------------------------------------------------
# Fake signal stream
# ---------------------------------------------------------------------------
random.seed(42)

BASE_TS = 1_700_000_000  # some arbitrary unix timestamp
SYMBOLS = [f"COIN{i}" for i in range(10)]

signals: list[Signal] = []
for i in range(200):
    signals.append(
        Signal(
            t=BASE_TS + i * 45,               # signals every 45s
            sym=random.choice(SYMBOLS),
            price=1.0 + random.random() * 0.5,
            score=random.uniform(40, 95),
            meta={"batch": i // 10},
        )
    )


# ---------------------------------------------------------------------------
# Fake candle provider
# ---------------------------------------------------------------------------
def synthetic_candles(sym: str, start_ts: float, duration_min: int) -> list[Candle]:
    """Random walk starting at price ~1.0, drift ~0, per-minute vol 0.4%."""
    rng = random.Random(hash((sym, int(start_ts))) & 0xFFFF_FFFF)
    price = 1.0 + rng.random() * 0.5
    out: list[Candle] = []
    for i in range(duration_min):
        step = rng.gauss(0, price * 0.004)
        new = max(0.001, price + step)
        h = max(price, new) * (1 + rng.uniform(0, 0.002))
        l = min(price, new) * (1 - rng.uniform(0, 0.002))
        out.append(Candle(t=start_ts + i * 60, o=price, h=h, l=l, c=new, v=1000))
        price = new
    return out


# ---------------------------------------------------------------------------
# Single run
# ---------------------------------------------------------------------------
config = BacktestConfig(
    n_slots=5,
    starting_balance=1000.0,
    cooldown_seconds=30 * 60,
    max_hold_min=240,
    fee_pct_round_trip=0.001,
    cycle_seconds=30,
    strategy_params={"tp_pct": 0.02, "sl_pct": 0.01},
)

print("=" * 70)
print("SINGLE RUN")
print("=" * 70)
result = run_backtest(signals, flat_take_profit_stop_loss, synthetic_candles, config)
print(result.summary_line())
print()
print("Exit-reason breakdown:")
for reason, data in sorted(result.exit_reason_breakdown().items(), key=lambda x: -x[1]["count"]):
    pct = (data["count"] / result.n_trades * 100) if result.n_trades else 0
    print(f"  {reason:<10} {int(data['count']):>4} ({pct:>5.1f}%)  PnL: ${data['pnl']:+.2f}")


# ---------------------------------------------------------------------------
# Sweep example — same signals, three different TP levels
# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
print("SWEEP — TP sensitivity")
print("=" * 70)
sweep_configs = []
for tp in [0.015, 0.02, 0.03, 0.05]:
    cfg = BacktestConfig(
        n_slots=5,
        cooldown_seconds=30 * 60,
        max_hold_min=240,
        fee_pct_round_trip=0.001,
        strategy_params={"tp_pct": tp, "sl_pct": 0.01},
    )
    sweep_configs.append((f"tp={tp:.3f}", cfg))

sweep(signals, flat_take_profit_stop_loss, synthetic_candles, sweep_configs)
