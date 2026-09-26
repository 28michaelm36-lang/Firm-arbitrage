"""Command line: python -m propfirm {simulate,compare,optimize,manual,backtest} ..."""
from __future__ import annotations

import argparse
import csv
import glob
import math
import random
import statistics
import sys
from datetime import date, timedelta

from .backtest import (DEFAULT_SUITE, Config, backtest, evaluate, format_results, load_sessions,
                       parse_filters)
from .bracket import tilt_from_winrate
from .dp import solve_goal
from .instruments import INSTRUMENTS
from .lifecycle import run_funded, simulate
from .policies import Asymmetry1, Bold, parse_policy
from .rules import load_program
from .stats import attempts_to_rule_out_luck, binomial_tail, wilson_interval

STRATEGIES = {
    # name: (evaluation policy, funded policy)
    "asymmetry1": ("asymmetry1", "asymmetry1"),
    "asymmetry1+": ("asymmetry1:land_exactly=1", "asymmetry1"),
    "asymmetry2": ("bold", "harvest"),
    "grind": ("asymmetry1:risk=167,reward=250,trades_per_day=2",
              "asymmetry1:risk=167,reward=250,trades_per_day=2"),
}


def _instrument(args):
    return INSTRUMENTS[args.instrument].with_overrides(args.round_turn, args.stop_ticks)


def _edge(args):
    return f"win rate {args.winrate:.3f} at reward:risk {args.winrate_rr:g}"


def _program(path, args):
    program = load_program(path)
    return program.with_eval_fee(args.fee) if args.fee is not None else program


def _policies(args, strategy):
    eval_spec, funded_spec = STRATEGIES[strategy] if strategy else (args.eval, args.funded)
    return parse_policy(eval_spec), parse_policy(funded_spec)


def _run(program, strategy, args, n=None):
    eval_policy, funded_policy = _policies(args, strategy)
    instrument = _instrument(args)
    tilt = tilt_from_winrate(args.winrate, args.winrate_rr)
    label = strategy or f"{eval_policy.name} -> {funded_policy.name}"
    return simulate(program, eval_policy, funded_policy, instrument, n=n or args.n,
                    seed=args.seed, tilt=tilt, horizon_days=args.horizon, strategy=label,
                    max_eval_days=args.max_eval_days)


def cmd_simulate(args):
    program = _program(args.program, args)
    print(_run(program, args.strategy, args).format())
    if program.notes:
        print("\n" + program.notes.strip())


def cmd_compare(args):
    strategies = args.strategies.split(",")
    rows = []
    for path in args.programs:
        program = _program(path, args)
        for strategy in strategies:
            rows.append(_run(program, strategy, args))
    rows.sort(key=lambda s: s.ev, reverse=True)
    header = (f"{'program':<24} {'strategy':<16} {'fee':>5} {'pass':>6} {'eff':>6} "
              f"{'net/funded':>10} {'EV/attempt':>14} {'ROI':>6} {'max price':>9}")
    print(header)
    print("-" * len(header))
    for s in rows:
        ev = s.program.evaluation
        print(f"{s.program.name:<24.24} {s.strategy:<16.16} {ev.fee:>5.0f} {s.p_pass:>6.1%} "
              f"{s.efficiency:>6.1%} {s.net_per_funded:>10,.0f} "
              f"{s.ev:>8,.0f} +/-{s.ev_stderr:>3.0f} {s.ev / max(s.fees_per_attempt, 1e-9):>6.0%} "
              f"{s.breakeven_eval_price:>9,.0f}")
    inst = _instrument(args)
    print(f"\n{inst.name} ${inst.round_turn:.2f} round turn, {inst.stop_ticks:g}-tick stop, "
          f"{_edge(args)}, n={args.n:,} per row.")
    print("eff = gross withdrawn per funded account / funded drawdown. "
          "max price = evaluation price (per billing period) at which EV is zero.")


