"""Account state: balance, liquidation floor, daily loss limit and trading days."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .bracket import Floor, Outcome, simulate_bracket
from .instruments import Instrument
from .rules import Drawdown, EvalRules, FundedRules


@dataclass
class Account:
    start: float
    drawdown: Drawdown
    daily_loss_limit: float | None
    daily_loss_mode: str
    max_minis: float
    balance: float = 0.0
    floor: float = 0.0
    hwm: float = 0.0
    blown: bool = False
    day_start: float = 0.0
    day_trades: int = 0
    day_over: bool = False
    trading_days: int = 0
    day_pnls: list[float] = field(default_factory=list)

    @classmethod
    def open(cls, rules: EvalRules | FundedRules) -> "Account":
        dd = rules.drawdown
        start = rules.start_balance
        return cls(
            start=start, drawdown=dd, daily_loss_limit=rules.daily_loss_limit,
            daily_loss_mode=rules.daily_loss_mode, max_minis=rules.max_contracts,
            balance=start, hwm=start, day_start=start,
            floor=min(start - dd.amount, dd.lock_level(start)),
        )

    @property
    def lock(self) -> float:
        return self.drawdown.lock_level(self.start)

    @property
    def room(self) -> float:
        return self.balance - self.floor

    @property
    def day_pnl(self) -> float:
        return self.balance - self.day_start

    @property
    def loss_limit_room(self) -> float:
        if self.daily_loss_limit is None:
            return math.inf
        return self.balance - (self.day_start - self.daily_loss_limit)

    def trade(self, rng: random.Random, risk: float, reward: float,
              instrument: Instrument, tilt: float = 0.0) -> Outcome | None:
        cost = instrument.cost(risk, self.max_minis)
        # The daily loss limit acts as a hard stop.
        stop = min(risk, self.loss_limit_room - cost)
        if stop <= 0:
            self.day_over = True
            return None
        trailing = self.drawdown.mode == "intraday_trailing"
        fl = Floor(self.floor, self.hwm, self.drawdown.amount if trailing else None, self.lock)
        out = simulate_bracket(rng, self.balance, fl, stop, reward, cost, tilt)
        self.balance += out.pnl
        self.day_trades += 1
        self.hwm, self.floor = out.hwm, out.floor
        if out.breached:
            self.blown = True
        elif self.loss_limit_room <= 1e-6:
            if self.daily_loss_mode == "fail":
                self.blown = True
            else:
                self.day_over = True
        return out

    def end_day(self) -> float | None:
        """Close the day. Returns the day's P&L if it was a trading day."""
        pnl = None
        if self.day_trades:
            pnl = self.balance - self.day_start
            self.trading_days += 1
            self.day_pnls.append(pnl)
        if self.drawdown.mode == "eod_trailing" and not self.blown:
            self.hwm = max(self.hwm, self.balance)
            self.floor = max(self.floor, min(self.hwm - self.drawdown.amount, self.lock))
        self.day_start = self.balance
        self.day_trades = 0
        self.day_over = False
        return pnl

    def withdraw(self, amount: float) -> None:
        self.balance -= amount
        self.day_start = self.balance
