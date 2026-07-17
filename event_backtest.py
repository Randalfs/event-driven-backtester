"""
event_backtest.py — Event-driven backtester with slot rotation and cooldowns.

The loop:
  1. Load a chronological stream of scored signals.
  2. Bucket them into fixed-length cycles (mimicking a scanner's polling cadence).
  3. Inside each cycle, sort candidates by score and open the top ones into
     free portfolio slots, honouring per-symbol cooldowns and dedup rules.
  4. For every opened position, hand entry price + a candle window to a
     user-supplied strategy function; it decides how the trade closes.
  5. Tally reasons, PnL, drawdown, exit-reason distribution.

None of that is strategy — it's the harness. Bring your own signals,
your own candle provider, and your own strategy function.

Original of this module ran on a live crypto-futures signal scanner in
production. The strategy body and the specific parameters are the alpha
and stay private; the loop, the slot rotation, the cooldown logic, and
the metrics output are what's here. This module is what you'd extract if
you wanted to give someone else the same testing rig without giving them
the edge.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
@dataclass
class Signal:
    """A single scored signal emitted by the scanner at time `t` (unix seconds)."""

    t: float
    sym: str
    price: float
    score: float
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Candle:
    """OHLCV candle. Use whatever timeframe you like — the strategy owns
    interpretation."""

    t: float
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


@dataclass
class Trade:
    sym: str
    entry: float
    open_ts: float
    close_ts: float
    hold_min: int
    pnl_pct: float          # gross, before fees
    pnl_dollar: float       # net of fees
    reason: str             # returned by the strategy
    notional: float
    score: float


@dataclass
class BacktestConfig:
    """All the knobs the harness itself needs. Pass strategy-specific knobs
    inside the strategy closure or via `strategy_params`."""

    n_slots: int = 5
    starting_balance: float = 1000.0
    cooldown_seconds: int = 30 * 60
    max_hold_min: int = 480
    fee_pct_round_trip: float = 0.0
    cycle_seconds: int = 30
    min_slot_size: float = 5.0
    strategy_params: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Provider interfaces
# ---------------------------------------------------------------------------
class CandleProvider(Protocol):
    """Fetches candles starting at `start_ts` for `duration_min` minutes."""

    def __call__(self, sym: str, start_ts: float, duration_min: int) -> list[Candle]: ...


class Strategy(Protocol):
    """Simulates one trade end to end.

    Given the entry price and a candle window, decide when to close and
    return (pnl_pct, reason, hold_minutes). This is where your alpha lives.
    """

    def __call__(
        self,
        entry: float,
        candles: list[Candle],
        params: dict[str, Any],
    ) -> tuple[float, str, int]: ...


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------
def run_backtest(
    signals: Iterable[Signal],
    strategy: Strategy,
    candles_for: CandleProvider,
    config: BacktestConfig | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> "BacktestResult":
    """Run one backtest pass over the full signal stream.

    Args:
        signals: chronological iterable of Signal
        strategy: user-supplied Strategy function (BYO alpha)
        candles_for: user-supplied CandleProvider (BYO data)
        config: BacktestConfig — slot count, cooldown, fees, cycle length
        on_progress: optional callback (n_fetches, n_trades) every 100 fetches

    Returns:
        BacktestResult with per-trade log and aggregate stats.
    """
    cfg = config or BacktestConfig()

    balance = cfg.starting_balance
    open_trades: list[Trade] = []
    closed_trades: list[Trade] = []
    cooldowns: dict[str, float] = {}

    # Sort by time and bucket into cycles.
    signals_sorted = sorted(signals, key=lambda s: s.t)
    cycles: dict[int, list[Signal]] = defaultdict(list)
    for s in signals_sorted:
        bucket = int(s.t // cfg.cycle_seconds) * cfg.cycle_seconds
        cycles[bucket].append(s)

    total = 0
    skipped_cd = 0
    skipped_slots = 0
    skipped_dup = 0
    n_fetches = 0

    for ts_key in sorted(cycles.keys()):
        cycle_signals = cycles[ts_key]
        cycle_ts = float(ts_key)

        # Retire trades that have already closed by the time this cycle starts.
        open_trades = [t for t in open_trades if t.close_ts > cycle_ts]

        # Strongest first — the "top of book" style prioritisation.
        cycle_signals.sort(key=lambda s: s.score, reverse=True)

        for sig in cycle_signals:
            if not sig.sym or not sig.price or sig.price <= 0:
                continue

            total += 1

            if sig.sym in cooldowns and (cycle_ts - cooldowns[sig.sym]) < cfg.cooldown_seconds:
                skipped_cd += 1
                continue

            if len(open_trades) >= cfg.n_slots:
                skipped_slots += 1
                continue

            if any(t.sym == sig.sym for t in open_trades):
                skipped_dup += 1
                continue

            slot_size = balance / cfg.n_slots
            if slot_size <= cfg.min_slot_size:
                continue

            candles = candles_for(sig.sym, cycle_ts, cfg.max_hold_min)
            n_fetches += 1
            if on_progress and n_fetches % 100 == 0:
                on_progress(n_fetches, len(closed_trades))

            if not candles or len(candles) < 5:
                continue

            pnl_pct, reason, hold_min = strategy(sig.price, candles, cfg.strategy_params)

            notional = slot_size
            fees = notional * cfg.fee_pct_round_trip
            pnl_dollar = pnl_pct * notional - fees

            close_ts = cycle_ts + hold_min * 60
            trade = Trade(
                sym=sig.sym,
                entry=sig.price,
                open_ts=cycle_ts,
                close_ts=close_ts,
                hold_min=hold_min,
                pnl_pct=pnl_pct * 100,
                pnl_dollar=pnl_dollar,
                reason=reason,
                notional=notional,
                score=sig.score,
            )
            open_trades.append(trade)
            closed_trades.append(trade)

            balance += pnl_dollar
            cooldowns[sig.sym] = cycle_ts

    return BacktestResult(
        trades=closed_trades,
        starting_balance=cfg.starting_balance,
        final_balance=balance,
        total_signals=total,
        skipped_cooldown=skipped_cd,
        skipped_slots_full=skipped_slots,
        skipped_duplicate=skipped_dup,
    )


# ---------------------------------------------------------------------------
# Results & aggregates
# ---------------------------------------------------------------------------
@dataclass
class BacktestResult:
    trades: list[Trade]
    starting_balance: float
    final_balance: float
    total_signals: int
    skipped_cooldown: int
    skipped_slots_full: int
    skipped_duplicate: int

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> list[Trade]:
        return [t for t in self.trades if t.pnl_dollar > 0]

    @property
    def losses(self) -> list[Trade]:
        return [t for t in self.trades if t.pnl_dollar <= 0]

    @property
    def win_rate_pct(self) -> float:
        return (len(self.wins) / self.n_trades * 100) if self.n_trades else 0.0

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl_dollar for t in self.trades)

    @property
    def avg_win(self) -> float:
        w = self.wins
        return (sum(t.pnl_dollar for t in w) / len(w)) if w else 0.0

    @property
    def avg_loss(self) -> float:
        l = self.losses
        return (sum(t.pnl_dollar for t in l) / len(l)) if l else 0.0

    @property
    def profit_factor(self) -> float:
        gross_win = sum(t.pnl_dollar for t in self.wins)
        gross_loss = sum(t.pnl_dollar for t in self.losses)
        return abs(gross_win / gross_loss) if gross_loss else float("inf")

    def exit_reason_breakdown(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "pnl": 0.0})
        for t in self.trades:
            out[t.reason]["count"] += 1
            out[t.reason]["pnl"] += t.pnl_dollar
        return dict(out)

    def summary_line(self) -> str:
        return (
            f"trades={self.n_trades:>5} "
            f"WR={self.win_rate_pct:>5.1f}% "
            f"pnl=${self.total_pnl:>8.2f} "
            f"final=${self.final_balance:>8.2f} "
            f"avgW=${self.avg_win:>7.2f} "
            f"avgL=${self.avg_loss:>7.2f} "
            f"PF={self.profit_factor:>5.2f} "
            f"skipCD={self.skipped_cooldown} slots={self.skipped_slots_full} dup={self.skipped_duplicate}"
        )


# ---------------------------------------------------------------------------
# Sweep helper — run the same harness across a grid of configs
# ---------------------------------------------------------------------------
def sweep(
    signals: list[Signal],
    strategy: Strategy,
    candles_for: CandleProvider,
    label_configs: list[tuple[str, BacktestConfig]],
) -> list[tuple[str, BacktestResult]]:
    """Run `run_backtest` once per (label, config) tuple, in order.

    Useful for threshold sweeps ("min score 60 vs 70 vs 80") or fee
    sensitivity. Same signal stream, different filtering/config per pass.
    """
    out: list[tuple[str, BacktestResult]] = []
    for label, cfg in label_configs:
        t0 = time.time()
        result = run_backtest(signals, strategy, candles_for, cfg)
        dt = time.time() - t0
        print(f"[{label}] {result.summary_line()}  ({dt:.1f}s)")
        out.append((label, result))
    return out
