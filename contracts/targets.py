"""C0 — Strategy output contract.

Owner: C (Strategy). Consumers: D (risk + reconcile).
FROZEN: `Target` fields and the `Strategy` ABC signature must not change without review.

KEY DESIGN RULE: a strategy emits a TARGET PORTFOLIO (desired weights), never orders.
It must not know about signing, order types, precision or the API. D turns targets
into OrderIntents; A executes them. This is what lets C and A/D evolve independently.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Literal

import pandas as pd

from contracts.bars import Ticker

Side = Literal["LONG", "SHORT", "FLAT"]
Urgency = Literal["MAKER", "TAKER"]


@dataclass(frozen=True)
class Target:
    """Desired position for one pair, expressed as a fraction of total equity."""

    pair: str
    side: Side
    weight: float          # fraction of equity, 0.0 .. 1.0; ignored when side == FLAT
    reason: str            # human-readable signal rationale; goes verbatim into the audit log
    urgency: Urgency = "TAKER"

    def __post_init__(self) -> None:
        if not 0.0 <= self.weight <= 1.0:
            raise ValueError(f"weight out of [0,1]: {self.weight}")
        if self.side == "FLAT" and self.weight != 0.0:
            raise ValueError("FLAT target must have weight 0")
        if not self.reason:
            raise ValueError("Target.reason is mandatory (Screen 1 audit trail)")


@dataclass
class Context:
    """Everything a strategy is allowed to see at one decision point.

    Point-in-time only: `bars[pair]` must contain bars CLOSED at or before ts_ms.
    No future bars, ever (lookahead = invalid backtest = invalid submission).
    """

    ts_ms: int
    tickers: Dict[str, Ticker]
    bars: Dict[str, pd.DataFrame]      # pair -> validated history, may be empty early on
    equity_usd: float
    positions: Dict[str, float]        # pair -> signed quantity (long +, short -)
    meta: dict = field(default_factory=dict)


class Strategy(ABC):
    """Implement `decide`; return the full desired book each cycle (idempotent targets)."""

    name: str = "base"

    @abstractmethod
    def decide(self, ctx: Context) -> list[Target]:
        ...

    def describe(self) -> str:
        """One-paragraph strategy explanation for the README / finalist deck."""
        return getattr(self, "__doc__", "") or ""
