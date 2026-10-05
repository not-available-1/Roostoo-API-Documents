"""A0 — Broker protocol: the single interface between "the world" and "our logic".

Owner: A (Platform). Consumers: everyone.
FROZEN: method names/signatures must not change without review.

Two implementations exist and MUST be interchangeable:
  - LiveBroker  (bot/roostoo/client.py)  talks to https://mock-api.roostoo.com
  - PaperBroker (bot/roostoo/paper.py)   in-memory simulator, same fees & rules

Because they share this protocol, B/C/D develop and test with PaperBroker and need
no API keys, no network and no AWS. Swapping to live is a one-line config change.
"""
from __future__ import annotations

from typing import Dict, Optional, Protocol, runtime_checkable

from contracts.bars import Ticker
from contracts.orders import BookState, OrderIntent, OrderResult


@runtime_checkable
class Broker(Protocol):
    def ticker(self, pair: Optional[str] = None) -> Dict[str, Ticker]:
        """Live snapshot. pair=None returns the WHOLE universe in ONE call (rate-limit friendly)."""
        ...

    def exchange_info(self) -> Dict[str, dict]:
        """pair -> {PricePrecision, AmountPrecision, MiniOrder, CanTrade, AssetType}. Cached."""
        ...

    def book(self) -> BookState:
        """Cash + open longs + open shorts, marked to market, plus high-water mark."""
        ...

    def execute(self, intent: OrderIntent) -> OrderResult:
        """The ONLY way to trade. Applies rate limiting, retries and precision internally."""
        ...

    def cancel(self, order_id: Optional[int] = None, pair: Optional[str] = None) -> list[int]:
        """Cancel pending orders; returns cancelled order ids."""
        ...

    def pending_ids(self) -> list[int]:
        """Open (unfilled) limit order ids, for reconcile to avoid double-placing."""
        ...
