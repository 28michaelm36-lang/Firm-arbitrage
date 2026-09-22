"""Prop-firm account economics: simulate evaluations, funded payouts and the
strategies that exploit the structure of the rules."""

from .bracket import Floor, simulate_bracket, tilt_from_winrate, win_probability
from .dp import solve_goal
from .instruments import INSTRUMENTS, Instrument
from .lifecycle import Attempt, Summary, run_attempt, simulate
from .policies import Asymmetry1, Bold, Harvest, Policy, parse_policy
from .rules import Drawdown, EvalRules, FundedRules, Program, load_program
