"""D-owned order contract and provisional adapters for team interfaces."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Mapping


@dataclass(frozen=True)
class Target:
    """Provisional C->D adapter: signed fraction of account equity, not percent.

    Replace this adapter when C's actual Target contract is agreed. Positive is
    long, negative is short, and zero is flat. Timestamp must be UTC aware.
    """

    symbol: str
    weight: Decimal
    decision_id: str
    reason: str
    timestamp: datetime


@dataclass(frozen=True)
class PriceQuote:
    """Provisional B/A price adapter; price is quote currency per base unit."""

    symbol: str
    price: Decimal
    timestamp: datetime


@dataclass(frozen=True)
class AccountState:
    """Provisional A account adapter; positions are signed base quantities."""

    equity: Decimal
    cash: Decimal
    positions: Mapping[str, Decimal]
    equity_peak: Decimal | None = None
    session_start_equity: Decimal | None = None
    turnover_notional: Decimal | None = None


@dataclass(frozen=True)
class OrderIntent:
    """D->A request. Quantity is positive base units, never quote notional.

    A must acknowledge a CLOSE fill before submitting the following OPEN of a
    flip. The deterministic intent_id is a broker deduplication key.
    """

    intent_id: str
    decision_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: Decimal
    reduce_only: bool
    phase: Literal["CLOSE", "OPEN", "ADJUST"]
    reason: str
