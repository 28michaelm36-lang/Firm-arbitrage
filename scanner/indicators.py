"""Rolling indicators over plain lists. None marks bars without enough history."""
from __future__ import annotations

import math
from collections.abc import Sequence

from .data import Bar


def sma(values: Sequence[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def rolling_std(values: Sequence[float], n: int) -> list[float | None]:
    """Sample standard deviation of the last n values, including the current one."""
    out: list[float | None] = [None] * len(values)
    for i in range(n - 1, len(values)):
        window = values[i - n + 1:i + 1]
        mean = sum(window) / n
        out[i] = math.sqrt(sum((v - mean) ** 2 for v in window) / (n - 1))
    return out


def zscore(values: Sequence[float], n: int) -> list[float | None]:
    """(value - rolling mean) / rolling std, both over the last n values."""
    means, stds = sma(values, n), rolling_std(values, n)
    return [None if m is None or not s else (v - m) / s
            for v, m, s in zip(values, means, stds)]


def donchian(bars: Sequence[Bar], n: int) -> tuple[list[float | None], list[float | None]]:
    """Highest high and lowest low of the n bars *before* each bar."""
    upper: list[float | None] = [None] * len(bars)
    lower: list[float | None] = [None] * len(bars)
    for i in range(n, len(bars)):
        window = bars[i - n:i]
        upper[i] = max(b.high for b in window)
        lower[i] = min(b.low for b in window)
    return upper, lower


def atr(bars: Sequence[Bar], n: int) -> list[float | None]:
    """Mean true range over the last n bars."""
    ranges = [bars[0].high - bars[0].low] + [
        max(b.high, prev.close) - min(b.low, prev.close) for prev, b in zip(bars, bars[1:])]
    return sma(ranges, n)


def log_returns(closes: Sequence[float]) -> list[float]:
    return [math.log(b / a) for a, b in zip(closes, closes[1:])]
