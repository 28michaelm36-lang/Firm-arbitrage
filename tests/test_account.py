import random
import unittest

from propfirm.account import Account
from propfirm.instruments import Instrument
from propfirm.rules import Drawdown, EvalRules

FREE = Instrument("free", tick_value=1.0, round_turn=0.0, stop_ticks=100)


def rules(**kw):
    base = dict(start_balance=50_000, profit_target=3000, fee=0.0,
                drawdown=Drawdown(2000, "eod_trailing", lock_offset=0))
    base.update(kw)
    return EvalRules(**base)


class EodTrailing(unittest.TestCase):
    def test_floor_moves_only_at_end_of_day_and_locks(self):
        acct = Account.open(rules())
        self.assertEqual(acct.floor, 48_000)
        acct.balance = 51_500           # a winning day
        self.assertEqual(acct.floor, 48_000)
        acct.end_day()
        self.assertEqual(acct.floor, 49_500)
        acct.balance = 53_000
        acct.end_day()
        self.assertEqual(acct.floor, 50_000)  # locked at the starting balance
        acct.balance = 49_000
        acct.end_day()
        self.assertEqual(acct.floor, 50_000)

    def test_intraday_floor_is_static(self):
        acct = Account.open(rules())
        rng = random.Random(0)
        for _ in range(200):
            if acct.blown:
                break
            acct.trade(rng, 200, 200, FREE)
            self.assertEqual(acct.floor, 48_000)


class DailyLossLimit(unittest.TestCase):
    def test_pause_mode_ends_the_day(self):
        acct = Account.open(rules(daily_loss_limit=500))
        rng = random.Random(1)
        while True:
            out = acct.trade(rng, 1500, 100, FREE)
            if out is None or not out.won:
                break
        self.assertFalse(acct.blown)
        self.assertTrue(acct.day_over)
        self.assertGreaterEqual(acct.balance, acct.day_start - 500 - 1e-9)

    def test_loss_exactly_at_the_limit_counts(self):
        acct = Account.open(rules(daily_loss_limit=500, daily_loss_mode="fail"))
        rng = random.Random(4)
        while not acct.blown:
            acct.trade(rng, 500, 500, FREE)   # sized to end exactly at the limit
            if acct.day_pnl > 0:
                acct.end_day()
        self.assertAlmostEqual(acct.balance, acct.day_start - 500)

    def test_fail_mode_blows_the_account(self):
        acct = Account.open(rules(daily_loss_limit=500, daily_loss_mode="fail"))
        rng = random.Random(1)
        while not acct.blown:
            out = acct.trade(rng, 1500, 100, FREE)
            if out is None:
                break
        self.assertTrue(acct.blown)


if __name__ == "__main__":
    unittest.main()
