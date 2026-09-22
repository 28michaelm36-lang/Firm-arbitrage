"""Trading policies: how much to risk, and for what target, given the account state.

Every policy only chooses brackets (risk, reward). The market is a fair coin
unless you pass a tilt, so what the policies control is how many trades it takes
to reach a goal, what those trades cost, and how much the trailing floor
ratchets along the way.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .instruments import Instrument
from .rules import EvalRules, FundedRules, Program


@dataclass(frozen=True)
class Bracket:
    risk: float
    reward: float


@dataclass(frozen=True)
class View:
    """What a policy may look at before each trade."""
    phase: str                        # "eval" or "funded"
    program: Program
    rules: EvalRules | FundedRules
    instrument: Instrument
    balance: float
    floor: float
    hwm: float
    day_pnl: float
    day_trades: int
    trading_days: int                 # completed trading days in this phase
    loss_limit_room: float
    window_start: float               # balance when the payout window opened (eval: start)
    window_days: int
    window_profitable_days: int
    window_best_day: float
    payouts: int = 0

    @property
    def start(self) -> float:
        return self.rules.start_balance

    @property
    def room(self) -> float:
        return self.balance - self.floor

    @property
    def min_risk(self) -> float:
        """Dollar risk of one contract at the instrument's typical stop."""
        return self.instrument.stop_ticks * self.instrument.tick_value

    def cost(self, risk: float) -> float:
        return self.instrument.cost(risk, self.rules.max_contracts)


class Policy:
    name = "policy"

    def next_trade(self, v: View) -> Bracket | None:
        raise NotImplementedError

    def withdraw(self, v: View, available: float) -> float:
        """Gross amount to withdraw when a payout is available. Default: all of it."""
        return available


def coast(v: View, size: float) -> Bracket:
    """Smallest sensible trade: registers a trading day at minimum risk."""
    return Bracket(risk=size, reward=size)


def bold_bracket(v: View, need: float, max_risk_frac: float, min_odds: float) -> Bracket:
    """One trade whose target lands exactly `need` above the current balance, net of costs.

    Risk is the distance to the floor (times max_risk_frac), capped at
    need / min_odds so the policy never risks much more than it is trying to
    make. With min_odds = 1 this is Dubins-Savage bold play.
    """
    risk = v.room * max_risk_frac
    if min_odds > 0:
        risk = min(risk, need / min_odds)
    risk = min(max(risk, v.min_risk), v.room)
    risk = min(risk, v.loss_limit_room - v.cost(risk))  # stay inside the daily loss limit
    return Bracket(risk=risk, reward=need + v.cost(risk))


@dataclass
class Asymmetry1(Policy):
    """asymmetry1: an opening-range-breakout bracket, one per session, risking
    `risk` to make `reward` ($2,000 for $400). The same bracket is traded once
    funded; the evaluation stops trading at the target.

    land_exactly=1 shrinks the bracket, keeping its shape, when the evaluation
    target is closer than one full win, so the last winner does not overshoot.
    """
    risk: float = 2000.0
    reward: float = 400.0
    trades_per_day: int = 1
    land_exactly: int = 0
    coast_size: float = 25.0
    name: str = "asymmetry1"

    def next_trade(self, v: View) -> Bracket | None:
        if v.day_trades >= self.trades_per_day:
            return None
        ev = v.program.evaluation
        if v.phase == "eval" and v.balance >= ev.target_balance:
            # Target hit but not passed yet: only trade to satisfy minimum days.
            return coast(v, self.coast_size) if v.trading_days < ev.min_days else None
        if self.land_exactly and v.phase == "eval":
            need = ev.target_balance + 1.0 - v.balance
            ratio = self.risk / self.reward
            reward = need + v.cost(need * ratio)
            if reward < self.reward:
                return Bracket(risk=reward * ratio, reward=reward)
        return Bracket(risk=self.risk, reward=self.reward)


