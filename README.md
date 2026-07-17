# event-driven-backtester

An event-driven backtest harness with portfolio slot rotation, per-symbol cooldowns, and exit-reason accounting. Everything strategy-adjacent (thresholds, TP/SL logic, fees) is passed in; the harness owns the loop, the slot management, the cooldowns, and the stats.

Pure standard library. No pandas, no numpy, no framework — a single file you drop into any Python 3.10+ project.

## What this is (and is not)

**Is:** the plumbing you need if you want to replay a stream of scored signals through a portfolio-sized simulation with realistic slot / cooldown / dedup constraints, and get back a per-trade log plus win rate, PF, exit-reason breakdown.

**Is not:** a strategy. A data source. A charting layer. A live executor. The point of extracting the harness from the alpha is that you can share the harness without sharing the strategy — and the same harness runs behind any strategy that fits the `Strategy` callable signature.

## Why this exists

The original of this module ran on a live crypto-futures signal scanner in production. When I wanted to tune the strategy, I needed to replay a few weeks of historical signals through the same slot / cooldown / dedup rules the live system used, then diff the metrics across parameter grids. The bit that survived the extraction is the plumbing: the chronological cycle bucket loop, the slot rotation, the per-symbol cooldown, the sorted-by-score prioritisation, the fee model, the exit-reason tally. The strategy body itself is my alpha and stays private.

The design constraints that shaped this repo:

- **Zero external dependencies.** Portable, auditable in one read, safe to vendor into anything.
- **Strategy as a pure callable.** `strategy(entry, candles, params) -> (pnl_pct, reason, hold_min)`. No inheritance, no plugin system, no config file. If it fits that shape, the harness runs it.
- **Data source as a callable.** `candles_for(sym, start_ts, duration_min) -> list[Candle]`. Bring your own exchange, your own cache, your own mocking.
- **Realistic slot rules by default.** A one-line backtest that ignores portfolio slot count and cooldowns and dedup will overstate a real strategy's PnL. This harness makes those constraints impossible to skip.
- **A sweep helper on top.** Threshold sensitivity, fee sensitivity, and "what if my cooldown were 15 minutes not 30" all fall out of the same `sweep()` call over a list of `(label, config)` pairs.

## Install

Requires Python 3.10+. No pip needed.

```bash
git clone https://github.com/Randalfs/event-driven-backtester.git
cd event-driven-backtester
python example.py
```

## Use it

```python
from event_backtest import BacktestConfig, Signal, run_backtest

def my_strategy(entry, candles, params):
    # ... your alpha here ...
    return pnl_pct, reason_str, hold_minutes

def my_candles(sym, start_ts, duration_min):
    # ... fetch from your exchange, your cache, your DB ...
    return [Candle(t=..., o=..., h=..., l=..., c=..., v=...)]

signals = [Signal(t=..., sym="...", price=..., score=...) for ...]

result = run_backtest(
    signals,
    my_strategy,
    my_candles,
    BacktestConfig(
        n_slots=5,
        starting_balance=1000.0,
        cooldown_seconds=30 * 60,
        max_hold_min=240,
        fee_pct_round_trip=0.001,
        strategy_params={"tp_pct": 0.02, "sl_pct": 0.01},
    ),
)

print(result.summary_line())
for reason, data in result.exit_reason_breakdown().items():
    print(reason, data)
```

A runnable end-to-end example with fake signals, synthetic random-walk candles, and a deliberately-dumb TP/SL strategy is in `example.py`. Run `python example.py` and you should see two blocks of output: a single run and a small TP-sensitivity sweep.

## Config knobs

`BacktestConfig`:

| Field                    | Default | Purpose                                                       |
| ------------------------ | ------: | ------------------------------------------------------------- |
| `n_slots`                |       5 | Max concurrent open trades                                    |
| `starting_balance`       |    1000 | Initial capital in dollars                                    |
| `cooldown_seconds`       |    1800 | Per-symbol cooldown after a trade closes                      |
| `max_hold_min`           |     480 | Candle-window length handed to the strategy                   |
| `fee_pct_round_trip`     |     0.0 | Total fees per trade as fraction of notional                  |
| `cycle_seconds`          |      30 | Signal-bucketing window (mimics scanner poll cadence)         |
| `min_slot_size`          |     5.0 | Skip trades if per-slot balance falls below this              |
| `strategy_params`        |      {} | Forwarded to your strategy callable as `params`               |

## Design notes

- **Cycle bucketing.** Signals inside the same `cycle_seconds` window are competing for the same slots. The harness sorts them by score descending and fills in that order — same shape as a scanner that polls exchanges every 30s and picks its top candidates each poll. If your signal stream is truly continuous, set `cycle_seconds` to 1.
- **Cooldowns are per-symbol.** When a trade opens for `X`, the cooldown starts at the entry timestamp, and any further `X` signals within `cooldown_seconds` are skipped. That's the same cooldown model most live scanners use, and it keeps the backtest honest about not doubling up on the same symbol.
- **Dedup happens before slot check.** If `X` is already in an open position, further `X` signals are dropped even if slots are free. Two trades on the same symbol simultaneously is usually a strategy bug rather than a feature.
- **Balance updates immediately.** Each trade's PnL applies to `balance` at open (not at close). That means slot sizing for the next trade sees the updated balance. If you want strict "close-first" accounting, you'd sort the closed_trades log by `close_ts` and reconstruct — the fields are all there.
- **Fees are simple.** `fee_pct_round_trip * notional` charged once per trade at close. If you want per-side fees or funding accrual, subclass `Trade` and do the math after the fact — the `pnl_pct` field is gross and untouched.
- **`sweep()` is intentionally trivial.** It's a for-loop over configs that runs the harness and prints one line per pass. No grid product, no cross-fold, no parallelism. Compose it yourself if you want more.

## What's deliberately not here

- **No exchange integration.** The `CandleProvider` protocol is one method. Point it at anything.
- **No live executor.** Backtest only. Live trading has real slippage, real partial fills, real API rate limits — a different problem.
- **No plotting.** You get a `list[Trade]` back; use matplotlib or a spreadsheet.
- **No Sharpe / Sortino / max drawdown.** Add them on top; the field access is public. I left them off because they invite over-fitting on tiny samples.

## License

MIT — see `LICENSE`.
