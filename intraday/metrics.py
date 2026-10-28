"""Performance statistics for a list of trades over a run of sessions."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date

from .data import Day
from .strategies import Costs, Trade


@dataclass
class Stats:
    sessions: int
    trades: int
    days_traded: int
    total: float              # $ net
    mean_day: float
    sd_day: float
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    expectancy_points: float  # per trade, net of slippage, before commission
    max_drawdown: float       # $, on daily closes
    worst_day: float
    best_day: float
    worst_trade: float
    losing_streak: int        # trades
    best_day_share: float     # best day / total profit (consistency rules)
    long_total: float
    short_total: float

    @property
    def trades_per_week(self) -> float:
        return self.trades / self.sessions * 5 if self.sessions else 0.0

    @property
    def sharpe(self) -> float:
        """Annualised, from daily P&L including days with no trade."""
        return self.mean_day / self.sd_day * math.sqrt(252) if self.sd_day > 0 else 0.0

    @property
    def t_stat(self) -> float:
        return self.mean_day / self.sd_day * math.sqrt(self.sessions) if self.sd_day > 0 else 0.0

    @property
    def per_year(self) -> float:
        return self.mean_day * 252


def daily_pnl(trades: list[Trade], days: list[Day], costs: Costs,
              contracts=1.0) -> list[tuple[date, float]]:
    """Net $ P&L of every session in `days`, zero on days with no trade.
    `contracts` is a number or a function of the trade."""
    size = contracts if callable(contracts) else (lambda t: contracts)
    pnl = {d.date: 0.0 for d in days}
    for t in trades:
        if t.day in pnl:
            pnl[t.day] += costs.dollars(t.points, size(t))
    return sorted(pnl.items())


def summarize(trades: list[Trade], days: list[Day], costs: Costs, contracts=1.0) -> Stats:
    size = contracts if callable(contracts) else (lambda t: contracts)
    span = {d.date for d in days}
    trades = [t for t in trades if t.day in span]
    series = [p for _, p in daily_pnl(trades, days, costs, contracts)]
    dollars = [costs.dollars(t.points, size(t)) for t in trades]
    wins = [x for x in dollars if x > 0]
    losses = [x for x in dollars if x <= 0]
    equity = peak = dd = 0.0
    for p in series:
        equity += p
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    streak = run = 0
    for x in dollars:
        run = run + 1 if x <= 0 else 0
        streak = max(streak, run)
    total = sum(series)
    best = max(series, default=0.0)
    return Stats(
        sessions=len(series),
        trades=len(trades),
        days_traded=len({t.day for t in trades}),
        total=total,
        mean_day=statistics.fmean(series) if series else 0.0,
        sd_day=statistics.stdev(series) if len(series) > 1 else 0.0,
        win_rate=len(wins) / len(dollars) if dollars else 0.0,
        avg_win=statistics.fmean(wins) if wins else 0.0,
        avg_loss=statistics.fmean(losses) if losses else 0.0,
        profit_factor=sum(wins) / -sum(losses) if losses and sum(losses) < 0 else math.inf,
        expectancy_points=statistics.fmean(t.points for t in trades) if trades else 0.0,
        max_drawdown=dd,
        worst_day=min(series, default=0.0),
        best_day=best,
        worst_trade=min(dollars, default=0.0),
        losing_streak=streak,
        best_day_share=best / total if total > 0 else math.inf,
        long_total=sum(x for t, x in zip(trades, dollars) if t.side > 0),
        short_total=sum(x for t, x in zip(trades, dollars) if t.side < 0),
    )


def by_year(trades: list[Trade], days: list[Day], costs: Costs, contracts=1.0) -> dict[int, Stats]:
    years = sorted({d.date.year for d in days})
    return {y: summarize(trades, [d for d in days if d.date.year == y], costs, contracts)
            for y in years}


def correlation(a: list[float], b: list[float]) -> float:
    if len(a) < 3 or statistics.pstdev(a) == 0 or statistics.pstdev(b) == 0:
        return math.nan
    return statistics.correlation(a, b)


HEADER = ("| strategy | trades/wk | win | PF | pts/trade | $/day | $/yr | Sharpe | t | "
          "max DD | worst day | best day share |\n"
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")


def row(name: str, s: Stats) -> str:
    share = f"{s.best_day_share:.0%}" if math.isfinite(s.best_day_share) else "n/a"
    pf = f"{s.profit_factor:.2f}" if math.isfinite(s.profit_factor) else "inf"
    return (f"| {name} | {s.trades_per_week:.1f} | {s.win_rate:.0%} | {pf} | "
            f"{s.expectancy_points:+.2f} | {s.mean_day:+,.0f} | {s.per_year:+,.0f} | "
            f"{s.sharpe:.2f} | {s.t_stat:.2f} | {s.max_drawdown:,.0f} | {s.worst_day:,.0f} | {share} |")
