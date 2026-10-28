"""Watchlist and settings, loaded from TOML (see configs/scanner.toml)."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

WEBHOOK_FORMATS = ("json", "ntfy", "slack", "discord")


@dataclass(frozen=True)
class Market:
    code: str                           # short code shown on the page, e.g. "ZL"
    symbol: str                         # data-provider symbol, e.g. "ZL=F" on Yahoo
    name: str
    sector: str = ""
    keywords: tuple[str, ...] = ()      # terms that make a headline about this market
    news_query: str | None = None       # Google News search; default: keywords joined by OR

    def query(self) -> str:
        if self.news_query:
            return self.news_query
        return " OR ".join(f'"{k}"' if " " in k else k for k in self.keywords)


@dataclass(frozen=True)
class DataSettings:
    range: str = "2y"                   # history to load, as a Yahoo range
    refresh_minutes: float = 15
    alert_on_live_bar: bool = True      # alert on the still-forming bar, marked provisional


@dataclass(frozen=True)
class EfficiencySettings:
    lag: int = 5                        # q in the variance ratio VR(q)
    window: int = 500                   # daily returns used
    z_threshold: float = 1.96


@dataclass(frozen=True)
class MeanReversionSettings:
    lookback: int = 20
    entry_z: float = 2.0


@dataclass(frozen=True)
class ContinuationSettings:
    channel: int = 20


@dataclass(frozen=True)
class RecordSettings:
    horizon_days: int = 5               # judge each past trigger this many bars later
    atr_days: int = 20


@dataclass(frozen=True)
class NewsSettings:
    refresh_minutes: float = 10
    google_news: bool = True            # one Google News RSS search per market
    feeds: tuple[str, ...] = ()         # extra RSS/Atom feeds, matched against every market
    min_score: float = 4
    max_age_hours: float = 36
    impact_terms: tuple[str, ...] = ()
    up_words: tuple[str, ...] = ()
    down_words: tuple[str, ...] = ()


@dataclass(frozen=True)
class AlertSettings:
    webhook_url: str = ""
    webhook_format: str = "json"

    def __post_init__(self):
        if self.webhook_format not in WEBHOOK_FORMATS:
            raise ValueError(f"webhook_format must be one of {WEBHOOK_FORMATS}")


@dataclass(frozen=True)
class Config:
    markets: tuple[Market, ...]
    data: DataSettings = field(default_factory=DataSettings)
    efficiency: EfficiencySettings = field(default_factory=EfficiencySettings)
    mean_reversion: MeanReversionSettings = field(default_factory=MeanReversionSettings)
    continuation: ContinuationSettings = field(default_factory=ContinuationSettings)
    record: RecordSettings = field(default_factory=RecordSettings)
    news: NewsSettings = field(default_factory=NewsSettings)
    alerts: AlertSettings = field(default_factory=AlertSettings)

    def market(self, code: str) -> Market | None:
        return next((m for m in self.markets if m.code == code), None)


def _build(cls, table: dict, where: str):
    known = {f.name for f in fields(cls)}
    unknown = set(table) - known
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)}")
    # TOML arrays arrive as lists; the dataclasses are frozen, so store tuples.
    return cls(**{k: tuple(v) if isinstance(v, list) else v for k, v in table.items()})


SECTIONS = {
    "data": DataSettings,
    "efficiency": EfficiencySettings,
    "mean_reversion": MeanReversionSettings,
    "continuation": ContinuationSettings,
    "record": RecordSettings,
    "news": NewsSettings,
    "alerts": AlertSettings,
}


def parse_config(data: dict, where: str = "config") -> Config:
    unknown = set(data) - set(SECTIONS) - {"markets"}
    if unknown:
        raise ValueError(f"{where}: unknown top-level keys {sorted(unknown)}")
    markets = tuple(_build(Market, m, f"{where} [[markets]] {m.get('code', '?')}")
                    for m in data.get("markets", ()))
    if not markets:
        raise ValueError(f"{where}: no [[markets]]")
    codes = [m.code for m in markets]
    if len(set(codes)) != len(codes):
        raise ValueError(f"{where}: duplicate market codes")
    sections = {name: _build(cls, data.get(name, {}), f"{where} [{name}]")
                for name, cls in SECTIONS.items()}
    return Config(markets=markets, **sections)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    return parse_config(tomllib.loads(path.read_text()), str(path))
