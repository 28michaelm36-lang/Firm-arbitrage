"""Exact dynamic program: the best way to reach a goal above a static floor.

States are the distance above the floor, on a grid from 0 (blown) to G = room +
need (goal). An action is a bracket: risk r, and a target that lands on a grid
state above the current one. Round-trip costs are paid at entry and
P(win) = (r / (r + w)) ** (1 - tilt), the same model the simulator uses.

This checks whether bold play is optimal for a given cost and edge, and how much
it gives up if not. It is exact for static floors, and for end-of-day trailing
floors within one session. Intraday trailing floors punish small bets even more
(every losing trade's open profit ratchets the floor), which strengthens the
case for bold play.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class GoalSolution:
    step: float
    values: list[float]                       # optimal P(goal) at distance i * step
    bold: list[float]                         # one all-in bracket straight to the goal
    actions: list[tuple[float, float] | None] # (risk, target P&L) of the best first trade

    def value_at(self, distance: float) -> float:
        return _interp(self.values, distance / self.step)

    def bold_at(self, distance: float) -> float:
        return _interp(self.bold, distance / self.step)

    def action_at(self, distance: float) -> tuple[float, float] | None:
        return self.actions[min(len(self.actions) - 1, max(0, round(distance / self.step)))]


def _interp(values: list[float], x: float) -> float:
    n = len(values) - 1
    if x <= 0:
        return 0.0
    if x >= n:
        return 1.0
    i = int(x)
    return values[i] + (x - i) * (values[i + 1] - values[i])


def solve_goal(room: float, need: float, cost: Callable[[float], float] = lambda r: 0.0,
               tilt: float = 0.0, grid: int = 50, tol: float = 1e-10,
               max_iter: int = 2000) -> GoalSolution:
    total = room + need
    h = total / grid
    n = grid
    power = 1.0 - tilt

    bold = [0.0] * (n + 1)
    bold[n] = 1.0
    actions: list[tuple[float, float] | None] = [None] * (n + 1)
    for i in range(1, n):
        x = i * h
        entry = x - cost(x)
        if entry > 0:
            bold[i] = (entry / total) ** power
            actions[i] = (entry, total - entry)

    values = bold[:]  # achievable, so value iteration climbs from here to the optimum
    costs = [cost(j * h) for j in range(n + 1)]
    for _ in range(max_iter):
        delta = 0.0
        for i in range(n - 1, 0, -1):
            x = i * h
            best, best_action = values[i], actions[i]
            for j in range(1, i + 1):
                entry = x - costs[j]
                if entry <= 0:
                    break
                r = min(j * h, entry)
                lose = _interp(values, (entry - r) / h)
                for k in range(i + 1, n + 1):
                    w = k * h - entry
                    p = (r / (r + w)) ** power
                    v = lose + p * (values[k] - lose)
                    if v > best + 1e-15:
                        best, best_action = v, (r, w)
            delta = max(delta, best - values[i])
            values[i], actions[i] = best, best_action
        if delta < tol:
            break
    return GoalSolution(h, values, bold, actions)
