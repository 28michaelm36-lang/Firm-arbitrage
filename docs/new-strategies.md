# New structural-alpha strategies

asymmetry1 exploits one mispricing: an evaluation's price against the value
of passing it. Prop-firm rules contain others. Each strategy below comes from a
different term of

```
EV per evaluation  ~=  P(pass) x (split x eta x D_funded - activation)  -  evaluation price
```

and none of them needs a market edge.

## asymmetry2: drawdown harvest

**Source.** The firm gives you D dollars of loss capacity for a flat activation
fee. For a zero-edge trader, expected withdrawals are at most D (result 3 in the
README), so D is the prize. Most traders keep trading a funded account like an
evaluation, or like a normal account, and extract a small fraction of it. The
rest is lost to the trailing ratchet, to commissions, and to balances left in
the account when it blows or graduates. The firm keeps all of that.

**Mechanics** (`Harvest` in `propfirm/policies.py`):

1. **Sprint to the payout unlock, and not a dollar past it.** Work out the
   balance that unlocks the *largest* payout the rules allow next. That covers
   the payout threshold, the safety-net balance that must remain, the cap for
   this payout number, any cap as a fraction of profit, and the minimum payout.
   Aim one bold bracket exactly at it. Profit above that level cannot be
   withdrawn yet but can still be lost, so it has no value.
2. **Spread the sprint only as far as the consistency rule forces.** A 30% rule
   means at least 4 days, so the daily target is 30% of the planned window
   profit less a 2% margin. If a big day already dominates, the goal rises
   until the rule is satisfied again.
3. **Coast at minimum risk.** Once at the unlock balance, trade only to satisfy
   minimum days and profitable-day counts: one contract, a tight bracket, one
   attempt a day.
4. **Withdraw the maximum the day you are allowed**, then start the next cycle.

**Results** (gross withdrawn per funded account as a fraction of the funded drawdown):

| program               | eta with asymmetry1 funded | eta with harvest | EV per evaluation, asymmetry1+ -> asymmetry2 |
|-----------------------|---------------------------:|-----------------:|---------------------------------------------:|
| Intraday-trailing 50K |                      15.5% |            27.8% |                                  $36 -> $126 |
| EOD-trailing 50K      |                      37.0% |            75.9% |                                 $115 -> $325 |
| Static 50K            |                      87.1% |            93.2% |                                 $447 -> $480 |

Static accounts are already near the 100% ceiling, because only commissions
leak. Trailing accounts are where harvesting pays: the pre-lock ratchet and the
multi-day payout windows are expensive, and a bad funded policy compounds them.

**Knobs:** `max_risk_frac` (compliance cap per trade), `min_odds` (never risk
more than `need / min_odds`), `chunk` (payout size when the firm sets no cap),
`coast_odds` (reward:risk on profitable-day trades).

**Risks:** funded-account conduct rules (all-in trades, gambling clauses,
reward-to-risk caps) can deny a payout. Check them, then set `max_risk_frac` to
match.

## asymmetry3: menu arbitrage

**Source.** Firms price evaluations by account size and marketing calendar. The
value a program actually delivers is set by its rules: drawdown type, D/T,
consistency rules, caps and activation fees. The two are only loosely related,
so the value per dollar varies by several times across one firm's own menu and
across the year.

**Mechanics:** put every program you can buy into `configs/programs/`, with
the price you would actually pay, and run

```bash
python -m propfirm compare configs/programs/*.toml --strategies asymmetry2
```

Buy the top of the list, and only below its `max price` column. On the
illustrative programs, the value per evaluation ranges from $480 on the static
50K to $126 on the intraday-trailing 50K. The 150K intraday program has a
bigger drawdown but a lower D/T, and its pass rate halves (16.5%).

**What makes a program valuable, in order:**

1. **Drawdown type.** Static or end-of-day beats intraday trailing:
   `D/(D+T)` vs `exp(-T/D)`, which is 45% vs 30% at the same 2,500/3,000.
2. **D/T ratio.** It drives the pass-rate ceiling directly. Look at the
   drawdown you get per dollar of price, not the account size.
3. **No multi-day forcing in the evaluation.** Consistency rules and tight
   daily loss limits turn an end-of-day floor into a ratchet. The EOD example
   loses 12 points of pass rate to them: 28% instead of 40%.
4. **Funded extraction.** Low safety-net thresholds, caps at or above the
   drawdown, few profitable-day requirements, and a high split on the first
   dollars. These set eta.
5. **Activation vs monthly billing.** Under asymmetry2, funded accounts last 4–6
   trading days on average and 90% end within 11–19, less than one billing
   month. Where a firm offers both, a monthly funded fee usually beats a larger
   one-time activation fee.
6. **Promotions.** Price enters EV one for one. Intraday-trailing programs are
   only worth buying on deep discounts.

## Ideas worth modelling next

- **Account-slot allocation.** Firms cap accounts per trader. With a slot cap,
  prefer programs with the highest EV *per slot-day*, where fast pass/fail
  cycling helps. Without one, prefer EV per dollar.
- **Live-conversion rules.** Some firms move a funded account to a live account
  after N payouts and reset its balance. Anything above the floor at conversion
  is stranded, so harvest harder as the conversion approaches.
- **Scaling plans.** Contract limits that grow with the buffer force smaller,
  more costly legs early on. Add them to `Account` to see the cost.
- **Counterparty diversification.** A zero-edge structural strategy is a bet
  that the firm honours payouts. Spread accounts across firms rather than
  concentrating.

## Off-limits

These show up in "prop firm arbitrage" discussions. They are not included and
should not be used:

- **Opposite-direction hedging across your own accounts or firms** (long in one
  account, short in another so one side passes). Every major firm bans it,
  audits for it, and denies payouts. Presenting the passing side as genuine
  trading is misrepresentation.
- **Passing services, account sharing, and copying other people's accounts.**
- **Exploiting platform or data faults** (stale quotes, latency, fill bugs).

Everything in this repository stays inside the rules as written: it only
chooses sizes, targets and which programs to buy.
