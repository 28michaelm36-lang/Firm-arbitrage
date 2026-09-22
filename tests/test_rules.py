import glob
import tempfile
import unittest
from pathlib import Path

from propfirm.rules import Drawdown, load_program


class Loading(unittest.TestCase):
    def test_bundled_programs_load(self):
        paths = sorted(glob.glob("configs/programs/*.toml"))
        self.assertTrue(paths)
        for path in paths:
            prog = load_program(path)
            self.assertGreater(prog.evaluation.profit_target, 0)
            self.assertGreater(prog.funded.drawdown.amount, 0)

    def test_unknown_keys_are_rejected(self):
        text = Path("configs/programs/static_50k.toml").read_text()
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.toml"
            bad.write_text(text.replace("min_days = 1", "min_dayz = 1"))
            with self.assertRaises(ValueError):
                load_program(bad)

    def test_bad_drawdown_mode(self):
        with self.assertRaises(ValueError):
            Drawdown(2000, "trailing")


if __name__ == "__main__":
    unittest.main()
