import contextlib
import csv
import io
import math
import os
import random
import tempfile
import unittest
from datetime import date

from intraday.boldharvest import (Coin, FirstCandle, Fixed, Gap, Momentum, Noise, OpeningRange,
                                  Settings, Sizer, Tape, all_runs, campaign, parse_signal, parse_stop,
                                  resolve, study, summarize_runs)
from intraday.cli import main
from intraday.data import Bar, Day
from intraday.strategies import INSTRUMENT_COSTS, Costs
from propfirm.bracket import Floor
from propfirm.policies import Bold, Harvest
from propfirm.rules import load_program
from tests.test_intraday import make_day, random_walk_days

FREE = Settings(nq=Costs(slip_ticks=0, commission=0),
                mnq=Costs(point_value=2.0, slip_ticks=0, commission=0))
NO_FLOOR = Floor(-math.inf, math.inf)
PROGRAMS = os.path.join(os.path.dirname(__file__), "..", "configs", "programs")


def tape_of(opens, highs, lows, closes, stop="10"):
    day = make_day(date(2024, 1, 2), closes, 100.0, opens, highs, lows)
    return Tape([day], stop, time_scaled=False)


def bracket(tape, side=1, balance=50_000.0, floor=NO_FLOOR, stop=200.0, reward=200.0,
            per_point=20.0, cost=0.0, slip_usd=0.0, k=0):
    return resolve(tape, 0, k, side, balance, floor, stop, reward, per_point, cost, slip_usd, 0.25)


class TestFills(unittest.TestCase):
    def test_target_needs_a_tick_through(self):
        # $200 at $20 a point is 10 points: the target is 110.
        t = tape_of([100, 100, 100], [100, 110, 110.25], [100, 100, 100], [100, 100, 100])
        f = bracket(t)
        self.assertEqual((f.reason, f.exit_index), ("target", 2))
        self.assertAlmostEqual(f.out.pnl, 200.0)
        self.assertTrue(f.out.won)

    def test_bar_touching_both_is_the_stop(self):
        t = tape_of([100, 100], [100, 111], [100, 89], [100, 100])
        f = bracket(t)
        self.assertEqual(f.reason, "stop")
        self.assertAlmostEqual(f.out.pnl, -200.0)

    def test_gap_through_the_stop_fills_at_the_open(self):
        t = tape_of([100, 85], [100, 86], [100, 84], [100, 85])
        f = bracket(t, slip_usd=5.0)
        self.assertEqual(f.reason, "stop")
        self.assertAlmostEqual(f.out.pnl, 20 * (85 - 100) - 5.0)

    def test_costs_come_off_both_ways(self):
        win = bracket(tape_of([100, 100], [100, 111], [100, 100], [100, 100]), cost=14.0, slip_usd=5.0)
        self.assertAlmostEqual(win.out.pnl, 200.0 - 14.0)       # reward is measured from the entry
        loss = bracket(tape_of([100, 100], [100, 100], [100, 89], [100, 100]), cost=14.0, slip_usd=5.0)
        self.assertAlmostEqual(loss.out.pnl, -200.0 - 14.0 - 5.0)
        self.assertAlmostEqual(loss.costs, 19.0)

    def test_floor_inside_the_stop_ends_the_account(self):
        t = tape_of([100, 100], [100, 100], [100, 94.9], [100, 100])
        f = bracket(t, floor=Floor(49_900.0, 50_000.0))
        self.assertEqual(f.reason, "floor")
        self.assertTrue(f.out.breached)
        self.assertAlmostEqual(f.out.pnl, -100.0)

    def test_trailing_floor_follows_the_best_price_first(self):
        # Up 50 points lifts the floor $1,000. The next bar makes a new high and
        # then falls back to it: the high is taken to come first.
        t = tape_of([100, 150, 150], [150, 150, 160], [100, 60, 59], [150, 150, 100])
        f = bracket(t, floor=Floor(48_000.0, 50_000.0, trail=2_000.0), stop=2_000.0, reward=3_000.0)
        self.assertEqual((f.reason, f.exit_index), ("floor", 2))
        self.assertAlmostEqual(f.out.hwm, 51_200.0)
        self.assertAlmostEqual(f.out.floor, 49_200.0)
        self.assertAlmostEqual(f.out.pnl, -800.0)

    def test_trailing_floor_stops_at_its_lock(self):
        t = tape_of([100, 100], [100, 300], [100, 100], [100, 100])
        f = bracket(t, floor=Floor(48_000.0, 50_000.0, trail=2_000.0, lock=50_100.0),
                    stop=2_000.0, reward=3_000.0)
        self.assertEqual(f.reason, "target")
        self.assertAlmostEqual(f.out.hwm, 53_000.0)
        self.assertAlmostEqual(f.out.floor, 50_100.0)

    def test_static_floor_does_not_move(self):
        t = tape_of([100, 100], [100, 300], [100, 100], [100, 100])
        f = bracket(t, floor=Floor(48_000.0, 50_000.0), stop=2_000.0, reward=3_000.0)
        self.assertAlmostEqual(f.out.floor, 48_000.0)

    def test_time_exit_at_the_last_close(self):
        t = tape_of([100, 100, 100], [100, 104, 104], [100, 99, 99], [100, 103, 102])
        f = bracket(t, slip_usd=5.0)
        self.assertEqual((f.reason, f.exit_index), ("time", 2))
        self.assertAlmostEqual(f.out.pnl, 20 * 2 - 5.0)

    def test_short_is_the_mirror(self):
        t = tape_of([100, 100], [100, 100], [100, 89], [100, 90])
        f = bracket(t, side=-1)
        self.assertEqual(f.reason, "target")
        self.assertAlmostEqual(f.out.pnl, 200.0)


