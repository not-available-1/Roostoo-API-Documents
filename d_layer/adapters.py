"""Boundary adapters between D's Decimal contracts and the team's current C/A shapes.

This module intentionally imports neither C's research package nor A's broker module.
That keeps D offline-testable and prevents circular ownership of team interfaces.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Sequence, TypeVar

from .contracts import OrderIntent, Target


T = TypeVar("T")
_MIN_MS_TIMESTAMP = 100_000_000_000  # Reject seconds accidentally supplied as milliseconds.
_MAX_MS_TIMESTAMP = 100_000_000_000_000


def _finite_decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric, not bool")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not decimal.is_finite():
        raise ValueError(f"{field} must be a finite number")
    return decimal


def _effective_timestamp(ts: object, now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now_utc = now.astimezone(timezone.utc)
    if ts is None:
        return now_utc
    if isinstance(ts, bool) or not isinstance(ts, int):
        raise ValueError("C Target.ts must be an integer millisecond timestamp or None")
    if ts < _MIN_MS_TIMESTAMP or ts > _MAX_MS_TIMESTAMP:
        raise ValueError("C Target.ts must be milliseconds since Unix epoch")
    try:
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("C Target.ts is outside supported datetime range") from exc


def adapt_c_targets(targets: Sequence[object], now: datetime) -> tuple[Target, ...]:
    """Adapt current C Target-like objects into D's internal immutable targets.

    Current C fields are ``symbol``, ``target_weight``, ``reason``, and ``ts``.
    An empty input means HOLD/no rebalance and therefore returns an empty tuple.
    The same batch contents and effective timestamps produce the same decision ID.
    """

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not targets:
        return ()

    normalized: list[tuple[str, Decimal, str, datetime]] = []
    for item in targets:
        try:
            symbol = item.symbol
            weight_raw = item.target_weight
            reason = item.reason
            ts = item.ts
        except AttributeError as exc:
            raise ValueError("C target must expose symbol, target_weight, reason, and ts") from exc
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("C Target.symbol must be a nonempty string")
        if not isinstance(reason, str):
            raise ValueError("C Target.reason must be a string")
        weight = _finite_decimal(weight_raw, field="target_weight")
        timestamp = _effective_timestamp(ts, now)
        normalized.append((symbol.strip(), weight, reason, timestamp))

    identity = "\n".join(
        f"{symbol}|{weight}|{reason}|{timestamp.isoformat()}"
        for symbol, weight, reason, timestamp in normalized
    )
    decision_id = sha256(identity.encode("utf-8")).hexdigest()[:24]
    return tuple(
        Target(symbol, weight, decision_id, reason, timestamp)
        for symbol, weight, reason, timestamp in normalized
    )


def to_a_order_intent(
    intent: OrderIntent,
    a_order_intent_cls: type[T],
    *,
    order_type: str = "MARKET",
    price: Decimal | float | None = None,
) -> T:
    """Convert D's richer internal intent to A's current ``base.OrderIntent`` shape.

    Decimal quantities remain exact inside D and are converted to ``float`` only at
    this explicit boundary because A's current dataclass uses floats.
    """

    if not isinstance(intent.quantity, Decimal) or not intent.quantity.is_finite() or intent.quantity <= 0:
        raise ValueError("intent quantity must be a finite positive Decimal")
    if intent.side not in ("BUY", "SELL"):
        raise ValueError("intent side must be BUY or SELL")
    order_type = order_type.upper()
    if order_type not in ("MARKET", "LIMIT"):
        raise ValueError("order_type must be MARKET or LIMIT")
    price_out: float | None
    if price is None:
        if order_type == "LIMIT":
            raise ValueError("LIMIT order requires price")
        price_out = None
    else:
        decimal_price = _finite_decimal(price, field="price")
        if decimal_price <= 0:
            raise ValueError("price must be positive")
        price_out = float(decimal_price)

    metadata = (
        f"d_intent={intent.intent_id};decision={intent.decision_id};"
        f"phase={intent.phase};reduce_only={str(intent.reduce_only).lower()}"
    )
    reason = f"{intent.reason} [{metadata}]" if intent.reason else f"[{metadata}]"
    return a_order_intent_cls(
        pair=intent.symbol,
        side=intent.side,
        quantity=float(intent.quantity),
        price=price_out,
        order_type=order_type,
        reason=reason,
    )
