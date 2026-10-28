# Futures inefficiency scanner

A local website (`python -m scanner serve`) that watches thinly traded futures
markets. It measures which of them behave least like a random walk, and alerts
when a mean-reversion or continuation signal fires or when news about the market
breaks.

## The brief

The original request was: *"make a website that tracks inefficient futures
markets, like soybean oil, and alert when simple strategies like mean reversion
and continuation trigger. Also alert based off of related news to the markets."*
This is the sharper version it was built from:

> **Build a futures inefficiency scanner.** It is a local website that watches
> thinly traded futures markets and shows which ones behave least like a random
> walk. It alerts when a mean-reversion or continuation signal fires, or when
> market-moving news breaks.
>
> **Markets.** A default watchlist of 16 less-followed contracts, anchored on
> soybean oil (ZL): soybean meal, soybeans, oats, rough rice, KC wheat, lean
> hogs, live cattle, feeder cattle, cocoa, coffee, cotton, sugar, orange juice,
> palladium and platinum. All of it lives in one TOML file with each market's
> symbol, name, sector and news keywords. Daily bars come from Yahoo Finance's
> free chart endpoint (continuous front month, delayed). The latest bar is marked
> live until the session closes.
>
> **Measure "inefficient", don't assume it.** For each market, compute the
> Lo–MacKinlay variance ratio VR(5) on the last ~500 daily returns, with the
> heteroskedasticity-robust z statistic and the lag-1 autocorrelation. Label each
> market mean-reverting (z ≤ −1.96), trending (z ≥ +1.96) or random walk, and
> rank the markets by |z|.
>
> **Signals** (daily closes, parameters in the config):
> - *Mean reversion:* z = (close − 20-day mean) / 20-day standard deviation.
>   Trigger long when z first closes at or below −2, short at or above +2.
> - *Continuation:* 20-day Donchian breakout. Trigger long when the close first
>   exceeds the prior 20-day high, short when it first falls below the prior
>   20-day low.
> - An alert fires once, on the bar where the condition first appears, and says
>   whether the market's regime agrees with the signal.
> - Every signal shows its own record in that market over the loaded history:
>   number of triggers, hit rate after 5 days with a 95% Wilson interval, mean
>   move in ATR units, and the chance of that hit rate from coin flips. No costs
>   are included and no edge is claimed.
>
> **News.** Poll Google News RSS for each market every 10 minutes, plus any
> extra RSS or Atom feeds in the config. Score each headline: a market keyword
> scores 3 in the title and 1 in the summary only, and each high-impact term
> (USDA, WASDE, EPA, tariff, export ban, drought, …) adds 1. Alert at a score of 4
> or more for items under 36 hours old, and deduplicate the same story across
> outlets.
>
> **Alerts and UI.** Store alerts in SQLite. Show them in a live feed, push them
> as browser notifications, and optionally POST them to a webhook (ntfy, Slack,
> Discord or raw JSON). Put everything on one page:
> - a market table with last price, change, regime, z-score, channel position,
>   today's triggers and each signal's record;
> - a chart per market with the mean ±2σ band, the Donchian channel and past
>   triggers;
> - the market's news;
> - the alert feed.
>
> The page must work on a phone and in light and dark mode.
>
> **Constraints.**
> - Python 3.11+ standard library only, like the rest of the repo.
> - `python -m scanner serve` runs the site and `python -m scanner scan` prints
>   a one-shot terminal report. `--demo` runs on synthetic, clearly labelled data
>   with no network.
> - Unit tests cover the indicators, the variance ratio, signals and records, feed
>   parsing, news scoring, alert deduplication and the HTTP API.
> - Out of scope: order routing, intraday bars, paid data and roll-adjusted
>   series. Document that the unadjusted front-month series jumps at contract
>   rolls and can fake signals.

## Running it

```bash
# Live data: Yahoo Finance prices, Google News headlines
python -m scanner serve                 # http://127.0.0.1:8000

# No network: synthetic prices and headlines, labelled DEMO on the page
python -m scanner serve --demo

# One-shot report in the terminal (cron-friendly). Add --news to poll headlines too.
python -m scanner scan
python -m scanner scan --news --json > snapshot.json

# Your own watchlist and settings
cp configs/scanner.toml my-scanner.toml
python -m scanner serve --config my-scanner.toml --port 8080
```

Alerts persist in `scanner.db` (SQLite, set with `--db`), so a restart does not
fire them again. The server listens on 127.0.0.1 and has no login. Do not expose
it to the internet as it is.

