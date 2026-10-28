"""Intraday strategies from the literature, at their published parameters.

Fills, for every strategy:

* A signal is read from the close of a bar, using that bar and earlier ones
  only. A market order then fills at the next bar's open, `slip_ticks`
  against you.
* A stop fills at the stop, or at the open if a bar gaps through it, also
  `slip_ticks` against you. A limit target fills at the target, without
  slippage, only if price trades one tick through it. A bar that touches both
  counts as the stop, since the order inside a bar is unknown.
* Positions still open at the close exit at the last bar's close (16:00),
  `slip_ticks` against you. Nothing is held overnight.

Every trade is for one contract. `Trade.path` holds the best and worst open
P&L in points for each bar the trade was open, so the prop-firm replay can
check intraday drawdown and loss limits, not just closed P&L.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime

from .data import Day


@dataclass(frozen=True)
class Costs:
    """Per contract. The defaults are NQ: $20 a point, a $4.50 round turn of
    commission and exchange fees (typical at prop firms), and one tick of
    slippage on every market and stop fill."""
    point_value: float = 20.0
    tick: float = 0.25
    commission: float = 4.50
    slip_ticks: float = 1.0

    @property
    def slip(self) -> float:
        return self.slip_ticks * self.tick

    def dollars(self, points: float, contracts: float = 1.0) -> float:
        return contracts * (points * self.point_value - self.commission)


INSTRUMENT_COSTS = {
    "NQ": Costs(),
    "MNQ": Costs(point_value=2.0, commission=1.50),
}


@dataclass
class Trade:
    strategy: str
    day: date
    side: int                     # +1 long, -1 short
    entry_index: int              # bar of the entry fill (minutes after 09:30)
    entry_time: datetime
    entry: float                  # fill price, slippage included
    exit_index: int
    exit_time: datetime
    exit: float
    reason: str                   # trail, flip, stop, target, close
    stop_distance: float | None = None   # points from entry to the initial stop
    path: list[tuple[float, float]] = field(default_factory=list, repr=False)

    @property
    def points(self) -> float:
        return self.side * (self.exit - self.entry)


class _Position:
    """The open position of one strategy in one session."""

    def __init__(self, name: str, day: Day, costs: Costs, out: list[Trade]):
        self.name, self.day, self.costs, self.out = name, day, costs, out
        self.side = 0
        self.i0 = 0
        self.entry = 0.0
        self.stop_distance: float | None = None

    def _pnl(self, price: float) -> float:
        return self.side * (price - self.entry)

    def _bar(self, k: int) -> tuple[float, float]:
        b = self.day.bars[k]
        hi, lo = self._pnl(b.h), self._pnl(b.l)
        return (hi, lo) if self.side > 0 else (lo, hi)

    def open(self, side: int, k: int, price: float | None = None,
             stop_distance: float | None = None) -> None:
        """Market order filled at bar k's open (or `price`)."""
        px = self.day.bars[k].o if price is None else price
        self.side, self.i0 = side, k
        self.entry = px + side * self.costs.slip
        self.stop_distance = stop_distance

    def _finish(self, k: int, fill: float, last: tuple[float, float], reason: str) -> None:
        path = [self._bar(j) for j in range(self.i0, k)]
        path.append(last)
        b = self.day.bars
        self.out.append(Trade(self.name, self.day.date, self.side, self.i0, b[self.i0].t,
                              self.entry, k, b[k].t, fill, reason, self.stop_distance, path))
        self.side = 0

    def close_at_open(self, k: int, reason: str) -> None:
        """Market order filled at bar k's open."""
        o = self.day.bars[k].o
        fill = o - self.side * self.costs.slip
        self._finish(k, fill, (self._pnl(o), self._pnl(fill)), reason)

    def close_at_close(self, k: int, reason: str = "close") -> None:
        c = self.day.bars[k].c
        fill = c - self.side * self.costs.slip
        best, worst = self._bar(k)
        self._finish(k, fill, (best, min(worst, self._pnl(fill))), reason)

    def close_intrabar(self, k: int, fill: float, reason: str) -> None:
        """Stop or target filled inside bar k at `fill`."""
        best, worst = self._bar(k)
        f = self._pnl(fill)
        if reason == "stop":
            self._finish(k, fill, (max(best, f), f), reason)
        else:
            self._finish(k, fill, (f, min(worst, f)), reason)


