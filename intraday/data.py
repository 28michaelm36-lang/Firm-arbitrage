"""1-minute bars: loading CSVs, regular-session days, and contract rolls.

Everything downstream works on `Day` objects: the 09:30-16:00 New York
session of one date, one bar per minute from 09:30, plus the previous
session's close. Bars are labelled by their start time, so the bar labelled
09:30 covers 09:30:00-09:30:59 and the session close is the close of the
15:59 bar.
"""
from __future__ import annotations

import csv
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
RTH_START = time(9, 30)
RTH_END = time(16, 0)
SESSION_MINUTES = 390


@dataclass(frozen=True, slots=True)
class Bar:
    t: datetime        # bar start, New York time, naive
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


@dataclass
class Day:
    """One regular session. `bars[i]` starts i minutes after 09:30; minutes
    with no trades are filled with a flat bar at the last close. An early
    close leaves fewer than 390 bars."""
    date: date
    bars: list[Bar]
    prev_close: float | None = None   # previous session's close, roll-adjusted
    roll: bool = False                # a contract roll was detected before this session
    usable: bool = True               # False if a roll falls inside the session itself
    _vwap: list[float] | None = field(default=None, repr=False)

    @property
    def open(self) -> float:
        return self.bars[0].o

    @property
    def close(self) -> float:
        return self.bars[-1].c

    @property
    def full(self) -> bool:
        return len(self.bars) >= SESSION_MINUTES

    def vwap(self) -> list[float]:
        """Session VWAP of the typical price at the close of each bar. Equal
        weights if the data has no volume."""
        if self._vwap is None:
            weighted = any(b.v > 0 for b in self.bars)
            num = den = 0.0
            out = []
            for b in self.bars:
                w = b.v if weighted else 1.0
                num += w * (b.h + b.l + b.c) / 3
                den += w
                out.append(num / den if den else b.c)
            self._vwap = out
        return self._vwap


@dataclass
class LoadInfo:
    rows: int = 0
    duplicates: int = 0
    data_tz: str = ""
    label: str = ""
    first: datetime | None = None
    last: datetime | None = None
    rolls: list[tuple[datetime, float]] = field(default_factory=list)
    days: int = 0
    full_days: int = 0
    dropped_days: int = 0

    def format(self) -> str:
        lines = [
            f"rows read             {self.rows:,} ({self.duplicates:,} duplicate timestamps dropped)",
            f"time zone of the file {self.data_tz}; bars labelled by their {self.label}",
            f"range (New York)      {self.first} to {self.last}",
            f"regular sessions      {self.days:,} ({self.full_days:,} full, "
            f"{self.dropped_days} dropped for too few bars)",
        ]
        if self.rolls:
            lines.append(f"contract rolls        {len(self.rolls)} detected and adjusted:")
            for t, jump in self.rolls:
                lines.append(f"    {t:%Y-%m-%d %H:%M}  {jump:+.2f} points")
        else:
            lines.append("contract rolls        none detected (back-adjusted, or a CFD)")
        return "\n".join(lines)


# --- reading ----------------------------------------------------------------

_TIME_NAMES = ("timestamp", "time", "datetime", "date", "ts_event", "ts")
_TZ_SUFFIXES = {"et": "America/New_York", "est": "America/New_York", "edt": "America/New_York",
                "ny": "America/New_York", "ct": "America/Chicago", "cst": "America/Chicago",
                "cdt": "America/Chicago", "utc": "UTC", "gmt": "UTC"}


def _find(header: list[str], names) -> int | None:
    for name in names:
        if name in header:
            return header.index(name)
    return None


def _time_column(header: list[str]) -> tuple[int | None, str | None]:
    """Index of the time column, and the time zone its name declares, if
    any: 'timestamp ET' means New York time."""
    for i, name in enumerate(header):
        base, _, suffix = name.replace("(", " ").replace(")", " ").strip().rpartition(" ")
        if base in _TIME_NAMES and suffix in _TZ_SUFFIXES:
            return i, _TZ_SUFFIXES[suffix]
    return _find(header, _TIME_NAMES), None