**Phone alerts.** Set `webhook_url` in `[alerts]` of the config. The simplest
option is [ntfy](https://ntfy.sh): set `webhook_format = "ntfy"` and
`webhook_url = "https://ntfy.sh/<a-long-random-topic>"`, then subscribe to that
topic in the ntfy app. Slack and Discord incoming webhooks work with
`webhook_format = "slack"` or `"discord"`. `"json"` posts the whole alert
record.

**Browser alerts.** Click *Enable notifications* on the page. The page polls
for new alerts every 20 seconds while it is open.

## How it works

### Which markets, and why "inefficient"

Index, Treasury, crude and gold futures are traded by many fast, well-funded
participants, so simple price patterns in them are arbitraged away quickly.
The default watchlist is the other end of the spectrum: smaller open interest,
more commercial hedgers, and prices driven by government reports, weather and
policy. Soybean oil is the anchor. It moves on EPA biofuel mandates (RVOs), the
45Z tax credit, palm oil and Argentine crush, often in steps rather than as a
random walk.

That is a hypothesis, not a fact, so the scanner tests it market by market.

### The variance ratio

If prices are a random walk, the variance of q-day returns is q times the
variance of 1-day returns, and

```
VR(q) = Var(q-day return) / (q · Var(1-day return))  =  1.
```

- **VR below 1:** moves tend to be partly undone, so the market is
  mean-reverting.
- **VR above 1:** moves tend to be followed by more of the same, so the market
  is trending.

Approximately, `VR(q) ≈ 1 + 2 Σ (1 − j/q) ρ(j)` over lags j = 1 … q−1, where ρ(j)
is the autocorrelation of daily returns.

`scanner/efficiency.py` uses the Lo–MacKinlay (1988) estimator: overlapping
q-day returns, bias-corrected variances, and the z\* statistic, which stays
valid when volatility clusters (it always does in commodities). The page shows
VR, z\* and ρ(1), and ranks the markets by |z\*|:

| z\*              | label          | the strategy it favours |
|------------------|----------------|-------------------------|
| ≤ −1.96          | mean-reverting | mean reversion          |
| ≥ +1.96          | trending       | continuation            |
| in between       | random walk    | neither                 |

Each alert says whether the market's regime *agrees* with the signal. A
mean-reversion trigger in a trending market is a fade against the evidence.

### The signals

Both use daily closes. The latest bar is still forming while the market is open.

- **Mean reversion** (`lookback = 20`, `entry_z = 2`):
  `z = (close − mean of last 20 closes) / their standard deviation`. A **long**
  trigger is the first close with z ≤ −2, a **short** trigger the first with
  z ≥ +2. It does not fire again while z stays beyond the threshold.
- **Continuation** (`channel = 20`): a **long** trigger is the first close
  above the highest high of the previous 20 bars, a **short** trigger the first
  close below their lowest low.

Each trigger fires one alert per market, strategy, direction and bar date.
With `alert_on_live_bar = true` (the default), a trigger on the forming bar
alerts immediately and is marked *provisional*. If the close then pulls back,
the alert stays in the feed but the signal is gone. Set it to `false` to alert
on settled bars only.

### The record

A signal alone is not an edge. For every past trigger in the loaded history,
the scanner records where price was `horizon_days = 5` later, signed by the
trigger's direction and divided by the 20-day ATR at the trigger. It shows:

- `n`: the number of past triggers with 5 days of history after them;
- **hit rate**: the share that moved the right way, with its 95% Wilson
  interval;
- **mean move**: in ATR, so markets compare;
- **p vs coin**: the chance of at least that many hits if each were a coin
  flip.

How to read it:

- An interval that straddles 50%, or a large `p vs coin`, means the record is
  indistinguishable from luck.
- With 16 markets × 2 strategies, about 1–2 records will look "significant" at
  5% by chance alone.
- Costs, slippage and roll gaps are not included.
- Triggers that fire close together are not independent, so the interval is
  optimistic.

### News

Every `refresh_minutes`, the scanner fetches one Google News RSS search per
market (built from `news_query`, or from its keywords) plus the extra `feeds` in
the config. Each item is scored against every market:

| match                               | points |
|-------------------------------------|-------:|
| market keyword in the headline      | 3 each |
| market keyword only in the summary  | 1 each |
| high-impact term (`impact_terms`)   | 1 each |

- An item with no market keyword is ignored for that market.
- An item scoring `min_score` (default 4) or more alerts once, naming every
  market it matches, if it is newer than `max_age_hours`.
- Outlets are deduplicated by headline, so the same story from four sites
  alerts once.
- *Headline tone* counts up-words minus down-words ("surge", "rally" vs
  "plunge", "glut"). It describes the wording, not the market impact. For
  example, an export ban can be bullish for the price.

## Limits

- **Roll gaps.** Yahoo's `=F` symbols chain front-month contracts without back
  adjustment. When the front month rolls, the price jumps by the calendar
  spread. That can trigger a false breakout or a false z-score extreme, and it
  adds a little noise to the variance ratio. Check the contract calendar before
  acting on a trigger near expiry.
- **Data.** Yahoo's endpoint is free, unofficial, delayed 10–30 minutes and can
  change without notice. Daily bars only.
- **No backtest engine.** The record is a quick check of how each signal
  behaved, not a strategy backtest: no position sizing, exits, costs or
  portfolio effects.
- **News scoring is keyword matching**, not language understanding. Tune the
  keywords and `impact_terms` for your markets.
- None of this is trading advice. Check every signal against the contract, the
  calendar and your own risk.
