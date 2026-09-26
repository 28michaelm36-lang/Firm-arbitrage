# asymmetry4: the range bracket

## The idea

asymmetry1 risks $2,000 to make $400, so it only loses to a move five times
the size of its target. asymmetry4 keeps that bracket and enters only when
such a move looks unlikely: when the market is calm, choppy, or sitting near
its mean. The backtester in `propfirm/backtest.py` tests that on real 1-minute
bars.

## What has to be true for it to work

On a fair game the entry filter cannot matter. Optional stopping fixes the
win rate at stop / (stop + target) = **83.3%**, whatever the volatility. A
calm market makes the target take longer too. It does not change which level
is hit first.

A filter can only help if "range-bound now" predicts **mean reversion over the
life of the trade**. If the market really does oscillate around a mean,
entering at that mean makes the far stop much harder to reach than the near
target. How much it helps depends on how tight the range is relative to the
stop (exact win rates, entering at the mean):

| range standard deviation, in stops | win rate |
|-----------------------------------:|---------:|
| 5 (a random walk)                  |    83.4% |
| 1                                  |    85.6% |
| 0.75                               |    87.3% |
| 0.5                                |    92.0% |

The range also has to hold until the trade closes. Breakouts are where the
losses come from. Whether a chop filter picks out such ranges is an empirical
question, so this is a market edge, like asymmetry1's, and not a structural
one like asymmetry2's.

The tests check both sides of this: on a simulated random walk the
backtester gives 83.3% with or without a calm filter, and on a simulated
mean-reverting market, entering near the mean clears 83.3% by a wide margin.

## Running it

```bash
python -m propfirm backtest "data/NAS100/*.csv" --stop 0.5% --split 2017-01-01 \
    --program configs/programs/intraday_trailing_50k.toml
```

- **Data.** Any 1-minute bar CSVs with a header naming a time column (`time`,
  `datetime`, `timestamp` or `date`) plus `open`, `high`, `low` and `close`.
  `volume` is optional. Timestamps are bar starts, in `--data-tz` (UTC by
  default) or ISO 8601 with an offset. Only the 09:30–16:00 New York session is
  used; change it with `--session-start`, `--session-end` and `--tz`.
- **Filters** (`--filters`, several at once; `+` means all must pass). Sizes
  are in stops:

  | filter | passes when |
  |---|---|
  | `all` | always: the baseline |
  | `range:n=30,width=1` | the last n bars span at most `width` stops (calm) |
  | `chop:n=30,above=61.8` | the choppiness index over n bars is at least `above` |
  | `er:n=30,below=0.2` | Kaufman's efficiency ratio over n bars is at most `below` |
  | `near:within=0.1` | the close is within `within` stops of the session mean |
  | `far:beyond=1` | the close is at least `beyond` stops from the session mean |
  | `time:start=1130,end=1400` | the bar closes inside the time window |

- **Side.** `fade` (the default) trades toward the mean, `follow` away from it,
  and `long`, `short` and `both` ignore it. `--mean` is the session VWAP or
  `sma:N`.
- **Stop.** In points (`--stop 40`) or as a share of price (`--stop 0.5%`).
  The target is the stop times `--reward / --risk`.
- **Fills.** Entry is at the next bar's open. The target fills only if price
  trades `--through` points past it (one tick by default). The stop fills at
  the stop, or at the open when a bar gaps past it. A bar that touches both
  counts as a loss. Trades still open at the close exit at the last close.
- **Output.** *win eq* is the win rate of a clean bracket with the same mean
  R, time exits included. It is the number to compare with 83.3% and to pass
  to `simulate --winrate ... --winrate-rr 0.2`. The confidence interval is
  clustered by day. *b/e* is the win rate that covers the round-trip cost.
  `--trades out.csv` writes every trade. `--program` prices each filter's win
  rate on a prop-firm program, trading the asymmetry1 bracket once a day.

## Results: NAS100 and S&P 500, 2012 to 2020

