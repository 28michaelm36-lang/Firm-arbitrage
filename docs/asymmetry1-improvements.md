# Improving asymmetry1

## What asymmetry1 is

An opening-range-breakout (ORB) trade: one per session, risking **$2,000 to
make $400**, in the evaluation and again once funded (`Asymmetry1` in
`propfirm/policies.py`). The first version of this doc wrongly modelled a 1:1
bracket; everything below is for the real 5:1 shape.

Run it at your own win rate. `--winrate-rr 0.2` says the win rate was
measured on a $400-for-$2,000 bracket:

```bash
python -m propfirm simulate configs/programs/intraday_trailing_50k.toml \
    --strategy asymmetry1 --winrate 0.88 --winrate-rr 0.2 --stop-ticks 160
```

Unless stated, numbers are for the illustrative intraday-trailing 50K program
($2,500 trailing drawdown, $3,000 target, $35 evaluation), NQ at $9 round
turn including slippage, and 20,000 simulated evaluations per cell.

## 1. The win rate that breaks even decides everything

A bracket risking $2,000 to make $400 wins **83.3%** of the time with no edge.
After a round-trip cost of c per trade, you need

```
break-even win rate = (2000 + c) / 2400
```

Each **$24 of cost per trade adds one point**:

| cost per trade                      | break-even win rate |
|-------------------------------------|--------------------:|
| none                                |               83.3% |
| 10 NQ at $9 ($90)                   |               87.1% |
| 100 MNQ at $2 ($200)                |               91.7% |

On a 5:1 shape, slippage on the stop matters as much as commission. Measure
the ORB's win rate on real fills, not on backtest prices.

## 2. Below about an 87% win rate, asymmetry2 is worth more than the ORB

EV per evaluation bought:

| ORB win rate | ORB $2,000:$400 | ORB $2,400:$480 | ORB $1,000:$200 | asymmetry2 (needs no edge) |
|-------------:|----------------:|----------------:|----------------:|---------------------------:|
| 83.3% (none) |             -$3 |             $20 |            -$53 |                       $125 |
|          86% |             $83 |            $132 |             $11 |                       $125 |
|          88% |            $250 |            $309 |            $237 |                       $125 |
|          90% |            $665 |            $675 |            $833 |                       $125 |
|          92% |          $1,557 |          $1,371 |          $2,023 |                       $125 |

asymmetry2 (bold evaluation plus drawdown harvest, see
[new-strategies.md](new-strategies.md)) earns its $125 from the rules alone.
If you cannot show at least 86–87% on live fills, switch to it. Above 88%,
keep the ORB. It is then a real edge, and the asymmetry of cheap evaluations
multiplies it.

Do not pass `--winrate-rr 0.2` edges to `bold` or `harvest`. The simulator
would carry the ORB's edge over to their different bracket shapes, and an ORB
win rate says nothing about those.

## 3. Cut the cost per trade (the $200 problem)

At $2,000 risk and a 10-point stop, MNQ needs 100 contracts, about $200 per
round trip. That is half of the $400 target:

| ORB win rate | NQ, 10 contracts | MNQ, 100 contracts |
|-------------:|-----------------:|-------------------:|
|          88% |             $250 |               -$24 |
|          90% |             $665 |                $30 |
|          92% |           $1,557 |               $185 |

- **Trade minis, not micros**, for the same dollar risk.
- **Set `--stop-ticks` to your real average ORB stop.** A wider stop means
  fewer contracts for the same $2,000, and a cost that is a smaller slice of
  the $400.
- **Check the commission template** if a session costs $200.

## 4. Size to the edge

The same ORB signal at a different size changes the result. Keep the 5:1
shape and scale the contract count:

- **Modest edge (86–88%): size up.** After $90 of cost, $2,400:$480 needs 8
  wins instead of 10 and beats $2,000:$400. Fewer trades means less cost and
  fewer chances to lose.
- **Large edge (90%+): size down.** $1,000:$200 has the highest EV, because
  the edge compounds over more trades, but it takes about 34 trading days to
  pass instead of 11.

This matches the dynamic program's rule: the less edge you have, the fewer and
bigger the bets; the more edge, the smaller. `python -m propfirm optimize`
shows it on a static floor.

## 5. Why a $2,000 stop on a $2,500 trailing drawdown is fragile

The trailing floor follows each $400 win up, so the room stays at $2,500. One
loss leaves about $500, and the next trade is effectively all-in for $400.
Passing therefore needs roughly **8–10 wins with at most one loss**:
`3000 / (400 - cost)` wins, so $90 of cost turns 8 required wins into 10. At
the no-edge win rate that gives about a 17% pass rate, against the 30%
ceiling that any strategy could reach on this program.

Program fit (no edge, EV per evaluation):

| program              | ORB   | note |
|----------------------|------:|------|
| Static 50K ($2,000)  | $277  | the stop equals the whole drawdown, so one loss ends it |
| EOD-trailing 50K     |  $19  | the $1,000 daily loss limit cuts the $2,000 stop in half |
| Intraday 50K         |  -$6  | see above |
| Intraday 150K        | -$90  | 23–29 wins needed; about 45 trading days, several billing months |

The 150K result also depends on `--max-eval-days` (default 60). Raise it if
you would keep paying for a slow evaluation.

## 6. Land the last trade exactly (asymmetry1+)

When the target is closer than one full $400 win, shrink the contracts
(keeping 5:1) so the last winner lands on the target instead of past it:
`asymmetry1:land_exactly=1`. Worth about +1 point of pass rate. Small, but
free.

## 7. Once funded, withdraw the maximum as soon as you can

Money left in a funded account is still at risk, and the firm keeps it when
the account is blown. The simulator always withdraws the maximum the rules
allow. Do the same.

## 8. Check the reward-to-risk rule

At least one major futures firm has capped funded-account trades at 5:1 risk
to reward. The ORB sits exactly on that line. A widened stop, or a stop that
slips, pushes it over, and a denied payout zeroes the EV. Read your firm's
current funded-account rules. If there is a cap, keep the planned stop
strictly inside it.

## 9. Never reset a live evaluation

Resetting to "refresh the drawdown" after a loss feels like buying back pass
probability. With unlimited re-buys it is always worse. Continuing is free,
and if the attempt fails you can still buy a fresh one. Let V be the value of
a pass, P your current pass probability, P_f a fresh one's, and f the fee.
Then

    value(continue) - value(reset now) = f x P / P_f > 0.

Use a reset only as a cheaper re-buy after a failure (`--fee <reset price>`).

## 10. Budget for the variance

At a 17–30% pass rate, you need 7–13 evaluations for a 90% chance of one
funded account (`simulate` prints this). Run several accounts on independent
days to smooth it. Copy-trading them in the same direction has correlation 1,
so it only multiplies the bet.

## 11. Calibrate against live fills

Log every trade: entry, stop, target, fill prices, commission, result. After
50–100 trades your win rate still has a standard error of 3–5 points. Put
its lower end into `--winrate` before deciding between items 2 and 4.
