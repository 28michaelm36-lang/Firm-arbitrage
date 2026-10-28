"""Command line: inspect | backtest | propfirm."""
from __future__ import annotations

import argparse
import csv
import glob
import math
import re
import sys
from dataclasses import replace
from datetime import date

from propfirm.rules import load_program

from .data import daily_sigma, load_days, rescale
from .metrics import HEADER, by_year, correlation, daily_pnl, row, summarize
from .replay import REPLAY_HEADER, day_paths, replay, replay_row
from .strategies import INSTRUMENT_COSTS, Costs, make, warmup_end


def _paths(patterns):
    out = []
    for p in patterns:
        hits = sorted(glob.glob(p))
        if not hits:
            raise SystemExit(f"no files match {p!r}")
        out.extend(hits)
    return out


def _load(args):
    days, info = load_days(_paths(args.data), args.data_tz, args.label,
                           adjust_rolls=not args.no_roll_adjust)
    if args.rescale:
        days = rescale(days, args.rescale)
    start = warmup_end(days)
    keep = [d for d in days[start:]
            if (args.start is None or d.date >= args.start) and (args.end is None or d.date < args.end)]
    return days, keep, info


def _costs(args, instrument="NQ") -> Costs:
    base = INSTRUMENT_COSTS[instrument]
    commission = args.commission if instrument == "NQ" else args.mnq_commission
    return replace(base, slip_ticks=args.slip_ticks,
                   commission=base.commission if commission is None else commission)


def _trades(spec: str, days, costs):
    """Trades for a strategy, or for several joined with '+' (a portfolio)."""
    out = []
    for name in spec.split("+"):
        out.extend(make(name).run(days, costs))
    return sorted(out, key=lambda t: (t.day, t.entry_index))


def cmd_inspect(args):
    days, keep, info = _load(args)
    print(info.format())
    if days:
        print(f"usable sessions       {sum(d.usable for d in days):,}, "
              f"{days[0].date} to {days[-1].date}")
        gaps = sorted((abs(d.open - d.prev_close) / d.prev_close, d.date, d.open - d.prev_close)
                      for d in days if d.prev_close)[-5:]
        print("largest opening gaps (after roll adjustment):")
        for g, d, pts in reversed(gaps):
            print(f"    {d}  {pts:+.2f} points ({g:.2%})")


def cmd_backtest(args):
    days, keep, info = _load(args)
    costs = _costs(args)
    print(f"{len(keep):,} sessions, {keep[0].date} to {keep[-1].date}; 1 NQ contract, "
          f"${costs.commission:.2f} round turn, {costs.slip_ticks:g} tick(s) slippage per fill\n")
    print(HEADER)
    series = {}
    all_trades = {}
    for spec in args.strategies.split(","):
        trades = _trades(spec, days, costs)
        all_trades[spec] = trades
        print(row(spec, summarize(trades, keep, costs)))
        series[spec] = [p for _, p in daily_pnl(trades, keep, costs)]
    if args.split:
        for label, part in (("before", [d for d in keep if d.date < args.split]),
                            ("from", [d for d in keep if d.date >= args.split])):
            if part:
                print(f"\n{label} {args.split} ({len(part)} sessions)\n\n{HEADER}")
                for spec, trades in all_trades.items():
                    print(row(spec, summarize(trades, part, costs)))
    if args.by_year:
        for spec, trades in all_trades.items():
            print(f"\n{spec} by year\n\n{HEADER.replace('strategy', 'year')}")
            for y, s in by_year(trades, keep, costs).items():
                print(row(str(y), s))
    if len(series) > 1:
        names = list(series)
        print("\ncorrelation of daily P&L")
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                print(f"    {a} / {b}: {correlation(series[a], series[b]):+.2f}")
    if args.trades:
        with open(args.trades, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["strategy", "day", "side", "entry_time", "entry", "exit_time", "exit",
                        "reason", "points", "net_usd_1nq"])
            for spec, trades in all_trades.items():
                for t in trades:
                    w.writerow([t.strategy, t.day, t.side, t.entry_time, f"{t.entry:.2f}",
                                t.exit_time, f"{t.exit:.2f}", t.reason, f"{t.points:.2f}",
                                f"{costs.dollars(t.points):.2f}"])
        print(f"\ntrades written to {args.trades}")


_SIZE = re.compile(r"^(\d+(?:\.\d+)?)(NQ|MNQ)$|^(risk|vol)(\d+(?:\.\d+)?)$", re.I)