class TestSizing(unittest.TestCase):
    def setUp(self):
        self.sizer = Sizer(50.0, INSTRUMENT_COSTS["NQ"], INSTRUMENT_COSTS["MNQ"])

    def test_minis_for_a_big_trade_micros_for_a_small_one(self):
        n, c = self.sizer.lots(2_000.0)
        self.assertEqual((n, c.point_value), (2, 20.0))
        n, c = self.sizer.lots(200.0)
        self.assertEqual((n, c.point_value), (2, 2.0))
        n, c = self.sizer.lots(25.0)
        self.assertEqual((n, c.point_value), (1, 2.0))

    def test_contract_cap(self):
        n, _ = self.sizer.lots(20_000.0, max_minis=5)
        self.assertEqual(n, 5)

    def test_cost_is_commission_plus_one_tick(self):
        self.assertAlmostEqual(self.sizer.cost(2_000.0), 2 * (4.50 + 5.00))

    def test_stop_specs(self):
        self.assertEqual(parse_stop("50"), ("points", 50.0))
        self.assertEqual(parse_stop("0.3%"), ("percent", 0.003))
        self.assertEqual(parse_stop("0.25r"), ("range", 0.25))


class TestSignals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.days = random_walk_days(40, seed=11, sd=6.0)
        cls.tape = Tape(cls.days)
        cls.signals = [Coin(), Fixed(1), Fixed(-1), OpeningRange(15), OpeningRange(30, fade=1),
                       FirstCandle(5), Momentum(30), Momentum(120, ref="open"), Gap(), Gap(fade=1),
                       Noise()]

    def test_no_signal_looks_ahead(self):
        # Scramble every bar from the entry bar on: the entry must not change.
        rng = random.Random(5)
        fired = 0
        for sig in self.signals:
            for i in range(20, 40):
                entry = sig(self.tape, i, 1)
                if entry is None:
                    continue
                fired += 1
                j = entry[0]
                day = self.days[i]
                junk = [Bar(b.t, *(sorted(rng.uniform(1, 20_000) for _ in range(4))[x]
                                   for x in (1, 3, 0, 2)), 1.0) for b in day.bars[j:]]
                changed = list(self.days)
                changed[i] = Day(day.date, day.bars[:j] + junk, day.prev_close)
                self.assertEqual(sig(Tape(changed), i, 1), entry, f"{sig.name} on day {i}")
        self.assertGreater(fired, 150)

    def test_entries_respect_the_earliest_bar(self):
        for sig in self.signals:
            for k in (1, 50, 200):
                entry = sig(self.tape, 25, k)
                if entry is not None:
                    self.assertGreaterEqual(entry[0], k, sig.name)
                    self.assertIn(entry[1], (1, -1))

    def test_opening_range_breakout(self):
        closes = [100.0] * 15 + [100.5, 101.5, 101.0]
        highs = [101.0] * 15 + [100.8, 101.6, 101.2]
        lows = [99.0] * 15 + [100.0, 100.5, 100.9]
        tape = Tape([make_day(date(2024, 1, 2), closes, 100.0, highs=highs, lows=lows)], "10")
        self.assertEqual(OpeningRange(15)(tape, 0, 1), (17, 1))      # the close above 101, next bar
        self.assertEqual(OpeningRange(15, fade=1)(tape, 0, 1), (17, -1))

    def test_parse(self):
        sig = parse_signal("orb:n=30,fade=1")
        self.assertEqual((sig.n, sig.fade, sig.name), (30, 1, "orb30-fade"))
        self.assertEqual(parse_signal("mom:t=120,ref=open").name, "mom120-open")
        with self.assertRaises(ValueError):
            parse_signal("nonsense")


