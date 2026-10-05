"""D0 — Order intent / result contract.

Owner: D (reconcile) produces OrderIntent; A (Broker) consumes it and returns OrderResult.
FROZEN: field names must not change without review.

Only ONE trading entry point exists in the whole system: Broker.execute(intent).
Nothing else may place, cancel or amend an order. This is what makes the
"no manual intervention / traceable commit history" compliance story provable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

Action = Literal["BUY", "SELL", "SHORT_OPEN", "SHORT_CLOSE"]
Urgency = Literal["MAKER", "TAKER"]


@dataclass(frozen=True)
class OrderIntent:
    """A single desired order, already sized, rounded and risk-approved."""

    pair: str
    action: Action
    quantity: Optional[float] = None    # base-asset qty; required for BUY/SELL
    collateral: Optional[float] = None  # USD locked; required for SHORT_OPEN
    close_pct: Optional[float] = None   # 0-100; for SHORT_CLOSE (None = close all)
    price: Optional[float] = None       # None => market order; set => LIMIT at this price
    urgency: Urgency = "TAKER"
    reason: str = ""

    def __post_init__(self) -> None:
        if self.action in ("BUY", "SELL") and not (self.quantity and self.quantity > 0):
            raise ValueError(f"{self.action} needs quantity > 0")
        if self.action == "SHORT_OPEN" and not (self.collateral and self.collateral >= 1):
            raise ValueError("SHORT_OPEN needs collateral >= 1 USD")
        if self.urgency == "MAKER" and self.price is None:
            raise ValueError("MAKER intent requires a limit price")


@dataclass(frozen=True)
class OrderResult:
    """Normalised outcome of one execute(); `raw` keeps the untouched API response."""

    success: bool
    pair: str
    action: Action
    err: str = ""
    order_id: Optional[int] = None
    status: str = ""          # FILLED / PENDING / CANCELED / REJECTED
    role: str = ""            # TAKER / MAKER
    price: float = 0.0        # fill price (0 for unfilled limit)
    quantity: float = 0.0     # requested
    filled_qty: float = 0.0
    fee: float = 0.0
    ts_ms: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def pending(self) -> bool:
        return self.success and self.status == "PENDING"


@dataclass(frozen=True)
class Position:
    """One open exposure, long or short."""

    pair: str
    qty: float               # signed: + long, - short
    entry_price: float
    collateral: float = 0.0  # >0 only for shorts


@dataclass(frozen=True)
class BookState:
    """Snapshot of everything the bot owns, in USD terms."""

    ts_ms: int
    cash_usd: float                       # free USD
    positions: list[Position]
    equity_usd: float                     # cash + mark-to-market of all positions
    peak_equity_usd: float                # running high-water mark (for drawdown)

    def qty_of(self, pair: str) -> float:
        for p in self.positions:
            if p.pair == pair:
                return p.qty
        return 0.0
