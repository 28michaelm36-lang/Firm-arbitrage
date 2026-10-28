"""Daily bars: Yahoo Finance's chart endpoint, and a synthetic demo feed."""
from __future__ import annotations

import json
import math
import random
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

USER_AGENT = "Mozilla/5.0 (compatible; futures-scanner/1.0)"
MAX_BYTES = 5_000_000


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Series:
    symbol: str
    bars: tuple[Bar, ...]
    live: bool = False          # the last bar is still forming
    source: str = "yahoo"
    currency: str = ""
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def http_get(url: str, timeout: float = 20) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError(f"response from {url} is over {MAX_BYTES} bytes")
    return body


def parse_yahoo_chart(payload: dict, now: datetime | None = None) -> Series:
    """A /v8/finance/chart response with interval=1d -> Series."""
    chart = payload.get("chart") or {}
    if chart.get("error"):
        error = chart["error"]
        raise ValueError(f"Yahoo: {error.get('description', error) if isinstance(error, dict) else error}")
    result = (chart.get("result") or [None])[0]
    if not result:
        raise ValueError("Yahoo returned no data")
    meta = result.get("meta") or {}
    offset = meta.get("gmtoffset") or 0
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]

    def column(key):
        values = quote.get(key) or []
        return [values[i] if i < len(values) else None for i in range(len(stamps))]

    opens, highs, lows, closes, volumes = (column(k) for k in ("open", "high", "low", "close", "volume"))
    by_day: dict[date, Bar] = {}
    for i, stamp in enumerate(stamps):
        c = closes[i]
        if c is None or not math.isfinite(c):
            continue
        o = opens[i] if opens[i] is not None else c
        h = max(x for x in (highs[i], o, c) if x is not None)
        l = min(x for x in (lows[i], o, c) if x is not None)
        day = datetime.fromtimestamp(stamp + offset, tz=timezone.utc).date()
        # Yahoo can append the live quote as a second bar for today; the later one wins.
        by_day[day] = Bar(day, o, h, l, c, volumes[i] or 0.0)
    bars = tuple(by_day[d] for d in sorted(by_day))
    if not bars:
        raise ValueError(f"Yahoo returned no bars for {meta.get('symbol', '?')}")

    now = now or datetime.now(timezone.utc)
    regular = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    start, end = regular.get("start"), regular.get("end")
    live = (start is not None and end is not None and start <= now.timestamp() < end
            and bars[-1].day == datetime.fromtimestamp(now.timestamp() + offset, tz=timezone.utc).date())
    return Series(symbol=meta.get("symbol", ""), bars=bars, live=live, source="yahoo",
                  currency=meta.get("currency") or "", fetched_at=now)


class YahooProvider:
    """Free, unofficial and delayed. Continuous front month, not roll-adjusted."""
    name = "yahoo"
    hosts = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")

    def __init__(self, range: str = "2y", get=http_get):
        self.range = range
        self.get = get

    def history(self, symbol: str) -> Series:
        query = urllib.parse.urlencode({"range": self.range, "interval": "1d", "includePrePost": "false"})
        path = f"/v8/finance/chart/{urllib.parse.quote(symbol)}?{query}"
        for host in self.hosts:
            try:
                return parse_yahoo_chart(json.loads(self.get(f"https://{host}{path}")))
            except OSError:     # HTTP errors and timeouts: try the other host, then give up
                if host == self.hosts[-1]:
                    raise


# Rough price levels so the demo looks like the real contracts.
DEMO_LEVELS = {"ZL": 50, "ZM": 300, "ZS": 1050, "ZO": 380, "ZR": 14, "KE": 560, "HE": 85,
               "LE": 190, "GF": 260, "CC": 7000, "KC": 330, "CT": 68, "SB": 18, "OJ": 300,
               "PA": 1100, "PL": 1000}


class DemoProvider:
    """Synthetic daily bars: no network, deterministic for a given seed.

    Each symbol gets its own return autocorrelation between -0.25 and +0.25, so
    some series mean-revert, some trend and some are close to a random walk.
    Every call moves the last (live) bar a little, so signals can fire while the
    demo server runs.
    """
    name = "demo"

    def __init__(self, seed: int = 7, days: int = 504, today: date | None = None):
        self.seed = seed
        self.days = days
        self.today = today or datetime.now(timezone.utc).date()
        self._series: dict[str, list[Bar]] = {}
        self._rng = random.Random(f"{seed}:ticks")

    def phi(self, symbol: str) -> float:
        return random.Random(f"{self.seed}:{symbol}:phi").uniform(-0.25, 0.25)

    def _generate(self, symbol: str) -> list[Bar]:
        rng = random.Random(f"{self.seed}:{symbol}")
        phi, sigma = self.phi(symbol), rng.uniform(0.012, 0.024)
        days = []
        d = self.today
        while len(days) < self.days:
            if d.weekday() < 5:
                days.append(d)
            d -= timedelta(days=1)
        price = DEMO_LEVELS.get(symbol.split("=")[0], 100) * rng.uniform(0.85, 1.15)
        r_prev, bars = 0.0, []
        for day in reversed(days):
            r = phi * r_prev + sigma * math.sqrt(1 - phi * phi) * rng.gauss(0, 1)
            o = price * math.exp(0.2 * sigma * rng.gauss(0, 1))
            c = price * math.exp(r)
            h = max(o, c) * math.exp(abs(rng.gauss(0, 0.5 * sigma)))
            l = min(o, c) * math.exp(-abs(rng.gauss(0, 0.5 * sigma)))
            bars.append(Bar(day, o, h, l, c, rng.randint(2_000, 40_000)))
            price, r_prev = c, r
        return bars

    def history(self, symbol: str) -> Series:
        bars = self._series.get(symbol)
        if bars is None:
            bars = self._series[symbol] = self._generate(symbol)
        else:
            last = bars[-1]
            sigma = abs(math.log(last.high / last.low)) or 0.01
            c = last.close * math.exp(0.1 * sigma * self._rng.gauss(0, 1))
            bars[-1] = Bar(last.day, last.open, max(last.high, c), min(last.low, c), c, last.volume)
        return Series(symbol=symbol, bars=tuple(bars), live=True, source="demo")
