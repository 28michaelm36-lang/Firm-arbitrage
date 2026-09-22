"""Futures contracts and round-trip costs."""
from __future__ import annotations

import math
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Instrument:
    name: str
    tick_value: float
    round_turn: float        # commission + exchange fees + expected slippage, per contract
    stop_ticks: float        # typical stop width, used to turn a $ risk into a contract count
    mini_equivalent: float = 1.0

    @property
    def cost_rate(self) -> float:
        """Round-trip cost as a fraction of the dollar risk of one contract."""
        return self.round_turn / (self.stop_ticks * self.tick_value)

    def contracts(self, risk: float, max_minis: float = math.inf) -> int:
        want = max(1, math.ceil(risk / (self.stop_ticks * self.tick_value) - 1e-9))
        if max_minis == math.inf:
            return want
        # At the contract cap the stop widens instead of adding contracts.
        return min(want, max(1, int(max_minis / self.mini_equivalent + 1e-9)))

    def cost(self, risk: float, max_minis: float = math.inf) -> float:
        return self.contracts(risk, max_minis) * self.round_turn

    def with_overrides(self, round_turn: float | None = None,
                       stop_ticks: float | None = None) -> "Instrument":
        changes = {}
        if round_turn is not None:
            changes["round_turn"] = round_turn
        if stop_ticks is not None:
            changes["stop_ticks"] = stop_ticks
        return replace(self, **changes)


# Round turns include roughly one tick of slippage on top of typical prop-firm
# commissions and exchange fees. Override them with your own fills.
INSTRUMENTS = {
    "NQ": Instrument("NQ", tick_value=5.00, round_turn=9.00, stop_ticks=40),
    "MNQ": Instrument("MNQ", tick_value=0.50, round_turn=2.00, stop_ticks=40, mini_equivalent=0.1),
    "ES": Instrument("ES", tick_value=12.50, round_turn=16.50, stop_ticks=16),
    "MES": Instrument("MES", tick_value=1.25, round_turn=2.75, stop_ticks=16, mini_equivalent=0.1),
}