@dataclass
class Bold(Policy):
    """asymmetry1+ evaluation policy: goal-directed bold play.

    * Each trade targets exactly the remaining distance to the pass level, plus
      its own round-trip cost, and risks the distance to the floor. Under zero
      edge, Dubins & Savage (1965) show bold play maximises the chance of
      reaching a goal when every bet costs something.
    * A consistency rule caps each day at c x the planned profit, so the target
      is spread over the fewest days the rule allows.
    * After the target, it only places minimum-risk trades to reach min days.
    """
    max_risk_frac: float = 1.0
    min_odds: float = 1.0
    trades_per_day: int = 20
    coast_size: float = 25.0
    margin: float = 0.02
    name: str = "bold"

    def goal(self, v: View) -> float:
        ev = v.rules
        goal = ev.target_balance
        if ev.consistency:
            goal = max(goal, v.start + v.window_best_day / ev.consistency * (1 + self.margin))
        return goal + 1.0

    def next_trade(self, v: View) -> Bracket | None:
        if v.day_trades >= self.trades_per_day:
            return None
        ev = v.rules
        goal = self.goal(v)
        if v.balance >= goal:
            if v.day_trades == 0 and v.trading_days < ev.min_days:
                return coast(v, self.coast_size)
            return None
        need = goal - v.balance
        if ev.consistency:
            need = min(need, ev.consistency * (goal - v.start) * (1 - self.margin) - v.day_pnl)
        if need <= 1.0:
            return None
        return bold_bracket(v, need, self.max_risk_frac, self.min_odds)


@dataclass
class Harvest(Policy):
    """asymmetry2 funded policy: harvest the funded drawdown.

    For a trader without an edge, the most a funded account can pay out in
    expectation is its drawdown (see docs/). Every dollar the rules strand or the
    costs eat comes out of that budget. So:

    * sprint (bold) to exactly the balance that unlocks the next maximum payout,
      spreading the sprint over days if there is a consistency rule;
    * once there, trade minimum risk only to satisfy day counts and
      profitable-day requirements;
    * withdraw the maximum the moment it is allowed. Money left in the account
      is still at risk and goes back to the firm when the account is blown.
    """
    max_risk_frac: float = 1.0
    min_odds: float = 1.0
    chunk: float = 0.0            # payout to aim for when there is no cap (0 = funded drawdown)
    coast_size: float = 25.0
    coast_odds: float = 1.0       # reward:risk on profitable-day trades
    trades_per_day: int = 20
    margin: float = 0.02
    name: str = "harvest"

    def payout_goal(self, v: View) -> float:
        fr = v.rules
        cap = fr.payout_cap(v.payouts)
        want = cap if cap < math.inf else (self.chunk or fr.drawdown.amount)
        want = max(want, fr.min_payout)
        goal = max(v.start + fr.payout_min_balance, v.start + fr.payout_keep_balance + want)
        if fr.payout_cap_fraction:
            goal = max(goal, v.start + want / fr.payout_cap_fraction)
        if fr.consistency:
            goal = max(goal, v.window_start + v.window_best_day / fr.consistency * (1 + self.margin))
        return goal + 1.0

    def next_trade(self, v: View) -> Bracket | None:
        if v.day_trades >= self.trades_per_day:
            return None
        fr = v.rules
        goal = self.payout_goal(v)
        if v.balance < goal:
            need = goal - v.balance
            if fr.consistency:
                cap = fr.consistency * (goal - v.window_start) * (1 - self.margin)
                need = min(need, cap - v.day_pnl)
            if need <= 1.0:
                return None
            return bold_bracket(v, need, self.max_risk_frac, self.min_odds)
        if v.day_trades > 0:
            return None
        if v.window_profitable_days < fr.min_profitable_days:
            target = max(fr.profitable_day_amount, 1.0) + 1.0
            risk = max(target / self.coast_odds, 1.0)
            return Bracket(risk=risk, reward=target + v.cost(risk))
        if v.window_days < fr.min_days_between_payouts:
            return coast(v, self.coast_size)
        return None


POLICIES = {
    "asymmetry1": Asymmetry1,
    "bold": Bold,
    "harvest": Harvest,
}


def parse_policy(spec: str) -> Policy:
    """Build a policy from "name" or "name:key=value,key=value"."""
    name, _, params = spec.partition(":")
    if name not in POLICIES:
        raise ValueError(f"unknown policy {name!r}; choose from {sorted(POLICIES)}")
    kwargs = {}
    for item in filter(None, params.split(",")):
        key, _, raw = item.partition("=")
        try:
            kwargs[key.strip()] = int(raw)
        except ValueError:
            kwargs[key.strip()] = float(raw)
    return POLICIES[name](**kwargs)
