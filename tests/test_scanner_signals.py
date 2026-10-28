import math
import random
import unittest
from datetime import date, timedelta

from scanner.data import Bar
from scanner.efficiency import MEAN_REVERTING, RANDOM_WALK, TRENDING, autocorrelation, measure, \
    variance_ratio
from scanner.indicators import atr, donchian, rolling_std, sma, zscore
from scanner.signals import CONTINUATION, LONG, MEAN_REVERSION, SHORT, Trigger, continuation, \
    mean_reversion, record


def bars_from(closes, spread=0.001):
    start = date(2025, 1, 1)
    return [Bar(start + timedelta(days=i), c, c * (1 + spread), c * (1 - spread), c)
            for i, c in enumerate(closes)]


def ar1_prices(phi, n, seed=1, sigma=0.01):
    rng = random.Random(seed)
    logs, r = [0.0], 0.0
    for _ in range(n):
        r = phi * r + sigma * rng.gauss(0, 1)
        logs.append(logs[-1] + r)
    return logs


class Indicators(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(sma([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])

    def test_rolling_std_is_sample_std(self):
        self.assertAlmostEqual(rolling_std([2, 4, 4, 4, 5, 5, 7, 9], 8)[-1], math.sqrt(32 / 7))

    def test_zscore(self):
        z = zscore([1, 1, 1, 4], 4)
        self.assertEqual(z[:3], [None, None, None])
        self.assertAlmostEqual(z[-1], (4 - 1.75) / math.sqrt(6.75 / 3))
        self.assertIsNone(zscore([5, 5, 5], 3)[-1])  # zero spread: undefined, not infinite

    def test_donchian_uses_prior_bars_only(self):
        bars = bars_from([10, 11, 12, 50], spread=0)
        upper, lower = donchian(bars, 3)
        self.assertEqual(upper, [None, None, None, 12])
        self.assertEqual(lower, [None, None, None, 10])

    def test_atr_includes_gaps(self):
        bars = [Bar(date(2025, 1, 1), 10, 11, 9, 10), Bar(date(2025, 1, 2), 14, 15, 14, 15)]
        self.assertEqual(atr(bars, 2), [None, (2 + 5) / 2])  # true range 15 - 10 spans the gap


class VarianceRatio(unittest.TestCase):
    def test_random_walk(self):
        vr, z = variance_ratio(ar1_prices(0.0, 2000), 5)
        self.assertAlmostEqual(vr, 1.0, delta=0.15)
        self.assertLess(abs(z), 2.5)

    def test_mean_reversion_and_trend(self):
        vr, z = variance_ratio(ar1_prices(-0.3, 1000), 5)
        self.assertLess(vr, 0.8)
        self.assertLess(z, -3)
        vr, z = variance_ratio(ar1_prices(0.3, 1000), 5)
        self.assertGreater(vr, 1.3)
        self.assertGreater(z, 3)

    def test_vr2_is_one_plus_rho1(self):
        logs = ar1_prices(0.2, 3000, seed=4)
        returns = [b - a for a, b in zip(logs, logs[1:])]
        vr, _ = variance_ratio(logs, 2)
        self.assertAlmostEqual(vr, 1 + autocorrelation(returns), delta=0.01)

    def test_robust_z_matches_homoskedastic_for_iid(self):
        logs = ar1_prices(0.0, 4000, seed=9)
        vr, z = variance_ratio(logs, 5)
        theta = 2 * (2 * 5 - 1) * (5 - 1) / (3 * 5)
        self.assertAlmostEqual(z, math.sqrt(4000) * (vr - 1) / math.sqrt(theta), delta=0.15)

    def test_measure_labels_regimes(self):
        closes = lambda phi: [math.exp(x) * 100 for x in ar1_prices(phi, 600)]
        self.assertEqual(measure(closes(-0.3)).regime, MEAN_REVERTING)
        self.assertEqual(measure(closes(0.3)).regime, TRENDING)
        self.assertEqual(measure(closes(0.0)).regime, RANDOM_WALK)
        self.assertEqual(measure(closes(0.0), window=100).n, 100)

    def test_too_short(self):
        with self.assertRaises(ValueError):
            variance_ratio([0.0, 0.1, 0.2], 5)


class Signals(unittest.TestCase):
    def setUp(self):
        self.closes = [100 + (0.5 if i % 2 else -0.5) for i in range(30)]

    def test_mean_reversion_fires_once_per_stretch(self):
        closes = self.closes + [96, 95, 100]
        bars = bars_from(closes)
        _, triggers = mean_reversion(closes, 20, 2.0, [b.day for b in bars])
        self.assertEqual([(t.index, t.direction) for t in triggers], [(30, LONG)])
        self.assertLess(triggers[0].level, -2)

    def test_mean_reversion_short(self):
        closes = self.closes + [104]
        _, triggers = mean_reversion(closes, 20, 2.0, [b.day for b in bars_from(closes)])
        self.assertEqual([(t.index, t.direction, t.strategy) for t in triggers], [(30, SHORT, MEAN_REVERSION)])

    def test_breakout_fires_on_the_first_close_outside(self):
        bars = bars_from(self.closes + [102, 103, 104, 95])
        upper, lower, triggers = continuation(bars, 20)
        self.assertEqual([(t.index, t.direction) for t in triggers], [(30, LONG), (33, SHORT)])
        self.assertEqual(triggers[0].level, upper[30])

    def test_record(self):
        bars = bars_from([100, 101, 102, 99, 98, 97])
        atrs = [2.0] * len(bars)
        triggers = [Trigger(0, bars[0].day, CONTINUATION, LONG, 100, 99),   # +2 after 2 bars: hit, +1 ATR
                    Trigger(1, bars[1].day, CONTINUATION, LONG, 101, 99),   # -2: miss, -1 ATR
                    Trigger(2, bars[2].day, CONTINUATION, SHORT, 102, 99),  # short, -4: hit, +2 ATR
                    Trigger(5, bars[5].day, CONTINUATION, LONG, 97, 99)]    # no bars after it yet
        r = record(triggers, bars, atrs, horizon=2)
        self.assertEqual((r.n, r.hits), (3, 2))
        self.assertAlmostEqual(r.mean_atr, (1 - 1 + 2) / 3)
        self.assertAlmostEqual(r.p_coin, 0.5)
        lo, hi = r.interval
        self.assertLess(lo, 2 / 3)
        self.assertGreater(hi, 2 / 3)
        self.assertIsNone(record([], bars, atrs, 2).hit_rate)


if __name__ == "__main__":
    unittest.main()