class Strategy:
    name = "strategy"
    paper = ""

    def run(self, days: list[Day], costs: Costs) -> list[Trade]:
        raise NotImplementedError


class NoiseArea(Strategy):
    """Zarattini, Aziz & Barbon (2024), "Beat the Market: An Effective
    Intraday Momentum Strategy for S&P500 ETF (SPY)", SFI Research Paper 24-97.

    The noise area at m minutes after the open is the average over the last
    14 sessions of |close at m / open - 1|, around max(open, previous close)
    above and min(open, previous close) below. At 10:00, 10:30, ..., 15:30:
    above the area, go long; below it, short. A long exits when price closes
    below max(upper band, VWAP) at a check (`exit='final'`, the paper's
    version) or below the lower band (`exit='base'`), and flips if price is
    below the lower band. Shorts mirror. Everything is flat at the close.
    """
    paper = "Zarattini, Aziz & Barbon (2024)"

    def __init__(self, lookback: int = 14, every: int = 30, first: int = 30,
                 exit: str = "final", min_obs: int | None = None):
        if exit not in ("final", "base"):
            raise ValueError("exit must be 'final' or 'base'")
        self.lookback, self.every, self.first, self.exit = lookback, every, first, exit
        self.min_obs = lookback // 2 if min_obs is None else min_obs
        self.name = "noise" if exit == "final" else "noise-base"
        self.checks = list(range(first, 361, every))

    def bands(self, days: list[Day]):
        """Yield (day, {check minute: (sigma, upper, lower)}) for every day."""
        history: deque[dict[int, float]] = deque(maxlen=self.lookback)
        for day in days:
            sig: dict[int, tuple[float, float, float]] = {}
            if day.prev_close is not None:
                for m in self.checks:
                    obs = [h[m] for h in history if m in h]
                    if len(obs) >= self.min_obs:
                        s = sum(obs) / len(obs)
                        sig[m] = (s, max(day.open, day.prev_close) * (1 + s),
                                  min(day.open, day.prev_close) * (1 - s))
            yield day, sig
            if day.usable:
                history.append({m: abs(day.bars[m - 1].c / day.open - 1)
                                for m in self.checks if m <= len(day.bars)})

    def run(self, days: list[Day], costs: Costs) -> list[Trade]:
        out: list[Trade] = []
        for day, sig in self.bands(days):
            if not day.usable or not sig:
                continue
            pos = _Position(self.name, day, costs, out)
            bars, vwap = day.bars, day.vwap()
            for m in self.checks:
                if m >= len(bars) or m not in sig:
                    continue
                _, upper, lower = sig[m]
                px = bars[m - 1].c
                if pos.side > 0:
                    trail = lower if self.exit == "base" else max(upper, vwap[m - 1])
                    if px < lower:
                        pos.close_at_open(m, "flip")
                        pos.open(-1, m)
                    elif px < trail:
                        pos.close_at_open(m, "trail")
                elif pos.side < 0:
                    trail = upper if self.exit == "base" else min(lower, vwap[m - 1])
                    if px > upper:
                        pos.close_at_open(m, "flip")
                        pos.open(1, m)
                    elif px > trail:
                        pos.close_at_open(m, "trail")
                elif px > upper:
                    pos.open(1, m)
                elif px < lower:
                    pos.open(-1, m)
            if pos.side:
                pos.close_at_close(len(bars) - 1)
        return out


