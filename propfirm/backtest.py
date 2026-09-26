"""asymmetry4: the $2,000-for-$400 bracket, entered only when the market looks
range-bound, backtested bar by bar on real 1-minute data.

The idea: a bracket that risks 5 to make 1 loses only to a move 5 times the
size of its target, so only enter when a move that size looks unlikely. On a
martingale the filter cannot work: optional stopping pins P(win) at
stop / (stop + target) = 83.3% whatever the volatility. It can only work if
"range-bound now" predicts mean reversion over the life of the trade, which
is a claim about the market. This module measures it.

Mechanics:

* Signals are computed at the close of a bar from that bar and earlier bars
  only. The trade enters at the next bar's open.
* The target is a limit order: it fills only if price trades `through` points
  beyond it. The stop fills at the stop, or at the open if a bar gaps past it.
  A bar that touches both counts as a loss, since the order inside a bar is
  unknown.
* Trades still open at the session close exit at the last bar's close.
* After a trade closes, the next signal is checked on the bar it closed in.

Results are in R (multiples of the dollar risk), so they scale to any size.
The EV-equivalent win rate converts the mean R, time exits included, into the
win rate of a clean bracket with the same expectancy. That is the number to
feed the prop-firm simulator (`--winrate`, `--winrate-rr`).
"""
from __future__ import annotations

import csv
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .instruments import Instrument


@dataclass(frozen=True, slots=True)
class Bar:
    t: datetime        # bar start, exchange-local, naive
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


# --- data -------------------------------------------------------------------

_TIME_COLUMNS = ("time", "datetime", "timestamp", "date")


def _column(header: list[str], names) -> int | None:
    for name in names:
        if name in header:
            return header.index(name)
    return None


def load_sessions(paths, data_tz: str = "UTC", local_tz: str = "America/New_York",
                  start: str = "09:30", end: str = "16:00", min_bars: int = 0) -> list[list[Bar]]:
    """Read 1-minute bar CSVs into sessions of bars between `start` and `end`
    local time, one list per day, oldest first. Sessions with fewer than
    `min_bars` bars (holidays, half days, gaps in the data) are dropped.

    Each CSV needs a header with a time column (time, datetime, timestamp or
    date) and open, high, low, close; volume is optional. Timestamps are bar
    starts, as "YYYY-MM-DD HH:MM[:SS]" in `data_tz`, or ISO 8601 with an
    offset. Duplicate timestamps keep the first bar seen.
    """
    src, dst = ZoneInfo(data_tz), ZoneInfo(local_tz)
    t0, t1 = time.fromisoformat(start), time.fromisoformat(end)
    days: dict[date, dict[datetime, Bar]] = {}
    hour_cache: dict[str, datetime] = {}

    def to_local(ts: str) -> datetime:
        # Fast path: offsets only change on the hour, so convert once per hour.
        if len(ts) <= 19 and ts[13:14] == ":":
            base = hour_cache.get(ts[:13])
            if base is None:
                naive = datetime.fromisoformat(ts[:13].replace("T", " ") + ":00")
                base = naive.replace(tzinfo=src).astimezone(dst).replace(tzinfo=None)
                hour_cache[ts[:13]] = base
            return base + timedelta(minutes=int(ts[14:16]))
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=src)
        return parsed.astimezone(dst).replace(tzinfo=None)

    for path in paths:
        with open(path, newline="") as f:
            reader = csv.reader(f)
            header = [h.strip().lower() for h in next(reader)]
            it = _column(header, _TIME_COLUMNS)
            io, ih, il, ic = (_column(header, (k,)) for k in ("open", "high", "low", "close"))
            iv = _column(header, ("volume", "vol"))
            if None in (it, io, ih, il, ic):
                raise ValueError(f"{path}: need time, open, high, low and close columns, got {header}")
            for row in reader:
                if not row:
                    continue
                t = to_local(row[it].strip())
                if not t0 <= t.time() < t1:
                    continue
                bars = days.setdefault(t.date(), {})
                if t not in bars:
                    bars[t] = Bar(t, float(row[io]), float(row[ih]), float(row[il]),
                                  float(row[ic]), float(row[iv]) if iv is not None and row[iv] else 0.0)
    return [sorted(bars.values(), key=lambda b: b.t) for _, bars in sorted(days.items())
            if len(bars) >= min_bars]


# --- look-back measures -----------------------------------------------------

