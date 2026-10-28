"""How far a market is from a random walk: the Lo-MacKinlay variance ratio.

Under a random walk the variance of q-day returns is q times the variance of
1-day returns, so VR(q) = 1. Below 1, moves tend to be undone (mean reversion);
above 1, they tend to continue (trending). z* is Lo and MacKinlay's (1988)
heteroskedasticity-robust statistic, asymptotically N(0, 1) under the null.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

MEAN_REVERTING, TRENDING, RANDOM_WALK = "mean-reverting", "trending", "random walk"


@dataclass(frozen=True)
class Efficiency:
    vr: float           # VR(q)
    z: float            # robust z*(q)
    q: int
    n: int              # 1-day returns used
    rho1: float         # lag-1 autocorrelation of daily returns
    regime: str

    @property
    def score(self) -> float:
        """Distance from a random walk, in standard errors."""
        return abs(self.z)


def autocorrelation(returns: Sequence[float], lag: int = 1) -> float:
    n = len(returns)
    mean = sum(returns) / n
    dev = [r - mean for r in returns]
    denom = sum(d * d for d in dev)
    return sum(dev[t] * dev[t - lag] for t in range(lag, n)) / denom if denom else 0.0


def variance_ratio(log_prices: Sequence[float], q: int) -> tuple[float, float]:
    """(VR(q), z*(q)) from log prices p_0 .. p_T, with overlapping q-day returns."""
    T = len(log_prices) - 1
    if q < 2 or T < 2 * q:
        raise ValueError(f"need at least {2 * q} returns for VR({q}), have {T}")
    mu = (log_prices[-1] - log_prices[0]) / T
    dev = [log_prices[t] - log_prices[t - 1] - mu for t in range(1, T + 1)]  # dev[t-1] is r_t - mu
    sq = [d * d for d in dev]
    sum_sq = sum(sq)
    if sum_sq == 0:
        return 1.0, 0.0
    var_a = sum_sq / (T - 1)
    m = q * (T - q + 1) * (1 - q / T)
    var_c = sum((log_prices[t] - log_prices[t - q] - q * mu) ** 2 for t in range(q, T + 1)) / m
    vr = var_c / var_a
    theta = 0.0
    for j in range(1, q):
        delta = T * sum(sq[t] * sq[t - j] for t in range(j, T)) / sum_sq ** 2
        theta += (2 * (q - j) / q) ** 2 * delta
    z = math.sqrt(T) * (vr - 1) / math.sqrt(theta) if theta > 0 else 0.0
    return vr, z


def measure(closes: Sequence[float], q: int = 5, window: int = 500,
            z_threshold: float = 1.96) -> Efficiency:
    closes = closes[-(window + 1):]
    logs = [math.log(c) for c in closes]
    vr, z = variance_ratio(logs, q)
    returns = [b - a for a, b in zip(logs, logs[1:])]
    regime = MEAN_REVERTING if z <= -z_threshold else TRENDING if z >= z_threshold else RANDOM_WALK
    return Efficiency(vr=vr, z=z, q=q, n=len(returns), rho1=autocorrelation(returns), regime=regime)
