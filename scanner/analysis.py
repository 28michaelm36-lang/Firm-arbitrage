"""One market's bars -> regime, indicators, triggers and records, as JSON-ready dicts."""
from __future__ import annotations

from dataclasses import dataclass

from .config import Config, Market
from .data import Series
from .efficiency import MEAN_REVERTING, TRENDING, Efficiency, measure
from .indicators import atr, rolling_std, sma
from .signals import CONTINUATION, LABELS, LONG, MEAN_REVERSION, SHORT, Record, Trigger, \
    continuation, mean_reversion, record

# The regime each strategy needs to have the evidence on its side.
FAVOURED = {MEAN_REVERSION: MEAN_REVERTING, CONTINUATION: TRENDING}
OPPOSED = {MEAN_REVERSION: TRENDING, CONTINUATION: MEAN_REVERTING}


@dataclass
class MarketView:
    market: Market
    series: Series
    efficiency: Efficiency | None
    mean: list[float | None]
    std: list[float | None]
    z: list[float | None]
    upper: list[float | None]
    lower: list[float | None]
    atr: list[float | None]
    triggers: list[Trigger]
    records: dict[str, Record]
    entry_z: float

    @property
    def bars(self):
        return self.series.bars

    @property
    def last(self) -> int:
        return len(self.bars) - 1

    def settled_index(self) -> int:
        """The latest bar that has closed."""
        return self.last - 1 if self.series.live else self.last

    def triggers_at(self, index: int) -> list[Trigger]:
        return [t for t in self.triggers if t.index == index]

    def agreement(self, strategy: str) -> str:
        """'agrees', 'against' or 'neutral': does the market's regime favour this strategy?"""
        if self.efficiency is None:
            return "neutral"
        if self.efficiency.regime == FAVOURED[strategy]:
            return "agrees"
        return "against" if self.efficiency.regime == OPPOSED[strategy] else "neutral"


def analyze(market: Market, series: Series, config: Config) -> MarketView:
    bars = series.bars
    closes = [b.close for b in bars]
    days = [b.day for b in bars]
    mr, co = config.mean_reversion, config.continuation
    z, mr_triggers = mean_reversion(closes, mr.lookback, mr.entry_z, days)
    upper, lower, co_triggers = continuation(bars, co.channel)
    atrs = atr(bars, config.record.atr_days)
    eff = config.efficiency
    try:
        efficiency = measure(closes, eff.lag, eff.window, eff.z_threshold)
    except ValueError:
        efficiency = None   # too little history
    horizon = config.record.horizon_days
    return MarketView(
        market=market, series=series, efficiency=efficiency,
        mean=sma(closes, mr.lookback), std=rolling_std(closes, mr.lookback), z=z,
        upper=upper, lower=lower, atr=atrs,
        triggers=sorted(mr_triggers + co_triggers, key=lambda t: (t.index, t.strategy)),
        records={MEAN_REVERSION: record(mr_triggers, bars, atrs, horizon),
                 CONTINUATION: record(co_triggers, bars, atrs, horizon)},
        entry_z=mr.entry_z,
    )


def _round(x, digits=6):
    return None if x is None else round(x, digits)


def _trigger(t: Trigger | None) -> dict | None:
    if t is None:
        return None
    return {"day": t.day.isoformat(), "strategy": t.strategy, "direction": t.direction,
            "price": _round(t.price), "level": _round(t.level, 4)}


def summary(view: MarketView) -> dict:
    """The market's row on the overview table."""
    m, bars, i = view.market, view.bars, view.last
    last, prev = bars[i].close, bars[i - 1].close if i else bars[i].close
    eff = view.efficiency

    def latest(strategy):
        return next((t for t in reversed(view.triggers) if t.strategy == strategy), None)

    def today(strategy):
        return next((t.direction for t in view.triggers_at(i) if t.strategy == strategy), 0)

    z = view.z[i]
    zone = 0
    if z is not None and z <= -view.entry_z:
        zone = LONG
    elif z is not None and z >= view.entry_z:
        zone = SHORT
    upper, lower = view.upper[i], view.lower[i]
    position = (last - lower) / (upper - lower) if upper is not None and upper > lower else None
    return {
        "code": m.code, "name": m.name, "sector": m.sector, "symbol": m.symbol,
        "currency": view.series.currency, "source": view.series.source,
        "day": bars[i].day.isoformat(), "live": view.series.live,
        "last": _round(last), "change": _round(last - prev), "change_pct": _round(last / prev - 1),
        "efficiency": None if eff is None else {
            "vr": _round(eff.vr, 4), "z": _round(eff.z, 3), "q": eff.q, "n": eff.n,
            "rho1": _round(eff.rho1, 4), "regime": eff.regime},
        MEAN_REVERSION: {
            "z": _round(z, 3), "entry_z": view.entry_z, "zone": zone,
            "trigger": today(MEAN_REVERSION), "agreement": view.agreement(MEAN_REVERSION),
            "last": _trigger(latest(MEAN_REVERSION)), "record": view.records[MEAN_REVERSION].as_dict()},
        CONTINUATION: {
            "upper": _round(upper), "lower": _round(lower), "position": _round(position, 4),
            "trigger": today(CONTINUATION), "agreement": view.agreement(CONTINUATION),
            "last": _trigger(latest(CONTINUATION)), "record": view.records[CONTINUATION].as_dict()},
    }


def chart(view: MarketView, bars: int = 260) -> dict:
    """The last `bars` bars with bands, channel and triggers, for the market's chart."""
    start = max(0, len(view.bars) - bars)
    k = view.entry_z

    def band(sign):
        return [None if m is None or s is None else _round(m + sign * k * s)
                for m, s in zip(view.mean[start:], view.std[start:])]

    return {
        "days": [b.day.isoformat() for b in view.bars[start:]],
        "open": [_round(b.open) for b in view.bars[start:]],
        "high": [_round(b.high) for b in view.bars[start:]],
        "low": [_round(b.low) for b in view.bars[start:]],
        "close": [_round(b.close) for b in view.bars[start:]],
        "mean": [_round(x) for x in view.mean[start:]],
        "band_upper": band(1), "band_lower": band(-1),
        "z": [_round(x, 3) for x in view.z[start:]],
        "channel_upper": [_round(x) for x in view.upper[start:]],
        "channel_lower": [_round(x) for x in view.lower[start:]],
        "triggers": [dict(_trigger(t), index=t.index - start, label=LABELS[t.strategy])
                     for t in view.triggers if t.index >= start],
    }