class Session:
    """One session's bars plus look-back measures, computed once per window
    length and shared by every filter."""

    def __init__(self, bars: list[Bar], mean: str = "vwap"):
        self.bars = bars
        self._windows: dict[int, tuple] = {}
        kind, _, n = mean.partition(":")
        if kind == "vwap":
            self.mean, self.mean_ready = self._vwap(), 0
        elif kind == "sma":
            n = int(n or 30)
            self.mean, self.mean_ready = self._sma(n), n - 1
        else:
            raise ValueError(f"unknown mean {mean!r}; use vwap or sma:N")

    def _vwap(self) -> list[float]:
        weighted = any(b.v > 0 for b in self.bars)
        num = den = 0.0
        out = []
        for b in self.bars:
            w = b.v if weighted else 1.0
            num += w * (b.h + b.l + b.c) / 3
            den += w
            out.append(num / den if den else b.c)
        return out

    def _sma(self, n: int) -> list[float]:
        out, total = [], 0.0
        for i, b in enumerate(self.bars):
            total += b.c
            if i >= n:
                total -= self.bars[i - n].c
            out.append(total / min(i + 1, n))
        return out

    def window(self, n: int):
        """Rolling n-bar high, low, sum of true ranges and sum of |close
        changes|, each ending at bar i (valid from i = n - 1)."""
        if n not in self._windows:
            bars = self.bars
            hi, lo, tr, path = [], [], [], []
            hq: deque[int] = deque()
            lq: deque[int] = deque()
            tr_cum, path_cum = [0.0], [0.0]
            for i, b in enumerate(bars):
                prev = bars[i - 1].c if i else b.o
                tr_cum.append(tr_cum[-1] + max(b.h, prev) - min(b.l, prev))
                path_cum.append(path_cum[-1] + (abs(b.c - prev) if i else 0.0))
                while hq and bars[hq[-1]].h <= b.h:
                    hq.pop()
                hq.append(i)
                while lq and bars[lq[-1]].l >= b.l:
                    lq.pop()
                lq.append(i)
                if hq[0] <= i - n:
                    hq.popleft()
                if lq[0] <= i - n:
                    lq.popleft()
                hi.append(bars[hq[0]].h)
                lo.append(bars[lq[0]].l)
                j = max(0, i + 1 - n)
                tr.append(tr_cum[i + 1] - tr_cum[j])
                # Close-to-close path over the window: n - 1 changes.
                path.append(path_cum[i + 1] - path_cum[j + 1])
            self._windows[n] = (hi, lo, tr, path)
        return self._windows[n]


# --- entry filters ----------------------------------------------------------
# Each filter is checked at the close of bar i against the stop distance (in
# points) the trade would use. Parameters are in stops where they are sizes.

@dataclass(frozen=True)
class All:
    """No filter: every bar is a signal. The baseline."""
    lookback = 1

    def ok(self, s: Session, i: int, stop: float) -> bool:
        return True


@dataclass(frozen=True)
class Range:
    """Calm: the last n bars span at most `width` stops."""
    n: int = 30
    width: float = 1.0

    @property
    def lookback(self) -> int:
        return self.n

    def ok(self, s: Session, i: int, stop: float) -> bool:
        hi, lo, _, _ = s.window(self.n)
        return hi[i] - lo[i] <= self.width * stop


@dataclass(frozen=True)
class Chop:
    """Choppiness index over n bars (0-100) at or above `above`. 100 means the
    bars went nowhere; 61.8 is the usual "choppy" line."""
    n: int = 30
    above: float = 61.8

    @property
    def lookback(self) -> int:
        return self.n

    def ok(self, s: Session, i: int, stop: float) -> bool:
        hi, lo, tr, _ = s.window(self.n)
        span = hi[i] - lo[i]
        if span <= 0:
            return True
        return 100 * math.log10(tr[i] / span) / math.log10(self.n) >= self.above


@dataclass(frozen=True)
class Efficiency:
    """Kaufman efficiency ratio over n bars at or below `below`: net move /
    distance travelled. Near 0 is chop, near 1 is a clean trend."""
    n: int = 30
    below: float = 0.3

    @property
    def lookback(self) -> int:
        return self.n + 1

    def ok(self, s: Session, i: int, stop: float) -> bool:
        _, _, _, path = s.window(self.n + 1)
        if path[i] <= 0:
            return True
        return abs(s.bars[i].c - s.bars[i - self.n].c) / path[i] <= self.below


@dataclass(frozen=True)
class Near:
    """Close to the mean: within `within` stops of the session mean (--mean)."""
    within: float = 0.25
    lookback = 1

    def ok(self, s: Session, i: int, stop: float) -> bool:
        return abs(s.bars[i].c - s.mean[i]) <= self.within * stop


@dataclass(frozen=True)
class Far:
    """Stretched: at least `beyond` stops from the session mean (--mean)."""
    beyond: float = 0.5
    lookback = 1

    def ok(self, s: Session, i: int, stop: float) -> bool:
        return abs(s.bars[i].c - s.mean[i]) >= self.beyond * stop


