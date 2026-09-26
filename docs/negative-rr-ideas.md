# High-win-rate, negative-RR ideas, tested

Every idea here is traded as the asymmetry1 bracket: risk $2,000 to make
$400. Each one is backtested with `python -m propfirm backtest` (see
[asymmetry4.md](asymmetry4.md)) on Oanda's NAS100 and SPX500 1-minute bars,
2012 to May 2020. NAS100 moves nearly point for point with NQ; it is not NQ
fills. Unless stated, the stop is 0.5% of price, targets fill one tick
through, and costs are NQ's for NAS100 and ES's for SPX500.

## First: the win rate is a property of the shape, not of the edge

With no edge, a bracket wins stop / (stop + target) of the time: 83.3% at
5:1, 50% at 1:1. A signal that predicts direction lifts both by amounts that
correspond (the simulator's edge model, `tilt_from_winrate`):

| win rate at 5:1 | same signal at 1:1 |
|----------------:|-------------------:|
|           83.3% |                50% |
|             85% |                54% |
|             87% |                59% |
|             90% |                67% |
|             92% |                73% |

The ORB needs about 87% live to beat asymmetry2. That is the same claim as a
59% win rate on 1:1 brackets, which is a very strong edge. If you would not
believe 59% at 1:1, do not believe 87% at 5:1.

Costs make it worse. A bracket earns the drift it captures while it is open,
and a 5:1 bracket is open a fifth as long as a 1:1 bracket with the same stop
(expected time = stop × target / variance rate). It captures a fifth of the
drift per trade and pays the same commission. So the same signal needs about
five times the directional edge to cover costs on a 5:1 bracket.

## Results

Win eq for each period. Once-a-day ideas have about 700–1,200 trades per
period, so ±2–3 points at 95%. No edge is 83.3%. Break-even after costs is
85.6% then 84.6% on NAS100, and 86.7% then 85.6% on SPX500 (2012–2016, then
2017–2020; lower prices meant more contracts for the same stop).

### The ORB (asymmetry1's own trade)

The first 1-minute close outside the opening range, traded the way of the
break (`orb:n=30 --side follow --mean open --max-per-day 1`):

| opening range | NAS100 2012–16 | NAS100 2017–20 | SPX500 2012–16 | SPX500 2017–20 |
|---|---:|---:|---:|---:|
| 15 minutes | 78.9% | 83.7% | 80.4% | 79.7% |
| 30 minutes | 79.6% | 81.8% | 78.1% | 83.0% |
| 60 minutes | 82.3% | 80.8% | 82.0% | 79.9% |
| 30 minutes, 40-point stop (the asymmetry1 spec) | 80.9% | 82.7% | | |
| 30 minutes, **fading** the break (control) | 83.7% | 83.5% | 83.0% | 80.8% |

The breakout beats 83.3% in one cell of 14 (83.7%, well inside the noise)
and never reaches break-even. Fading the 30-minute break did better in three
of four periods. A plain ORB has no edge on this data. If yours has one, it comes from
something specific to your version (entry, stop placement, extra filters),
and only your live fills can show it.

### Gap at the open

At 09:31, trade toward or away from the previous session's close when the
open has gapped at least k stops (`--mean prev`,
`time:start=930,end=931+far:beyond=k`):

| gap | side | NAS100 2012–16 | NAS100 2017–20 | SPX500 2012–16 | SPX500 2017–20 |
|---|---|---:|---:|---:|---:|
| ≥ 0.1 stops | fade | 84.0% | 83.6% | 83.1% | 82.7% |
| ≥ 0.1 stops | follow | 80.6% | 78.0% | 80.1% | 81.0% |
| ≥ 0.5 stops | fade | 83.8% | 84.2% | 81.7% | 80.9% |
| ≥ 0.5 stops | follow | 79.6% | 76.2% | 78.9% | 80.9% |
| ≥ 1 stop | fade | 83.6% | 86.0% | 80.6% | 82.3% |

Gaps do tend to fill: fading beats or ties following in every comparison.
On NAS100 the two sit about 2 points either side of the unfiltered 81.7%. But
the market's slight intraday trend (see asymmetry4) takes most of that back. Fading reaches
83–84% on NAS100 and less on SPX500.

### Late-day momentum

At 15:00 or 15:30, trade with or against the day's move since the previous
close (`--mean prev`, `time:start=1500,end=1501+far:beyond=k`):

| entry | day's move | side | NAS100 2012–16 | NAS100 2017–20 | SPX500 2012–16 | SPX500 2017–20 |
|---|---|---|---:|---:|---:|---:|
| 15:00 | any | follow | 82.8% | 84.8% | 83.6% | 82.4% |
| 15:00 | any | fade | 82.2% | 83.2% | 81.0% | 83.2% |
| 15:00 | ≥ 1 stop | follow | 82.4% | **86.3%** | 83.8% | 84.8% |
| 15:30 | any | follow | 82.4% | 83.7% | 83.1% | 82.2% |

Following beats fading in most rows, which matches published work on
last-half-hour momentum. Following a day's move of at least one stop at 15:00
is the most consistent idea here: above 83.3% in three of four cells, but
above break-even only on NAS100 in 2017–2020. That makes it a lead to check,
not a result.

### Overnight session

A long or short bracket in the 18:00–09:30 session
(`--session-start 18:00 --session-end 09:30`):

| trade | NAS100 2012–16 | NAS100 2017–20 | SPX500 2012–16 | SPX500 2017–20 |
|---|---:|---:|---:|---:|
| long, once at 18:01 | **84.9%** | **84.5%** | 80.2% | 79.8% |
| short, once at 18:01 | 80.8% | 79.5% | 82.2% | 79.8% |
| long, all night | 82.4% | 82.8% | 80.8% | 81.0% |

On NAS100 this is the best result in both periods, with a 4–5 point gap
between long and short. That fits the well-documented overnight drift of US
stock indices. It is just below break-even in both periods, it is missing
from SPX500 (whose overnight data here is sparser), and 2012–2020 was an unusual
bull run for the Nasdaq. Check your firm's rules on trading the overnight
session.

### Fading short-term spikes

Fade price when it is at least k stops from its 5- or 15-bar average
(`--mean sma:15`, `far:beyond=k`). Smaller spikes sit at 80–83%. The largest
(1 stop from the 15-bar average) faded at 85.0% on NAS100 and 83.8% on SPX500
in 2017–2020, but on few trades before that: 80.6% on 144 trades and 86.2% on
60. Too thin to call.

## A structural use: counting profitable days

Some payout rules count profitable days (for example, 5 days of at least
$50). A 5:1 bracket makes a day profitable 83% of the time instead of 50%, so
it should earn those days faster. In the simulator it makes no difference.
The funded EV under harvest moves by at most ±$12 as the coast bracket goes
from 1:1 to 10:1 (`--funded "harvest:coast_odds=0.2"`), because funded
accounts usually end, or reach the payout, before that rule binds.

## Not testable here: selling options premium

Short strangles and credit spreads are the classic high-win-rate,
negative-RR trade with a documented edge: implied volatility has on average
been above the volatility that follows (the variance risk premium). It is
not in this repository, for three reasons. Most futures prop firms allow only
futures. The edge is paid for with rare, large losses, which a trailing
drawdown cannot survive. And there are no options data here to test it.

## Verdict

None of these is a go. What they show:

- **A plain ORB has no edge on this data.** asymmetry1 depends on something
  your version does that this one does not. Log its live fills.
- **Two leads to check on real NQ data:** the overnight long bracket, and
  following a large day's move at 15:00. Both are near break-even here, not
  clearly above it.
- **Gaps fill, but not by enough** to carry a 5:1 bracket.
- **Direction is the only source of edge.** The bracket shape only changes
  how it shows up. Pick the shape for the prop-firm rules (bold play in the
  evaluation, see asymmetry2) and for costs, not for the win rate.

To reproduce: the runs above are `backtest` calls with the flags shown, on
the data described in [asymmetry4.md](asymmetry4.md).
