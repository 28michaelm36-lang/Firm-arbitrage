# Firm-arbitrage

A simulator and optimizer for prop-firm account economics: evaluation fees,
pass probabilities, funded payouts, and the strategies that exploit the
*structure* of the rules rather than a market edge.

It contains:

- **asymmetry1**: the opening-range-breakout trade, risking $2,000 to make $400
  once a session (`Asymmetry1` in `propfirm/policies.py`). What decides it is
  whether its live win rate beats about 87%. See
  [docs/asymmetry1-improvements.md](docs/asymmetry1-improvements.md).
- **asymmetry1+**: the same trade, with the last one shrunk to land exactly on
  the target.
- **asymmetry2, drawdown harvest**: needs no edge. Bold play in the evaluation
  (`Bold`) and extraction of the funded drawdown (`Harvest`).
- **asymmetry3, menu arbitrage**: pick programs and prices by the value their
  rules imply (`compare` command). See [docs/new-strategies.md](docs/new-strategies.md).
- **manual evaluations**: take your real pass rate as given, test it against
  luck, and value the funded side (`manual` command). See
  [docs/manual-evaluations.md](docs/manual-evaluations.md).
- **futures inefficiency scanner**: a local website that watches thinly traded
  futures (soybean oil, meal, oats, rice, livestock, softs, PGMs). It ranks
  them by how far they are from a random walk, using a variance-ratio test. It
  alerts when a mean-reversion or breakout signal fires, or when related news
  breaks. See [docs/market-scanner.md](docs/market-scanner.md).

Pure Python 3.11+, standard library only.

## Quick start

```bash
# Full report for one program and strategy
python -m propfirm simulate configs/programs/intraday_trailing_50k.toml --strategy asymmetry2

# Rank programs x strategies by EV per evaluation bought, with the break-even price
python -m propfirm compare configs/programs/*.toml --strategies asymmetry1,asymmetry1+,asymmetry2

# The ORB at your live win rate, measured on its own $400-for-$2,000 bracket
python -m propfirm simulate configs/programs/intraday_trailing_50k.toml \
    --strategy asymmetry1 --winrate 0.88 --winrate-rr 0.2 --stop-ticks 160

# Your own costs and price
python -m propfirm compare configs/programs/*.toml --instrument MNQ --round-turn 1.60 --fee 29

# Custom policy parameters
python -m propfirm simulate configs/programs/eod_trailing_50k.toml \
    --eval "asymmetry1:risk=2400,reward=480,land_exactly=1" --funded "harvest:max_risk_frac=0.5"

# Evaluations you pass by hand: is 50% an edge, and what is each one worth?
python -m propfirm manual configs/programs/intraday_trailing_50k.toml --pass-rate 0.5 --days 2 --attempts 10

# Exact optimal staking to pass (dynamic program), and whether bold play is optimal for you
python -m propfirm optimize --program configs/programs/static_50k.toml --winrate 0.5

# Futures scanner website on http://127.0.0.1:8000 (add --demo to run offline on synthetic data)
python -m scanner serve
python -m scanner scan --news          # one-shot report in the terminal

# Tests
python -m unittest discover -s tests -t .
```

The programs in `configs/programs/` are **illustrative**. They have the shape
of common futures programs, but firms change rules and prices constantly.
Copy one, set it to your firm's current rulebook and the price you actually
pay, and rerun.

## The three results everything else follows from

For a trader **without an edge**, P&L is a fair game before costs. From that:

1. **Pass probability has a ceiling set by the rules, not by sizing.**
   - Static floor, or end-of-day trailing within one session: `P(pass) <= D / (D + T)`.
   - Unlocked intraday trailing floor: `P(pass) = exp(-T / D)`. The running high
     at the first drawdown of size D is exponentially distributed.

   Here D is the drawdown and T the profit target. With no costs and no
   overshoot, *every* zero-edge strategy hits the ceiling exactly; the tests
   check this. Sizing only decides how much you **leak** below it, through
   commissions, overshooting the target, and multi-day ratchets. A $2,500
   drawdown against a $3,000 target gives 45% if the floor is static and 30%
   if it trails intraday.
2. **Bold play minimises the leak.** When every trade costs something, the
   fewest, largest trades aimed *exactly* at the target maximise the chance of
   reaching it (Dubins and Savage, *How to Gamble If You Must*). The exact
   dynamic program in `propfirm/dp.py` confirms this. At zero edge bold play is
   within 0.05 percentage points of optimal with NQ and 0.3 points with MNQ.
   From about a 51–52% win rate on 1:1 brackets, smaller bets start to win.
3. **A funded account is worth at most its drawdown.** Let X be balance minus
   floor. X plus the withdrawals so far is a supermartingale: trading is fair
   minus costs, and the floor only rises. So expected gross withdrawals are at
   most the funded drawdown D, however you trade. What you control is the
   **efficiency** `eta = E[withdrawals] / D`: how much of D the rules and your
   policy leave stranded, ratcheted away, or spent on commissions.

