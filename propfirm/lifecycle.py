"""Monte Carlo of the full account lifecycle: evaluation, activation, funded payouts."""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass

from .account import Account
from .instruments import Instrument
from .policies import Policy, View
from .rules import FundedRules, Program

MAX_TRADES_PER_DAY = 50


@dataclass(frozen=True)
class Attempt:
    """One evaluation purchase and everything that followed from it."""
    passed: bool
    eval_days: int
    eval_periods: int        # billing periods paid for the evaluation
    funded_days: int = 0
    payouts: int = 0
    gross: float = 0.0       # withdrawn from the funded account
    net: float = 0.0         # trader's share of `gross`
    eval_fees: float = 0.0
    funded_fees: float = 0.0
    graduated: bool = False
    alive: bool = False      # funded account still open at the horizon
    stranded: float = 0.0    # balance above the floor left in an open account

    @property
    def fees(self) -> float:
        return self.eval_fees + self.funded_fees

    @property
    def profit(self) -> float:
        return self.net - self.fees


def payout_available(fr: FundedRules, balance: float, payouts: int, window_start: float,
                     window_days: int, profitable_days: int, best_day: float) -> float:
    """Largest gross payout the rules allow right now (0 if not eligible)."""
    start = fr.start_balance
    if fr.max_payouts is not None and payouts >= fr.max_payouts:
        return 0.0
    if window_days < fr.min_days_between_payouts or profitable_days < fr.min_profitable_days:
        return 0.0
    if balance < start + fr.payout_min_balance:
        return 0.0
    window_profit = balance - window_start
    if fr.consistency is not None and (window_profit <= 0 or best_day > fr.consistency * window_profit):
        return 0.0
    amount = min(balance - (start + fr.payout_keep_balance), fr.payout_cap(payouts))
    if fr.payout_cap_fraction is not None:
        amount = min(amount, fr.payout_cap_fraction * (balance - start))
    return amount if amount >= max(fr.min_payout, 0.01) else 0.0


class _Window:
    """Trading days since the payout window opened (the whole phase for an evaluation)."""

    def __init__(self, balance: float, profitable_amount: float = 0.0):
        self.start = balance
        self.days = 0
        self.profitable = 0
        self.best = 0.0
        self.profitable_amount = max(profitable_amount, 0.01)

    def add(self, pnl: float | None) -> None:
        if pnl is None:
            return
        self.days += 1
        self.profitable += pnl >= self.profitable_amount
        self.best = max(self.best, pnl)


def _view(phase, program, rules, instrument, acct: Account, win: _Window, payouts=0) -> View:
    return View(
        phase=phase, program=program, rules=rules, instrument=instrument,
        balance=acct.balance, floor=acct.floor, hwm=acct.hwm, day_pnl=acct.day_pnl,
        day_trades=acct.day_trades, trading_days=acct.trading_days,
        loss_limit_room=acct.loss_limit_room, window_start=win.start, window_days=win.days,
        window_profitable_days=win.profitable, window_best_day=win.best, payouts=payouts,
    )


def _trade_day(acct: Account, policy: Policy, view, instrument: Instrument,
               rng: random.Random, tilt: float) -> None:
    while not acct.blown and not acct.day_over and acct.day_trades < MAX_TRADES_PER_DAY:
        bracket = policy.next_trade(view())
        if bracket is None:
            return
        acct.trade(rng, bracket.risk, bracket.reward, instrument, tilt)


def run_attempt(program: Program, eval_policy: Policy, funded_policy: Policy,
                instrument: Instrument, rng: random.Random, tilt: float = 0.0,
                horizon_days: int = 250, max_eval_days: int = 60) -> Attempt:
    ev = program.evaluation
    acct = Account.open(ev)
    win = _Window(acct.balance)
    days = 0
    passed = False
    while days < max_eval_days and (ev.max_days is None or days < ev.max_days):
        view = lambda: _view("eval", program, ev, instrument, acct, win)
        _trade_day(acct, eval_policy, view, instrument, rng, tilt)
        win.add(acct.end_day())
        days += 1
        if acct.blown:
            break
        if (acct.balance >= ev.target_balance and acct.trading_days >= ev.min_days
                and (ev.consistency is None
                     or win.best <= ev.consistency * (acct.balance - ev.start_balance))):
            passed = True
            break
    periods = max(1, math.ceil(days / ev.fee_period_days)) if ev.fee_period_days else 1
    eval_fees = ev.fee * periods
    if not passed:
        return Attempt(False, days, periods, eval_fees=eval_fees)

    fr = program.funded
    acct = Account.open(fr)
    win = _Window(acct.balance, fr.profitable_day_amount)
    payouts = fdays = 0
    gross = net = 0.0
    graduated = False
    while fdays < horizon_days:
        view = lambda: _view("funded", program, fr, instrument, acct, win, payouts)
        _trade_day(acct, funded_policy, view, instrument, rng, tilt)
        win.add(acct.end_day())
        fdays += 1
        if acct.blown:
            break
        available = payout_available(fr, acct.balance, payouts, win.start, win.days,
                                     win.profitable, win.best)
        if available > 0:
            amount = min(available, funded_policy.withdraw(view(), available))
            if amount >= max(fr.min_payout, 0.01):
                acct.withdraw(amount)
                net += fr.trader_share(amount, gross)
                gross += amount
                payouts += 1
                win = _Window(acct.balance, fr.profitable_day_amount)
                if fr.max_payouts is not None and payouts >= fr.max_payouts:
                    graduated = True
                    break
    funded_fees = fr.activation_fee
    if fr.monthly_fee:
        funded_fees += fr.monthly_fee * math.ceil(fdays / fr.fee_period_days)
    alive = not acct.blown and not graduated
    return Attempt(
        True, days, periods, funded_days=fdays, payouts=payouts,
        gross=gross, net=net, eval_fees=eval_fees, funded_fees=funded_fees,
        graduated=graduated, alive=alive,
        stranded=acct.room if not acct.blown else 0.0,
    )


