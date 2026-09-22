import math
import random
import unittest

from propfirm.bracket import Floor, simulate_bracket, tilt_from_winrate, win_probability


def brute_force(rng, equity, fl, stop, reward, step, n):
    """Fair +/-step random walk on P&L with an intraday trailing floor."""
    wins = breaches = 0
    for _ in range(n):
        pnl, hwm, level = 0.0, fl.hwm, fl.level
        while True:
            pnl += step if rng.random() < 0.5 else -step
            equity_now = equity + pnl
            if fl.trail is not None and equity_now > hwm:
                hwm = equity_now
                level = max(level, min(hwm - fl.trail, fl.lock))
            if pnl >= reward - 1e-9:
                wins += 1
                break
            if equity_now <= level + 1e-9:
                breaches += 1
                break
            if pnl <= -stop + 1e-9:
                break
    return wins / n, breaches / n


def model_rates(rng, equity, fl, stop, reward, n, cost=0.0, tilt=0.0):
    outs = [simulate_bracket(rng, equity, fl, stop, reward, cost, tilt) for _ in range(n)]
    return sum(o.won for o in outs) / n, sum(o.breached for o in outs) / n, outs


class ClosedForms(unittest.TestCase):
    def test_static_floor_is_gamblers_ruin(self):
        fl = Floor(level=47_500, hwm=50_000)
        self.assertAlmostEqual(win_probability(50_000, fl, 1000, 1500), 1000 / 2500)
        # stop beyond the floor: the floor is the stop
        self.assertAlmostEqual(win_probability(50_000, fl, 9999, 3000), 2500 / 5500)

    def test_tilt_matches_winrate_on_one_to_one(self):
        fl = Floor(level=0, hwm=50_000)
        for wr in (0.45, 0.5, 0.55):
            p = win_probability(50_000, fl, 100, 100, tilt=tilt_from_winrate(wr))
            self.assertAlmostEqual(p, wr)
        self.assertEqual(tilt_from_winrate(0.5), 0.0)

    def test_trailing_all_in_is_exponential(self):
        fl = Floor(level=47_500, hwm=50_000, trail=2500)
        self.assertAlmostEqual(win_probability(50_000, fl, 2500, 3000), math.exp(-3000 / 2500))

    def test_trailing_with_tight_stop(self):
        d, r, w = 2500, 1000, 2000
        fl = Floor(level=50_000 - d, hwm=50_000, trail=d)
        expected = (r / d) * math.exp(-(w - (d - r)) / d)
        self.assertAlmostEqual(win_probability(50_000, fl, r, w), expected)

    def test_trailing_lock(self):
        # floor trails until start + 100, then is static above entry
        fl = Floor(level=47_500, hwm=50_000, trail=2500, lock=50_100)
        expected = math.exp(-2600 / 2500) * 2500 / (4600 - 100)
        self.assertAlmostEqual(win_probability(50_000, fl, 2500, 4600), expected)

    def test_costs_are_paid_at_entry(self):
        fl = Floor(level=47_500, hwm=50_000)
        self.assertAlmostEqual(win_probability(50_000, fl, 9999, 3100, cost=100), 2400 / 5500)


class Sampling(unittest.TestCase):
    def test_sampled_win_rate_matches_closed_form(self):
        rng = random.Random(1)
        for fl, stop, reward in [
            (Floor(47_500, 50_000), 1000, 1500),
            (Floor(47_500, 50_000, 2500), 2500, 3000),
            (Floor(47_500, 50_000, 2500, 50_100), 700, 3500),
        ]:
            n = 20_000
            won, _, _ = model_rates(rng, 50_000, fl, stop, reward, n)
            p = win_probability(50_000, fl, stop, reward)
            self.assertLess(abs(won - p), 4 * math.sqrt(p * (1 - p) / n))

    def test_matches_brute_force_random_walk(self):
        rng = random.Random(2)
        fl = Floor(level=47_500, hwm=50_000, trail=2500)
        n = 3000
        bw, bb = brute_force(rng, 49_400, fl, 800, 2500, 25.0, n)
        mw, mb, _ = model_rates(rng, 49_400, fl, 800, 2500, 20_000)
        self.assertLess(abs(bw - mw), 4 * math.sqrt(mw * (1 - mw) / n) + 0.01)
        self.assertLess(abs(bb - mb), 4 * math.sqrt(mb * (1 - mb) / n) + 0.01)

    def test_outcomes_are_consistent(self):
        rng = random.Random(3)
        fl = Floor(level=47_500, hwm=50_000, trail=2500, lock=50_100)
        _, _, outs = model_rates(rng, 50_000, fl, 1200, 2000, 5000, cost=20)
        for o in outs:
            self.assertGreaterEqual(o.floor, fl.level)
            self.assertLessEqual(o.floor, fl.lock)
            if o.won:
                self.assertAlmostEqual(o.pnl, 1980)
            elif o.breached:
                self.assertAlmostEqual(50_000 + o.pnl, o.floor)
            else:
                self.assertAlmostEqual(o.pnl, -1220)
                self.assertLess(o.floor, 50_000 - 1220)

    def test_pnl_is_a_martingale_before_costs(self):
        rng = random.Random(4)
        fl = Floor(level=47_500, hwm=50_000, trail=2500)
        n = 40_000
        pnls = [simulate_bracket(rng, 50_000, fl, 1500, 2000).pnl for _ in range(n)]
        mean = sum(pnls) / n
        sd = (sum((p - mean) ** 2 for p in pnls) / n) ** 0.5
        self.assertLess(abs(mean), 4 * sd / math.sqrt(n))


if __name__ == "__main__":
    unittest.main()
