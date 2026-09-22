"""One bracket trade against a stop, a target and the account's liquidation floor.

Model: price is driftless Brownian motion from entry until it reaches the target
or the level that kills the trade. Follow the trade's running high m (its best
open P&L so far). While the high is m, the trade dies at

    kill(m) = max(-stop, floor(m) - entry)

where floor(m) is the liquidation level given that high. It only rises with m for
an intraday trailing drawdown. For Brownian motion the chance of dying before the
high advances by dm is dm / (m - kill(m)), so

    P(high reaches m) = exp(-(1 - tilt) * integral_0^m du / (u - kill(u)))

`tilt` scales that hazard: 0 is a fair game, positive is an edge, negative is a
leak. Two useful closed forms fall out:

  * static floor:  P(win) = (stop / (stop + target)) ** (1 - tilt)
  * unlocked intraday trailing floor, stop at the floor, starting at the high:
                   P(win) = exp(-(1 - tilt) * target / drawdown)

The second is why intraday trailing drawdowns are so expensive: with a $2,500
trailing drawdown and a $3,000 target, one all-in trade passes 30% of the time
instead of the 45% a static or end-of-day floor gives.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Floor:
    level: float                 # current liquidation balance
    hwm: float                   # high-water mark the floor trails
    trail: float | None = None   # trailing distance if the floor trails intraday
    lock: float = math.inf       # the floor never trails above this

    def after_high(self, high: float) -> tuple[float, float]:
        """(hwm, floor) once equity has touched `high`."""
        if self.trail is None:
            return self.hwm, self.level
        hwm = max(self.hwm, high)
        return hwm, max(self.level, min(hwm - self.trail, self.lock))


@dataclass(frozen=True)
class Outcome:
    pnl: float        # realised P&L including costs
    won: bool
    breached: bool    # equity touched the floor: account is dead
    hwm: float
    floor: float


def tilt_from_winrate(winrate: float) -> float:
    """Tilt giving `winrate` on a 1:1 bracket with a static floor (0.5 -> 0, no edge)."""
    if not 0.0 < winrate < 1.0:
        raise ValueError("winrate must be between 0 and 1")
    return 1.0 - math.log(winrate) / math.log(0.5)


def _segments(entry: float, fl: Floor, stop: float):
    """kill(m) as [(m0, m1, kind, v)]: kind "c" is kill = v, kind "t" is kill = m - v."""
    k0 = max(-stop, fl.level - entry)
    if fl.trail is None or fl.level >= fl.lock:
        return [(0.0, math.inf, "c", k0)]
    a = max(0.0, fl.level + fl.trail - entry)   # high at which the floor starts to rise
    b = max(a, fl.lock + fl.trail - entry)      # high at which the floor locks
    c = min(max(a, fl.trail - stop), b)         # below this the stop is still above the floor
    segs = []
    if a > 0:
        segs.append((0.0, a, "c", k0))
    if c > a:
        segs.append((a, c, "c", -stop))
    if b > c:
        segs.append((c, b, "t", fl.trail))
    segs.append((b, math.inf, "c", max(-stop, fl.lock - entry)))
    return segs


def _integral(segs, m: float) -> float:
    total = 0.0
    for m0, m1, kind, v in segs:
        if m <= m0:
            break
        hi = min(m, m1)
        total += math.log((hi - v) / (m0 - v)) if kind == "c" else (hi - m0) / v
    return total


def _invert(segs, j: float) -> float:
    """The high m at which the hazard integral reaches j."""
    for m0, m1, kind, v in segs:
        if kind == "c":
            span = math.inf if m1 == math.inf else math.log((m1 - v) / (m0 - v))
            if j <= span:
                return v + (m0 - v) * math.exp(j)
        else:
            span = (m1 - m0) / v
            if j <= span:
                return m0 + j * v
        j -= span
    return math.inf


def win_probability(equity: float, fl: Floor, stop: float, reward: float,
                    cost: float = 0.0, tilt: float = 0.0) -> float:
    """Probability the bracket reaches its target. Costs are paid at entry."""
    entry = equity - cost
    if entry <= fl.level or stop <= 0:
        return 0.0
    return math.exp(-(1.0 - tilt) * _integral(_segments(entry, fl, stop), reward))


def simulate_bracket(rng: random.Random, equity: float, fl: Floor, stop: float,
                     reward: float, cost: float = 0.0, tilt: float = 0.0) -> Outcome:
    """Sample one bracket trade. Costs are paid at entry."""
    entry = equity - cost
    if entry <= fl.level:
        return Outcome(fl.level - equity, False, True, fl.hwm, fl.level)
    segs = _segments(entry, fl, stop)
    high = _invert(segs, -math.log(1.0 - rng.random()) / (1.0 - tilt))
    if high >= reward:
        hwm, floor = fl.after_high(entry + reward)
        return Outcome(reward - cost, True, False, hwm, floor)
    hwm, floor = fl.after_high(entry + high)
    if floor >= entry - stop:
        return Outcome(floor - equity, False, True, hwm, floor)
    return Outcome(-stop - cost, False, False, hwm, floor)