@dataclass(frozen=True)
class Clock:
    """Only between `start` and `end` local time (HHMM, e.g. 1130 to 1400)."""
    start: int = 1130
    end: int = 1400
    lookback = 1

    def ok(self, s: Session, i: int, stop: float) -> bool:
        t = s.bars[i].t
        return self.start <= t.hour * 100 + t.minute < self.end


FILTERS = {"all": All, "range": Range, "chop": Chop, "er": Efficiency,
           "near": Near, "far": Far, "time": Clock}


def parse_filters(spec: str) -> list:
    """"range:n=30,width=1+near:within=0.25" -> [Range(...), Near(...)] (all must pass)."""
    out = []
    for part in filter(None, (p.strip() for p in spec.split("+"))):
        name, _, params = part.partition(":")
        if name not in FILTERS:
            raise ValueError(f"unknown filter {name!r}; choose from {sorted(FILTERS)}")
        kwargs = {}
        for item in filter(None, params.split(",")):
            key, _, raw = item.partition("=")
            kwargs[key.strip()] = float(raw) if "." in raw else int(raw)
        out.append(FILTERS[name](**kwargs))
    return out


# Filters worth testing first. The calm-only filter (range) is the control:
# theory says it should not beat the baseline on its own.
DEFAULT_SUITE = (
    "all",
    "range:n=30,width=1",
    "range:n=60,width=1",
    "chop:n=30,above=61.8",
    "er:n=30,below=0.2",
    "near:within=0.1",
    "time:start=1130,end=1400",
    "range:n=30,width=1+chop:n=30,above=61.8",
    "range:n=60,width=1+near:within=0.1",
    "chop:n=30,above=61.8+near:within=0.1+time:start=1130,end=1400",
)


# --- trades -----------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    stop: float = 40.0            # stop distance in points, or a fraction of price if pct
    pct: bool = False
    rr: float = 0.2               # target / stop
    side: str = "fade"            # fade (toward the mean), follow (away from it), long, short or both
    mean: str = "vwap"            # vwap or sma:N, for near, far, fade and follow
    through: float = 0.25         # the target fills only if price trades this far past it
    last_entry: str = "15:30"     # no new signals after this local time
    max_per_day: int = 0          # 0 = no limit
    cooldown: int = 0             # bars to wait after an exit

    def stop_points(self, price: float) -> float:
        return self.stop * price if self.pct else self.stop


@dataclass(frozen=True)
class Trade:
    day: date
    entry_time: datetime
    side: int                     # +1 long, -1 short
    entry: float
    stop: float                   # points
    outcome: str                  # "win", "loss" or "time"
    r: float                      # P&L in multiples of the risk, before costs
    minutes: int
    ambiguous: bool = False       # a bar touched both the stop and the target


