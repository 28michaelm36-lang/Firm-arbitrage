# Improving asymmetry1

## What asymmetry1 was taken to be

The asymmetry1 code and notes were not in this repository, so the baseline
(`Asymmetry1` in `propfirm/policies.py`) is a reconstruction of the idea:

> An evaluation costs a small fixed fee. Passing unlocks a funded account worth
> far more. The downside is capped at the fee, so trade the evaluation for a
> pass, not for P&L: one fixed bracket per day, sized so that N straight
> winners (default 2) hit the target. Keep using that bracket once funded.

If your version differs (sizes, R:R, trades per day, stop rules, how you trade
funded), set it with `--eval "asymmetry1:wins=3,reward_to_risk=1.5,trades_per_day=1"`,
or add it as a policy class, and rerun everything below. The improvements are
ranked by how much EV they added on the illustrative programs. All numbers are
at zero edge with NQ at $9 round turn unless stated.

## 1. Aim at the exact remaining distance, net of commissions

**The biggest single fix.** A fixed bracket leaks in two ways:

- **Commission shortfall.** Two $1,500 winners at $72 of commission each net
  $2,856, not $3,000, so a third win is needed.
- **Overshoot.** The third win lands $1,300 past the target. That profit
  counts for nothing, but it was paid for with pass probability.

With the costs set to zero, asymmetry1 passes the intraday-trailing 50K
evaluation 30.6% of the time, which is the theoretical ceiling. **Any**
commission drops it to about 16%:

| round turn (NQ) | asymmetry1 pass | asymmetry1+ pass |
|----------------:|----------------:|-----------------:|
|              $0 |           30.6% |            30.0% |
|           $4.50 |           16.2% |            29.4% |
|              $9 |           16.1% |            28.9% |
|             $18 |           15.4% |            28.0% |

That cliff matches the earlier observation that commissions push up the
consecutive-win requirement. The fix is in `bold_bracket`: every trade's
target is the remaining distance to the pass level **plus that trade's own
round-trip cost**, so a winner lands exactly on the target.

## 2. Go bold unless you have a measured edge

Result 1 in the README says sizing cannot beat the pass-rate ceiling; it can
only reduce the leak. The fewest trades means the least commission, and aiming
at the target means no overshoot. `Bold` risks the distance to the floor on one
bracket aimed at the target, and never risks more than it is trying to make
(`min_odds=1`, which is Dubins–Savage bold play near the goal).

The dynamic program shows when this stops being right
(`python -m propfirm optimize --program configs/programs/static_50k.toml --winrate W`):

| 1:1 win rate | optimal P(pass) | bold P(pass) | best first trade |
|-------------:|----------------:|-------------:|------------------|
| 50% (no edge)|          40.01% |       40.01% | all-in to target |
|          52% |          47.44% |       42.14% | risk ~$114 to make ~$918 |

With a real edge, the optimum becomes a **ladder** of small, long-odds brackets
aimed at the target: `bold:max_risk_frac=0.1,min_odds=0`. In the full simulator
the ladder matches bold at 50%, beats it from about 51%, and at 55% the plain
grind wins outright. Only move off bold play if your own fills show that edge
*after* costs.

## 3. Use a different policy once funded

The evaluation objective is "reach the target once". The funded objective is
"maximise total withdrawals". Using the evaluation bracket funded left most of
the account's value on the table. See `Harvest` and
[new-strategies.md](new-strategies.md#asymmetry2--drawdown-harvest). Gross
withdrawn per funded account, as a fraction of the funded drawdown:

| program               | asymmetry1 bracket funded | Harvest |
|-----------------------|--------------------------:|--------:|
| Intraday-trailing 50K |                     15.5% |   27.8% |
| EOD-trailing 50K      |                     37.0% |   75.9% |
| Static 50K            |                     87.1% |   93.2% |

Both columns use the same `Bold` evaluation (asymmetry1+ vs asymmetry2), so the
only difference is the funded policy.

## 4. Pick the program for its drawdown type before its size

The pass-rate ceiling depends on how the floor moves:

- a static floor, or an end-of-day trailing floor with the whole target in one
  session: `D / (D + T)`;
- an intraday trailing floor: `exp(-T / D)`. It follows every open high, so
  even a losing trade's unrealised profit costs you drawdown.

A $2,500 / $3,000 program gives 45% static and 30% intraday trailing. Rules that
force the target over several days, like consistency rules and daily loss
limits, re-introduce the end-of-day ratchet. That is why the EOD-trailing
example, which has a 50% consistency rule and a $1,000 daily limit, passes at
28% instead of 40%. See asymmetry3 in [new-strategies.md](new-strategies.md).

## 5. Only buy below the break-even price

`compare` prints the evaluation price at which EV is zero. On the intraday
trailing 50K example it is $43 for asymmetry1 and $161 for asymmetry2, against
the $35 promo price in the config. At list prices of $150 or more, the same
program is negative EV for the baseline and barely positive for the best
strategy. Treat promotions as the whole game for intraday-trailing programs.

## 6. Cut cost per dollar of risk

What matters is round-trip cost as a fraction of the dollar risk
(`Instrument.cost_rate`):

- **Use full-size contracts over micros** for the same dollar risk. At the
  defaults NQ costs 4.5% of risk and MNQ 10%. Intraday-trailing 50K asymmetry2
  EV: $122 with NQ vs $74 with MNQ.
- **Use wider stops** with fewer contracts (`--stop-ticks`).
- **Check your commission template.** At $18 per NQ round turn the baseline is
  negative EV on the intraday example.

## 7. Never reset a live evaluation

Resetting to "refresh the drawdown" feels like buying back pass probability.
With unlimited re-buys it is always worse. Continuing is free, and if the
attempt fails you can still buy a fresh one. Let V be the value of a pass, P
your current pass probability, P_f a fresh one's, and f the fee. Then

    value(continue) - value(reset now) = f x P / P_f > 0.

(A reset rule was tried in the simulator and lowered return on fees.) Use a
reset only as a cheaper **re-buy after a failure**, and model that with
`--fee <reset price>`. Resetting early only pays when you are short on time or
account slots.

## 8. Budget the bankroll for the variance

At a 28% pass rate you need 7 to 8 evaluations for a 90% chance of one funded
account (`attempts for 90% chance of a pass` in `simulate`). Under asymmetry2,
only 15–20% of funded accounts on the trailing examples ever reach a payout
(48% on the static one). The EV comes from the few that pay repeatedly. Size
the fee budget so a normal losing streak does not end the programme. Running several
accounts on independent days keeps the same EV with lower variance.
Copy-trading them in the same direction has correlation 1, so it only
multiplies the bet.

## 9. Check compliance before relying on the EV

Many firms restrict funded-account behaviour: maximum risk per trade,
reward-to-risk caps, "gambling" or all-in clauses, consistency rules and
hedging bans. A denied payout zeroes everything above. Model the compliant
version: `harvest:max_risk_frac=0.5` shows what a 50%-of-drawdown per-trade cap
costs on your program. Then decide with your firm's actual rulebook open.

## 10. Calibrate against real fills

Log every attempt: entries, exits, commissions, slippage, pass or fail. If your
real pass rate sits well below the model's ceiling, you are leaking through
slippage or rule friction. If it sits above, you may have an edge worth
measuring, and then item 2 says to size down.