class LastHalfHour(Strategy):
    """Market intraday momentum: the day's return so far predicts the last
    half hour. At 15:30, trade the sign of the return from the previous
    close; exit at the 16:00 close.

    `signal='rod'` uses the return to 15:30 (the "rest of day" return of
    Baltussen, Da, Lammers & Martens (2021), "Hedging demand and market
    intraday momentum", JFE 142). `signal='first'` uses the return to 10:00
    (Gao, Han, Li & Zhou (2018), "Market intraday momentum", JFE 129).
    """

    def __init__(self, signal: str = "rod"):
        if signal not in ("rod", "first"):
            raise ValueError("signal must be 'rod' or 'first'")
        self.signal = signal
        self.name = "lasthalf" if signal == "rod" else "lasthalf-gao"
        self.paper = ("Baltussen, Da, Lammers & Martens (2021)" if signal == "rod"
                      else "Gao, Han, Li & Zhou (2018)")

    def run(self, days: list[Day], costs: Costs) -> list[Trade]:
        out: list[Trade] = []
        for day in days:
            if not day.usable or not day.full or day.prev_close is None:
                continue
            ref = day.bars[359 if self.signal == "rod" else 29].c
            r = ref - day.prev_close
            if r == 0:
                continue
            pos = _Position(self.name, day, costs, out)
            pos.open(1 if r > 0 else -1, 360)
            pos.close_at_close(389)
        return out


class OpeningRangeBreakout(Strategy):
    """Zarattini & Aziz (2023), "Can Day Trading Really Be Profitable?
    Evidence of Sustainable Long-term Profits from Opening Range Breakout (ORB)
    Day Trading Strategy vs. Benchmark in the US Stock Market", SSRN 4416622.

    If the first 5-minute candle closes up, buy at the open of the second; if
    down, sell; if flat, no trade. The stop is the other end of the first
    candle, the target 10 times that risk, and anything still open exits at
    the close. The paper sizes each trade to risk a fixed fraction of the
    account, so `Trade.stop_distance` is recorded for risk-based sizing.
    """
    paper = "Zarattini & Aziz (2023)"

    def __init__(self, minutes: int = 5, target_r: float = 10.0):
        self.minutes, self.target_r = minutes, target_r
        self.name = "orb" if minutes == 5 else f"orb{minutes}"

    def run(self, days: list[Day], costs: Costs) -> list[Trade]:
        out: list[Trade] = []
        n, tick = self.minutes, costs.tick
        for day in days:
            bars = day.bars
            if not day.usable or len(bars) <= n:
                continue
            first = bars[:n]
            o, c = first[0].o, first[-1].c
            if c == o:
                continue
            side = 1 if c > o else -1
            stop = min(b.l for b in first) if side > 0 else max(b.h for b in first)
            pos = _Position(self.name, day, costs, out)
            entry = bars[n].o + side * costs.slip
            risk = side * (entry - stop)
            if risk <= 0:
                continue
            pos.open(side, n, stop_distance=risk)
            target = pos.entry + side * self.target_r * risk
            for k in range(n, len(bars)):
                b = bars[k]
                if side > 0:
                    if b.l <= stop:
                        pos.close_intrabar(k, min(stop, b.o) - costs.slip, "stop")
                    elif b.h >= target + tick:
                        pos.close_intrabar(k, max(target, b.o), "target")
                else:
                    if b.h >= stop:
                        pos.close_intrabar(k, max(stop, b.o) + costs.slip, "stop")
                    elif b.l <= target - tick:
                        pos.close_intrabar(k, min(target, b.o), "target")
                if not pos.side:
                    break
            if pos.side:
                pos.close_at_close(len(bars) - 1)
        return out


STRATEGIES = {
    "noise": lambda: NoiseArea(exit="final"),
    "noise-base": lambda: NoiseArea(exit="base"),
    "lasthalf": lambda: LastHalfHour("rod"),
    "lasthalf-gao": lambda: LastHalfHour("first"),
    "orb": lambda: OpeningRangeBreakout(5),
}


def make(name: str) -> Strategy:
    try:
        return STRATEGIES[name]()
    except KeyError:
        raise ValueError(f"unknown strategy {name!r}; choose from {', '.join(STRATEGIES)}") from None


def run_all(names, days: list[Day], costs: Costs) -> dict[str, list[Trade]]:
    return {name: make(name).run(days, costs) for name in names}


def warmup_end(days: list[Day], lookback: int = 14) -> int:
    """Index of the first day every strategy can trade on."""
    return min(len(days), lookback + 1)
