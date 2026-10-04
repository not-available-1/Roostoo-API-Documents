"""Pure, deterministic approved-target to order-intent planning."""

from decimal import Decimal, ROUND_DOWN
from hashlib import sha256
from typing import Mapping, Sequence

from .contracts import AccountState, OrderIntent, PriceQuote, Target


ZERO = Decimal("0")


def _valid_decimal(value: object, positive: bool = False) -> bool:
    return (isinstance(value, Decimal) and value.is_finite() and
            (not positive or value > ZERO))


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Floor a nonnegative quantity toward zero to an executable step."""

    if value <= ZERO:
        return ZERO
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _intent(target: Target, side: str, quantity: Decimal,
            phase: str, reduce_only: bool, current: Decimal) -> OrderIntent:
    identity = "|".join((target.decision_id, target.timestamp.isoformat(), target.symbol, phase, side,
                         str(quantity), str(current)))
    intent_id = sha256(identity.encode("utf-8")).hexdigest()[:24]
    return OrderIntent(intent_id, target.decision_id, target.symbol, side,
                       quantity, reduce_only, phase, target.reason)


def reconcile(approved_targets: Sequence[Target], account: AccountState,
              prices: Mapping[str, PriceQuote],
              quantity_steps: Mapping[str, Decimal]) -> tuple[OrderIntent, ...]:
    """Plan positive base-unit quantities in target order, with closes first.

    Existing broker positions may contain sub-step residuals from fees, partial
    fills, or historical rounding. D never rejects such state solely for that
    reason. Instead it rounds each *new executable order* conservatively toward
    zero. For a sign flip with a residual that cannot be fully flattened, D emits
    only the executable CLOSE and requires a fresh replan before opening the
    opposite exposure.
    """

    if not _valid_decimal(account.equity, positive=True):
        raise ValueError("equity must be finite and positive")
    symbols = [target.symbol for target in approved_targets]
    if len(symbols) != len(set(symbols)):
        raise ValueError("duplicate target symbol")
    if not approved_targets:
        # Current C semantics: an empty batch is HOLD/no rebalance.
        return ()

    # Current C backtest semantics treat a non-empty list as the complete target
    # portfolio. Mirror that live: held symbols omitted from the new portfolio
    # receive an explicit zero target so stale positions are reduced/closed.
    template = approved_targets[0]
    all_targets = list(approved_targets)
    mentioned = set(symbols)
    for held_symbol in sorted(account.positions):
        if held_symbol not in mentioned and account.positions[held_symbol] != ZERO:
            all_targets.append(Target(
                held_symbol, ZERO, template.decision_id,
                "implicit_flatten: omitted from complete C target portfolio",
                template.timestamp,
            ))

    result: list[OrderIntent] = []
    for target in all_targets:
        quote = prices.get(target.symbol)
        step = quantity_steps.get(target.symbol)
        current = account.positions.get(target.symbol, ZERO)
        if (not _valid_decimal(target.weight) or not target.decision_id or
                quote is None or quote.symbol != target.symbol or
                not _valid_decimal(quote.price, positive=True) or
                not _valid_decimal(step, positive=True) or
                not _valid_decimal(current)):
            raise ValueError(f"invalid target, price, position, or quantity step for {target.symbol}")

        desired_raw = target.weight * account.equity / quote.price

        # Position flip: close existing exposure first. Only emit the OPEN if the
        # current position can be fully closed to the exchange step. Otherwise a
        # residual would remain and opening opposite exposure could increase risk.
        if current and desired_raw and (current > ZERO) != (desired_raw > ZERO):
            close_qty = _floor_to_step(abs(current), step)
            if close_qty >= step:
                result.append(_intent(target, "SELL" if current > ZERO else "BUY",
                                      close_qty, "CLOSE", True, current))
            if close_qty == abs(current):
                open_qty = _floor_to_step(abs(desired_raw), step)
                if open_qty >= step:
                    result.append(_intent(target, "BUY" if desired_raw > ZERO else "SELL",
                                          open_qty, "OPEN", False, current))
            continue

        difference_raw = desired_raw - current
        executable_qty = _floor_to_step(abs(difference_raw), step)
        if executable_qty < step:
            continue

        side = "BUY" if difference_raw > ZERO else "SELL"
        reduce_only = bool(current and abs(desired_raw) < abs(current))
        phase = "CLOSE" if desired_raw == ZERO else "ADJUST"
        result.append(_intent(target, side, executable_qty, phase, reduce_only, current))

    return tuple(sorted(result, key=lambda intent: not intent.reduce_only))