class _TimeParser:
    """Turns timestamp strings into naive New York datetimes. Handles ISO 8601
    (with or without an offset), US-style M/D/YYYY H:MM[:SS] [AM|PM] and epoch
    seconds, milliseconds or nanoseconds. Zone offsets are looked up once per
    hour, since they only change on the hour."""

    def __init__(self, data_tz: str):
        self.src = ZoneInfo(data_tz)
        self.same = data_tz == "America/New_York"
        self._hours: dict[tuple, timedelta] = {}
        self._dates: dict[str, date] = {}

    def _localise(self, naive: datetime) -> datetime:
        if self.same:
            return naive
        key = (naive.year, naive.month, naive.day, naive.hour)
        shift = self._hours.get(key)
        if shift is None:
            hour = naive.replace(minute=0, second=0, microsecond=0)
            local = hour.replace(tzinfo=self.src).astimezone(NEW_YORK).replace(tzinfo=None)
            shift = self._hours[key] = local - hour
        return naive + shift

    def __call__(self, s: str) -> datetime:
        s = s.strip()
        if s.replace(".", "", 1).isdigit():
            x = float(s)
            scale = 1e9 if x > 1e17 else 1e3 if x > 1e11 else 1.0
            utc = datetime.fromtimestamp(x / scale, ZoneInfo("UTC"))
            return utc.astimezone(NEW_YORK).replace(tzinfo=None)
        if "/" in s[:10]:
            return self._localise(self._us(s))
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(NEW_YORK).replace(tzinfo=None)
        return self._localise(parsed)

    def _us(self, s: str) -> datetime:
        day_part, _, rest = s.partition(" ")
        d = self._dates.get(day_part)
        if d is None:
            m, dd, y = (int(x) for x in day_part.split("/"))
            d = self._dates[day_part] = date(y if y > 100 else 2000 + y, m, dd)
        rest = rest.strip().upper()
        pm = rest.endswith("PM")
        rest = rest.removesuffix("AM").removesuffix("PM").strip()
        parts = [int(float(x)) for x in rest.split(":")] if rest else [0]
        hh = parts[0]
        if pm or "AM" in s.upper():
            hh = hh % 12 + (12 if pm else 0)
        return datetime(d.year, d.month, d.day, hh, parts[1] if len(parts) > 1 else 0,
                        parts[2] if len(parts) > 2 else 0)


def read_bars(paths, data_tz: str | None = None, label: str = "auto") -> tuple[list[Bar], LoadInfo]:
    """Read 1-minute bar CSVs into one sorted list in New York time.

    Each file needs a header with a time column (timestamp, time, datetime or
    date, optionally followed by a zone such as 'timestamp ET') and open, high,
    low, close; volume is optional. `data_tz` overrides the zone; without it
    the column name decides, and UTC is the fallback. `label` says whether
    timestamps mark the start or the end of each bar; 'auto' decides from the
    volume spike at the 09:30 open. Duplicate timestamps keep the first bar.
    """
    rows: list[tuple] = []
    info = LoadInfo()
    zones = set()
    for path in paths:
        with open(path, newline="") as f:
            reader = csv.reader(f)
            header = [h.strip().lower() for h in next(reader)]
            it, declared = _time_column(header)
            io, ih, il, ic = (_find(header, (k, k[0])) for k in ("open", "high", "low", "close"))
            iv = _find(header, ("volume", "vol", "v"))
            if None in (it, io, ih, il, ic):
                raise ValueError(f"{path}: need time, open, high, low and close columns, got {header}")
            tz = data_tz or declared or "UTC"
            zones.add(tz)
            parse = _TimeParser(tz)
            for row in reader:
                if not row or not row[it].strip():
                    continue
                v = row[iv].strip() if iv is not None and iv < len(row) else ""
                rows.append((parse(row[it]), float(row[io]), float(row[ih]), float(row[il]),
                             float(row[ic]), float(v) if v else 0.0))
    info.rows = len(rows)
    info.data_tz = ", ".join(sorted(zones))
    rows.sort(key=lambda r: r[0])
    bars: list[Bar] = []
    last_t = None
    for r in rows:
        if r[0] == last_t:
            info.duplicates += 1
            continue
        last_t = r[0]
        bars.append(Bar(*r))
    if label == "auto":
        label = detect_label(bars)
    if label not in ("start", "end"):
        raise ValueError("label must be 'start', 'end' or 'auto'")
    if label == "end":
        minute = timedelta(minutes=1)
        bars = [Bar(b.t - minute, b.o, b.h, b.l, b.c, b.v) for b in bars]
    info.label = label
    if bars:
        info.first, info.last = bars[0].t, bars[-1].t
    return bars, info


def detect_label(bars: list[Bar]) -> str:
    """'end' if the opening volume spike sits on the bar labelled 09:31 rather
    than 09:30, i.e. timestamps mark when each bar closed."""
    vol: dict[time, list[float]] = {time(9, 29): [], time(9, 30): [], time(9, 31): []}
    for b in bars:
        bucket = vol.get(b.t.time())
        if bucket is not None:
            bucket.append(b.v)
    means = {k: (sum(v) / len(v) if v else 0.0) for k, v in vol.items()}
    v29, v30, v31 = means[time(9, 29)], means[time(9, 30)], means[time(9, 31)]
    if min(v29, v30, v31) <= 0:
        return "start"
    return "end" if v31 / v30 > v30 / v29 else "start"


# --- contract rolls ---------------------------------------------------------

