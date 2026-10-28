# Published intraday strategies on Nasdaq-100 futures

Three intraday strategies from the finance literature, each trading at
least 3 times a week, flat by the 16:00 close. They are coded at their
published parameters (`intraday/`) and backtested on real 1-minute bars. Their
fills are then replayed through the prop-firm programs in `configs/programs/`.

**Status.** The requested dataset (NQ futures, 1-minute, December 2022 to
December 2025, Kaggle) could not be downloaded from this environment, so
everything below is on the closest real data that could be: Oanda's NAS100
CFD, 1-minute, January 2005 to May 2020. NAS100 tracks the Nasdaq-100 index
point for point, but it is not NQ fills, and it ends before 2022. The NQ run
is one command once the file is in `data/` (see the end), and its pass/fail
test is fixed in advance below.

## 1. What the literature says

| paper | strategy | claim | what later work found |
|---|---|---|---|
| Zarattini, Aziz & Barbon (2024), *Beat the Market*, SFI 24-97 | **noise area**: trade breakouts of a band of typical intraday moves, checked every 30 min, VWAP trailing exit | SPY 2007–2024: 19.6%/yr net, Sharpe 1.33 | Chuk (2026, SSRN 7609644) on MNQ 2020–2026: $10.26/day per contract net, Sharpe 1.14, in-sample, one of two survivors of 144 preregistered tests. An independent ES/SPY replication (2026) finds it strong in 2020–24 and about zero in 2025–26 |
| Gao, Han, Li & Zhou (2018), *Market intraday momentum*, JFE 129 | **last half hour**: the return from the previous close to 10:00 predicts 15:30–16:00 | SPY 1993–2013, R² 1.6%, stronger on volatile days | a 2015–2020 ETF replication lost money |
| Baltussen, Da, Lammers & Martens (2021), *Hedging demand and market intraday momentum*, JFE 142 | same trade, predicted by the return from the previous close to 15:30 | 60+ futures, 1974–2020, Sharpe 0.87–1.73 per asset class; tied to option dealers' gamma hedging | strongest when dealers are short gamma; absent when they are long |
| Zarattini & Aziz (2023), *Can Day Trading Really Be Profitable?*, SSRN 4416622 | **5-minute ORB**: trade the direction of the first 5-minute candle, stop at its other end, target 10R | QQQ 2016–2023, large outperformance (no slippage modelled) | on five index CFDs 2015–2026, NQ +0.147R gross but +0.002R net; an arXiv falsification study on MNQ (2605.04004) rejects ORB variants |
| Holmberg, Lönnbark & Lundström (2013), FRL 10 | ORB on crude oil futures | above-fair-game success rate | |

Not used: the overnight drift (Boyarchenko, Larsen & Whelan), which is held
outside the regular session and so is not intraday in the prop-firm sense;
and Yu, Rentzler & Wolf's NQ momentum/reversal pattern, whose conditioning on
yesterday's return, last night's return and Mondays adds parameters to fit.

