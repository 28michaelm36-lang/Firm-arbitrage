import unittest

from propfirm.bracket import tilt_from_winrate
from propfirm.dp import solve_goal


class GoalProgram(unittest.TestCase):
    def test_fair_and_free_is_linear(self):
        sol = solve_goal(2000, 3000, grid=20)
        for i, v in enumerate(sol.values):
            self.assertAlmostEqual(v, i / 20, places=9)

    def test_bold_is_near_optimal_without_edge(self):
        sol = solve_goal(2500, 3000, lambda r: 0.04 * r, grid=22)
        opt, bold = sol.value_at(2500), sol.bold_at(2500)
        self.assertGreaterEqual(opt, bold - 1e-12)
        self.assertLess(opt - bold, 0.01)

    def test_edge_favours_smaller_bets(self):
        sol = solve_goal(2500, 3000, lambda r: 0.0, tilt=tilt_from_winrate(0.53), grid=22)
        self.assertGreater(sol.value_at(2500) - sol.bold_at(2500), 0.1)
        risk, _ = sol.action_at(2500)
        self.assertLess(risk, 2500)


if __name__ == "__main__":
    unittest.main()
