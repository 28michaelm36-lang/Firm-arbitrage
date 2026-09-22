import glob
import math
import unittest
from dataclasses import replace

from propfirm.instruments import INSTRUMENTS
from propfirm.lifecycle import payout_available, simulate
from propfirm.policies import Asymmetry1, Bold, Harvest, parse_policy
from propfirm.rules import Drawdown, EvalRules, FundedRules, Program, load_program

FREE = INSTRUMENTS["NQ"].with_overrides(round_turn=0.0)


def static_program(**funded):
    ev = EvalRules(start_balance=50_000, profit_target=3000, fee=50.0,
                   drawdown=Drawdown(2000, "static"))
    fr = dict(start_balance=50_000, drawdown=Drawdown(2000, "static"), profit_split=1.0)
    fr.update(funded)
    return Program("static", ev, FundedRules(**fr))


class Payouts(unittest.TestCase):
    fr = FundedRules(start_balance=50_000, drawdown=Drawdown(2500, "intraday_trailing", 100),
                     payout_min_balance=2600, payout_keep_balance=2600,
                     min_days_between_payouts=8, min_profitable_days=5,
                     consistency=0.3, payout_caps=(2000, 3000), min_payout=500)

    def test_eligible(self):
        amount = payout_available(self.fr, 55_000, 0, 50_000, 8, 5, 1000)
        self.assertEqual(amount, 2000)                 # capped
        self.assertEqual(payout_available(self.fr, 55_000, 1, 50_000, 8, 5, 1000), 2400)

    def test_ineligible(self):
        self.assertEqual(payout_available(self.fr, 55_000, 0, 50_000, 7, 5, 1000), 0)   # days
        self.assertEqual(payout_available(self.fr, 55_000, 0, 50_000, 8, 4, 1000), 0)   # profitable days
        self.assertEqual(payout_available(self.fr, 55_000, 0, 50_000, 8, 5, 2000), 0)   # consistency
        self.assertEqual(payout_available(self.fr, 52_900, 0, 50_000, 8, 5, 500), 0)    # below min payout

    def test_split(self):
        fr = FundedRules(start_balance=0, drawdown=Drawdown(1), split_full_amount=1000, profit_split=0.9)
        self.assertAlmostEqual(fr.trader_share(500, 0), 500)
        self.assertAlmostEqual(fr.trader_share(1000, 500), 500 + 450)
        self.assertAlmostEqual(fr.trader_share(1000, 5000), 900)


class Theory(unittest.TestCase):
    def test_every_zero_edge_strategy_passes_at_the_same_rate_without_costs(self):
        # Static floor: P(pass) = D / (D + T) whatever the sizing, if you never overshoot.
        prog = static_program()
        for policy in (Bold(), Bold(max_risk_frac=0.2, min_odds=0), Asymmetry1(wins=3)):
            s = simulate(prog, policy, Harvest(), FREE, n=6000, seed=4)
            self.assertLess(abs(s.p_pass - 0.4), 4 * math.sqrt(0.24 / 6000), policy)

    def test_intraday_trailing_pass_rate_is_exponential(self):
        prog = load_program("configs/programs/intraday_trailing_50k.toml")
        p = math.exp(-3000 / 2500)
        for policy in (Bold(), Asymmetry1(wins=4)):
            s = simulate(prog, policy, Harvest(), FREE, n=6000, seed=5)
            self.assertLess(abs(s.p_pass - p), 4 * math.sqrt(p * (1 - p) / 6000), policy)

    def test_funded_withdrawals_are_bounded_by_the_drawdown(self):
        # Zero edge: expected gross withdrawals <= funded drawdown.
        prog = static_program(min_days_between_payouts=1)
        s = simulate(prog, Bold(), Harvest(), FREE, n=4000, seed=6, horizon_days=500)
        gross = [a.gross for a in s.funded]
        mean = sum(gross) / len(gross)
        sd = (sum((g - mean) ** 2 for g in gross) / len(gross)) ** 0.5
        self.assertLess(mean, 2000 + 4 * sd / math.sqrt(len(gross)))
        self.assertGreater(s.efficiency, 0.85)  # harvest leaves little on the table


class Smoke(unittest.TestCase):
    def test_all_programs_and_policies_run(self):
        nq = INSTRUMENTS["NQ"]
        for path in sorted(glob.glob("configs/programs/*.toml")):
            prog = load_program(path)
            for e, f in (("asymmetry1", "asymmetry1"), ("bold", "harvest"),
                         ("bold:max_risk_frac=0.3,min_odds=0", "harvest:min_odds=0.5")):
                s = simulate(prog, parse_policy(e), parse_policy(f), nq, n=300, seed=1)
                self.assertTrue(0 <= s.p_pass <= 1)
                self.assertTrue(all(a.gross >= 0 and a.net <= a.gross + 1e-9 for a in s.attempts))
                s.format()

    def test_edge_helps(self):
        prog = load_program("configs/programs/static_50k.toml")
        nq = INSTRUMENTS["NQ"]
        fair = simulate(prog, Bold(), Harvest(), nq, n=4000, seed=2)
        edge = simulate(prog, Bold(), Harvest(), nq, n=4000, seed=2, tilt=0.2)
        self.assertGreater(edge.p_pass, fair.p_pass)
        self.assertGreater(edge.ev, fair.ev)

    def test_monthly_fees(self):
        prog = static_program(monthly_fee=100, fee_period_days=5, activation_fee=0)
        prog = replace(prog, evaluation=replace(prog.evaluation, fee_period_days=21))
        s = simulate(prog, Bold(), Harvest(), FREE, n=300, seed=3, horizon_days=12)
        for a in s.funded:
            self.assertEqual(a.funded_fees, 100 * math.ceil(a.funded_days / 5))


if __name__ == "__main__":
    unittest.main()
