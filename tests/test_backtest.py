import contextlib
import io
import math
import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta

from propfirm.backtest import (All, Bar, Config, Far, Near, Range, backtest, evaluate,
                               load_sessions, parse_filters, run_trade)
from propfirm.cli import main
from propfirm.instruments import INSTRUMENTS

FREE = INSTRUMENTS["NQ"].with_overrides(round_turn=0.0)
T0 = datetime(2024, 3, 4, 9, 30)


def bars(*ohlc):
    return [Bar(T0 + timedelta(minutes=i), *x) for i, x in enumerate(ohlc)]


def synthetic(rng, days, step, theta=0.0, subs=20, n=390):
    """1-minute bars from a random walk sampled `subs` times a minute, pulled
    back toward 100 at rate theta per minute (0 = no pull)."""
    sessions = []
    for d in range(days):
        x, out = 100.0, []
        t = datetime(2024, 1, 1, 9, 30) + timedelta(days=d)
        for i in range(n):
            o = hi = lo = x
            for _ in range(subs):
                x += -theta / subs * (x - 100.0) + step * rng.gauss(0, 1) / math.sqrt(subs)
                hi, lo = max(hi, x), min(lo, x)
            out.append(Bar(t + timedelta(minutes=i), o, hi, lo, x, 1.0))
        sessions.append(out)
    return sessions


class Trades(unittest.TestCase):
    cfg = Config(stop=10.0, rr=0.2, through=0.0)

    def test_long_target(self):
        t, k = run_trade(bars((100, 101, 99, 100), (100, 102.5, 99, 102)), 0, 1, 10.0, self.cfg)
        self.assertEqual((t.outcome, t.r, k, t.minutes), ("win", 0.2, 1, 2))

    def test_long_stop_and_gap(self):
        t, _ = run_trade(bars((100, 101, 90, 91)), 0, 1, 10.0, self.cfg)
        self.assertEqual((t.outcome, t.r), ("loss", -1.0))
        t, _ = run_trade(bars((100, 101, 95, 95), (85, 86, 84, 85)), 0, 1, 10.0, self.cfg)
        self.assertAlmostEqual(t.r, -1.5)                 # gapped through the stop

    def test_ambiguous_bar_is_a_loss(self):
        t, _ = run_trade(bars((100, 103, 89, 95)), 0, 1, 10.0, self.cfg)
        self.assertEqual(t.outcome, "loss")
        self.assertTrue(t.ambiguous)

    def test_short_and_through(self):
        t, _ = run_trade(bars((100, 101, 98, 99)), 0, -1, 10.0, self.cfg)
        self.assertEqual(t.outcome, "win")
        through = Config(stop=10.0, rr=0.2, through=0.25)
        t, _ = run_trade(bars((100, 101, 98, 99)), 0, -1, 10.0, through)
        self.assertEqual(t.outcome, "time")               # touched 98 but never traded through
        self.assertAlmostEqual(t.r, 0.1)

    def test_follow_is_the_mirror_of_fade(self):
        sessions = synthetic(random.Random(5), days=3, step=1.0)
        fade = backtest(sessions, [Far(beyond=0.5)], Config(stop=4.0, side="fade"))
        follow = backtest(sessions, [Far(beyond=0.5)], Config(stop=4.0, side="follow"))
        self.assertTrue(fade)
        self.assertEqual(fade[0].entry_time, follow[0].entry_time)
        self.assertEqual(fade[0].side, -follow[0].side)

    def test_filters_parse(self):
        f = parse_filters("range:n=60,width=0.75+near:within=0.1")
        self.assertEqual(f, [Range(n=60, width=0.75), Near(within=0.1)])
        with self.assertRaises(ValueError):
            parse_filters("bogus")


class Theory(unittest.TestCase):
    """The backtester must reproduce the martingale result, and must detect
    mean reversion when there is some."""

    def test_random_walk_is_five_sixths_with_or_without_a_calm_filter(self):
        sessions = synthetic(random.Random(7), days=60, step=1.0)
        cfg = Config(stop=12.0, rr=0.2, side="both", through=0.0)
        for f in ([All()], [Range(n=30, width=1.0)]):
            r = evaluate("x", backtest(sessions, f, cfg), cfg, FREE)
            self.assertGreater(r.n, 500)
            self.assertAlmostEqual(r.win_rate, 5 / 6, delta=3 * r.win_rate_se + 0.005)

    def test_mean_reversion_near_the_mean_beats_it(self):
        # Stationary spread about 0.56 stops: the exact win rate from the mean is about 90%.
        sessions = synthetic(random.Random(7), days=40, step=1.0, theta=0.1)
        cfg = Config(stop=4.0, rr=0.2, side="fade", mean="vwap", through=0.0)
        r = evaluate("x", backtest(sessions, [Near(within=0.1)], cfg), cfg, FREE)
        self.assertGreater(r.win_rate - 3 * r.win_rate_se, 5 / 6)


class Loading(unittest.TestCase):
    def test_utc_to_session_across_dst(self):
        rows = ["time,open,high,low,close,volume",
                "2024-03-08 14:29:00,1,1,1,1,1",     # 09:29 EST: before the open
                "2024-03-08 14:30:00,2,2,2,2,1",     # 09:30 EST
                "2024-03-08 14:30:00,9,9,9,9,1",     # duplicate: first wins
                "2024-03-08 15:00:00,6,6,6,6,1",     # 10:00 EST
                "2024-03-11 13:30:00,3,3,3,3,1",     # 09:30 EDT, after the clocks change
                "2024-03-11 20:59:00,4,4,4,4,1",     # 16:59 EDT: after the close
                "2024-03-11 19:59:00,5,5,5,5,1"]     # 15:59 EDT
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bars.csv")
            with open(path, "w") as f:
                f.write("\n".join(rows))
            sessions = load_sessions([path])
        self.assertEqual([[b.c for b in s] for s in sessions], [[2.0, 6.0], [3.0, 5.0]])
        self.assertEqual(sessions[1][0].t, datetime(2024, 3, 11, 9, 30))

    def test_cli(self):
        sessions = synthetic(random.Random(3), days=5, step=1.0)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bars.csv")
            with open(path, "w") as f:
                f.write("datetime,open,high,low,close\n")
                for s in sessions:
                    for b in s:
                        f.write(f"{b.t.isoformat()}-05:00,{b.o},{b.h},{b.l},{b.c}\n")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main(["backtest", path, "--filters", "all", "range:n=30,width=1",
                      "--stop", "10", "--split", "2024-01-03"])
        text = out.getvalue()
        self.assertIn("5 sessions", text)
        self.assertIn("range:n=30,width=1", text)
        self.assertIn("2024-01..2024-01", text)


if __name__ == "__main__":
    unittest.main()