Data: Oanda's NAS100 and SPX500 1-minute bars, January 2012 to May 2020,
regular session only (about 2,000 sessions each). They are published in the
[FutureSharks/financial-data](https://github.com/FutureSharks/financial-data)
repository:

```bash
mkdir -p data/NAS100 && cd data/NAS100
for y in 2012 2013 2014 2015 2016 2017 2018 2019 2020; do for m in $(seq 1 12); do
  curl -sfO "https://raw.githubusercontent.com/FutureSharks/financial-data/master/pyfinancialdata/data/currencies/oanda/NAS100_USD/$y/oanda-NAS100_USD-$y-$m.csv"
done; done
```

`data/` is in `.gitignore`. Numbers below use a stop of 0.5% of price (about
40 NQ points at 2019 prices), NQ costs for NAS100 and ES costs for SPX500,
unless stated.

### 1. The unfiltered bracket is already below 83.3%

| market | stop | trade toward VWAP | both sides |
|---|---:|---:|---:|
| NAS100 | 0.25% | 80.3% | 80.5% |
| NAS100 | 0.5% | 81.7% | 81.7% |
| NAS100 | 1% | 82.1% | 81.9% |
| SPX500 | 0.25% | 78.3% | 78.8% |
| SPX500 | 0.5% | 80.8% | 81.5% |
| SPX500 | 1% | 81.7% | 82.0% |

(2012–2016, win eq.) Both sides fall short together, so this is not drift.
It is the signature of a market that trends slightly at intraday scale. With
targets filled on a touch, trades that resolve before the close win 83.7%. The
shortfall comes from the ones still open at the close, which lose 0.35R on
average. The one-tick fill-through rule costs about another 0.7 points and
weighs more on small stops.

### 2. The filters, one at a time (NAS100)

| filter | 2012–2016 | 2017–2020 |
|---|---:|---:|
| none (`all`) | 81.7% | 81.5% |
| calm: `range:n=30,width=1` | 81.3% | 81.5% |
| calm: `range:n=60,width=1` | 81.5% | 81.6% |
| choppy: `chop:n=30,above=61.8` | **80.5%** | **80.5%** |
| choppy: `er:n=30,below=0.2` | 81.9% | 81.2% |
| near the mean: `near:within=0.1` | 82.0% | 82.4% |
| lunch hours: `time:start=1130,end=1400` | 81.6% | 81.7% |
| calm and near the mean | 82.6% | 82.5% |
| no edge | 83.3% | 83.3% |
| break-even after costs | 85.6% | 84.6% |

- **Calm alone does nothing**, as theory predicts.
- **Choppy makes it worse.** A high choppiness index marks compression, and
  compression tends to come right before expansion: the move the bracket
  cannot survive.
- **Near the mean is the only filter that helps,** by 0.5 to 1 point, and most
  when combined with a calm range. That gets back toward 83.3%, never above
  it.

On SPX500 every row of the same suite is between 78.3% and 82.6%.

### 3. A sweep, then an out-of-sample check

402 configurations per market: every filter family over a grid of windows and
thresholds, stops of 0.25%, 0.5% and 1%, fading or both sides, VWAP or a
60-bar mean. Selected on 2012–2016 only:

| | NAS100 | SPX500 |
|---|---:|---:|
| configurations with 300+ trades | 381 | 382 |
| median win eq | 81.7% | 80.1% |
| best win eq | 83.5% | 83.1% |
| at or above 83.3% | 3 | 0 |
| at or above break-even | 0 | 0 |

The best NAS100 configuration in 2012–2016 (`chop:n=15,above=70`, 1% stop,
83.5%) fell to 81.3% in 2017–2020. Across the ten best, 2017–2020 ranged from
81.3% to 83.5%, against break-even rates of 84.1–84.7%. Averaged over each
family, filtering moved the win rate by −0.05 to +0.46 points on NAS100 and
by −0.74 to −0.11 points on SPX500. Beating costs needs about +3.

Filling targets on a touch instead of one tick through adds under a point.
The best configuration then reaches 84.0% over 2012–2020, still short of its
85.3% break-even.

### 4. What it is worth on a prop-firm program

Intraday-trailing 50K ($35), the asymmetry1 bracket once a day, at each
filter's 2017–2020 win eq (from `--program`):

| filter | win eq | pass | EV per evaluation |
|---|---:|---:|---:|
| none | 81.5% | 17.4% | $23 |
| near the mean | 82.4% | 19.9% | $39 |
| calm and near the mean | 82.5% | 20.3% | $46 |
| choppy | 80.5% | 15.4% | $5 |
| **asymmetry2** (no edge needed) | | 28.8% | **$144** |

All rows use the median 2017–2020 stop, 143 NQ ticks. The EV is positive even
below 83.3% because that stop needs only 3 NQ contracts, so costs are low, and
a $35 evaluation is cheap. That is the program's structure at work, not the
filter. The best filter adds about $20 per evaluation over no filter, and
asymmetry2 earns about $100 more than that without needing an edge.

## Verdict

Filtering for a quiet market to protect a 5:1 bracket does not survive the
data. Calm filters do nothing, choppiness filters hurt, and "near the mean"
helps by about a point: enough to recover some of the slight intraday trend,
not enough to reach 83.3%, and far from the 84–86% that pays for costs. On
these programs, use asymmetry2, or asymmetry1 only if it has a measured edge.

## Caveats

- **These are CFD prices, not futures fills.** NAS100 and SPX500 track the
  Nasdaq-100 and S&P 500 closely point for point, but they are not NQ or ES
  trades. Oanda's volume is a tick count, so the VWAP is approximate.
- **1-minute bars.** The order of the high and the low inside a bar is
  unknown. The ambiguous-bar rule affected 21 of 33,000 trades.
- **Only up to May 2020, and only the regular session.** No news calendar is
  used.
- **Your own data wins.** Export 1-minute NQ bars from your platform, run
  `backtest` on them, and log live fills. At 88%, about 190 live trades are
  needed to tell the result from 83.3% (`propfirm.stats.attempts_to_rule_out_luck`).