Put together:

```
EV per evaluation  ~=  P(pass) x (split x eta x D_funded - activation)  -  evaluation price
```

Every strategy here improves one factor of that product.

## Headline numbers

`python -m propfirm compare configs/programs/*.toml --strategies grind,asymmetry1,asymmetry1+,asymmetry2 --n 20000`,
NQ at $9 round turn including slippage, 40-tick stop, no edge:

| program (illustrative)  | price | strategy    | pass  | eta   | EV / evaluation | break-even price |
|-------------------------|------:|-------------|------:|------:|----------------:|-----------------:|
| Intraday-trailing 50K   |   $35 | grind       |  8.2% |  7.6% |        -$75 ± 2 |               $2 |
|                         |       | asymmetry1  | 17.4% | 12.4% |         -$6 ± 3 |              $29 |
|                         |       | asymmetry1+ | 18.2% | 12.5% |         -$4 ± 3 |              $31 |
|                         |       | asymmetry2  | 29.1% | 27.8% |        $126 ± 7 |             $161 |
| EOD-trailing 50K        |   $49 | asymmetry1  | 15.9% | 29.7% |         $19 ± 3 |              $67 |
|                         |       | asymmetry1+ | 17.3% | 29.7% |         $25 ± 3 |              $73 |
|                         |       | asymmetry2  | 28.1% | 75.9% |       $325 ± 17 |             $374 |
| Static 50K              |   $99 | asymmetry1  | 30.7% | 76.7% |        $277 ± 8 |             $376 |
|                         |       | asymmetry1+ | 31.9% | 77.6% |        $297 ± 8 |             $396 |
|                         |       | asymmetry2  | 38.8% | 93.2% |       $480 ± 11 |             $579 |
| Intraday-trailing 150K  |   $60 | asymmetry1  |  2.7% |  6.7% |        -$90 ± 1 |               $3 |
|                         |       | asymmetry2  | 16.5% | 25.5% |        $127 ± 9 |             $187 |

These rows assume **no edge**: the ORB wins its fair 83.3%. With an edge the
ORB rows change completely: at an 88% live win rate it makes $250 per
evaluation on the intraday 50K, at 90% $665. See the table in
[docs/asymmetry1-improvements.md](docs/asymmetry1-improvements.md). `grind` is
a typical small-size trader ($167 risk for $250). It loses money on every
program at zero edge.

## Layout

```
propfirm/
  rules.py        program rules, loaded from TOML
  bracket.py      one bracket trade vs stop, target and (trailing) floor; closed forms
  account.py      balance, floor, daily loss limit, trading days
  policies.py     Asymmetry1 (the ORB), Bold and Harvest (asymmetry2)
  lifecycle.py    Monte Carlo of evaluation -> activation -> funded payouts; economics
  dp.py           exact dynamic program for optimal staking to a goal
  instruments.py  NQ / MNQ / ES / MES with default all-in costs
  stats.py        binomial checks for a real track record
  cli.py          simulate | compare | manual | optimize
scanner/
  data.py         Yahoo Finance daily bars, synthetic demo bars
  efficiency.py   Lo-MacKinlay variance ratio: mean-reverting, trending or random walk
  signals.py      mean-reversion and breakout triggers, and each one's past record
  news.py         RSS/Atom headlines scored against each market's keywords
  engine.py       scan cycle: new triggers and headlines -> alerts
  store.py        SQLite: alerts are stored once, so they never repeat
  alerts.py       webhook delivery (ntfy, Slack, Discord, JSON)
  server.py       JSON API, the page in static/, and the background scan loop
  cli.py          serve | scan
configs/programs/ illustrative program rules
configs/scanner.toml  scanner watchlist, signal parameters, news keywords, alerts
docs/             strategy write-ups
tests/            unittest suite, including the theory checks above
```

## Model assumptions

- Price is driftless Brownian motion between entry and exit. A trade ends at
  its stop, its target, or the floor. The intraday trailing floor follows the
  trade's best open P&L. The bracket maths is exact under this model and is
  checked against a brute-force random walk in the tests.
- Commission, exchange fees and expected slippage are one round-trip cost per
  contract, paid at entry. Contracts = dollar risk / (stop ticks x tick value),
  capped at the program's contract limit. At the cap the stop widens instead.
- An edge is a single number: your win rate on a bracket of a given
  reward:risk (`--winrate`, `--winrate-rr`). It reduces the hazard of hitting
  the stop uniformly. That carries it over to other bracket shapes, which is
  only an approximation, so compare policies at the shape you measured.
  Calibrate it from your own fills; do not assume one.
- Trades finish within the session, with no gaps or halts. Payouts are always
  honoured. Rule changes, payout denials and firm failure are not modelled.
  Those are real risks, and a denied payout zeroes the EV.
