"""Command line: inspect | backtest | propfirm | signals | boldharvest."""
from __future__ import annotations

import argparse
import csv
import glob
import math
import re
import statistics
import sys
from dataclasses import replace
from datetime import date

from propfirm.policies import parse_policy
from propfirm.rules import load_program

from .boldharvest import (Settings, Tape, all_runs, campaign, parse_signal, study,
                          summarize_runs)
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


# --- Bold + Harvest on real bars ----------------------------------------------

def _tape(args):
    days, keep, info = _load(args)
    if not keep:
        raise SystemExit("no sessions in that date range")
    tape = Tape(days, args.stop, not args.no_time_scale)
    index = {d.date: i for i, d in enumerate(days)}
    cfg = Settings(nq=_costs(args), mnq=_costs(args, "MNQ"), max_eval_days=args.max_eval_days,
                   horizon_days=args.horizon)
    return tape, index[keep[0].date], index[keep[-1].date] + 1, cfg


def cmd_signals(args):
    tape, lo, hi, cfg = _tape(args)
    days = tape.days
    print(f"{hi - lo:,} sessions, {days[lo].date} to {days[hi - 1].date}; one bracket a day "
          f"risking 1 to make {args.rr:g}, stop {args.stop}, costs included.")
    print(f"A coin flip wins {1 / (1 + args.rr):.1%} of these before costs, and its mean R is "
          f"zero before costs.\n")
    parts = [("all", None, None)]
    if args.split:
        parts += [(f"before {args.split}", None, args.split), (f"from {args.split}", args.split, None)]
    head = "| signal | trades | target | stop | time exit |"
    rule = "|---|---:|---:|---:|---:|"
    for label, _, _ in parts:
        head += f" mean R, {label} | t | win eq |"
        rule += "---:|---:|---:|"
    print(head + "\n" + rule)
    for spec in args.signals.split(";"):
        full = study(tape, parse_signal(spec), args.rr, args.risk, cfg, lo)
        full.r = [x for x in full.r if x[0] <= days[hi - 1].date]
        line = (f"| {spec} | {full.n:,} | {full.share('target'):.0%} | "
                f"{full.share('stop'):.0%} | {full.share('time'):.0%} |")
        for _, a, b in parts:
            s = full.part(a, b)
            line += f" {s.mean:+.3f} | {s.t:+.1f} | {s.win_eq:.1%} |"
        print(line)


def _usd(x: float) -> str:
    return f"-${-x:,.0f}" if x < 0 else f"${x:,.0f}"


def _evaluate(program, spec, eval_policy, funded_policy, tape, cfg, lo, hi):
    """(per-evaluation summary, campaign from the first session, campaigns from
    20 start days, runs) for one signal, or None if nothing could be decided."""
    runs = all_runs(program, eval_policy, funded_policy, tape, parse_signal(spec), cfg, lo, hi)
    if not runs:
        return None
    camps = [campaign(runs, lo + o, hi) for o in range(20)]
    return summarize_runs(program, spec, runs.values()), camps[0], camps, runs


def _rank(value: float, baseline: list[float]) -> str:
    return f"{sum(b < value for b in baseline) / len(baseline):.0%}" if baseline else ""