def sizing(spec: str, max_minis: float, days=None):
    """(instrument, contracts per trade) for a size, capped at the program's
    contract limit:

    * '2MNQ', '1NQ': fixed.
    * 'risk300': MNQ contracts risking about $300 to the initial stop (orb).
    * 'vol1000': MNQ contracts whose one-standard-deviation daily move is
      about $1,000, from the last 14 sessions' volatility. This is the noise
      area paper's own sizing; 'vol1000' is its 2% daily target on $50,000.
      Rounded down, so the most volatile sessions are not traded.
    """
    m = _SIZE.match(spec)
    if not m:
        raise SystemExit(f"bad size {spec!r}: use e.g. 2MNQ, 1NQ, risk300 or vol1000")
    if m.group(3):
        budget = float(m.group(4))
        cap = math.floor(max_minis * 10 + 1e-9)
        mnq = INSTRUMENT_COSTS["MNQ"].point_value
        if m.group(3).lower() == "risk":
            def contracts(t):
                if t.stop_distance is None:
                    raise SystemExit("risk sizing needs a strategy with a stop (orb)")
                return min(cap, math.floor(budget / (t.stop_distance * mnq)))
            return "MNQ", contracts
        move = {d.date: daily_sigma(days, i) * (d.prev_close or d.open)
                for i, d in enumerate(days)}

        def contracts(t):
            m = move.get(t.day, math.nan)
            return min(cap, math.floor(budget / (m * mnq))) if m > 0 else 0
        return "MNQ", contracts
    q, inst = float(m.group(1)), m.group(2).upper()
    cap = max_minis * (10 if inst == "MNQ" else 1)
    if q > cap:
        print(f"note: {spec} capped at the program's {cap:g} contracts", file=sys.stderr)
    return inst, min(q, cap)


def cmd_propfirm(args):
    days, keep, info = _load(args)
    programs = [load_program(p) for p in _paths(args.programs)]
    every = "each session" if args.step == 1 else f"every {args.step} sessions"
    print(f"{len(keep):,} sessions, {keep[0].date} to {keep[-1].date}; an evaluation starts on "
          f"{every}; {args.slip_ticks:g} tick(s) slippage per fill\n")
    print(REPLAY_HEADER)
    for spec in args.strategies.split(","):
        base = _costs(args)
        trades = _trades(spec, days, base)
        for program in programs:
            for size in args.sizes.split(","):
                inst, contracts = sizing(size, program.evaluation.max_contracts, days)
                costs = _costs(args, inst)
                paths = day_paths(trades, keep, costs, contracts)
                r = replay(program, paths, spec, max_eval_days=args.max_eval_days,
                           horizon_days=args.horizon, step=args.step)
                print(replay_row(program.name, spec, size, r))


def _date(s: str) -> date:
    return date.fromisoformat(s)


def build_parser():
    p = argparse.ArgumentParser(prog="python -m intraday",
                                description="Backtest published intraday strategies on 1-minute NQ bars.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("data", nargs="+", help="CSV files or glob patterns")
        sp.add_argument("--data-tz", default=None,
                        help="zone of the timestamps (default: from the column name, e.g. "
                             "'timestamp ET', else UTC)")
        sp.add_argument("--label", default="auto", choices=("auto", "start", "end"),
                        help="timestamps mark the start or end of each bar (default: detect)")
        sp.add_argument("--no-roll-adjust", action="store_true",
                        help="do not detect and adjust contract rolls")
        sp.add_argument("--rescale", type=float, default=None, metavar="LEVEL",
                        help="scale each session so its previous close is LEVEL (compare "
                             "years at today's NQ price level)")
        sp.add_argument("--start", type=_date, help="first session to score (YYYY-MM-DD)")
        sp.add_argument("--end", type=_date, help="score sessions before this date")
        sp.add_argument("--slip-ticks", type=float, default=1.0,
                        help="slippage per market or stop fill, in ticks (default 1)")
        sp.add_argument("--commission", type=float, default=None,
                        help="NQ round turn per contract incl. fees (default 4.50)")
        sp.add_argument("--mnq-commission", type=float, default=None,
                        help="MNQ round turn per contract incl. fees (default 1.50)")

    sp = sub.add_parser("inspect", help="check how a data file was read")
    common(sp)
    sp.set_defaults(func=cmd_inspect)

    sp = sub.add_parser("backtest", help="statistics per strategy, per 1 NQ contract")
    common(sp)
    sp.add_argument("--strategies", default="noise,noise-base,lasthalf,lasthalf-gao,orb",
                    help="comma-separated; join with + for a portfolio, e.g. noise+lasthalf")
    sp.add_argument("--split", type=_date, help="also report before and from this date")
    sp.add_argument("--by-year", action="store_true")
    sp.add_argument("--trades", help="write every trade to this CSV")
    sp.set_defaults(func=cmd_backtest)

    sp = sub.add_parser("propfirm", help="replay the trades through prop-firm programs")
    common(sp)
    sp.add_argument("--programs", nargs="+", default=["configs/programs/*.toml"])
    sp.add_argument("--strategies", default="noise,lasthalf,orb")
    sp.add_argument("--sizes", default="2MNQ,5MNQ,vol1000",
                    help="comma-separated: 2MNQ, 1NQ, risk300 (MNQ risking ~$300 per trade) "
                         "or vol1000 (MNQ with a ~$1,000 daily standard deviation)")
    sp.add_argument("--max-eval-days", type=int, default=60,
                    help="give up on an evaluation after this many sessions")
    sp.add_argument("--horizon", type=int, default=250, help="funded sessions to follow")
    sp.add_argument("--step", type=int, default=1,
                    help="start an evaluation every STEP sessions (default every one)")
    sp.set_defaults(func=cmd_propfirm)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)