def third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def detect_rolls(bars: list[Bar], min_jump: float = 0.006) -> list[tuple[datetime, float]]:
    """Contract switches in an unadjusted continuous futures series.

    In liquid futures a bar opens within a tick or two of the previous close,
    even across the daily halt. A switch to the next contract instead shows up
    as one upward jump the size of the calendar spread: the next contract
    trades above the expiring one by about (rate - dividend yield) x 3 months,
    about 1% of price for NQ at 2023-2025 rates. In each quarterly roll window
    (the two weeks before expiration) the largest upward open-to-previous-close
    jump is a roll if it is at least `min_jump` of price and 10 times the
    typical jump in that window. Weekend gaps can pass that test too, so check
    the list `inspect` prints, and use --no-roll-adjust on CFDs and
    back-adjusted data. Returns (time of the first bar of the new contract,
    jump in points).
    """
    if len(bars) < 2:
        return []
    out = []
    years = range(bars[0].t.year, bars[-1].t.year + 1)
    windows = [(third_friday(y, m) - timedelta(days=15), third_friday(y, m) + timedelta(days=1))
               for y in years for m in (3, 6, 9, 12)]
    starts = [datetime.combine(a, time()) for a, _ in windows]
    i = 1
    for (a, b), start in zip(windows, starts):
        end = datetime.combine(b, time())
        while i < len(bars) and bars[i].t < start:
            i += 1
        best, best_i, moves = 0.0, None, []
        j = i
        while j < len(bars) and bars[j].t < end:
            jump = bars[j].o - bars[j - 1].c
            moves.append(abs(jump))
            if jump > best:
                best, best_i = jump, j
            j += 1
        if best_i is None or len(moves) < 100:
            continue
        typical = statistics.median(moves) or bars[best_i].o * 1e-5
        if best >= min_jump * bars[best_i].o and best >= 10 * typical:
            out.append((bars[best_i].t, best))
    return out


# --- sessions ---------------------------------------------------------------

def sessions(bars: list[Bar], min_bars: int = 120, rolls=None) -> tuple[list[Day], dict]:
    """Group bars into regular sessions, oldest first. Missing minutes are
    filled with flat bars at the last close; sessions with fewer than
    `min_bars` real bars (holidays, data holes) are dropped. `rolls` from
    `detect_rolls`: the previous close is shifted by the jump, so it is quoted
    in the new contract, and a session with a roll inside it is marked
    unusable."""
    by_day: dict[date, dict[int, Bar]] = {}
    t0 = RTH_START.hour * 60 + RTH_START.minute
    for b in bars:
        m = b.t.hour * 60 + b.t.minute - t0
        if 0 <= m < SESSION_MINUTES:
            by_day.setdefault(b.t.date(), {})[m] = b
    days: list[Day] = []
    dropped = 0
    for d in sorted(by_day):
        real = by_day[d]
        if len(real) < min_bars:
            dropped += 1
            continue
        last = max(real)
        first_bar = real[min(real)]
        filled, prev_c = [], first_bar.o
        for m in range(last + 1):
            b = real.get(m)
            if b is None:
                t = datetime.combine(d, RTH_START) + timedelta(minutes=m)
                b = Bar(t, prev_c, prev_c, prev_c, prev_c, 0.0)
            filled.append(b)
            prev_c = b.c
        days.append(Day(d, filled))
    for prev, day in zip(days, days[1:]):
        day.prev_close = prev.close
    for t, jump in rolls or ():
        for prev, day in zip([None] + days, days):
            open_t, close_t = day.bars[0].t, day.bars[-1].t + timedelta(minutes=1)
            prev_close_t = prev.bars[-1].t + timedelta(minutes=1) if prev else datetime.min
            if open_t < t < close_t:
                day.usable = False
                day.roll = True
            elif prev_close_t <= t <= open_t and day.prev_close is not None:
                day.prev_close += jump
                day.roll = True
    return days, {"dropped": dropped}


def load_days(paths, data_tz: str | None = None, label: str = "auto", min_bars: int = 120,
              adjust_rolls: bool = True) -> tuple[list[Day], LoadInfo]:
    bars, info = read_bars(paths, data_tz, label)
    info.rolls = detect_rolls(bars) if adjust_rolls else []
    days, extra = sessions(bars, min_bars, info.rolls)
    info.days = len(days)
    info.full_days = sum(d.full for d in days)
    info.dropped_days = extra["dropped"]
    return days, info


def rescale(days: list[Day], level: float) -> list[Day]:
    """Scale each session's prices so its previous close (or, without one, its
    open) equals `level`. Every strategy here is scale-free within a session,
    so signals are unchanged; points, tick costs and dollars then read as if
    the index stood at `level` every day, which makes decades of history
    comparable with today's NQ."""
    out = []
    for d in days:
        f = level / (d.prev_close or d.open)
        bars = [Bar(b.t, b.o * f, b.h * f, b.l * f, b.c * f, b.v) for b in d.bars]
        out.append(Day(d.date, bars, d.prev_close * f if d.prev_close else None,
                       d.roll, d.usable))
    return out


def daily_sigma(days: list[Day], i: int, n: int = 14) -> float:
    """Standard deviation of the n daily close-to-close returns before day i,
    measured from each day's roll-adjusted previous close."""
    rets = [math.log(days[k].close / days[k].prev_close)
            for k in range(max(0, i - n), i) if days[k].prev_close]
    return statistics.stdev(rets) if len(rets) >= 2 else math.nan
