"""Small binomial helpers for judging a track record."""
from __future__ import annotations

import math


def wilson_interval(passes: int, attempts: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a pass rate."""
    phat = passes / attempts
    denom = 1 + z * z / attempts
    center = (phat + z * z / (2 * attempts)) / denom
    half = z * math.sqrt(phat * (1 - phat) / attempts + z * z / (4 * attempts ** 2)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def binomial_tail(passes: int, attempts: int, p: float) -> float:
    """P(at least `passes` passes in `attempts`) if the true pass rate were p."""
    return sum(math.comb(attempts, i) * p ** i * (1 - p) ** (attempts - i)
               for i in range(passes, attempts + 1))


def attempts_to_rule_out_luck(rate: float, baseline: float, alpha: float = 0.05,
                              limit: int = 1000) -> int | None:
    """Fewest attempts at which passing `rate` of them (rounded down) would
    happen with probability below alpha if the true rate were `baseline`."""
    if rate <= baseline:
        return None
    for n in range(1, limit + 1):
        passes = math.floor(rate * n + 1e-9)
        if passes and binomial_tail(passes, n, baseline) < alpha:
            return n
    return None
