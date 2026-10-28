"""Mean-reversion and continuation triggers, and how each has done before."""
from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from propfirm.stats import binomial_tail, wilson_interval

from .data import Bar
from .indicators import donchian, zscore

LONG, SHORT = 1, -1
MEAN_REVERSION, CONTINUATION = "mean_reversion", "continuation"
LABELS = {MEAN_REVERSION: "Mean reversion", CONTINUATION: "Continuation"}


@dataclass(frozen=True)
class Trigger:
    index: int
    day: date
    strategy: str
    direction: int
    price: float
    level: float        # mean reversion: the z-score; continuation: the channel edge broken


def mean_reversion(closes: Sequence[float], lookback: int, entry_z: float,
                   days: Sequence[date]) -> tuple[list[float | None], list[Trigger]]:
    """Fade stretches: long the first close at z <= -entry_z, short the first at z >= +entry_z."""
    z = zscore(closes, lookback)
    triggers = []
    for i in range(1, len(closes)):
        now, before = z[i], z[i - 1]
        if now is None or before is None:
            continue
        if now <= -entry_z < before:
            triggers.append(Trigger(i, days[i], MEAN_REVERSION, LONG, closes[i], now))
        elif now >= entry_z > before:
            triggers.append(Trigger(i, days[i], MEAN_REVERSION, SHORT, closes[i], now))
    return z, triggers


def continuation(bars: Sequence[Bar], channel: int
                 ) -> tuple[list[float | None], list[float | None], list[Trigger]]:
    """Donchian breakout: long the first close above the prior `channel`-bar high, short below the low."""
    upper, lower = donchian(bars, channel)
    triggers = []
    for i in range(1, len(bars)):
        if upper[i] is None or upper[i - 1] is None:
            continue
        close, prev = bars[i].close, bars[i - 1].close
        if close > upper[i] and not prev > upper[i - 1]:
            triggers.append(Trigger(i, bars[i].day, CONTINUATION, LONG, close, upper[i]))
        elif close < lower[i] and not prev < lower[i - 1]:
            triggers.append(Trigger(i, bars[i].day, CONTINUATION, SHORT, close, lower[i]))
    return upper, lower, triggers


@dataclass(frozen=True)
class Record:
    """Past triggers judged `horizon` bars later. Costs, slippage and roll gaps are ignored."""
    horizon: int
    n: int = 0
    hits: int = 0
    mean_atr: float | None = None   # mean signed move in ATRs at the trigger

    @property
    def hit_rate(self) -> float | None:
        return self.hits / self.n if self.n else None

    @property
    def interval(self) -> tuple[float, float] | None:
        return wilson_interval(self.hits, self.n) if self.n else None

    @property
    def p_coin(self) -> float | None:
        """Chance of at least this many hits if each trigger were a coin flip."""
        return binomial_tail(self.hits, self.n, 0.5) if self.n else None

    def as_dict(self) -> dict:
        lo, hi = self.interval or (None, None)
        return {"horizon": self.horizon, "n": self.n, "hits": self.hits, "hit_rate": self.hit_rate,
                "lo": lo, "hi": hi, "mean_atr": self.mean_atr, "p_coin": self.p_coin}


def record(triggers: Sequence[Trigger], bars: Sequence[Bar], atrs: Sequence[float | None],
           horizon: int) -> Record:
    moves = []
    for t in triggers:
        later = t.index + horizon
        if later >= len(bars) or not atrs[t.index]:
            continue
        moves.append(t.direction * (bars[later].close - t.price) / atrs[t.index])
    if not moves:
        return Record(horizon)
    return Record(horizon, len(moves), sum(m > 0 for m in moves), statistics.fmean(moves))
