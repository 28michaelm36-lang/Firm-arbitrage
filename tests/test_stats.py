import contextlib
import io
import unittest

from propfirm.cli import main
from propfirm.stats import attempts_to_rule_out_luck, binomial_tail, wilson_interval


class Stats(unittest.TestCase):
    def test_wilson_interval(self):
        lo, hi = wilson_interval(5, 10)
        self.assertAlmostEqual(lo, 0.2366, places=3)
        self.assertAlmostEqual(hi, 0.7634, places=3)

    def test_binomial_tail(self):
        self.assertAlmostEqual(binomial_tail(0, 10, 0.3), 1.0)
        self.assertAlmostEqual(binomial_tail(5, 10, 0.3), 0.150268, places=5)

    def test_attempts_to_rule_out_luck(self):
        self.assertEqual(attempts_to_rule_out_luck(0.5, 0.3), 20)
        self.assertIsNone(attempts_to_rule_out_luck(0.3, 0.4))


class ManualCommand(unittest.TestCase):
    def test_report(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["manual", "configs/programs/intraday_trailing_50k.toml",
                  "--pass-rate", "0.5", "--attempts", "10", "--n", "300"])
        text = out.getvalue()
        self.assertIn("5 of 10", text)
        self.assertIn("best pass rate without an edge", text)
        self.assertIn("value once funded", text)


if __name__ == "__main__":
    unittest.main()