class TestRandomWalk(unittest.TestCase):
    """On a driftless market nothing has an edge, and the pass rates are the
    ones the rules imply (README, result 1)."""

    @classmethod
    def setUpClass(cls):
        cls.tape = Tape(random_walk_days(900, seed=3, sd=6.0))

    def _pass_rate(self, name):
        program = load_program(os.path.join(PROGRAMS, name))
        runs = all_runs(program, Bold(), Harvest(), self.tape, Coin(), FREE, first=20)
        return summarize_runs(program, "coin", runs.values())

    def test_no_signal_has_an_edge(self):
        for sig in (Coin(), Fixed(1), OpeningRange(15), Noise()):
            s = study(self.tape, sig, rr=1.5, cfg=FREE, first=20)
            self.assertLess(abs(s.t), 3.0, f"{sig.name}: mean R {s.mean:+.3f}, t {s.t:+.1f}")

    def test_static_floor_passes_at_d_over_d_plus_t(self):
        s = self._pass_rate("static_50k.toml")
        self.assertAlmostEqual(s.p_pass, 2000 / 5000, delta=0.05)

    def test_intraday_trailing_floor_passes_at_exp_minus_t_over_d(self):
        s = self._pass_rate("intraday_trailing_50k.toml")
        self.assertAlmostEqual(s.p_pass, math.exp(-3000 / 2500), delta=0.05)

    def test_funded_account_is_worth_at_most_its_drawdown(self):
        s = self._pass_rate("static_50k.toml")
        self.assertLess(s.efficiency, 1.15)
        self.assertGreater(s.efficiency, 0.7)


class TestDrift(unittest.TestCase):
    def test_a_real_edge_shows_up(self):
        rng = random.Random(2)
        days, price, prev = [], 10_000.0, None
        for n in range(300):
            closes = []
            for _ in range(390):
                price += rng.gauss(0.15, 6.0)        # a steady upward drift
                closes.append(price)
            day = make_day(date.fromordinal(date(2021, 1, 1).toordinal() + n), closes, prev)
            days.append(day)
            prev = day.close
        tape = Tape(days)
        up = study(tape, Fixed(1), cfg=FREE, first=20)
        down = study(tape, Fixed(-1), cfg=FREE, first=20)
        self.assertGreater(up.mean, 0.1)
        self.assertLess(down.mean, -0.1)
        self.assertGreater(up.win_eq, 0.44)


class TestCampaign(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tape = Tape(random_walk_days(300, seed=9, sd=6.0))
        cls.program = load_program(os.path.join(PROGRAMS, "eod_trailing_50k.toml"))
        cls.runs = all_runs(cls.program, Bold(), Harvest(), cls.tape, Coin(), Settings(), first=20)

    def test_runs_follow_one_another(self):
        c = campaign(self.runs, 20, len(self.tape.days))
        self.assertGreater(c.evaluations, 10)
        for a, b in zip(c.runs, c.runs[1:]):
            self.assertEqual(b.start, a.end + 1)

    def test_net_is_cash_in_minus_cash_out(self):
        c = campaign(self.runs, 20, len(self.tape.days))
        self.assertAlmostEqual(c.net, sum(r.profit for r in c.runs))
        self.assertAlmostEqual(c.net, sum(x for _, x in c.cash_flows()))
        self.assertAlmostEqual(c.net, c.trader_share - c.eval_fees - c.funded_fees)
        self.assertAlmostEqual(sum(c.by_year(self.tape.days).values()), c.net)
        self.assertGreaterEqual(c.worst_drawdown, self.program.evaluation.fee)

    def test_every_evaluation_pays_its_fee_and_costs(self):
        ev = self.program.evaluation
        for r in self.runs.values():
            self.assertGreaterEqual(r.attempt.eval_fees, ev.fee)
            self.assertGreater(r.costs, 0)
            self.assertGreater(r.trades, 0)
            if r.attempt.passed:
                self.assertGreaterEqual(r.attempt.eval_days, ev.min_days)
                self.assertAlmostEqual(r.attempt.funded_fees, self.program.funded.activation_fee)

    def test_undecided_evaluations_are_left_out(self):
        last = len(self.tape.days) - 1
        self.assertTrue(all(r.end <= last for r in self.runs.values()))
        self.assertLess(len(self.runs), len(self.tape.days) - 20 + 1)


class TestCommandLine(unittest.TestCase):
    def test_boldharvest_and_signals_run_on_a_csv(self):
        days = random_walk_days(60, seed=4, sd=6.0)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bars.csv")
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["timestamp ET", "open", "high", "low", "close", "volume"])
                for d in days:
                    for b in d.bars:
                        w.writerow([b.t.isoformat(sep=" "), b.o, b.h, b.l, b.c, 1])
            ledger = os.path.join(tmp, "ledger.csv")
            common = [path, "--label", "start", "--no-roll-adjust"]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main(["boldharvest", *common, "--signals", "coin;orb:n=15", "--coins", "2",
                      "--programs", os.path.join(PROGRAMS, "static_50k.toml"), "--ledger", ledger])
                main(["signals", *common, "--signals", "coin;noise"])
            text = out.getvalue()
            self.assertIn("net profit", text)
            self.assertIn("coin flip, mean of 2", text)
            self.assertIn("mean R", text)
            with open(ledger) as f:
                rows = list(csv.DictReader(f))
            self.assertGreater(len(rows), 5)
            self.assertIn("trader_share", rows[0])


if __name__ == "__main__":
    unittest.main()
