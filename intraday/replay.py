"""Replay a strategy's real trades through a prop-firm program's rules.

An evaluation is started on every session of the backtest in turn. Each one
trades the strategy's actual fills until it passes, fails, runs out of days
or runs out of data. A pass opens a funded account on the next session, which
keeps trading the same strategy and withdraws the largest payout the rules
allow as soon as it is allowed (`propfirm.lifecycle.payout_available`).

The trailing floor and the daily loss limit are checked against open P&L,
bar by bar. Inside a bar the best price is taken to come before the worst,
which is the order that hurts most under a trailing floor. Commissions are
charged at entry. Evaluations still open when the data ends are left out,
since their outcome is unknown; funded accounts still open are counted with
what they had withdrawn so far, so funded values are a lower bound.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, replace

from propfirm.instruments import Instrument
from propfirm.lifecycle import Attempt, Summary, _Window, payout_available
from propfirm.rules import EvalRules, FundedRules, Program

from .data import Day
from .strategies import Costs, Trade


@dataclass(frozen=True)
class DayPath:
    """One session's equity, in $ from the day's starting balance. Each
    segment is a new high of open equity followed by the lowest open equity
    before the next new high."""
    segments: tuple[tuple[float, float], ...]
    final: float
    traded: bool


NO_TRADE = DayPath((), 0.0, False)


def day_paths(trades: list[Trade], days: list[Day], costs: Costs, contracts=1.0) -> list[DayPath]:
    """Equity paths for every session in `days`. `contracts` is a number or a
    function of the trade; trades sized to zero are skipped."""
    size = contracts if callable(contracts) else (lambda t: contracts)
    by_day: dict = defaultdict(list)
    for t in trades:
        q = size(t)
        if q > 0:
            by_day[t.day].append((t, q))
    out = []
    pv, comm = costs.point_value, costs.commission
    for d in days:
        sized = by_day.get(d.date)
        if not sized:
            out.append(NO_TRADE)
            continue
        lo = min(t.entry_index for t, _ in sized)
        hi = max(t.exit_index for t, _ in sized)
        n = hi - lo + 1
        best = [0.0] * n
        worst = [0.0] * n
        realized = [0.0] * (n + 1)   # realized[m - lo]: closed P&L before minute m
        final = 0.0
        for t, q in sized:
            fee = q * comm
            for u, (b, w) in enumerate(t.path):
                m = t.entry_index + u - lo
                best[m] += q * b * pv - fee
                worst[m] += q * w * pv - fee
            pnl = q * t.points * pv - fee
            realized[t.exit_index - lo + 1] += pnl
            final += pnl
        segs: list[list[float]] = []
        closed = 0.0
        for m in range(n):
            closed += realized[m]
            b, w = closed + best[m], closed + worst[m]
            if not segs or b > segs[-1][0]:
                segs.append([b, w])
            elif w < segs[-1][1]:
                segs[-1][1] = w
        out.append(DayPath(tuple((b, w) for b, w in segs), final, True))
    return out


class _Account:
    def __init__(self, rules: EvalRules | FundedRules):
        dd = rules.drawdown
        self.start = rules.start_balance
        self.balance = self.hwm = self.start
        self.amount, self.mode = dd.amount, dd.mode
        self.lock = dd.lock_level(self.start)
        self.floor = min(self.start - dd.amount, self.lock)
        self.dll = rules.daily_loss_limit
        self.dll_mode = rules.daily_loss_mode

    def trade_day(self, p: DayPath) -> bool:
        """Apply one session. Returns False if the account is blown."""
        if not p.traded:
            return True
        b0 = self.balance
        dll_level = b0 - self.dll if self.dll is not None else -math.inf
        for top, low in p.segments:
            if self.mode == "intraday_trailing":
                self.hwm = max(self.hwm, b0 + top)
                self.floor = max(self.floor, min(self.hwm - self.amount, self.lock))
            floor_hit = b0 + low <= self.floor + 1e-9
            dll_hit = b0 + low <= dll_level + 1e-9
            # Price falls through the higher of the two levels first.
            if floor_hit and (not dll_hit or self.floor >= dll_level):
                self.balance = self.floor
                return False
            if dll_hit:
                self.balance = dll_level
                if self.dll_mode == "fail":
                    return False
                break
        else:
            self.balance = b0 + p.final
        if self.mode == "eod_trailing":
            self.hwm = max(self.hwm, self.balance)
            self.floor = max(self.floor, min(self.hwm - self.amount, self.lock))
        return True


def run_eval(paths: list[DayPath], i0: int, ev: EvalRules,
             max_eval_days: int = 60) -> tuple[str, int, int]:
    """('pass' | 'blown' | 'timeout' | 'open', sessions used, index of the
    last session)."""
    acct = _Account(ev)
    win = _Window(acct.balance)
    limit = min(max_eval_days, ev.max_days or math.inf)
    for k in range(i0, len(paths)):
        used = k - i0 + 1
        before = acct.balance
        alive = acct.trade_day(paths[k])
        if paths[k].traded:
            win.add(acct.balance - before)
        if not alive:
            return "blown", used, k
        if (acct.balance >= ev.target_balance and win.days >= ev.min_days
                and (ev.consistency is None
                     or win.best <= ev.consistency * (acct.balance - ev.start_balance))):
            return "pass", used, k
        if used >= limit:
            return "timeout", used, k
    return "open", len(paths) - i0, len(paths) - 1


def run_funded(paths: list[DayPath], i0: int, fr: FundedRules, horizon_days: int = 250) -> Attempt:
    acct = _Account(fr)
    win = _Window(acct.balance, fr.profitable_day_amount)
    payouts = fdays = 0
    gross = net = 0.0
    graduated = blown = False
    for k in range(i0, min(len(paths), i0 + horizon_days)):
        before = acct.balance
        fdays += 1
        if not acct.trade_day(paths[k]):
            blown = True
            break
        if paths[k].traded:
            win.add(acct.balance - before)
        amount = payout_available(fr, acct.balance, payouts, win.start, win.days,
                                  win.profitable, win.best)
        if amount > 0:
            acct.balance -= amount
            net += fr.trader_share(amount, gross)
            gross += amount
            payouts += 1
            win = _Window(acct.balance, fr.profitable_day_amount)
            if fr.max_payouts is not None and payouts >= fr.max_payouts:
                graduated = True
                break
    fees = fr.activation_fee
    if fr.monthly_fee:
        fees += fr.monthly_fee * math.ceil(fdays / fr.fee_period_days)
    alive = not blown and not graduated
    return Attempt(True, 0, 0, funded_days=fdays, payouts=payouts, gross=gross, net=net,
                   funded_fees=fees, graduated=graduated, alive=alive,
                   stranded=acct.balance - acct.floor if alive else 0.0)


@dataclass
class Replay:
    summary: Summary
    starts: int          # evaluations started
    censored: int        # still open when the data ended, left out
    blown: int
    timeouts: int
    pass_days: list[int]

    @property
    def median_days_to_pass(self) -> float:
        return statistics.median(self.pass_days) if self.pass_days else math.nan


def replay(program: Program, paths: list[DayPath], name: str, first: int = 0,
           max_eval_days: int = 60, horizon_days: int = 250, step: int = 1) -> Replay:
    """Start an evaluation on every `step`-th session from `first`."""
    ev, fr = program.evaluation, program.funded
    attempts, pass_days = [], []
    censored = starts = blown = timeouts = 0
    for i0 in range(first, len(paths), step):
        starts += 1
        outcome, used, k = run_eval(paths, i0, ev, max_eval_days)
        if outcome == "open":
            censored += 1
            continue
        periods = max(1, math.ceil(used / ev.fee_period_days)) if ev.fee_period_days else 1
        fees = ev.fee * periods
        if outcome != "pass":
            blown += outcome == "blown"
            timeouts += outcome == "timeout"
            attempts.append(Attempt(False, used, periods, eval_fees=fees))
            continue
        pass_days.append(used)
        funded = run_funded(paths, k + 1, fr, horizon_days)
        attempts.append(replace(funded, eval_days=used, eval_periods=periods, eval_fees=fees))
    inst = Instrument(name, 0.0, 0.0, 0.0)
    return Replay(Summary(program, name, inst, 0.0, attempts), starts, censored, blown,
                  timeouts, pass_days)


REPLAY_HEADER = ("| program | strategy | size | pass | days to pass | blown (eval) | "
                 "payouts per funded | trader net per funded | EV per eval |\n"
                 "|---|---|---|---:|---:|---:|---:|---:|---:|")


def replay_row(program: str, strategy: str, size: str, r: Replay) -> str:
    s = r.summary
    if not s.attempts:
        return f"| {program} | {strategy} | {size} | n/a | | | | | |"
    f = s.funded
    blown = r.blown / s.n
    days = f"{r.median_days_to_pass:.0f}" if r.pass_days else "-"
    payouts = statistics.fmean(a.payouts for a in f) if f else 0.0
    return (f"| {program} | {strategy} | {size} | {s.p_pass:.0%} | {days} | {blown:.0%} | "
            f"{payouts:.1f} | ${s.net_per_funded:,.0f} | ${s.ev:,.0f} |")
