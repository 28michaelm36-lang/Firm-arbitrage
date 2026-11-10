# Bold + Harvest on real NQ bars

asymmetry2 (bold play in the evaluation, drawdown harvest once funded, see
[new-strategies.md](new-strategies.md)) had only been run in the simulator,
where every bracket is resolved by a formula for a driftless market. Here it
trades real 1-minute bars: `intraday/boldharvest.py` asks the same `Bold` and
`Harvest` policies for the same brackets, and fills them on the tape.

A **signal** decides when to enter and which way. That is the only place a
market edge can come from, because the account sets the dollar risk and the
dollar target. `coin` is the no-edge control.

## Data

- **NQ futures, 1-minute, back-adjusted, 27 Dec 2022 to 11 Dec 2025**: 764
  regular sessions, 749 scored after a 15-session warm-up.
- **Oanda NAS100, 1-minute, 2005 to May 2020**, each session rescaled to a
  19,000 index so points and tick costs read like today's NQ. It tracks the
  Nasdaq-100 but is not NQ fills. It is used to choose entry signals *before*
  looking at them on NQ.

Fills: market orders at the next bar's open, one tick against you. Stops one
more tick against you, or at the open after a gap. Targets only once price
trades a tick through. A bar touching both counts as the stop. NQ $4.50 and
MNQ $1.50 a round turn. The stop width that sizes each trade is a quarter of
the 14-session average range (`--stop 0.25r`): 56 points on the median day of
the NQ file, so a $2,000 risk is 1 or 2 NQ contracts on most days.

## Net profit with no edge (coin-flip entries), NQ 2023–2025

Mean of 200 coin-flip runs on the three illustrative 50K programs in
`configs/programs/`. The range is the middle 90% of those runs.

Per evaluation bought:

| program | pass | net per funded account | EV per evaluation | simulator said |
|---|---:|---:|---:|---:|
| EOD-trailing 50K ($49) | 32.9% | $1,441 | $383 ($98 to $804) | $390 |
| Intraday-trailing 50K ($35) | 29.9% | $772 | $154 ($73 to $253) | $169 |
| Static 50K ($99) | 41.3% | $1,533 | $536 ($416 to $677) | $507 |

One account at a time for the whole 2.9 years: buy an evaluation, trade it and
the funded account it earns to the end, buy the next one the session after:

| program | evaluations bought | passed | payouts | trader's share | evaluation fees | funded fees | net profit | a year |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| EOD-trailing 50K | 114 | 38 | 13 | $52,251 | -$5,574 | -$5,600 | **$41,076** ($8,238 to $79,493) | $14,181 |
| Intraday-trailing 50K | 285 | 85 | 33 | $66,618 | -$9,983 | -$11,918 | **$44,717** ($24,205 to $69,485) | $15,438 |
| Static 50K | 216 | 89 | 84 | $136,273 | -$21,344 | $0 | **$114,929** ($97,184 to $133,915) | $39,676 |

Commissions and slippage are paid inside the accounts, so they are already out
of the payouts: $9,000 to $14,000 over the period in the runs further down.
The worst run of net cash (fees paid before the next payout) averaged $2,000
to $4,300.

Real bars agree with the simulator. What real bars add:

- **On the EOD-trailing program the money comes from a handful of accounts.**
  In the campaigns further down, 93% of evaluations lost their $49–$198 of
  fees, fewer than one funded account in five was ever paid, and the three
  best accounts made essentially all of the net profit: once the floor locks,
  an account can be paid again and again, and the best one paid $23,000 to
  $40,000. The static program is far more even: about half of funded accounts
  were paid and the best three made 20–30% of the profit.
- **That makes three years a small sample.** The same strategy with a different
  run of coin flips made $8,000 or $79,000 on the EOD program.

## Looking for an edge

