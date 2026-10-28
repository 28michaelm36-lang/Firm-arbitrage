"""Command line: python -m scanner {serve,scan} ..."""
from __future__ import annotations

import argparse
import json

from .alerts import Notifier
from .config import load_config
from .data import DemoProvider, YahooProvider
from .engine import Scanner
from .news import DemoNews, Feed, GoogleNews
from .server import Runner, serve
from .signals import CONTINUATION, LONG, MEAN_REVERSION
from .store import Store

DEFAULT_CONFIG = "configs/scanner.toml"


def build(args, echo: bool = True) -> Scanner:
    config = load_config(args.config)
    if args.demo:
        provider, news, feeds = DemoProvider(seed=args.seed), DemoNews(), []
    else:
        provider = YahooProvider(config.data.range)
        news = GoogleNews() if config.news.google_news else None
        feeds = [Feed(url) for url in config.news.feeds]
    db = args.db or ("scanner-demo.db" if args.demo else "scanner.db")
    return Scanner(config, provider, Store(db), Notifier(config.alerts, echo=echo), news, feeds)


def cmd_serve(args):
    scanner = build(args)
    config = scanner.config
    minutes = args.refresh or (1 if args.demo else config.data.refresh_minutes)
    runner = Runner(scanner, minutes, config.news.refresh_minutes, news=not args.no_news)
    server = serve(scanner, args.host, args.port, runner)
    runner.start()
    source = "DEMO data, synthetic" if args.demo else "Yahoo Finance prices, delayed"
    print(f"Futures scanner on http://{args.host}:{server.server_address[1]}  "
          f"({source}; prices every {minutes:g} min; Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop()
        server.server_close()
        runner.thread.join(timeout=5)
        scanner.store.close()


def _pct(x):
    return "-" if x is None else f"{x:.0%}"


def _record(r):
    return f"{_pct(r['hit_rate'])} ({r['n']})"


def _today(row):
    out = []
    for strategy, tag in ((MEAN_REVERSION, "MR"), (CONTINUATION, "CO")):
        direction = row[strategy]["trigger"]
        if direction:
            out.append(f"{tag} {'long' if direction == LONG else 'short'}{'*' if row['live'] else ''}")
    return ", ".join(out)


def report(overview: dict, alerts) -> str:
    s = overview["settings"]
    lines = [
        f"Futures inefficiency scanner · {overview['source']} · {overview['generated_at']}",
        f"Regime: VR({s['vr_lag']}) on up to {s['vr_window']} daily returns, robust z*. "
        f"MR: z vs {s['lookback']}-day mean at ±{s['entry_z']:g}. CO: {s['channel']}-day channel. "
        f"Record: hit rate {s['horizon']} days after each past trigger.",
        "",
        f"{'code':<5} {'market':<15} {'last':>10} {'chg':>7}  {'regime':<15} {'VR':>5} {'z*':>6} "
        f"{'MR z':>6} {'chan':>5}  {'record MR':>11} {'record CO':>11}  today",
    ]
    rows = [r for r in overview["markets"] if "last" in r]
    rows.sort(key=lambda r: -abs(r["efficiency"]["z"]) if r["efficiency"] else 0)
    for r in rows:
        eff, mr, co = r["efficiency"], r[MEAN_REVERSION], r[CONTINUATION]
        z = "-" if mr["z"] is None else f"{mr['z']:+.2f}"
        lines.append(
            f"{r['code']:<5} {r['name'][:15]:<15} {r['last']:>10,.2f} {r['change_pct']:>+7.1%}  "
            f"{eff['regime'] if eff else '-':<15} {eff['vr'] if eff else 0:>5.2f} {eff['z'] if eff else 0:>+6.2f} "
            f"{z:>6} {_pct(co['position']):>5}  {_record(mr['record']):>11} {_record(co['record']):>11}  "
            f"{_today(r)}")
    for r in overview["markets"]:
        if r.get("error"):
            lines.append(f"{r['code']:<5} {r['name'][:15]:<15} error: {r['error']}")
    if any(r.get("live") for r in rows):
        lines.append("* on the bar that is still forming")
    lines.append("")
    lines.append(f"{len(alerts)} new alert{'s' if len(alerts) != 1 else ''}")
    for a in alerts:
        lines.append(f"  [{a.kind}] {a.title}{' (provisional)' if a.provisional else ''}")
        lines.append(f"      {a.body}")
        if a.url:
            lines.append(f"      {a.url}")
    return "\n".join(lines)


def cmd_scan(args):
    scanner = build(args, echo=False)
    try:
        alerts = scanner.scan_markets()
        if args.news:
            alerts += scanner.scan_news()
        overview = scanner.overview()
    finally:
        scanner.store.close()
    if args.json:
        print(json.dumps({"overview": overview, "new_alerts": [a.as_dict() for a in alerts]}, indent=2))
    else:
        print(report(overview, alerts))


def build_parser():
    p = argparse.ArgumentParser(prog="python -m scanner", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--config", default=DEFAULT_CONFIG)
        sp.add_argument("--demo", action="store_true", help="synthetic prices and headlines, no network")
        sp.add_argument("--seed", type=int, default=7, help="demo data seed")
        sp.add_argument("--db", help="SQLite file for alerts and headlines "
                                     "(default scanner.db, or scanner-demo.db with --demo)")

    s = sub.add_parser("serve", help="run the website and the background scanner")
    common(s)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--refresh", type=float, help="minutes between price scans (overrides the config)")
    s.add_argument("--no-news", action="store_true", help="prices and signals only")
    s.set_defaults(func=cmd_serve)

    c = sub.add_parser("scan", help="scan once and print a report")
    common(c)
    c.add_argument("--news", action="store_true", help="poll headlines too")
    c.add_argument("--json", action="store_true", help="print the overview and new alerts as JSON")
    c.set_defaults(func=cmd_scan)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)