Two lessons from the follow-up work shaped the method: **published intraday
edges shrink**, and **tuning their parameters does not help out of sample**
(on the noise area, neither a 27-variant grid nor a quarterly walk-forward
beat the paper's own parameters). So nothing here is tuned.

## 2. The rules, frozen

| name | rule | trades/week |
|---|---|---:|
| `noise` | σ_m = mean over the last 14 sessions of \|close m minutes after the open / open − 1\|. Upper = max(open, previous close) × (1 + σ_m), lower = min(open, previous close) × (1 − σ_m). At 10:00, 10:30, … 15:30: above upper → long, below lower → short. A long exits below max(upper, VWAP), flips below lower; shorts mirror. Flat at 16:00. | 4.6 |
| `noise-base` | the same, exiting only on the opposite band (the paper's first version) | 3.2 |
| `lasthalf` | at 15:30, the sign of the return from the previous close to 15:30; exit 16:00 (Baltussen et al.) | 4.9 |
| `lasthalf-gao` | the same, signed by the return to 10:00 (Gao et al.) | 4.9 |
| `orb` | first 5-minute candle up → buy 09:35, down → sell, doji → none. Stop at the candle's other end, target 10× that risk, else exit 16:00 | 4.9 |
| `noise+lasthalf+orb` | all three at once, one size each | 14.4 |

Fills: signals from a bar's close, market orders at the next bar's open. Stops
fill at the stop or a gapped open, the 10R target only one tick through, and
a bar touching both counts as the stop. Every market and stop fill pays **one
tick of slippage**, and every round turn **$4.50** (NQ) or **$1.50** (MNQ).
The look-ahead test in `tests/test_intraday.py` scrambles every bar after
each entry and checks that the entry does not change. On a simulated random
walk every strategy's mean is zero within noise.

## 3. Results on NAS100, 2005–2020

3,891 sessions. `--rescale 15000` scales each session so its previous close
is 15,000, roughly NQ in 2023–2025. That keeps every signal unchanged, but
puts 2005's index at 1,500 and 2019's at 7,500 on the same footing, with NQ
tick costs in today's terms. Per 1 NQ contract per strategy:

| strategy | trades/wk | win | PF | pts/trade | $/day | Sharpe | t | max DD | worst day |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| noise | 4.6 | 36% | 1.16 | +4.0 | +70 | 0.68 | 2.67 | $52,678 | −$17,838 |
| noise-base | 3.2 | 51% | 1.11 | +4.8 | +59 | 0.43 | 1.70 | $117,995 | −$23,259 |
| lasthalf | 4.9 | 50% | 1.15 | +2.6 | +46 | 0.62 | 2.43 | $63,830 | −$12,250 |
| lasthalf-gao | 4.9 | 51% | 1.15 | +2.6 | +46 | 0.63 | 2.46 | $56,680 | −$12,250 |
| orb | 4.9 | 21% | 1.14 | +3.6 | +66 | 0.60 | 2.36 | $79,760 | −$5,375 |
| **noise+lasthalf+orb** | 14.4 | 36% | 1.15 | +3.4 | +182 | **0.89** | **3.48** | $97,119 | −$32,594 |

Robustness:

| Sharpe | 2005–2012 | 2013–2020 | 0 ticks slippage | 2 ticks | positive years |
|---|---:|---:|---:|---:|---:|
| noise | 0.56 | 0.86 | 0.77 | 0.59 | 11 of 16 |
| lasthalf | 0.71 | 0.50 | 0.75 | 0.48 | 8 of 16 |
| orb | 0.45 | 0.81 | 0.68 | 0.52 | 10 of 16 |
| noise+lasthalf+orb | 0.78 | 1.04 | 1.02 | 0.75 | 11 of 16 |

- **All three have a positive, cost-robust edge on 15 years of data they
  were not designed on.** The two variants (`noise-base`, `lasthalf-gao`)
  are no better than the main versions, so keep the main ones.
- **The edge is lumpy.** Each strategy makes most of its money in volatile
  years (2007–08, 2018, 2020) and drifts near zero in calm ones. The
  portfolio by year: −2.2, −0.8, 1.9, 2.7, −0.2, −0.8, 1.3, 1.0, 0.8, 1.5,
  −0.4, 0.8, 0.3, 2.7, 0.5, 1.4 (Sharpe, 2005 to 2020). That matches the
  papers: momentum pays when volatility, and dealers' short gamma, is high.
- **They diversify.** Daily correlations: `lasthalf`/`orb` +0.08, `noise`/
  `lasthalf` +0.31, `noise`/`orb` +0.35. Together the Sharpe rises to 0.89.
- **The ORB does better here than in the replications** (Sharpe 0.6, against
  about zero net on NQ CFDs 2015–2026). One reason: at NQ's 15,000 level, a
  $14.50 round trip is a small fraction of a 5-minute range. Treat it as the
  weakest of the three until NQ data confirms it.

## 4. On prop-firm programs

`python -m intraday propfirm` starts an evaluation on every third session.
Each one trades these exact fills, checking the trailing floor and the daily
loss limit against open P&L bar by bar (best before worst inside a bar). On
a pass it trades a funded account with them for up to 250 sessions,
withdrawing the maximum as soon as the rules allow. The illustrative programs
are those in `configs/programs/`. Columns: pass rate; trader's net
payouts per funded account (a lower bound, since the data ends); EV per
evaluation bought.

| program | strategy, size | pass | blown in eval | net per funded | EV / eval | no-edge asymmetry2 EV |
|---|---|---:|---:|---:|---:|---:|
| Static 50K | noise+lasthalf+orb, 2 MNQ each | 38% | 48% | $2,132 | **$718** | $480 |
| | same, volatility-targeted (`vol1000`) | 38% | 59% | $2,228 | **$755** | |
| | orb, 2 MNQ | 22% | 25% | $3,236 | $608 | |
| | lasthalf, 10 MNQ | 29% | 69% | $3,173 | $837 | |
| EOD-trailing 50K | orb, 5 MNQ | 21% | 76% | $3,023 | **$536** | $325 |
| | lasthalf, 10 MNQ | 23% | 77% | $2,129 | $393 | |
| | noise+lasthalf+orb, 2 MNQ | 27% | 68% | $1,857 | $376 | |
| Intraday-trailing 50K | lasthalf, 5 MNQ | 24% | 57% | $973 | $132 | $126 |
| | noise+lasthalf+orb, 2 MNQ | 28% | 68% | $379 | $11 | |
| Intraday-trailing 150K | every strategy and size | 1–17% | | | −$177 to $84 | $127 |

What the replay says:

1. **The edge does not buy a higher pass rate.** Pass rates sit at or below
   the ceilings that any no-edge strategy reaches (README result 1: about 40%
   static, 30% intraday trailing). A Sharpe of 0.9 is $3,000 of drift every
   few months at these sizes, and the evaluation is decided by variance long
   before that. Larger size passes faster, and blows up more often.
2. **It pays once funded, on static and end-of-day floors.** A no-edge
   funded account is worth at most its drawdown (README result 3). These
   strategies take $2,100–3,200 net out of a $2,000-drawdown static account
   and $1,900–3,000 out of the EOD one, 1.2–2 times harvest's $1,500–1,600
   on the same programs. EV per evaluation is $540–840, against $325–480 for
   asymmetry2.
3. **Intraday-trailing floors are the wrong home for them.** The floor follows
   open profit, and all three strategies give much of it back: the noise
   area's trailing exits, the ORB's 10R runners, a 30-minute hold that
   reverses. On those programs they are worth no more than asymmetry2.
4. **The worst days are crash days.** Fixed size walks into 2008 and March
   2020. Volatility targeting, the noise area paper's own sizing, skips the
   wildest sessions and does at least as well (38% / $755 against 38% /
   $718), so use it.

## 5. Recommendation

- **Evaluation: do not expect these strategies to pass it for you.** Pass the
  evaluation the cheapest way the rules allow (asymmetry2's bold play), or
  trade the portfolio at small size and accept a pass rate near 35–40%.
- **Funded, on a static or end-of-day drawdown: trade `noise+lasthalf+orb`**,
  about 2 MNQ per strategy on a $2,000–2,500 drawdown, or `vol1000`.
  Withdraw the maximum the day it is allowed (asymmetry2, rule 4). Expect
  long flat stretches in calm markets. The edge arrives in volatile ones.
- **On intraday-trailing funded accounts**, use `lasthalf` alone if you trade
  these at all: one 30-minute position a day leaves the least open profit for
  the floor to ratchet on.
- **Check the funded rules.** All exits are by 16:00 ET, ahead of the usual
  flat-by deadlines (4:10 PM ET or later). The noise area and the ORB will
  sometimes trade through 08:30 and 10:00 data releases, so read any news
  clause. Consistency rules are not a problem: the best day is 6–10% of
  profit.
- **Verify on NQ before risking a payout.** These numbers are a CFD's,
  through May 2020. The 2025–26 compression the ES replication found may
  well show in NQ too.

## 6. The NQ 2022–2025 test, fixed in advance

Run on the Kaggle file (December 26, 2022 to December 11, 2025, about 740
sessions), at 1 tick of slippage, NQ costs and the rules above, nothing
changed. A strategy **passes** if, over the whole file:

- its Sharpe is above 0.5 and its t-statistic above 1.0 (three years is too
  short for 2.0: at a true Sharpe of 0.9, t ≈ 1.6), and
- it is positive in at least two of the three calendar years 2023–2025, and
- it stays positive at 2 ticks of slippage.

The portfolio is the primary test, and the three strategies are secondary.
If the portfolio fails, the recommendation in section 5 is withdrawn, not
re-tuned.

## Running it

Put the bars in `data/`, which git ignores. Any 1-minute file with a time
column and open, high, low, close and volume works: a CSV, a `.zip` holding
one, or an Excel workbook (`.xlsx`, first sheet; Excel's serial-number
timestamps are converted). A million-row workbook loads in about 30 seconds.

```bash
# How the file was read: zone (from 'timestamp ET'), bar labelling, contract rolls
python -m intraday inspect data/nq_1min.csv

# Statistics per 1 NQ contract, by year
python -m intraday backtest data/nq_1min.csv --by-year --trades data/trades.csv

# The pre-registered slippage check
python -m intraday backtest data/nq_1min.csv --slip-ticks 2 --strategies noise,lasthalf,orb,noise+lasthalf+orb

# Prop-firm replay
python -m intraday propfirm data/nq_1min.csv --strategies noise,lasthalf,orb,noise+lasthalf+orb \
    --sizes 2MNQ,5MNQ,vol1000
```

What `inspect` should show for the Kaggle file: zone America/New_York, and
bars labelled by their start, unless the opening volume spike says
otherwise. If the series is not back-adjusted, it should also show about 12
contract rolls, each a jump of +150 to +250 points in the two weeks before a
quarterly expiration. The previous close is shifted across each roll, so
gaps and bands stay in one contract's terms. If a detected roll looks wrong,
rerun with `--no-roll-adjust`. If the file starts before 09:30 and its
open-volume spike sits on the 09:31 bar, `--label end` is detected
automatically.

The NAS100 numbers above come from:

```bash
python -m intraday backtest "data/NAS100/*/*.csv" --no-roll-adjust --rescale 15000 --by-year --split 2013-01-01
python -m intraday propfirm "data/NAS100/*/*.csv" --no-roll-adjust --rescale 15000 --step 3 \
    --strategies noise,lasthalf,orb,noise+lasthalf+orb --sizes 2MNQ,5MNQ,10MNQ
```

with the CSVs from `pyfinancialdata/data/currencies/oanda/NAS100_USD` in the
[FutureSharks/financial-data](https://github.com/FutureSharks/financial-data)
repository, one folder per year.

## Caveats

- **CFD prices, not NQ fills, and nothing after May 2020.** Oanda's volume is
  a tick count, so its VWAP is approximate.
- **1-minute bars.** The order of a bar's high and low is unknown. The
  replay assumes the worst order for trailing floors.
- **Overlapping evaluations.** Starting one every third session makes them
  overlap, so the pass rates are descriptive, not independent trials.
- **Programs are illustrative.** Put your firm's current rules and prices in
  a TOML file and rerun `propfirm`.
- **No news calendar, no halts, no partial fills, no payout denials.**
