import contextlib
import io
import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

from scanner.alerts import Alert, Notifier, webhook_request
from scanner.cli import main
from scanner.config import AlertSettings, load_config, parse_config
from scanner.data import Bar, DemoProvider, Series, YahooProvider, parse_yahoo_chart
from scanner.engine import Scanner
from scanner.news import DemoNews, NewsItem, item_id
from scanner.server import serve
from scanner.store import Store

NOW = datetime(2026, 10, 28, 18, tzinfo=timezone.utc)
CONFIG = parse_config({
    "markets": [{"code": "ZL", "symbol": "ZL=F", "name": "Soybean oil", "keywords": ["soybean oil"]},
                {"code": "CC", "symbol": "CC=F", "name": "Cocoa", "keywords": ["cocoa"]}],
    "efficiency": {"window": 50},
    "news": {"impact_terms": ["EPA", "record"], "min_score": 4, "max_age_hours": 36},
})


def yahoo_payload(closes, live_now=None):
    start = datetime(2026, 9, 1, 5, tzinfo=timezone.utc)      # 00:00 in Chicago (UTC-5)
    stamps = [int((start + timedelta(days=i)).timestamp()) for i in range(len(closes))]
    period = {}
    if live_now:
        period = {"regular": {"start": int(live_now.timestamp()) - 3600, "end": int(live_now.timestamp()) + 3600}}
    return {"chart": {"error": None, "result": [{
        "meta": {"symbol": "ZL=F", "currency": "USX", "gmtoffset": -18000, "currentTradingPeriod": period},
        "timestamp": stamps,
        "indicators": {"quote": [{"open": closes, "high": [c and c + 1 for c in closes],
                                  "low": [c and c - 1 for c in closes], "close": closes,
                                  "volume": [100] * len(closes)}]}}]}}


def breakout_series(symbol="ZL=F", live=False):
    """60 quiet bars, then a close far above the range: a breakout and a z-score extreme."""
    closes = [100 + (0.5 if i % 2 else -0.5) for i in range(60)] + [110]
    day = date(2026, 8, 1)
    bars = tuple(Bar(day + timedelta(days=i), c, c + 0.6, c - 0.6, c) for i, c in enumerate(closes))
    return Series(symbol, bars, live=live, source="stub")


class Stub:
    name = "stub"

    def __init__(self, fail=(), live=False):
        self.fail, self.live = fail, live

    def history(self, symbol):
        if symbol in self.fail:
            raise OSError("connection refused")
        return breakout_series(symbol, self.live)


class StubNews:
    def __init__(self, items):
        self.items = items

    def fetch(self, market):
        return self.items


STORES = []


def store():
    STORES.append(Store())
    return STORES[-1]


def tearDownModule():
    for s in STORES:
        s.close()


def scanner(provider=None, news=None):
    notifier = Notifier(AlertSettings(), log=lambda line: None)
    return Scanner(CONFIG, provider or Stub(), store(), notifier, news, clock=lambda: NOW, log=lambda line: None)


class Yahoo(unittest.TestCase):
    def test_parse(self):
        payload = yahoo_payload([10.0, None, 11.0, 12.0])
        # Yahoo can repeat today's bar with the live quote; the later one wins.
        payload["chart"]["result"][0]["timestamp"].append(payload["chart"]["result"][0]["timestamp"][-1] + 3600)
        payload["chart"]["result"][0]["indicators"]["quote"][0]["close"].append(12.5)
        series = parse_yahoo_chart(payload, now=NOW)
        self.assertEqual([b.close for b in series.bars], [10.0, 11.0, 12.5])
        self.assertEqual(series.bars[0].day, date(2026, 9, 1))
        self.assertEqual(series.currency, "USX")
        self.assertFalse(series.live)

    def test_live_bar(self):
        closes = [10.0] * 58
        now = datetime(2026, 9, 1, 5, tzinfo=timezone.utc) + timedelta(days=57, hours=14)
        self.assertTrue(parse_yahoo_chart(yahoo_payload(closes, live_now=now), now=now).live)

    def test_falls_back_to_second_host(self):
        urls = []

        def get(url):
            urls.append(url)
            if "query1" in url:
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
            return json.dumps(yahoo_payload([10.0, 11.0])).encode()
        self.assertEqual(len(YahooProvider(get=get).history("ZL=F").bars), 2)
        self.assertEqual([u.split("/")[2] for u in urls], ["query1.finance.yahoo.com", "query2.finance.yahoo.com"])
        self.assertIn("/v8/finance/chart/ZL%3DF?range=2y&interval=1d", urls[0])

    def test_error(self):
        with self.assertRaisesRegex(ValueError, "No data found"):
            parse_yahoo_chart({"chart": {"result": None, "error": {"code": "Not Found",
                                                                   "description": "No data found"}}})


