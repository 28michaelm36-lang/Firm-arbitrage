"""Bold + Harvest (asymmetry2) on real 1-minute bars.

The simulator in `propfirm/` resolves every bracket with a formula for a
driftless market. Here the same policies (`Bold` in the evaluation, `Harvest`
once funded) choose the same brackets from the same account state, and each
bracket is resolved on the session's actual bars. A *signal* decides when to
enter and in which direction, which is the only place a market edge can come
from: the bracket's dollar risk and target are set by the account.

Fills:

* A signal is read from the close of a bar, using that bar and earlier ones
  only. The market order fills at the next bar's open, one tick against you.
* The stop fills at the stop, or at the open when a bar gaps through it, one
  more tick against you. The target is a limit order: it fills at the target
  only once price trades a tick through it. A bar that touches both counts
  as the stop.
* The account is liquidated the moment open equity touches the floor. An
  intraday trailing floor follows the best open equity; inside a bar the best
  price is taken to come before the worst, the order that hurts most.
* Anything still open at the last bar exits at its close, a tick against you.

Sizing: the day's stop width (by default a quarter of the average session
range of the last 14 sessions, narrowed late in the day so a trade can still
finish) turns a dollar risk into contracts. Full-size contracts are used when
the trade is big enough, micros otherwise. Rounding to whole contracts moves
the stop, never the dollar risk.
"""
from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass, field, replace
from datetime import date

from propfirm.account import Account
from propfirm.bracket import Floor, Outcome
from propfirm.lifecycle import MAX_TRADES_PER_DAY, Attempt, Summary, _view, _Window, payout_available
from propfirm.policies import Policy
from propfirm.rules import Program

from .data import SESSION_MINUTES, Day
from .strategies import INSTRUMENT_COSTS, Costs, NoiseArea

LOOKBACK = 14


# --- the tape ---------------------------------------------------------------