def cmd_optimize(args):
    instrument = _instrument(args)
    if args.program:
        ev = load_program(args.program).evaluation
        room, need, max_minis = ev.drawdown.amount, ev.profit_target, ev.max_contracts
    else:
        room, need, max_minis = args.room, args.need, math.inf
    if args.max_contracts is not None:
        max_minis = args.max_contracts
    cost = (lambda r: 0.0) if args.no_costs else (lambda r: instrument.cost(r, max_minis))
    tilt = tilt_from_winrate(args.winrate, args.winrate_rr)
    sol = solve_goal(room, need, cost, tilt, grid=args.grid)
    print(f"Reach +${need:,.0f} before losing ${room:,.0f} (static floor), "
          f"{_edge(args)}, "
          f"{'no costs' if args.no_costs else f'{instrument.name} ${instrument.round_turn:.2f} RT'}")
    print(f"{'room':>8} {'optimal':>8} {'bold':>8} {'gain':>7}   best first trade")
    for frac in (1.0, 0.8, 0.6, 0.4, 0.2):
        i = max(1, round(frac * room / sol.step))  # nearest grid state
        a = sol.actions[i]
        trade = f"risk ${a[0]:,.0f} to make ${a[1]:,.0f}" if a else "-"
        opt, bold = sol.values[i], sol.bold[i]
        print(f"{i * sol.step:>8,.0f} {opt:>8.2%} {bold:>8.2%} {100 * (opt - bold):>+6.2f}pp   {trade}")
    print("\nbold = one all-in bracket straight to the goal. A gain near zero means bold "
          "play is optimal; a large gain means you have enough edge to grind.")


def cmd_manual(args):
    """Evaluations traded by hand: take the pass rate as given, simulate the funded side."""
    program = _program(args.program, args)
    instrument = _instrument(args)
    ev = program.evaluation
    rate = args.pass_rate
    no_edge = simulate(program, Bold(), Bold(), instrument, n=args.n, seed=args.seed,
                       horizon_days=0, max_eval_days=args.max_eval_days).p_pass

    funded_policy = parse_policy(args.funded)
    tilt = tilt_from_winrate(args.winrate, args.winrate_rr)
    rng = random.Random(args.seed)
    runs = [run_funded(program, funded_policy, instrument, rng, tilt, args.horizon)
            for _ in range(args.n)]
    values = [r.net - r.funded_fees for r in runs]
    value = statistics.fmean(values)
    value_se = statistics.stdev(values) / math.sqrt(len(values))

    periods = math.ceil(args.days / ev.fee_period_days) if ev.fee_period_days else 1
    price = ev.fee * periods
    ev_of = lambda p: p * value - price

    row = lambda label, value: print(f"  {label:<42}{value}")
    print(f"Manual evaluations: {program.name}")
    row("your pass rate", f"{rate:.1%} in about {args.days:g} days per attempt")
    if args.attempts:
        passes = round(rate * args.attempts)
        lo, hi = wilson_interval(passes, args.attempts)
        luck = binomial_tail(passes, args.attempts, no_edge)
        row("from your record", f"{passes} of {args.attempts}; 95% range {lo:.1%} to {hi:.1%}")
    row("best pass rate without an edge", f"{no_edge:.1%} (bold play with costs, simulated)")
    if args.attempts:
        verdict = "unlikely to be luck" if luck < 0.05 else "could still be luck: keep logging"
        row(f"chance of {passes}+ of {args.attempts} with no edge", f"{luck:.1%} -> {verdict}")
    needed = attempts_to_rule_out_luck(rate, no_edge)
    if needed:
        row(f"attempts at {rate:.0%} before luck is under 5%", f"about {needed}")
    elif rate <= no_edge:
        row("", "this pass rate is reachable without an edge")
    print(f"Funded account: {funded_policy.name}, {_edge(args)}")
    row("value once funded", f"${value:,.0f} (+/- {value_se:,.0f}), net payouts minus funded fees")
    print(f"Per evaluation (${ev.fee:,.0f} x {periods} billing period{'s' if periods > 1 else ''})")
    ev_line = f"${ev_of(rate):,.0f}"
    if args.attempts:
        ev_line += f" (${ev_of(lo):,.0f} to ${ev_of(hi):,.0f} across the 95% range)"
    row("EV", ev_line)
    row("EV per evaluation day", f"${ev_of(rate) / args.days:,.0f}")
    if value > 0:
        row("break-even pass rate", f"{price / value:.1%}")
    if 0 < rate < 1:
        row("evaluations for a 90% chance of a pass", math.ceil(math.log(0.1) / math.log(1 - rate)))