class Config(unittest.TestCase):
    def test_default_watchlist(self):
        config = load_config("configs/scanner.toml")
        self.assertEqual(len(config.markets), 16)
        self.assertEqual(config.markets[0].code, "ZL")
        self.assertIn("biodiesel", config.markets[0].keywords)
        self.assertIsInstance(config.news.impact_terms, tuple)

    def test_rejects_unknown_keys(self):
        with self.assertRaisesRegex(ValueError, "unknown keys"):
            parse_config({"markets": [{"code": "ZL", "symbol": "ZL=F", "name": "x"}],
                          "mean_reversion": {"lookbak": 20}})
        with self.assertRaisesRegex(ValueError, "webhook_format"):
            AlertSettings(webhook_format="email")

    def test_query_from_keywords(self):
        self.assertEqual(CONFIG.markets[0].query(), '"soybean oil"')


class Signals(unittest.TestCase):
    def test_alerts_fire_once(self):
        s = scanner()
        first = s.scan_markets()
        self.assertEqual(sorted((a.markets, a.strategy, a.direction) for a in first),
                         [(("CC",), "continuation", 1), (("CC",), "mean_reversion", -1),
                          (("ZL",), "continuation", 1), (("ZL",), "mean_reversion", -1)])
        self.assertIn("above the prior 20-day high", next(a.body for a in first if a.strategy == "continuation"))
        self.assertEqual(s.scan_markets(), [])
        self.assertEqual(len(s.store.alerts()), 4)

    def test_live_bar_is_provisional(self):
        alerts = scanner(Stub(live=True)).scan_markets()
        self.assertTrue(alerts and all(a.provisional for a in alerts))
        self.assertIn("Bar still forming", alerts[0].body)

    def test_settled_only(self):
        config = parse_config({"markets": [{"code": "ZL", "symbol": "ZL=F", "name": "x"}],
                               "data": {"alert_on_live_bar": False}})
        s = Scanner(config, Stub(live=True), store(), Notifier(AlertSettings(), log=lambda line: None),
                    log=lambda line: None)
        self.assertEqual(s.scan_markets(), [])     # the breakout is on the forming bar

    def test_one_failing_market(self):
        s = scanner(Stub(fail={"CC=F"}))
        self.assertEqual({a.markets[0] for a in s.scan_markets()}, {"ZL"})
        rows = {r["code"]: r for r in s.overview()["markets"]}
        self.assertIn("connection refused", rows["CC"]["error"])
        self.assertEqual(rows["ZL"]["continuation"]["trigger"], 1)
        self.assertEqual(rows["ZL"]["mean_reversion"]["zone"], -1)


class News(unittest.TestCase):
    def item(self, title, hours):
        return NewsItem(item_id(title), title, "https://example.com/a", "Wire", NOW - timedelta(hours=hours))

    def test_alerts(self):
        items = [self.item("Soybean oil hits a record as EPA acts", 2),      # 3 + 2: alert
                 self.item("Soybean oil steady", 2),                          # 3: stored, no alert
                 self.item("Cocoa sets a record after EPA ruling", 48),       # too old to alert
                 self.item("EPA record", 1)]                                  # no market keyword
        s = scanner(news=StubNews(items))
        alerts = s.scan_news()
        self.assertEqual([a.title for a in alerts], ["ZL: Soybean oil hits a record as EPA acts"])
        self.assertIn("impact record, EPA", alerts[0].body)
        self.assertEqual(alerts[0].url, "https://example.com/a")
        self.assertEqual(len(s.store.news("ZL")), 2)
        self.assertEqual(len(s.store.news("CC")), 1)
        self.assertEqual(s.scan_news(), [])

    def test_failing_source(self):
        class Broken:
            def fetch(self, market):
                raise OSError("blocked")
        s = scanner(news=Broken())
        self.assertEqual(s.scan_news(), [])
        self.assertEqual(set(s.news_errors), {"ZL", "CC"})


