"""Prop-firm program rules.

A Program is an evaluation phase followed by a funded phase. Every number comes
from a TOML file (see configs/programs/). Firms change their rules often, so
nothing in this package is a statement about any particular firm: check your
firm's current rulebook and edit the config.
"""
from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path

DRAWDOWN_MODES = ("static", "intraday_trailing", "eod_trailing")
LOSS_LIMIT_MODES = ("pause", "fail")


@dataclass(frozen=True)
class Drawdown:
    amount: float
    mode: str = "intraday_trailing"
    # The floor stops trailing once it reaches start_balance + lock_offset.
    # None means it never locks.
    lock_offset: float | None = None

    def __post_init__(self):
        if self.amount <= 0:
            raise ValueError("drawdown amount must be positive")
        if self.mode not in DRAWDOWN_MODES:
            raise ValueError(f"drawdown mode must be one of {DRAWDOWN_MODES}, got {self.mode!r}")

    def lock_level(self, start_balance: float) -> float:
        return math.inf if self.lock_offset is None else start_balance + self.lock_offset


@dataclass(frozen=True)
class EvalRules:
    start_balance: float
    profit_target: float
    drawdown: Drawdown
    fee: float                          # price you actually pay per billing period
    fee_period_days: int | None = None  # trading days per billing period; None = one-time fee
    daily_loss_limit: float | None = None
    daily_loss_mode: str = "pause"      # "pause": done for the day; "fail": account fails
    min_days: int = 1
    max_days: int | None = None
    consistency: float | None = None    # best day must be <= this fraction of total profit
    max_contracts: float = math.inf     # in full-size (mini) contract equivalents

    def __post_init__(self):
        if self.daily_loss_mode not in LOSS_LIMIT_MODES:
            raise ValueError(f"daily_loss_mode must be one of {LOSS_LIMIT_MODES}")

    @property
    def target_balance(self) -> float:
        return self.start_balance + self.profit_target


@dataclass(frozen=True)
class FundedRules:
    start_balance: float
    drawdown: Drawdown
    activation_fee: float = 0.0
    monthly_fee: float = 0.0
    fee_period_days: int = 21
    daily_loss_limit: float | None = None
    daily_loss_mode: str = "pause"
    max_contracts: float = math.inf
    # Payout eligibility. Balances are offsets above start_balance.
    payout_min_balance: float = 0.0     # needed to request a payout
    payout_keep_balance: float = 0.0    # must remain after the payout ("safety net")
    min_days_between_payouts: int = 1   # trading days in the payout window
    min_profitable_days: int = 0
    profitable_day_amount: float = 0.0
    consistency: float | None = None    # best day <= this fraction of window profit
    payout_caps: tuple[float, ...] = () # cap for payout 1, 2, ...; the last value repeats
    payout_cap_fraction: float | None = None  # cap as a fraction of balance above start
    min_payout: float = 0.0
    split_full_amount: float = 0.0      # first $X of cumulative payouts paid at 100%
    profit_split: float = 0.9
    max_payouts: int | None = None      # account graduates (simulation stops) after this many

    def __post_init__(self):
        if self.daily_loss_mode not in LOSS_LIMIT_MODES:
            raise ValueError(f"daily_loss_mode must be one of {LOSS_LIMIT_MODES}")

    def payout_cap(self, index: int) -> float:
        if not self.payout_caps:
            return math.inf
        return self.payout_caps[min(index, len(self.payout_caps) - 1)]

    def trader_share(self, amount: float, paid_before: float) -> float:
        """What the trader receives from a gross payout of `amount`."""
        full = max(0.0, min(amount, self.split_full_amount - paid_before))
        return full + (amount - full) * self.profit_split


@dataclass(frozen=True)
class Program:
    name: str
    evaluation: EvalRules
    funded: FundedRules
    notes: str = ""

    def with_eval_fee(self, fee: float) -> "Program":
        return replace(self, evaluation=replace(self.evaluation, fee=fee))


def _build(cls, table: dict, where: str):
    known = {f.name for f in fields(cls)}
    unknown = set(table) - known
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)}")
    kwargs = dict(table)
    kwargs["drawdown"] = Drawdown(**kwargs["drawdown"])
    if "payout_caps" in kwargs:
        kwargs["payout_caps"] = tuple(float(c) for c in kwargs["payout_caps"])
    return cls(**kwargs)


def load_program(path: str | Path) -> Program:
    path = Path(path)
    data = tomllib.loads(path.read_text())
    unknown = set(data) - {"name", "notes", "evaluation", "funded"}
    if unknown:
        raise ValueError(f"{path}: unknown top-level keys {sorted(unknown)}")
    return Program(
        name=data.get("name", path.stem),
        evaluation=_build(EvalRules, data["evaluation"], f"{path} [evaluation]"),
        funded=_build(FundedRules, data["funded"], f"{path} [funded]"),
        notes=data.get("notes", ""),
    )