Each signal is first tested as one bracket a day, risking 1 to make 1.5 (bold
play's shape on a $2,000 drawdown and $3,000 target). *Mean R* is the average
result in units of the risk, after costs: zero is a coin flip before costs,
about -0.01 after. Signals were screened on 2005–2020, then run on NQ.

| signal | 2005–2020 mean R | t | NQ 2023–2025 mean R | t |
|---|---:|---:|---:|---:|
| `coin` (three seeds) | +0.034, -0.007, -0.006 | +1.8, -0.4, -0.3 | +0.006, +0.057, +0.008 | +0.1, +1.3, +0.2 |
| `long` | +0.030 | +1.5 | +0.052 | +1.2 |
| `short` | -0.048 | -2.5 | +0.014 | +0.3 |
| `orb:n=15` (asymmetry1's entry) | -0.006 | -0.3 | +0.054 | +1.2 |
| `orb:n=30` | -0.010 | -0.5 | +0.024 | +0.6 |
| `first:n=5` (first 5-minute candle) | +0.050 | +2.6 | +0.015 | +0.3 |
| `first:n=30` | +0.041 | +2.1 | +0.034 | +0.8 |
| `mom:t=30` (day's move from the previous close at 10:00) | -0.000 | -0.0 | -0.051 | -1.2 |
| `mom:t=120` | +0.010 | +0.5 | -0.004 | -0.1 |
| `gap` (with the overnight gap) | -0.067 | -3.5 | -0.004 | -0.1 |
| `gap:fade=1` (against it) | +0.049 | +2.5 | +0.070 | +1.6 |
| **`noise`** (noise-area breakout) | **+0.066** | **+2.6** | **+0.112** | **+2.0** |

- **`noise` is the one that holds up.** It is the breakout of Zarattini, Aziz &
  Barbon (2024): on each half hour from 10:00, buy if price is above the band
  of typical moves from the open, sell if below. Positive in both halves of
  2005–2020 and in both halves of the NQ file. It wins about 42–44% of
  brackets that a coin wins 40% of. It only trades on about 6 days in 10.
- **`gap:fade=1` and `first:n=5` are weaker leads**: positive on both
  datasets, but not distinguishable from luck on NQ alone.
- **The opening-range breakout has no edge** as a bold bracket: nothing in
  2005–2020, and on NQ it does no better than always buying (t = 1.2 for
  both).
- **Three years of NQ cannot prove a small edge by itself.** A coin flip
  reaches t = 1.3 in this table. `noise` rests on 2005–2020 and NQ agreeing.
- One thing looked strong on NQ and was thrown out: trading against the day's
  move at 15:30 (t = +2.8). At 15:15 it is zero, at 15:00 it is strongly
  negative, and 2005–2020 does not show it. That is what a chance result
  looks like.

Through the full account lifecycle, EV per evaluation and where it falls among
the coin-flip runs:

| program | coin flip | `noise` | `first:n=5` | `orb:n=15` |
|---|---:|---:|---:|---:|
| **NQ 2023–2025** (200 coin runs) | | | | |
| EOD-trailing 50K | $383 | $391 (beats 59%) | $867 (96%) | $567 (78%) |
| Intraday-trailing 50K | $154 | $326 (99%) | $239 (94%) | $138 (44%) |
| Static 50K | $536 | $716 (97%) | $464 (18%) | $545 (57%) |
| **2005–2020** (30 coin runs) | | | | |
| EOD-trailing 50K | $275 | $451 (93%) | $407 (93%) | $344 (80%) |
| Intraday-trailing 50K | $154 | $174 (87%) | $241 (100%) | $145 (37%) |
| Static 50K | $502 | $617 (100%) | $598 (97%) | $460 (17%) |

Net profit a year from one account at a time:

| program | coin flip | `noise` | `first:n=5` | `orb:n=15` |
|---|---:|---:|---:|---:|
| **NQ 2023–2025** | | | | |
| EOD-trailing 50K | $14,181 | $12,102 | $19,203 | $12,299 |
| Intraday-trailing 50K | $15,438 | $14,047 | $26,930 | $13,466 |
| Static 50K | $39,676 | $27,985 | $38,881 | $37,093 |
| **2005–2020** | | | | |
| EOD-trailing 50K | $10,600 | $10,711 | $19,036 | $12,128 |
| Intraday-trailing 50K | $15,415 | $9,658 | $19,008 | $12,942 |
| Static 50K | $37,311 | $27,071 | $40,850 | $32,810 |

`noise` earns more **per evaluation** and less **per year from one account**,
because it sits out 4 days in 10 and so buys about half as many evaluations.
If the number of evaluations you can run is what limits you, use `noise`. If
one account slot is what limits you, an entry that trades every day is worth
more: `first:n=5` matched or beat the coin flip in five of these six cells.

## What to take from it

1. **Bold + Harvest behaves on real NQ bars as the simulator said.** The
   profit comes from the programs' rules and prices, not from the entry.
2. **The entry adds a little, not a lot.** `noise` moved EV per evaluation by
   $10 to $180. The opening-range breakout adds nothing.
3. **The size of the profit is a statement about the three program files.**
   They are illustrative. A real firm's rules decide whether any of this
   exists: put your firm's rulebook and the price you pay into a TOML file
   before trusting a dollar figure.

## Caveats

- **Payouts are assumed to be honoured.** Many firms ban all-in trades or
  "gambling" on funded accounts, cap reward to risk, or review payouts by hand.
  A denied payout zeroes that account. `--eval "bold:max_risk_frac=0.5"` and
  `--funded "harvest:max_risk_frac=0.5"` show the cost of trading smaller:
  risking half the cushion left EV per evaluation about where it was ($379,
  $187 and $560 with coin flips) but each account took about twice as long,
  so one account slot made 25–45% less a year.
- **Payouts are taken the first day the rules allow**, with no processing delay.
- **1-minute bars.** The order of a bar's high and low is unknown, so the
  worst order is assumed. That is harsh on intraday-trailing accounts when the
  stop is narrow: keep `--stop` at 0.25r or wider there.
- **The NQ file is back-adjusted.** Point moves are right. Price levels before
  the latest contract are shifted, which slightly distorts percentage bands.
- **The 2005–2020 data is a CFD**, rescaled to today's index level.
- No news calendar, halts, partial fills or rejected orders.

## The 2026 test, fixed in advance

Run this unchanged on 2026 NQ data when the year is in:

```bash
python -m intraday signals data/NQ_2026.csv --signals "coin;noise;first:n=5;gap:fade=1;orb:n=15"
python -m intraday boldharvest data/NQ_2026.csv --signals "noise;first:n=5" --coins 200
```

One year is about 150 `noise` trades, which cannot confirm an edge this small.
So the rule is about when to stop: **keep `noise` while its mean R over
2023–2026 stays above zero, and drop it if it falls below.** Do not swap in
whichever signal looks best on 2026.

## Running it

Put the bars in `data/` (git ignores it): a CSV, a `.zip` holding one, or an
`.xlsx`. The NQ file's `timestamp ET` column and end-of-bar labels are
detected.

```bash
# The numbers above: net profit after payouts and costs, against 200 coin flips
python -m intraday boldharvest data/NQ_1min_2022_2025.zip \
    --signals "orb:n=15;noise;gap:fade=1;first:n=5;long" --coins 200 --ledger ledger.csv

# One program, your own costs, trading half the cushion instead of all of it
python -m intraday boldharvest data/NQ_1min_2022_2025.zip --programs configs/programs/eod_trailing_50k.toml \
    --signals "noise" --commission 4.20 --slip-ticks 2 --eval "bold:max_risk_frac=0.5"

# Does an entry have an edge? One bracket a day, before and after a date
python -m intraday signals data/NQ_1min_2022_2025.zip --signals "coin;orb:n=15;noise" --split 2024-07-01
```

`--ledger` writes one row per evaluation of the campaign: dates, pass or fail,
trades, payouts, fees and net.

Signals: `coin`, `long`, `short`, `orb:n=15` (add `fade=1` to trade against the
break), `first:n=5`, `mom:t=30` (add `ref=open`), `gap`, `gap:fade=1`, `noise`.

## NinjaTrader

`ninjatrader/BoldHarvest.cs` is the same logic for one account: pick the phase
(evaluation or funded), the drawdown type, the goal and the entry. It has
**not been compiled or run** here, so compile it in the NinjaScript editor and
check it in Playback before it goes near an account. It trades one account
phase; it does not buy evaluations, track payouts or model fees, so the
net-profit numbers come from the Python backtest.
