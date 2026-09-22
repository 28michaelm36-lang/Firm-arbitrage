# Manual evaluations

When you pass evaluations by hand, your pass rate is data, not something to
simulate. `manual` takes it as given and answers three questions:

1. Is the pass rate above what is possible **without an edge** on this
   program? If so, is the record long enough to rule out luck?
2. What is a funded account worth, under whichever funded policy you choose?
3. What is each evaluation worth, and how many do you need to buy?

```bash
python -m propfirm manual configs/programs/intraday_trailing_50k.toml \
    --pass-rate 0.5 --days 2 --attempts 10
```

```
Manual evaluations: Intraday-trailing 50K
  your pass rate                            50.0% in about 2 days per attempt
  from your record                          5 of 10; 95% range 23.7% to 76.3%
  best pass rate without an edge            28.8% (bold play with costs, simulated)
  chance of 5+ of 10 with no edge           13.0% -> could still be luck: keep logging
  attempts at 50% before luck is under 5%   about 18
Funded account: harvest, win rate 0.500 at reward:risk 1
  value once funded                         $553 (+/- 19), net payouts minus funded fees
Per evaluation ($35 x 1 billing period)
  EV                                        $242 ($96 to $387 across the 95% range)
  EV per evaluation day                     $121
  break-even pass rate                      6.3%
  evaluations for a 90% chance of a pass    4
```

## What a 50% pass rate in 2 days means

**It beats what the rules allow without an edge, on most programs.** No
strategy, however it is sized, passes a $2,500 intraday-trailing / $3,000
target evaluation more than about 30% of the time without an edge. With costs,
bold play reaches 29%. The best case for a no-edge trader is a static or
end-of-day floor: D/(D+T), which is 45% at $2,500/$3,000 and 40% at
$2,000/$3,000. A sustained 50% means an edge, unless the program's drawdown is
as large as its target (D/(D+T) = 50%).

**It needs a longer record.** How many attempts it takes before a 50% record
has under a 5% chance of being luck, depending on the program's no-edge pass
rate:

| no-edge pass rate of the program | attempts needed |
|---------------------------------:|----------------:|
|              29% (intraday trailing) |              18 |
|                              30% |              20 |
|                   40% (static $2,000) |              76 |
|                   45% (static $2,500) |             290 |

On intraday-trailing programs, 20 logged attempts settle it. On static ones,
the pass rate alone barely distinguishes skill from the rules' own 40–45%.

## Pair it with the best funded policy

Your pass rate only buys the funded account. What the account is worth depends
on how you trade it. EV per evaluation at a 50% manual pass rate:

| program               | price | harvest (no edge) | ORB 2,000:400 at 83.3% | ORB at 88% | ORB at 90% |
|-----------------------|------:|------------------:|-----------------------:|-----------:|-----------:|
| Intraday-trailing 50K |   $35 |              $242 |                    $53 |       $430 |       $827 |
| EOD-trailing 50K      |   $49 |              $629 |                   $182 |       $629 |     $1,109 |
| Static 50K            |   $99 |              $643 |                   $529 |     $1,085 |     $1,568 |

To value your own combination, run it with the funded policy and edge you
actually use:

```bash
python -m propfirm manual <program>.toml --pass-rate 0.5 --attempts 20 \
    --funded asymmetry1 --winrate 0.88 --winrate-rr 0.2
```

- If the ORB's live win rate is **below about 86%**, trade funded accounts
  with harvest, not the ORB.
- If you also trade funded accounts **by hand** with the edge that passes your
  evaluations, their value is higher than either column. The funded account is
  then no longer capped at its drawdown. Track manual funded results separately
  and compare them with the harvest value above.

## Experiments worth running

- **Half size, twice the days.** With a real edge, the dynamic program says
  smaller bets over more trades raise the pass rate (`optimize --winrate`).
  Pass a few evaluations at half your usual size and compare. If the pass rate
  rises, the edge is per trade, and it scales.
- **Log the failures too.** Record the day, the setup and the size of every
  attempt, passes and blows alike. A pass rate built only from memorable days
  is not a pass rate.