def run_trade(bars: list[Bar], j: int, side: int, stop: float, cfg: Config) -> tuple[Trade, int]:
    """Enter at the open of bars[j] and walk forward. Returns the trade and
    the index of the bar it closed in."""
    entry, day = bars[j].o, bars[j].t.date()
    target = cfg.rr * stop
    stop_px = entry - side * stop
    tgt_px = entry + side * target
    through = side * cfg.through
    for k in range(j, len(bars)):
        b = bars[k]
        if side > 0:
            stopped = b.l <= stop_px
            hit = b.h >= tgt_px + through
            gap = min(b.o, stop_px)
        else:
            stopped = b.h >= stop_px
            hit = b.l <= tgt_px + through
            gap = max(b.o, stop_px)
        if stopped or hit:
            minutes = int((b.t - bars[j].t).total_seconds() // 60) + 1
            if stopped:
                r = side * (gap - entry) / stop
                return Trade(day, bars[j].t, side, entry, stop, "loss", r, minutes, ambiguous=hit), k
            return Trade(day, bars[j].t, side, entry, stop, "win", cfg.rr, minutes), k
    last = bars[-1]
    minutes = int((last.t - bars[j].t).total_seconds() // 60) + 1
    r = side * (last.c - entry) / stop
    return Trade(day, bars[j].t, side, entry, stop, "time", r, minutes), len(bars) - 1


def backtest(sessions: list[list[Bar]], filters: list, cfg: Config) -> list[Trade]:
    """Every trade the filter set would have taken, in order."""
    last_entry = time.fromisoformat(cfg.last_entry)
    warmup = max((f.lookback for f in filters), default=1) - 1
    uses_mean = cfg.side in ("fade", "follow") or any(isinstance(f, (Near, Far)) for f in filters)
    trades: list[Trade] = []
    for bars in sessions:
        s = Session(bars, cfg.mean)
        taken, i = 0, max(warmup, s.mean_ready if uses_mean else 0)
        while i < len(bars) - 1:
            if bars[i].t.time() > last_entry or (cfg.max_per_day and taken >= cfg.max_per_day):
                break
            stop = cfg.stop_points(bars[i].c)
            if not all(f.ok(s, i, stop) for f in filters):
                i += 1
                continue
            if cfg.side in ("fade", "follow"):
                toward = 1 if bars[i].c <= s.mean[i] else -1
                sides = (toward if cfg.side == "fade" else -toward,)
            elif cfg.side == "both":
                sides = (1, -1)
            else:
                sides = (1,) if cfg.side == "long" else (-1,)
            closed = i + 1
            for side in sides:
                trade, k = run_trade(bars, i + 1, side, stop, cfg)
                trades.append(trade)
                closed = max(closed, k)
            taken += 1
            i = max(closed + cfg.cooldown, i + 1)
    return trades


# --- results ----------------------------------------------------------------

@dataclass
class Result:
    label: str
    trades: list[Trade] = field(repr=False)
    rr: float
    risk: float
    costs: list[float] = field(repr=False)

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def days(self) -> int:
        return len({t.day for t in self.trades})

    def count(self, outcome: str) -> int:
        return sum(t.outcome == outcome for t in self.trades)

    @property
    def hit_rate(self) -> float:
        return self.count("win") / self.n if self.n else math.nan

    @property
    def mean_r(self) -> float:
        return sum(t.r for t in self.trades) / self.n if self.n else math.nan

    @property
    def win_rate(self) -> float:
        """EV-equivalent win rate: the clean-bracket win rate with this mean R."""
        return (self.mean_r + 1) / (1 + self.rr)

    @property
    def win_rate_se(self) -> float:
        """Standard error of win_rate, clustered by day (trades on one day are
        not independent)."""
        if self.n < 2:
            return math.nan
        by_day: dict[date, list[float]] = {}
        for t in self.trades:
            by_day.setdefault(t.day, []).append(t.r)
        m, g = self.mean_r, len(by_day)
        if g < 2:
            return math.nan
        var = sum((sum(rs) - len(rs) * m) ** 2 for rs in by_day.values()) / self.n ** 2
        return math.sqrt(var * g / (g - 1)) / (1 + self.rr)

    @property
    def cost(self) -> float:
        return sum(self.costs) / len(self.costs) if self.costs else 0.0

    @property
    def breakeven(self) -> float:
        """Win rate a clean bracket needs to cover the average cost."""
        return (self.risk + self.cost) / (self.risk * (1 + self.rr))

    @property
    def dollars(self) -> float:
        """Average net P&L per trade at `risk` dollars risked."""
        return self.mean_r * self.risk - self.cost

    @property
    def minutes(self) -> float:
        return sum(t.minutes for t in self.trades) / self.n if self.n else math.nan


def trade_cost(instrument: Instrument, risk: float, stop_points: float, tick_size: float = 0.25,
               max_minis: float = math.inf) -> float:
    """Round-trip cost of one bracket risking `risk` over a stop of `stop_points`."""
    return instrument.with_overrides(stop_ticks=stop_points / tick_size).cost(risk, max_minis)


def evaluate(label: str, trades: list[Trade], cfg: Config, instrument: Instrument,
             risk: float = 2000.0, max_minis: float = math.inf) -> Result:
    costs = [trade_cost(instrument, risk, t.stop, max_minis=max_minis) for t in trades]
    return Result(label, trades, cfg.rr, risk, costs)


def format_results(results: list[Result], baseline: float) -> str:
    header = (f"{'filter':<44} {'period':<16} {'trades':>6} {'/day':>5} {'hit':>6} {'time':>5} "
              f"{'win eq':>7} {'95% CI':>13} {'edge':>7} {'b/e':>6} {'$/trade':>8} {'min':>5}")
    lines = [header, "-" * len(header)]
    for r in results:
        label, _, period = r.label.partition("|")
        if not r.n:
            lines.append(f"{label:<44.44} {period:<16} {0:>6}")
            continue
        lo, hi = r.win_rate - 1.96 * r.win_rate_se, r.win_rate + 1.96 * r.win_rate_se
        lines.append(
            f"{label:<44.44} {period:<16} {r.n:>6} {r.n / r.days:>5.1f} {r.hit_rate:>6.1%} "
            f"{r.count('time') / r.n:>5.1%} {r.win_rate:>7.1%} {lo:>6.1%}-{hi:<6.1%} "
            f"{100 * (r.win_rate - baseline):>+6.1f}p {r.breakeven:>6.1%} {r.dollars:>8,.0f} "
            f"{r.minutes:>5.0f}")
    return "\n".join(lines)