@dataclass(frozen=True)
class Summary:
    program: Program
    strategy: str
    instrument: Instrument
    tilt: float
    attempts: list[Attempt]

    @property
    def n(self) -> int:
        return len(self.attempts)

    @property
    def funded(self) -> list[Attempt]:
        return [a for a in self.attempts if a.passed]

    @property
    def p_pass(self) -> float:
        return len(self.funded) / self.n

    def _funded_mean(self, attr) -> float:
        f = self.funded
        return statistics.fmean(attr(a) for a in f) if f else 0.0

    @property
    def gross_per_funded(self) -> float:
        return self._funded_mean(lambda a: a.gross)

    @property
    def net_per_funded(self) -> float:
        return self._funded_mean(lambda a: a.net)

    @property
    def efficiency(self) -> float:
        """Gross withdrawn per funded account, as a fraction of the funded drawdown."""
        return self.gross_per_funded / self.program.funded.drawdown.amount

    @property
    def value_of_pass(self) -> float:
        """What a funded account is worth once you have it: net payouts minus funded fees."""
        return self._funded_mean(lambda a: a.net - a.funded_fees)

    @property
    def ev(self) -> float:
        return statistics.fmean(a.profit for a in self.attempts)

    @property
    def ev_stderr(self) -> float:
        if self.n < 2:
            return 0.0
        return statistics.stdev(a.profit for a in self.attempts) / math.sqrt(self.n)

    @property
    def fees_per_attempt(self) -> float:
        return statistics.fmean(a.fees for a in self.attempts)

    @property
    def breakeven_eval_price(self) -> float:
        """Highest evaluation price (per billing period) at which EV is zero."""
        periods = statistics.fmean(a.eval_periods for a in self.attempts)
        return self.p_pass * self.value_of_pass / periods

    def attempts_for(self, confidence: float = 0.9) -> float:
        if self.p_pass <= 0:
            return math.inf
        if self.p_pass >= 1:
            return 1
        return math.ceil(math.log(1 - confidence) / math.log(1 - self.p_pass))

    def format(self) -> str:
        f = self.funded
        pct = lambda x: f"{100 * x:5.1f}%"
        usd = lambda x: f"${x:,.0f}"
        p = self.p_pass
        se_p = math.sqrt(p * (1 - p) / self.n)
        lines = [
            f"{self.program.name}",
            f"  strategy {self.strategy} | {self.instrument.name} ${self.instrument.round_turn:.2f} RT, "
            f"{self.instrument.stop_ticks:g}-tick stop | tilt {self.tilt:+.3f} | n={self.n:,}",
            "  Evaluation",
            f"    pass rate                         {pct(p)} (+/- {100 * se_p:.1f})",
            f"    trading days to pass (median)     {statistics.median(a.eval_days for a in f) if f else 0:g}",
            f"    eval fees per attempt             {usd(statistics.fmean(a.eval_fees for a in self.attempts))}",
            "  Funded (per funded account)",
            f"    at least one payout               {pct(sum(a.payouts > 0 for a in f) / len(f)) if f else '  n/a'}",
            f"    payouts                           {self._funded_mean(lambda a: a.payouts):.2f}",
            f"    gross withdrawn                   {usd(self.gross_per_funded)}",
            f"    trader net                        {usd(self.net_per_funded)}",
            f"    drawdown efficiency (gross / DD)  {pct(self.efficiency)}",
            f"    funded fees                       {usd(self._funded_mean(lambda a: a.funded_fees))}",
            f"    funded trading days (mean)        {self._funded_mean(lambda a: a.funded_days):.1f}",
            f"    graduated / open at horizon       {pct(self._funded_mean(lambda a: a.graduated))} / "
            f"{pct(self._funded_mean(lambda a: a.alive))}",
            "  Economics (per evaluation bought)",
            f"    EV                                {usd(self.ev)} (+/- {self.ev_stderr:,.0f})",
            f"    ROI on fees                       {pct(self.ev / self.fees_per_attempt) if self.fees_per_attempt else '  n/a'}",
            f"    break-even evaluation price       {usd(self.breakeven_eval_price)}",
            f"    attempts for 90% chance of a pass {self.attempts_for(0.9)}",
        ]
        return "\n".join(lines)


def simulate(program: Program, eval_policy: Policy, funded_policy: Policy,
             instrument: Instrument, n: int = 10_000, seed: int = 1, tilt: float = 0.0,
             horizon_days: int = 250, strategy: str | None = None) -> Summary:
    rng = random.Random(seed)
    attempts = [run_attempt(program, eval_policy, funded_policy, instrument, rng, tilt, horizon_days)
                for _ in range(n)]
    name = strategy or f"{eval_policy.name} -> {funded_policy.name}"
    return Summary(program, name, instrument, tilt, attempts)
