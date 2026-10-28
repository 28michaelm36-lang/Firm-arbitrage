"""A scan cycle: fetch bars and headlines, find what is new, store it and send alerts."""
from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone

from .alerts import Alert, Notifier
from .analysis import MarketView, analyze, chart, summary
from .config import Config
from .news import Feed, Match, NewsItem, Scorer
from .signals import LABELS, LONG, MEAN_REVERSION, Trigger
from .store import Store


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _price(x: float) -> str:
    return f"{x:,.3f}" if abs(x) < 10 else f"{x:,.2f}" if abs(x) < 2000 else f"{x:,.0f}"


def _error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


class Scanner:
    def __init__(self, config: Config, provider, store: Store, notifier: Notifier,
                 news_source=None, feeds: Sequence[Feed] = (), clock: Callable[[], datetime] = utcnow,
                 log=None):
        self.config = config
        self.provider = provider
        self.store = store
        self.notifier = notifier
        self.news_source = news_source
        self.feeds = list(feeds)
        self.clock = clock
        self.log = log or (lambda line: print(line, file=sys.stderr, flush=True))
        self.scorer = Scorer(config.markets, config.news)
        # The schedule the page shows; the server's runner may override the config.
        self.refresh_minutes = config.data.refresh_minutes
        self.news_refresh_minutes: float | None = config.news.refresh_minutes
        self.views: dict[str, MarketView] = {}
        self.errors: dict[str, str] = {}
        self.news_errors: dict[str, str] = {}
        self.last_scan: datetime | None = None
        self.last_news: datetime | None = None
        self.status = "starting"
        self.lock = threading.Lock()

    # ---- prices and signals ------------------------------------------------

    def scan_markets(self) -> list[Alert]:
        self.status = "scanning"
        sent = []
        for market in self.config.markets:
            try:
                view = analyze(market, self.provider.history(market.symbol), self.config)
            except Exception as exc:     # one bad symbol must not stop the others
                with self.lock:
                    self.errors[market.code] = _error(exc)
                self.log(f"[scan] {market.code} ({market.symbol}): {_error(exc)}")
                continue
            with self.lock:
                self.views[market.code] = view
                self.errors.pop(market.code, None)
            sent += self._signal_alerts(view)
        self.last_scan = self.clock()
        self.status = "idle"
        return sent

    def _signal_alerts(self, view: MarketView) -> list[Alert]:
        indices = {view.settled_index()}
        if view.series.live and self.config.data.alert_on_live_bar:
            indices.add(view.last)
        sent = []
        for i in sorted(i for i in indices if i > 0):
            for trigger in view.triggers_at(i):
                provisional = view.series.live and i == view.last
                stored = self.store.add_alert(self.signal_alert(view, trigger, provisional))
                if stored:
                    self.notifier.send(stored)
                    sent.append(stored)
        return sent

    def signal_alert(self, view: MarketView, t: Trigger, provisional: bool) -> Alert:
        m = view.market
        side = "long" if t.direction == LONG else "short"
        day = f"{t.day.day} {t.day:%b}"
        if t.strategy == MEAN_REVERSION:
            what = (f"{day} close {_price(t.price)}, z {t.level:+.2f} vs the "
                    f"{self.config.mean_reversion.lookback}-day mean (threshold {t.direction * -view.entry_z:+.1f}).")
        else:
            edge = "high" if t.direction == LONG else "low"
            what = (f"{day} close {_price(t.price)} {'above' if t.direction == LONG else 'below'} the prior "
                    f"{self.config.continuation.channel}-day {edge} of {_price(t.level)}.")
        eff = view.efficiency
        regime = ("Regime unknown (not enough history)." if eff is None else
                  f"Regime {eff.regime} (VR({eff.q}) {eff.vr:.2f}, z* {eff.z:+.2f}): "
                  f"{view.agreement(t.strategy)}.")
        rec = view.records[t.strategy]
        if rec.n:
            lo, hi = rec.interval
            history = (f"Record: {rec.hit_rate:.0%} of {rec.n} moved the right way after {rec.horizon} days "
                       f"(95% {lo:.0%}-{hi:.0%}), mean {rec.mean_atr:+.2f} ATR.")
        else:
            history = "Record: no past triggers to judge."
        live = " Bar still forming." if provisional else ""
        return Alert(
            key=f"signal:{m.code}:{t.strategy}:{t.direction}:{t.day.isoformat()}",
            kind="signal", markets=(m.code,),
            title=f"{m.code} {m.name}: {LABELS[t.strategy].lower()} {side}",
            body=f"{what} {regime} {history}{live}",
            strategy=t.strategy, direction=t.direction, provisional=provisional, created_at=self.clock())

    # ---- news ----------------------------------------------------------------

    def scan_news(self) -> list[Alert]:
        now = self.clock()
        items: dict[str, NewsItem] = {}
        errors = {}
        if self.news_source is not None:
            for market in self.config.markets:
                try:
                    for item in self.news_source.fetch(market):
                        items.setdefault(item.id, item)
                except Exception as exc:
                    errors[market.code] = _error(exc)
        for feed in self.feeds:
            try:
                for item in feed.fetch():
                    items.setdefault(item.id, item)
            except Exception as exc:
                errors[feed.url] = _error(exc)
        for key, message in errors.items():
            self.log(f"[news] {key}: {message}")

        sent = []
        for item in sorted(items.values(), key=lambda i: i.published or now):
            matches = self.scorer.score(item)
            for match in matches:
                self.store.add_news(item, match, now)
            hot = [m for m in matches if m.score >= self.config.news.min_score]
            if hot and self.scorer.is_fresh(item, now):
                stored = self.store.add_alert(self.news_alert(item, hot, now))
                if stored:
                    self.notifier.send(stored)
                    sent.append(stored)
        self.store.prune(now)
        with self.lock:
            self.news_errors = errors
        self.last_news = now
        return sent

    def news_alert(self, item: NewsItem, matches: list[Match], now: datetime) -> Alert:
        best = matches[0]
        codes = tuple(m.market for m in matches)
        parts = [item.source or "unknown source"]
        if item.published:
            parts.append(item.published.astimezone(timezone.utc).strftime("%d %b %H:%M UTC"))
        parts.append(f"score {best.score:g}")
        parts.append("matched " + ", ".join(best.keywords))
        if best.impact:
            parts.append("impact " + ", ".join(best.impact))
        if best.tone:
            parts.append(f"headline tone {best.tone:+d}")
        return Alert(key=f"news:{item.id}", kind="news", markets=codes,
                     title=f"{'/'.join(codes)}: {item.title}", body=" · ".join(parts),
                     url=item.link, created_at=now)

    # ---- views for the page ----------------------------------------------------

    def overview(self) -> dict:
        now = self.clock()
        with self.lock:
            views, errors, news_errors = dict(self.views), dict(self.errors), dict(self.news_errors)
        counts = self.store.news_counts(now - timedelta(hours=24))
        rows = []
        for market in self.config.markets:
            view = views.get(market.code)
            row = summary(view) if view else {"code": market.code, "name": market.name,
                                              "sector": market.sector, "symbol": market.symbol}
            row["error"] = errors.get(market.code)
            row["news_24h"] = counts.get(market.code, 0)
            rows.append(row)
        c = self.config
        return {
            "generated_at": now.isoformat(timespec="seconds"),
            "source": getattr(self.provider, "name", "unknown"),
            "demo": getattr(self.provider, "name", "") == "demo",
            "status": self.status,
            "last_scan": self.last_scan.isoformat(timespec="seconds") if self.last_scan else None,
            "last_news": self.last_news.isoformat(timespec="seconds") if self.last_news else None,
            "refresh_minutes": self.refresh_minutes,
            "news_refresh_minutes": self.news_refresh_minutes,
            "settings": {"lookback": c.mean_reversion.lookback, "entry_z": c.mean_reversion.entry_z,
                         "channel": c.continuation.channel, "horizon": c.record.horizon_days,
                         "atr_days": c.record.atr_days, "vr_lag": c.efficiency.lag,
                         "vr_window": c.efficiency.window, "z_threshold": c.efficiency.z_threshold,
                         "min_news_score": c.news.min_score},
            "markets": rows,
            "news_errors": news_errors,
        }

    def detail(self, code: str) -> dict | None:
        market = self.config.market(code)
        if market is None:
            return None
        with self.lock:
            view = self.views.get(code)
            error = self.errors.get(code)
        out = {"code": code, "name": market.name, "error": error,
               "news": self.store.news(code, limit=30)}
        if view:
            out["summary"] = summary(view)
            out["chart"] = chart(view)
        return out