def cmd_boldharvest(args):
    tape, lo, hi, cfg = _tape(args)
    days = tape.days
    programs = [load_program(p) for p in _paths(args.programs)]
    eval_policy, funded_policy = parse_policy(args.eval), parse_policy(args.funded)
    years = (days[hi - 1].date - days[lo].date).days / 365.25
    specs = [s for s in args.signals.split(";") if s]
    print(f"{hi - lo:,} sessions, {days[lo].date} to {days[hi - 1].date} ({years:.1f} years); "
          f"evaluation {args.eval}, funded {args.funded}; stop {args.stop}; "
          f"{args.slip_ticks:g} tick(s) slippage per fill, NQ ${cfg.nq.commission:.2f} and "
          f"MNQ ${cfg.mnq.commission:.2f} a round turn.")
    coins = args.coins
    vs = f" beats this share of {coins} coin flips |" if coins else ""
    per_eval = [f"| program | signal | evaluations | pass | payouts per funded | "
                f"net per funded | EV per evaluation |{vs}",
                "|---|---|---:|---:|---:|---:|---:|" + ("---:|" if coins else "")]
    per_camp = [f"| program | signal | evaluations bought | passed | payouts | trader's share | "
                f"evaluation fees | funded fees | net profit | a year | worst run of net cash |{vs}",
                "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|" + ("---:|" if coins else "")]
    notes, ledger = [], []
    mean = statistics.fmean
    for program in programs:
        base_ev, base_net = [], []
        if coins:
            got = [_evaluate(program, f"coin:seed={k}", eval_policy, funded_policy, tape, cfg, lo, hi)
                   for k in range(1, coins + 1)]
            got = [g for g in got if g]
            base_ev = sorted(g[0].ev for g in got)
            base_net = sorted(g[1].net for g in got)
            cut = lambda xs, q: xs[min(len(xs) - 1, int(q * len(xs)))]
            ss, cs = [g[0] for g in got], [g[1] for g in got]
            pay = mean(mean(a.payouts for a in s.funded) if s.funded else 0.0 for s in ss)
            per_eval.append(
                f"| {program.name} | coin flip, mean of {coins} | {ss[0].n:,} | "
                f"{mean(s.p_pass for s in ss):.1%} | {pay:.2f} | "
                f"{_usd(mean(s.net_per_funded for s in ss))} | {_usd(mean(base_ev))} "
                f"({_usd(cut(base_ev, 0.05))} to {_usd(cut(base_ev, 0.95))}) | |")
            per_camp.append(
                f"| {program.name} | coin flip, mean of {coins} | {mean(c.evaluations for c in cs):,.0f} | "
                f"{mean(c.passes for c in cs):,.0f} | {mean(c.payouts for c in cs):,.0f} | "
                f"{_usd(mean(c.trader_share for c in cs))} | {_usd(-mean(c.eval_fees for c in cs))} | "
                f"{_usd(-mean(c.funded_fees for c in cs))} | {_usd(mean(base_net))} "
                f"({_usd(cut(base_net, 0.05))} to {_usd(cut(base_net, 0.95))}) | "
                f"{_usd(mean(base_net) / years)} | {_usd(-mean(c.worst_drawdown for c in cs))} | |")
        for spec in specs:
            got = _evaluate(program, spec, eval_policy, funded_policy, tape, cfg, lo, hi)
            if got is None:
                per_eval.append(f"| {program.name} | {spec} | 0 | | | | |")
                continue
            s, c, camps, runs = got
            f = s.funded
            payouts = mean(a.payouts for a in f) if f else 0.0
            per_eval.append(
                f"| {program.name} | {spec} | {s.n:,} | {s.p_pass:.1%} | {payouts:.2f} | "
                f"{_usd(s.net_per_funded)} | {_usd(s.ev)} |"
                + (f" {_rank(s.ev, base_ev)} |" if coins else ""))
            per_camp.append(
                f"| {program.name} | {spec} | {c.evaluations:,} | {c.passes:,} | {c.payouts:,} | "
                f"{_usd(c.trader_share)} | {_usd(-c.eval_fees)} | {_usd(-c.funded_fees)} | "
                f"{_usd(c.net)} | {_usd(c.net / years)} | {_usd(-c.worst_drawdown)} |"
                + (f" {_rank(c.net, base_net)} |" if coins else ""))
            nets = sorted(x.net for x in camps)
            by_year = ", ".join(f"{y}: {_usd(v)}" for y, v in sorted(c.by_year(days).items()))
            notes.append(f"{program.name}, {spec}: by year {by_year}. Starting on each of the "
                         f"first 20 sessions instead: {_usd(nets[0])} to {_usd(nets[-1])}. "
                         f"Commissions and slippage paid inside the accounts: "
                         f"{_usd(c.trading_costs)}.")
            ledger.append((program, spec, c))
    print("\nPer evaluation bought, one started on every session:\n")
    print("\n".join(per_eval))
    print("\nNet profit, one account at a time: buy an evaluation, trade it and the funded "
          "account it earns to the end, buy the next one the session after.\n")
    print("\n".join(per_camp))
    print()
    print("\n".join(notes))
    if args.ledger:
        with open(args.ledger, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["program", "signal", "evaluation_bought", "ended", "passed", "eval_days",
                        "funded_days", "trades", "payouts", "gross_payouts", "trader_share",
                        "eval_fees", "funded_fees", "net", "commissions_and_slippage"])
            for program, spec, c in ledger:
                for r in c.runs:
                    a = r.attempt
                    w.writerow([program.name, spec, days[r.start].date, days[r.end].date,
                                int(a.passed), a.eval_days, a.funded_days, r.trades, a.payouts,
                                f"{a.gross:.2f}", f"{a.net:.2f}", f"{a.eval_fees:.2f}",
                                f"{a.funded_fees:.2f}", f"{a.profit:.2f}", f"{r.costs:.2f}"])
        print(f"\nledger written to {args.ledger}")


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

    def bars(sp):
        sp.add_argument("--stop", default="0.25r",
                        help="stop width that sizes each trade: points (50), a share of price "
                             "(0.3%%) or a multiple of the 14-session average range (0.25r, "
                             "the default)")
        sp.add_argument("--no-time-scale", action="store_true",
                        help="keep the stop width fixed all session (default: narrower late in "
                             "the day, so a trade can still finish)")
        sp.add_argument("--signals", default="coin;long;orb:n=15",
                        help="semicolon-separated entry signals: coin, long, short, orb:n=15, "
                             "orb:n=30,fade=1, first:n=5, mom:t=30, mom:t=30,ref=open, gap, "
                             "gap:fade=1, noise")
        sp.add_argument("--max-eval-days", type=int, default=60,
                        help="give up on an evaluation after this many sessions")
        sp.add_argument("--horizon", type=int, default=250, help="funded sessions to follow")

    sp = sub.add_parser("signals", help="does an entry signal have an edge? one bracket a day")
    common(sp)
    bars(sp)
    sp.add_argument("--rr", type=float, default=1.5,
                    help="reward:risk of the test bracket (default 1.5, bold play on a $2,000 "
                         "drawdown and $3,000 target)")
    sp.add_argument("--risk", type=float, default=2000.0, help="dollar risk of the test bracket")
    sp.add_argument("--split", type=_date, help="also report before and from this date")
    sp.set_defaults(func=cmd_signals)

    sp = sub.add_parser("boldharvest",
                        help="Bold + Harvest on real bars: net profit after payouts and costs")
    common(sp)
    bars(sp)
    sp.add_argument("--programs", nargs="+", default=["configs/programs/*_50k.toml"])
    sp.add_argument("--eval", default="bold", help='evaluation policy, e.g. "bold:max_risk_frac=0.5"')
    sp.add_argument("--funded", default="harvest", help='funded policy, e.g. "harvest:min_odds=0.5"')
    sp.add_argument("--coins", type=int, default=0, metavar="N",
                    help="also run N coin-flip entries as the no-edge baseline, and show where "
                         "each signal falls among them (default 0; 100 takes a few minutes)")
    sp.add_argument("--ledger", help="write every evaluation of the campaign to this CSV")
    sp.set_defaults(func=cmd_boldharvest)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)