class Delivery(unittest.TestCase):
    alert = Alert(key="k", kind="news", markets=("ZL",), title="ZL: Soybean oil — EPA", body="Wire · score 5",
                  url="https://example.com/a", created_at=NOW)

    def test_ntfy(self):
        req = webhook_request(self.alert, AlertSettings("https://ntfy.sh/my-topic", "ntfy"))
        self.assertEqual(req.full_url, "https://ntfy.sh/")
        body = json.loads(req.data)
        self.assertEqual((body["topic"], body["title"], body["click"]), ("my-topic", self.alert.title, self.alert.url))

    def test_slack_discord_json(self):
        slack = json.loads(webhook_request(self.alert, AlertSettings("https://hooks.slack.test/x", "slack")).data)
        self.assertTrue(slack["text"].startswith("*ZL: Soybean oil — EPA*"))
        long = Alert(key="k", kind="news", markets=("ZL",), title="t", body="x" * 5000)
        discord = json.loads(webhook_request(long, AlertSettings("https://discord.test/x", "discord")).data)
        self.assertEqual(len(discord["content"]), 2000)
        raw = json.loads(webhook_request(self.alert, AlertSettings("https://example.test/hook")).data)
        self.assertEqual(raw["markets"], ["ZL"])

    def test_failure_is_logged(self):
        lines = []

        def post(request):
            raise urllib.error.URLError("down")
        Notifier(AlertSettings("https://example.test/hook"), post=post, log=lines.append).send(self.alert)
        self.assertTrue(any("delivery failed" in line for line in lines))

    def test_store_dedup_and_filter(self):
        db = store()
        self.assertIsNotNone(db.add_alert(self.alert))
        self.assertIsNone(db.add_alert(self.alert))
        self.assertEqual(len(db.alerts(market="ZL")), 1)
        self.assertEqual(db.alerts(market="Z"), [])
        self.assertEqual(db.alerts(kind="signal"), [])


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scanner = Scanner(load_config("configs/scanner.toml"), DemoProvider(), store(),
                              Notifier(AlertSettings(), log=lambda line: None), DemoNews(),
                              log=lambda line: None)
        cls.scanner.scan_markets()
        cls.scanner.scan_news()
        cls.server = serve(cls.scanner, "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as response:
            return response.status, response.headers, response.read()

    def status(self, path, **kw):
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, **kw)) as response:
                return response.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_page(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Futures inefficiency scanner", body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(self.get("/app.js")[0], 200)

    def test_api(self):
        overview = json.loads(self.get("/api/overview")[2])
        self.assertTrue(overview["demo"])
        self.assertEqual(len(overview["markets"]), 16)
        self.assertTrue(all(m["efficiency"] for m in overview["markets"]))
        detail = json.loads(self.get("/api/markets/ZL")[2])
        self.assertEqual(len(detail["chart"]["close"]), 260)
        self.assertTrue(detail["news"])
        alerts = json.loads(self.get("/api/alerts?limit=5")[2])["alerts"]
        self.assertTrue(0 < len(alerts) <= 5)
        after = json.loads(self.get(f"/api/alerts?after={alerts[0]['id']}")[2])["alerts"]
        self.assertEqual(after, [])

    def test_errors_and_refresh(self):
        self.assertEqual(self.status("/api/markets/XX"), 404)
        self.assertEqual(self.status("/../scanner/server.py"), 404)
        self.assertEqual(self.status("/api/alerts?after=x"), 400)
        self.assertEqual(self.status("/api/refresh", method="POST"), 403)
        self.assertEqual(self.status("/api/refresh", method="POST", headers={"X-Scanner": "1"}), 503)


class Cli(unittest.TestCase):
    def test_scan_report(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["scan", "--demo", "--news", "--db", ":memory:"])
        text = out.getvalue()
        self.assertIn("Futures inefficiency scanner · demo", text)
        self.assertIn("Soybean oil", text)
        self.assertIn("new alert", text)


if __name__ == "__main__":
    unittest.main()