def cmd_backtest(args):
    """asymmetry4: the bracket with entry filters, bar by bar on your own data."""
    paths = sorted({p for pattern in args.data for p in glob.glob(pattern)})
    if not paths:
        sys.exit(f"no files match {args.data}")
    sessions = load_sessions(paths, args.data_tz, args.tz, args.session_start, args.session_end,
                             args.min_bars)
    if not sessions:
        sys.exit("no sessions in the data between --session-start and --session-end")
    pct = args.stop.endswith("%")
    stop = float(args.stop.rstrip("%")) / (100 if pct else 1)
    rr = args.reward / args.risk
    cfg = Config(stop=stop, pct=pct, rr=rr, side=args.side, mean=args.mean, through=args.through,
                 last_entry=args.last_entry, max_per_day=args.max_per_day, cooldown=args.cooldown)
    program = _program(args.program, args) if args.program else None
    max_minis = program.evaluation.max_contracts if program else math.inf
    instrument = INSTRUMENTS[args.instrument].with_overrides(args.round_turn)

    first, last = sessions[0][-1].t.date(), sessions[-1][-1].t.date()
    split = date.fromisoformat(args.split) if args.split else None
    span = lambda a, b: f"{a:%Y-%m}..{b:%Y-%m}"
    periods = [(span(first, last), lambda d: True)]
    if split:
        periods = [(span(first, split - timedelta(days=1)), lambda d: d < split),
                   (span(split, last), lambda d: d >= split)]
    results, all_trades = [], []
    for spec in args.filters:
        trades = backtest(sessions, parse_filters(spec), cfg)
        all_trades += [(spec, t) for t in trades]
        for name, keep in periods:
            results.append(evaluate(f"{spec}|{name}", [t for t in trades if keep(t.day)], cfg,
                                    instrument, args.risk, max_minis))

    baseline = 1 / (1 + rr)
    stop_text = f"{100 * stop:g}% of price" if pct else f"{stop:g} points"
    print(f"asymmetry4 backtest: {len(sessions):,} sessions, {first} to {last}, "
          f"{args.session_start}-{args.session_end} {args.tz}")
    print(f"  risk ${args.risk:,.0f} to make ${args.reward:,.0f}, stop {stop_text}, side {args.side}, "
          f"mean {args.mean}, target fills {args.through:g} points through, "
          f"{instrument.name} ${instrument.round_turn:.2f} round turn\n")
    print(format_results(results, baseline))
    print(f"\nhit = share of trades reaching the target. time = closed at the session end.\n"
          f"win eq = win rate of a clean bracket with the same mean R, time exits included.\n"
          f"edge = win eq - {baseline:.1%}, the no-edge rate. 95% CI is clustered by day.\n"
          f"b/e = win eq needed to cover costs. $/trade = mean net P&L at ${args.risk:,.0f} risk. "
          f"min = minutes held.")
    print("Each row is a separate test: with 20 rows, one clears a 95% interval by luck. "
          "Trust a filter only if it\nholds in the later period too (--split).")

    if args.trades:
        with open(args.trades, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["filter", "entry_time", "side", "entry", "stop_points", "outcome", "r",
                        "minutes", "ambiguous"])
            for spec, t in all_trades:
                w.writerow([spec, t.entry_time.isoformat(sep=" "), t.side, t.entry, round(t.stop, 4),
                            t.outcome, round(t.r, 4), t.minutes, int(t.ambiguous)])
        print(f"\n{len(all_trades):,} trades written to {args.trades}")

    if program:
        final = [r for r in results if r.label.endswith(periods[-1][0]) and r.n]
        policy = Asymmetry1(risk=args.risk, reward=args.reward, trades_per_day=args.trades_per_day)
        print(f"\nProp-firm EV per evaluation: {program.name}, asymmetry1 bracket "
              f"({args.trades_per_day} per day) at each filter's {periods[-1][0]} win eq, n={args.n:,}")
        header = f"{'filter':<44} {'win eq':>7} {'pass':>6} {'EV':>12} {'EV at CI low':>13}"
        print(header + "\n" + "-" * len(header))
        for r in final:
            stops = sorted(t.stop for t in r.trades)
            inst = instrument.with_overrides(stop_ticks=stops[len(stops) // 2] / 0.25)

            def run(p):
                p = min(max(p, 0.01), 0.999)
                return simulate(program, policy, policy, inst, n=args.n, seed=args.seed,
                                tilt=tilt_from_winrate(p, rr), horizon_days=args.horizon,
                                max_eval_days=args.max_eval_days)
            mid, low = run(r.win_rate), run(r.win_rate - 1.96 * r.win_rate_se)
            print(f"{r.label.partition('|')[0]:<44.44} {r.win_rate:>7.1%} {mid.p_pass:>6.1%} "
                  f"{mid.ev:>7,.0f} +/-{mid.ev_stderr:<3.0f} {low.ev:>13,.0f}")
        print(f"Evaluation price ${program.evaluation.fee:,.0f}. Compare with asymmetry2, which "
              f"needs no edge: python -m propfirm simulate {args.program} --strategy asymmetry2")


def build_parser():
    p = argparse.ArgumentParser(prog="python -m propfirm", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--instrument", default="NQ", choices=sorted(INSTRUMENTS))
        sp.add_argument("--round-turn", type=float, help="all-in cost per contract round turn")
        sp.add_argument("--stop-ticks", type=float, help="typical stop width in ticks")
        sp.add_argument("--winrate", type=float, default=0.5,
                        help="your win rate on brackets of --winrate-rr (0.5 at 1:1 = no edge)")
        sp.add_argument("--winrate-rr", type=float, default=1.0,
                        help="reward:risk of the bracket --winrate was measured on "
                             "(0.2 for risking $2,000 to make $400; no edge is then 0.833)")

    def sim_opts(sp):
        common(sp)
        sp.add_argument("--n", type=int, default=10_000, help="evaluation attempts to simulate")
        sp.add_argument("--seed", type=int, default=1)
        sp.add_argument("--horizon", type=int, default=250, help="funded trading days to simulate")
        sp.add_argument("--max-eval-days", type=int, default=60,
                        help="give up on an evaluation after this many trading days")
        sp.add_argument("--fee", type=float,
                        help="override the evaluation price (e.g. a promo or reset price)")

    s = sub.add_parser("simulate", help="full report for one program and strategy")
    s.add_argument("program")
    s.add_argument("--strategy", choices=sorted(STRATEGIES))
    s.add_argument("--eval", default="bold", help='evaluation policy, e.g. "bold:max_risk_frac=0.5"')
    s.add_argument("--funded", default="harvest", help='funded policy, e.g. "harvest:min_odds=0.5"')
    sim_opts(s)
    s.set_defaults(func=cmd_simulate)

    c = sub.add_parser("compare", help="rank programs x strategies by EV per evaluation")
    c.add_argument("programs", nargs="+")
    c.add_argument("--strategies", default="asymmetry1,asymmetry1+,asymmetry2")
    sim_opts(c)
    c.set_defaults(func=cmd_compare)

    m = sub.add_parser("manual", help="evaluations traded by hand: your pass rate, simulated funded value")
    m.add_argument("program")
    m.add_argument("--pass-rate", type=float, required=True, help="your evaluation pass rate, e.g. 0.5")
    m.add_argument("--days", type=float, default=2.0, help="trading days per evaluation attempt")
    m.add_argument("--attempts", type=int, help="how many evaluations the pass rate is based on")
    m.add_argument("--funded", default="harvest", help='funded policy, e.g. "asymmetry1"')
    sim_opts(m)
    m.set_defaults(func=cmd_manual)

    b = sub.add_parser("backtest", help="asymmetry4: the bracket with entry filters on 1-minute bars")
    b.add_argument("data", nargs="+", help="1-minute bar CSV files or glob patterns")
    b.add_argument("--filters", nargs="+", default=list(DEFAULT_SUITE),
                   help='filter sets to test, e.g. "range:n=30,width=1+near:within=0.1"')
    b.add_argument("--stop", default="0.5%%", help="stop in points (40) or percent of price (0.5%%)")
    b.add_argument("--risk", type=float, default=2000.0)
    b.add_argument("--reward", type=float, default=400.0)
    b.add_argument("--side", default="fade", choices=("fade", "follow", "long", "short", "both"),
                   help="fade trades toward the mean, follow away from it")
    b.add_argument("--mean", default="vwap",
                   help="reference level: session vwap, sma:N, open, or prev (previous close)")
    b.add_argument("--through", type=float, default=0.25,
                   help="points price must trade past the target to fill it")
    b.add_argument("--split", help="report before and after this date (YYYY-MM-DD) separately")
    b.add_argument("--last-entry", default="15:30",
                   help="no new trades after this local time on the session's last day")
    b.add_argument("--max-per-day", type=int, default=0, help="0 = no limit")
    b.add_argument("--cooldown", type=int, default=0, help="bars to wait after each exit")
    b.add_argument("--data-tz", default="UTC", help="time zone of timestamps without an offset")
    b.add_argument("--tz", default="America/New_York", help="exchange time zone for the session")
    b.add_argument("--session-start", default="09:30")
    b.add_argument("--session-end", default="16:00")
    b.add_argument("--min-bars", type=int, default=300,
                   help="drop shorter sessions (holidays, half days, data gaps)")
    b.add_argument("--trades", help="write every trade to this CSV")
    b.add_argument("--program", help="also price each filter's win rate on this program")
    b.add_argument("--trades-per-day", type=int, default=1,
                   help="brackets per day in the prop-firm simulation")
    b.add_argument("--instrument", default="NQ", choices=sorted(INSTRUMENTS))
    b.add_argument("--round-turn", type=float, help="all-in cost per contract round turn")
    b.add_argument("--n", type=int, default=4000, help="evaluation attempts to simulate per filter")
    b.add_argument("--seed", type=int, default=1)
    b.add_argument("--horizon", type=int, default=250, help="funded trading days to simulate")
    b.add_argument("--max-eval-days", type=int, default=60)
    b.add_argument("--fee", type=float, help="override the evaluation price")
    b.set_defaults(func=cmd_backtest)

    o = sub.add_parser("optimize", help="exact optimal staking to pass (dynamic program)")
    o.add_argument("--program", help="take room / target / contract cap from a program's evaluation")
    o.add_argument("--room", type=float, default=2500.0)
    o.add_argument("--need", type=float, default=3000.0)
    o.add_argument("--max-contracts", type=float)
    o.add_argument("--grid", type=int, default=44)
    o.add_argument("--no-costs", action="store_true")
    common(o)
    o.set_defaults(func=cmd_optimize)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if getattr(args, "strategies", None):
        unknown = set(args.strategies.split(",")) - set(STRATEGIES)
        if unknown:
            sys.exit(f"unknown strategies {sorted(unknown)}; choose from {sorted(STRATEGIES)}")
    args.func(args)


if __name__ == "__main__":
    main()