class Tape:
    """Sessions plus what the engine precomputes from them: the stop width for
    each day and price arrays for the fill loop."""

    def __init__(self, days: list[Day], stop: str = "0.25r", time_scaled: bool = True):
        self.days = days
        self.time_scaled = time_scaled
        self._stop_kind, self._stop_value = parse_stop(stop)
        self.stop_spec = stop
        self.avg_range: list[float | None] = []
        recent: list[float] = []
        for d in days:
            self.avg_range.append(sum(recent) / len(recent) if len(recent) >= LOOKBACK // 2 else None)
            if d.usable:
                recent.append(max(b.h for b in d.bars) - min(b.l for b in d.bars))
                del recent[:-LOOKBACK]
        self._arrays: dict[tuple[int, int], tuple] = {}
        self._noise: list[dict] | None = None

    def tradable(self, i: int) -> bool:
        d = self.days[i]
        return d.usable and d.prev_close is not None and self.base_stop(i) is not None

    def base_stop(self, i: int) -> float | None:
        """The day's stop width in points, for a trade entered at the open."""
        if self._stop_kind == "points":
            return self._stop_value
        if self._stop_kind == "percent":
            d = self.days[i]
            return self._stop_value * (d.prev_close or d.open)
        r = self.avg_range[i]
        return None if r is None else self._stop_value * r

    def stop_points(self, i: int, k: int) -> float:
        """Stop width for an entry at bar k: narrower when less of the session is left."""
        s = self.base_stop(i)
        if not self.time_scaled:
            return s
        left = max(15, len(self.days[i].bars) - k)
        return s * math.sqrt(min(1.0, left / SESSION_MINUTES))

    def arrays(self, i: int, side: int) -> tuple[list, list, list, list]:
        """(open, favourable extreme, adverse extreme, close) of each bar, signed
        so that up is always good: prices for a long, negated prices for a short."""
        key = (i, side)
        a = self._arrays.get(key)
        if a is None:
            bars = self.days[i].bars
            if side > 0:
                a = ([b.o for b in bars], [b.h for b in bars], [b.l for b in bars], [b.c for b in bars])
            else:
                a = ([-b.o for b in bars], [-b.l for b in bars], [-b.h for b in bars], [-b.c for b in bars])
            self._arrays[key] = a
        return a

    def noise_bands(self, i: int) -> dict:
        if self._noise is None:
            self._noise = [sig for _, sig in NoiseArea().bands(self.days)]
        return self._noise[i]


def parse_stop(spec: str) -> tuple[str, float]:
    """'50' is points, '0.3%' a share of the previous close, '0.25r' a multiple
    of the average session range of the last 14 sessions."""
    s = spec.strip().lower()
    if s.endswith("%"):
        return "percent", float(s[:-1]) / 100
    if s.endswith("r"):
        return "range", float(s[:-1])
    return "points", float(s)


# --- signals: when to enter, and which way ------------------------------------

class Signal:
    """`__call__(tape, i, k)` returns (entry bar, side) for the first entry on
    day i whose order can fill at bar k or later, or None. The decision uses
    bars before the entry bar only."""
    name = "signal"

    def __call__(self, tape: Tape, i: int, k: int) -> tuple[int, int] | None:
        raise NotImplementedError


@dataclass
class Fixed(Signal):
    """Always long (side=1) or always short (side=-1), at the first allowed bar."""
    side: int = 1
    start: int = 1

    def __post_init__(self):
        self.name = "long" if self.side > 0 else "short"

    def __call__(self, tape, i, k):
        return max(k, self.start), self.side


@dataclass
class Coin(Signal):
    """A fair coin, the no-edge control. Reproducible: the flip is a hash of
    the date, the bar and the seed."""
    seed: int = 1
    start: int = 1
    name: str = "coin"

    def __call__(self, tape, i, k):
        j = max(k, self.start)
        h = hashlib.blake2b(f"{tape.days[i].date}:{j}:{self.seed}".encode(), digest_size=1).digest()
        return j, 1 if h[0] & 1 else -1


@dataclass
class OpeningRange(Signal):
    """asymmetry1's entry: the first 1-minute close outside the range of the
    first n minutes, traded with the break (or against it with fade=1)."""
    n: int = 15
    fade: int = 0
    last: int = 360

    def __post_init__(self):
        self.name = f"orb{self.n}" + ("-fade" if self.fade else "")

    def __call__(self, tape, i, k):
        bars = tape.days[i].bars
        if len(bars) <= self.n:
            return None
        hi = max(b.h for b in bars[:self.n])
        lo = min(b.l for b in bars[:self.n])
        for d in range(max(self.n, k) - 1, min(self.last, len(bars) - 1)):
            c = bars[d].c
            if c > hi or c < lo:
                side = 1 if c > hi else -1
                return d + 1, -side if self.fade else side
        return None


@dataclass
class FirstCandle(Signal):
    """The direction of the first n-minute candle (Zarattini & Aziz 2023)."""
    n: int = 5

    def __post_init__(self):
        self.name = f"first{self.n}"

    def __call__(self, tape, i, k):
        bars = tape.days[i].bars
        if len(bars) <= self.n:
            return None
        move = bars[self.n - 1].c - bars[0].o
        return None if move == 0 else (max(k, self.n), 1 if move > 0 else -1)


@dataclass
class Momentum(Signal):
    """The sign of the day's move so far, measured t minutes after the open or
    at any later entry. ref='prev' measures from the previous close (the
    intraday momentum of Gao et al. 2018 and Baltussen et al. 2021), ref='open'
    from today's open. fade=1 trades against it."""
    t: int = 30
    ref: str = "prev"
    fade: int = 0

    def __post_init__(self):
        if self.ref not in ("prev", "open"):
            raise ValueError("ref must be 'prev' or 'open'")
        self.name = f"mom{self.t}" + ("" if self.ref == "prev" else "-open") + ("-fade" if self.fade else "")

    def __call__(self, tape, i, k):
        day = tape.days[i]
        j = max(k, self.t, 1)
        if j >= len(day.bars):
            return None
        move = day.bars[j - 1].c - (day.prev_close if self.ref == "prev" else day.open)
        if move == 0:
            return None
        side = 1 if move > 0 else -1
        return j, -side if self.fade else side


@dataclass
class Gap(Signal):
    """The overnight gap, at 09:31: with it, or against it (fade=1)."""
    fade: int = 0

    def __post_init__(self):
        self.name = "gap" + ("-fade" if self.fade else "")

    def __call__(self, tape, i, k):
        day = tape.days[i]
        gap = day.open - day.prev_close
        if gap == 0:
            return None
        side = 1 if gap > 0 else -1
        return max(k, 1), -side if self.fade else side


@dataclass
class Noise(Signal):
    """The noise-area breakout (Zarattini, Aziz & Barbon 2024): at each half
    hour from 10:00, long above the band of typical moves from the open, short
    below it."""
    name: str = "noise"

    def __call__(self, tape, i, k):
        bars = tape.days[i].bars
        for m, (_, upper, lower) in sorted(tape.noise_bands(i).items()):
            if m < k or m >= len(bars):
                continue
            px = bars[m - 1].c
            if px > upper:
                return m, 1
            if px < lower:
                return m, -1
        return None


SIGNALS = {
    "coin": Coin,
    "long": lambda **kw: Fixed(side=1, **kw),
    "short": lambda **kw: Fixed(side=-1, **kw),
    "orb": OpeningRange,
    "first": FirstCandle,
    "mom": Momentum,
    "gap": Gap,
    "noise": Noise,
}


def parse_signal(spec: str) -> Signal:
    """Build a signal from "name" or "name:key=value,key=value"."""
    name, _, params = spec.partition(":")
    if name not in SIGNALS:
        raise ValueError(f"unknown signal {name!r}; choose from {', '.join(SIGNALS)}")
    kwargs = {}
    for item in filter(None, params.split(",")):
        key, _, raw = item.partition("=")
        try:
            kwargs[key.strip()] = int(raw)
        except ValueError:
            kwargs[key.strip()] = raw.strip()
    sig = SIGNALS[name](**kwargs)
    sig.spec = spec
    return sig


# --- sizing -------------------------------------------------------------------

@dataclass(frozen=True)
class Sizer:
    """Turns a dollar risk into contracts at the day's stop width. Stands in
    for `propfirm.instruments.Instrument` so the policies can ask what a trade
    costs: commission plus one tick of slippage on the entry."""
    stop_points: float
    nq: Costs
    mnq: Costs
    name: str = "NQ/MNQ"

    @property
    def tick_value(self) -> float:
        return self.mnq.tick * self.mnq.point_value

    @property
    def stop_ticks(self) -> float:
        return self.stop_points / self.mnq.tick

    @property
    def round_turn(self) -> float:
        return self.nq.commission + self.nq.slip * self.nq.point_value

    def lots(self, risk: float, max_minis: float = math.inf) -> tuple[int, Costs]:
        """(contracts, contract): full-size from about one contract's worth of
        risk, micros below that. At the program's contract cap the stop widens."""
        exposure = risk / self.stop_points          # dollars per point wanted
        if exposure >= 0.75 * self.nq.point_value and max_minis >= 1:
            n = max(1, int(exposure / self.nq.point_value + 0.5))
            return (n if max_minis == math.inf else min(n, int(max_minis + 1e-9))), self.nq
        n = max(1, int(exposure / self.mnq.point_value + 0.5))
        return (n if max_minis == math.inf else min(n, max(1, int(max_minis * 10 + 1e-9)))), self.mnq

    def cost(self, risk: float, max_minis: float = math.inf) -> float:
        n, c = self.lots(risk, max_minis)
        return n * (c.commission + c.slip * c.point_value)


# --- one bracket on real bars -------------------------------------------------

@dataclass(frozen=True)
class Fill:
    out: Outcome
    exit_index: int
    reason: str          # target, stop, floor, time
    costs: float         # commission and slippage paid, in dollars


def resolve(tape: Tape, i: int, k: int, side: int, balance: float, fl: Floor,
            stop: float, reward: float, per_point: float, cost: float, slip_usd: float,
            through: float, last: int | None = None) -> Fill:
    """Trade one bracket entered at the open of bar k on day i.

    `stop` and `reward` are dollars from the equity right after entry, which
    is `balance - cost` (cost = commission + entry slippage). `per_point` is
    the position's dollars per point, `slip_usd` one tick of slippage on the
    whole position, `through` how far past the target price must trade.
    """
    xo, xf, xa, xc = tape.arrays(i, side)
    last = len(xo) - 1 if last is None else min(last, len(xo) - 1)
    base = balance - cost
    hwm, floor = fl.hwm, fl.level
    if base <= floor:
        return Fill(Outcome(floor - balance, False, True, hwm, floor), k, "floor", cost)
    origin = xo[k]
    eq = lambda x: base + per_point * (x - origin)
    stop_eq = base - stop
    target_eq = base + reward
    kill_x = origin - min(stop, base - floor) / per_point
    target_x = origin + reward / per_point + through
    trailing = fl.trail is not None and floor < fl.lock
    if trailing:
        peak_x = origin + (hwm - base) / per_point
        trail_x = fl.trail / per_point
        lock_x = origin + (fl.lock - base) / per_point
        floor_x = origin - (base - floor) / per_point
    for j in range(k, last + 1):
        a = xa[j]
        if a <= kill_x:
            if floor >= stop_eq:
                return Fill(Outcome(floor - balance, False, True, hwm, floor), j, "floor", cost)
            fill = min(stop_eq, eq(xo[j])) - slip_usd       # a gap through the stop fills at the open
            return Fill(Outcome(fill - balance, False, fill <= floor, hwm, floor), j, "stop",
                        cost + slip_usd)
        f = xf[j]
        if f >= target_x:
            fill = max(target_eq, eq(xo[j])) if j > k else target_eq
            hwm, floor = Floor(floor, hwm, fl.trail, fl.lock).after_high(fill)
            return Fill(Outcome(fill - balance, True, False, hwm, floor), j, "target", cost)
        if trailing and f > peak_x:
            peak_x = f
            hwm = eq(f)
            new_floor_x = min(f - trail_x, lock_x)
            if new_floor_x > floor_x:
                floor_x = new_floor_x
                floor = max(floor, min(hwm - fl.trail, fl.lock))
                if floor_x > kill_x:
                    kill_x = floor_x
                if a <= floor_x:                              # best before worst inside the bar
                    return Fill(Outcome(floor - balance, False, True, hwm, floor), j, "floor", cost)
    fill = eq(xc[last]) - slip_usd
    return Fill(Outcome(fill - balance, False, fill <= floor, hwm, floor), last, "time",
                cost + slip_usd)


# --- the account lifecycle on real bars -----------------------------------------

@dataclass(frozen=True)
class Settings:
    nq: Costs = INSTRUMENT_COSTS["NQ"]
    mnq: Costs = INSTRUMENT_COSTS["MNQ"]
    first_entry: int = 1                 # 09:31
    last_entry: int = 360                # 15:30
    flat: int = SESSION_MINUTES - 1      # exit at the close of the 15:59 bar
    max_eval_days: int = 60
    horizon_days: int = 250


@dataclass(frozen=True)
class Run:
    """One evaluation bought on session `start` and everything that followed."""
    attempt: Attempt
    start: int
    end: int             # last session it used
    trades: int
    costs: float         # commission and slippage paid inside the accounts
    payout_days: tuple[tuple[int, float], ...] = ()     # (session, trader's share)

    @property
    def profit(self) -> float:
        return self.attempt.profit


@dataclass
class _Tally:
    trades: int = 0
    costs: float = 0.0


def trade_day(acct: Account, policy: Policy, view, tape: Tape, i: int, signal: Signal,
              cfg: Settings, tally: _Tally, sizer_out: list | None = None) -> None:
    """Let the policy trade session i: it names a bracket, the signal an entry."""
    if not tape.tradable(i):
        return
    day = tape.days[i]
    last = min(cfg.flat, len(day.bars) - 1)
    k = cfg.first_entry
    while not acct.blown and not acct.day_over and acct.day_trades < MAX_TRADES_PER_DAY:
        entry = signal(tape, i, k)
        if entry is None or entry[0] > min(cfg.last_entry, last - 1):
            return
        j, side = entry
        sizer = Sizer(tape.stop_points(i, j), cfg.nq, cfg.mnq)
        bracket = policy.next_trade(view(sizer))
        if bracket is None:
            return
        n, c = sizer.lots(bracket.risk, acct.max_minis)
        per_point = n * c.point_value
        slip_usd = per_point * c.slip
        cost = n * c.commission + slip_usd
        # The daily loss limit is a hard stop, with room for the stop's own slippage.
        stop = min(bracket.risk, acct.loss_limit_room - cost - slip_usd)
        if stop <= 0:
            acct.day_over = True
            return
        trail = acct.drawdown.amount if acct.drawdown.mode == "intraday_trailing" else None
        fill = resolve(tape, i, j, side, acct.balance, Floor(acct.floor, acct.hwm, trail, acct.lock),
                       stop, bracket.reward, per_point, cost, slip_usd, c.tick, last)
        acct.settle(fill.out)
        tally.trades += 1
        tally.costs += fill.costs
        k = fill.exit_index + 1


def run_attempt(program: Program, eval_policy: Policy, funded_policy: Policy, tape: Tape,
                i0: int, signal: Signal, cfg: Settings = Settings()) -> Run | None:
    """Buy an evaluation on session i0 and follow it to the end. None if the
    data runs out before the evaluation is decided."""
    ev, fr = program.evaluation, program.funded
    n = len(tape.days)
    tally = _Tally()
    acct = Account.open(ev)
    win = _Window(acct.balance)
    limit = min(cfg.max_eval_days, ev.max_days or math.inf)
    days = 0
    passed = False
    i = i0
    while i < n:
        view = lambda sizer: _view("eval", program, ev, sizer, acct, win)
        trade_day(acct, eval_policy, view, tape, i, signal, cfg, tally)
        win.add(acct.end_day())
        days += 1
        i += 1
        if acct.blown:
            break
        if (acct.balance >= ev.target_balance and acct.trading_days >= ev.min_days
                and (ev.consistency is None
                     or win.best <= ev.consistency * (acct.balance - ev.start_balance))):
            passed = True
            break
        if days >= limit:
            break
    else:
        return None
    periods = max(1, math.ceil(days / ev.fee_period_days)) if ev.fee_period_days else 1
    eval_fees = ev.fee * periods
    if not passed:
        return Run(Attempt(False, days, periods, eval_fees=eval_fees), i0, i - 1,
                   tally.trades, tally.costs)

    acct = Account.open(fr)
    win = _Window(acct.balance, fr.profitable_day_amount)
    payouts = fdays = 0
    gross = net = 0.0
    graduated = False
    paid: list[tuple[int, float]] = []
    while i < n and fdays < cfg.horizon_days:
        view = lambda sizer: _view("funded", program, fr, sizer, acct, win, payouts)
        trade_day(acct, funded_policy, view, tape, i, signal, cfg, tally)
        win.add(acct.end_day())
        fdays += 1
        i += 1
        if acct.blown:
            break
        available = payout_available(fr, acct.balance, payouts, win.start, win.days,
                                     win.profitable, win.best)
        if available > 0:
            sizer = Sizer(tape.base_stop(i - 1) or 1.0, cfg.nq, cfg.mnq)
            amount = min(available, funded_policy.withdraw(view(sizer), available))
            if amount >= max(fr.min_payout, 0.01):
                acct.withdraw(amount)
                share = fr.trader_share(amount, gross)
                net += share
                gross += amount
                payouts += 1
                paid.append((i - 1, share))
                win = _Window(acct.balance, fr.profitable_day_amount)
                if fr.max_payouts is not None and payouts >= fr.max_payouts:
                    graduated = True
                    break
    funded_fees = fr.activation_fee
    if fr.monthly_fee:
        funded_fees += fr.monthly_fee * math.ceil(fdays / fr.fee_period_days)
    alive = not acct.blown and not graduated
    attempt = Attempt(True, days, periods, funded_days=fdays, payouts=payouts, gross=gross,
                      net=net, eval_fees=eval_fees, funded_fees=funded_fees, graduated=graduated,
                      alive=alive, stranded=acct.room if alive else 0.0)
    return Run(attempt, i0, i - 1, tally.trades, tally.costs, tuple(paid))


def all_runs(program: Program, eval_policy: Policy, funded_policy: Policy, tape: Tape,
             signal: Signal, cfg: Settings = Settings(), first: int = 0, last: int | None = None,
             step: int = 1) -> dict[int, Run]:
    """An evaluation started on every `step`-th session in [first, last). Runs
    whose evaluation the data cannot decide are left out."""
    out = {}
    for i0 in range(first, len(tape.days) if last is None else last, step):
        run = run_attempt(program, eval_policy, funded_policy, tape, i0, signal, cfg)
        if run is not None:
            out[i0] = run
    return out


def summarize_runs(program: Program, name: str, runs) -> Summary:
    sizer = Sizer(1.0, INSTRUMENT_COSTS["NQ"], INSTRUMENT_COSTS["MNQ"])
    return Summary(program, name, sizer, 0.0, [r.attempt for r in runs])


# --- a campaign: one account at a time, back to back ----------------------------

@dataclass
class Campaign:
    """Buy an evaluation, trade it and whatever funded account it earns to the
    end, then buy the next one on the following session."""
    runs: list[Run]
    first: int
    last: int            # last session used

    def _sum(self, attr) -> float:
        return sum(attr(r.attempt) for r in self.runs)

    @property
    def evaluations(self) -> int:
        return len(self.runs)

    @property
    def passes(self) -> int:
        return sum(r.attempt.passed for r in self.runs)

    @property
    def payouts(self) -> int:
        return int(self._sum(lambda a: a.payouts))

    @property
    def gross(self) -> float:
        return self._sum(lambda a: a.gross)

    @property
    def trader_share(self) -> float:
        return self._sum(lambda a: a.net)

    @property
    def eval_fees(self) -> float:
        return self._sum(lambda a: a.eval_fees)

    @property
    def funded_fees(self) -> float:
        return self._sum(lambda a: a.funded_fees)

    @property
    def trading_costs(self) -> float:
        return sum(r.costs for r in self.runs)

    @property
    def net(self) -> float:
        """Cash in minus cash out: the trader's share of payouts minus every fee.
        Commissions and slippage were paid inside the accounts, so they are
        already in the payouts."""
        return self.trader_share - self.eval_fees - self.funded_fees

    def cash_flows(self) -> list[tuple[int, float]]:
        """(session, dollars), in order: fees when an account is bought or
        activated, the trader's share when a payout is made."""
        flows = []
        for r in self.runs:
            a = r.attempt
            flows.append((r.start, -a.eval_fees))
            if a.passed:
                flows.append((r.start + a.eval_days, -a.funded_fees))
            flows.extend(r.payout_days)
        return sorted(flows)

    @property
    def worst_drawdown(self) -> float:
        """Largest peak-to-trough fall in cumulative net cash."""
        total = peak = worst = 0.0
        for _, x in self.cash_flows():
            total += x
            peak = max(peak, total)
            worst = max(worst, peak - total)
        return worst

    def by_year(self, days: list[Day]) -> dict[int, float]:
        out: dict[int, float] = {}
        for i, x in self.cash_flows():
            y = days[min(i, len(days) - 1)].date.year
            out[y] = out.get(y, 0.0) + x
        return out


def campaign(runs: dict[int, Run], first: int, stop: int) -> Campaign:
    """Chain the runs: each starts the session after the previous one ended.
    `runs` must hold a run for every session (step 1). Ends at `stop` or when
    the next evaluation could not be decided."""
    out, i = [], first
    while i < stop and i in runs:
        r = runs[i]
        out.append(r)
        i = r.end + 1
    return Campaign(out, first, out[-1].end if out else first)


# --- does a signal have an edge? ------------------------------------------------

@dataclass
class Study:
    """One bracket a day, risking 1 to make `rr`, in units of the stop."""
    name: str
    rr: float
    r: list[tuple[date, float, str]] = field(default_factory=list)   # (day, R net of costs, reason)

    def part(self, start: date | None = None, end: date | None = None) -> "Study":
        keep = [x for x in self.r if (start is None or x[0] >= start) and (end is None or x[0] < end)]
        return replace(self, r=keep)

    @property
    def n(self) -> int:
        return len(self.r)

    @property
    def mean(self) -> float:
        return statistics.fmean(x[1] for x in self.r) if self.r else math.nan

    @property
    def stderr(self) -> float:
        return statistics.stdev(x[1] for x in self.r) / math.sqrt(self.n) if self.n > 1 else math.nan

    @property
    def t(self) -> float:
        return self.mean / self.stderr if self.n > 1 and self.stderr > 0 else math.nan

    def share(self, reason: str) -> float:
        return sum(x[2] == reason for x in self.r) / self.n if self.r else math.nan

    @property
    def win_eq(self) -> float:
        """Win rate of a clean bracket with the same mean R. Compare with
        1 / (1 + rr), what a coin flip wins before costs."""
        return (self.mean + 1) / (1 + self.rr)


def study(tape: Tape, signal: Signal, rr: float = 1.5, risk: float = 2000.0,
          cfg: Settings = Settings(), first: int = 0) -> Study:
    """The signal's first entry of each session, as a bracket risking `risk`
    dollars to make rr times that, far from any floor."""
    out = Study(signal.name, rr)
    fl = Floor(-math.inf, math.inf)
    for i in range(first, len(tape.days)):
        if not tape.tradable(i):
            continue
        day = tape.days[i]
        last = min(cfg.flat, len(day.bars) - 1)
        entry = signal(tape, i, cfg.first_entry)
        if entry is None or entry[0] > min(cfg.last_entry, last - 1):
            continue
        j, side = entry
        sizer = Sizer(tape.stop_points(i, j), cfg.nq, cfg.mnq)
        n, c = sizer.lots(risk)
        per_point = n * c.point_value
        slip_usd = per_point * c.slip
        cost = n * c.commission + slip_usd
        fill = resolve(tape, i, j, side, 0.0, fl, risk, rr * risk, per_point, cost, slip_usd,
                       c.tick, last)
        out.r.append((day.date, fill.out.pnl / risk, fill.reason))
    return out
