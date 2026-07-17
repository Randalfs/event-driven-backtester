"""
example_strategy.py — a deliberately dumb strategy so the example runs.

Not a suggestion, not a starting point for real trading. Its whole job is
to show the shape of the callable the harness expects: take an entry
price and a candle window, decide when to exit, return (pnl_pct, reason,
hold_minutes).

Swap this out for whatever you actually want to test. The harness does
not care what happens inside.
"""
from __future__ import annotations

from typing import Any

from event_backtest import Candle


def flat_take_profit_stop_loss(
    entry: float,
    candles: list[Candle],
    params: dict[str, Any],
) -> tuple[float, str, int]:
    """First-touch TP or SL on a long, timeout on the tail.

    params:
        tp_pct: fractional take-profit above entry, default 0.02
        sl_pct: fractional stop-loss below entry, default 0.01

    Returns (pnl_pct, reason, hold_min). Long-only, no BE, no ladder, no
    trailing — bring your own logic if you want any of that.
    """
    tp = entry * (1.0 + float(params.get("tp_pct", 0.02)))
    sl = entry * (1.0 - float(params.get("sl_pct", 0.01)))

    for i, c in enumerate(candles):
        minute = i + 1
        # Stop first (conservative order — a real backtest should model
        # touch-order more carefully).
        if c.l <= sl:
            return (sl - entry) / entry, "SL", minute
        if c.h >= tp:
            return (tp - entry) / entry, "TP", minute

    if candles:
        exit_price = candles[-1].c
        return (exit_price - entry) / entry, "TIMEOUT", len(candles)
    return 0.0, "NO_DATA", 0
