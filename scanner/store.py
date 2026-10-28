"""SQLite persistence for alerts and headlines. A key is stored once, so alerts never repeat."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from .alerts import Alert
from .news import Match, NewsItem

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    markets TEXT NOT NULL,          -- ",ZL,ZM," so LIKE '%,ZL,%' finds one market
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    strategy TEXT NOT NULL DEFAULT '',
    direction INTEGER NOT NULL DEFAULT 0,
    provisional INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS news (
    item_id TEXT NOT NULL,
    market TEXT NOT NULL,
    title TEXT NOT NULL,
    link TEXT NOT NULL,
    source TEXT NOT NULL,
    published TEXT,
    fetched_at TEXT NOT NULL,
    score REAL NOT NULL,
    keywords TEXT NOT NULL,
    impact TEXT NOT NULL,
    tone INTEGER NOT NULL,
    PRIMARY KEY (item_id, market)
);
CREATE INDEX IF NOT EXISTS news_by_market ON news (market, published);
"""


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds") if t else None


class Store:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock, self.db:
            self.db.executescript(SCHEMA)

    def close(self):
        with self.lock:
            self.db.close()

    def add_alert(self, alert: Alert) -> Alert | None:
        """Store the alert and return it with its id, or None if its key was already stored."""
        with self.lock, self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO alerts (key, created_at, kind, markets, title, body, url,"
                " strategy, direction, provisional) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (alert.key, _iso(alert.created_at), alert.kind, f",{','.join(alert.markets)},",
                 alert.title, alert.body, alert.url, alert.strategy, alert.direction, int(alert.provisional)))
            if cur.rowcount == 0:
                return None
            return alert.with_id(cur.lastrowid)

    def alerts(self, after: int = 0, limit: int = 100, market: str | None = None,
               kind: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM alerts WHERE id > ?", [after]
        if market:
            sql += " AND markets LIKE ?"
            args.append(f"%,{market},%")
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            rows = self.db.execute(sql, args).fetchall()
        return [dict(row, markets=row["markets"].strip(",").split(","), provisional=bool(row["provisional"]))
                for row in rows]

    def add_news(self, item: NewsItem, match: Match, fetched_at: datetime) -> bool:
        with self.lock, self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO news VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item.id, match.market, item.title, item.link, item.source, _iso(item.published),
                 _iso(fetched_at), match.score, json.dumps(match.keywords), json.dumps(match.impact),
                 match.tone))
            return cur.rowcount == 1

    def news(self, market: str | None = None, limit: int = 50, since: datetime | None = None) -> list[dict]:
        sql, args = "SELECT * FROM news WHERE 1 = 1", []
        if market:
            sql += " AND market = ?"
            args.append(market)
        if since:
            sql += " AND published >= ?"
            args.append(_iso(since))
        sql += " ORDER BY published DESC, score DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            rows = self.db.execute(sql, args).fetchall()
        return [dict(row, keywords=json.loads(row["keywords"]), impact=json.loads(row["impact"]))
                for row in rows]

    def news_counts(self, since: datetime) -> dict[str, int]:
        with self.lock:
            rows = self.db.execute("SELECT market, COUNT(*) AS n FROM news WHERE published >= ? GROUP BY market",
                                   (_iso(since),)).fetchall()
        return {row["market"]: row["n"] for row in rows}

    def prune(self, now: datetime, news_days: int = 14, alert_days: int = 90):
        with self.lock, self.db:
            self.db.execute("DELETE FROM news WHERE fetched_at < ?", (_iso(now - timedelta(days=news_days)),))
            self.db.execute("DELETE FROM alerts WHERE created_at < ?", (_iso(now - timedelta(days=alert_days)),))
