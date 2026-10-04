"""Pure target approval against explicitly configured safety limits."""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, Mapping, Sequence

from .contracts import AccountState, PriceQuote, Target


ZERO = Decimal("0")
ONE = Decimal("1")


def _finite(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


@dataclass(frozen=True, kw_only=True)
class RiskConfig:
    """All required limits are explicit; no production thresholds are baked in."""

    max_position_weight: Decimal
    max_gross_weight: Decimal
    max_net_weight: Decimal
    min_cash_weight: Decimal
    max_order_notional: Decimal
    max_turnover_weight: Decimal
    max_drawdown: Decimal
    max_daily_loss: Decimal
    max_market_age_seconds: int
    allowed_symbols: frozenset[str]
    allow_short: bool
    short_collateral_ratio: Decimal
    emergency_halt: bool = False

    def __post_init__(self) -> None:
        limits = (self.max_position_weight, self.max_gross_weight,
                  self.max_net_weight, self.max_order_notional,
                  self.max_turnover_weight, self.max_drawdown,
                  self.max_daily_loss, self.short_collateral_ratio)
        if any(not _finite(value) or value < ZERO for value in limits):
            raise ValueError("risk limits must be finite nonnegative Decimals")
        if not _finite(self.min_cash_weight) or not ZERO <= self.min_cash_weight <= ONE:
            raise ValueError("min_cash_weight must be a Decimal between 0 and 1")
        if self.short_collateral_ratio < ONE:
            raise ValueError("short_collateral_ratio must be at least 1")
        if type(self.max_market_age_seconds) is not int or self.max_market_age_seconds <= 0:
            raise ValueError("max_market_age_seconds must be a positive integer")
        if not self.allowed_symbols or any(not symbol for symbol in self.allowed_symbols):
            raise ValueError("allowed_symbols must be nonempty")
        if type(self.allow_short) is not bool or type(self.emergency_halt) is not bool:
            raise ValueError("boolean risk flags must be bool")


@dataclass(frozen=True)
class RiskViolation:
    """Stable machine code and explanatory text for one rule decision."""

    code: str
    message: str


@dataclass(frozen=True)
class RiskResult:
    """Requested target and its approved replacement, or an explicit rejection."""

    requested: Target
    approved: Target | None
    status: Literal["unchanged", "clipped", "rejected"]
    violations: tuple[RiskViolation, ...]
    reason: str


def _reject(target: Target, code: str, message: str) -> RiskResult:
    violation = RiskViolation(code, message)
    return RiskResult(target, None, "rejected", (violation,), message)


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return min(max(value, low), high)


def _reduce_only(current: Decimal, desired: Decimal) -> Decimal:
    if current == ZERO or (current > ZERO) != (desired > ZERO):
        return ZERO
    return _clamp(desired, ZERO, current) if current > ZERO else _clamp(desired, current, ZERO)


def _cash_after(cash: Decimal, equity: Decimal, current: Decimal,
                desired: Decimal, collateral_ratio: Decimal) -> Decimal:
    long_change = max(desired, ZERO) - max(current, ZERO)
    short_change = max(-desired, ZERO) - max(-current, ZERO)
    return cash - equity * (long_change + collateral_ratio * short_change)


def _valid_quote(symbol: str, prices: Mapping[str, PriceQuote], now: datetime,
                 max_age: int) -> str | None:
    quote = prices.get(symbol)
    if quote is None or quote.symbol != symbol or not _finite(quote.price) or quote.price <= ZERO:
        return "invalid_price"
    if quote.timestamp.tzinfo is None or now.tzinfo is None:
        return "invalid_timestamp"
    age = (now.astimezone(timezone.utc) - quote.timestamp.astimezone(timezone.utc)).total_seconds()
    if age < 0 or age > max_age:
        return "stale_market_data"
    return None


def evaluate_targets(targets: Sequence[Target], account: AccountState,
                     prices: Mapping[str, PriceQuote], config: RiskConfig,
                     now: datetime) -> tuple[RiskResult, ...]:
    """Approve targets in input order, projecting each accepted change forward.

    Each target weight is a signed equity fraction. Missing safety state or a
    needed quote rejects an affected target. Caller must not execute rejected
    targets. Duplicate symbols in a batch are an input error.
    """

    symbols = [target.symbol for target in targets]
    if len(symbols) != len(set(symbols)):
        raise ValueError("duplicate target symbol")
    if not _finite(account.equity) or account.equity <= ZERO or not _finite(account.cash):
        return tuple(_reject(t, "invalid_account", "equity/cash invalid") for t in targets)
    if (not _finite(account.equity_peak) or account.equity_peak <= ZERO or
            not _finite(account.session_start_equity) or account.session_start_equity <= ZERO or
            not _finite(account.turnover_notional) or account.turnover_notional < ZERO):
        return tuple(_reject(t, "missing_risk_state", "peak, session equity, or turnover missing") for t in targets)
    weights: dict[str, Decimal] = {}
    for symbol, quantity in account.positions.items():
        if not _finite(quantity):
            return tuple(_reject(t, "invalid_account", "position quantity invalid") for t in targets)
        error = _valid_quote(symbol, prices, now, config.max_market_age_seconds)
        if error:
            return tuple(_reject(t, error, f"held position {symbol} has {error}") for t in targets)
        weights[symbol] = quantity * prices[symbol].price / account.equity

    cash = account.cash
    turnover = account.turnover_notional
    results: list[RiskResult] = []
    drawdown = max(ZERO, (account.equity_peak - account.equity) / account.equity_peak)
    daily_loss = max(ZERO, (account.session_start_equity - account.equity) /
                     account.session_start_equity)
    for target in targets:
        if not target.symbol or not target.decision_id or not _finite(target.weight):
            results.append(_reject(target, "invalid_target", "target identity or weight invalid"))
            continue
        if (target.timestamp.tzinfo is None or now.tzinfo is None or
                target.timestamp.astimezone(timezone.utc) > now.astimezone(timezone.utc)):
            results.append(_reject(target, "invalid_target_timestamp", "target timestamp missing or future"))
            continue
        if target.symbol not in config.allowed_symbols:
            results.append(_reject(target, "symbol_not_allowed", "symbol is not allowed"))
            continue
        error = _valid_quote(target.symbol, prices, now, config.max_market_age_seconds)
        if error:
            results.append(_reject(target, error, f"target quote has {error}"))
            continue
        current = weights.get(target.symbol, ZERO)
        desired = target.weight
        violations: list[RiskViolation] = []

        def clip(value: Decimal, code: str, message: str) -> None:
            nonlocal desired
            if value != desired:
                desired = value
                violations.append(RiskViolation(code, message))

        if not config.allow_short:
            clip(max(desired, ZERO), "short_disabled", "short exposure disabled")
        clip(_clamp(desired, -config.max_position_weight, config.max_position_weight),
             "max_position_weight", "position concentration limit")
        other_gross = sum((abs(v) for s, v in weights.items() if s != target.symbol), ZERO)
        gross_room = max(ZERO, config.max_gross_weight - other_gross)
        clip(_clamp(desired, -gross_room, gross_room),
             "max_gross_weight", "gross exposure limit")
        other_net = sum((v for s, v in weights.items() if s != target.symbol), ZERO)
        net_candidate = _clamp(desired, -config.max_net_weight - other_net,
                               config.max_net_weight - other_net)
        if net_candidate != desired:
            violations.append(RiskViolation("max_net_weight", "net exposure limit"))
            # An existing portfolio breach must not make a target trade away
            # from its requested direction or create an opposite position.
            desired = _clamp(net_candidate, min(current, desired), max(current, desired))
        if config.emergency_halt:
            clip(_reduce_only(current, desired), "emergency_halt", "trading halted")
        if drawdown >= config.max_drawdown:
            clip(_reduce_only(current, desired), "max_drawdown", "drawdown cutoff")
        if daily_loss >= config.max_daily_loss:
            clip(_reduce_only(current, desired), "max_daily_loss", "daily loss cutoff")
        order_room = config.max_order_notional / account.equity
        clip(_clamp(desired, current - order_room, current + order_room),
             "max_order_notional", "order notional limit")
        turnover_room = max(ZERO, config.max_turnover_weight - turnover / account.equity)
        clip(_clamp(desired, current - turnover_room, current + turnover_room),
             "max_turnover_weight", "turnover limit")
        cash_floor = config.min_cash_weight * account.equity
        projected_cash = _cash_after(cash, account.equity, current, desired,
                                     config.short_collateral_ratio)
        if projected_cash < min(cash_floor, cash):
            available = (cash - cash_floor) / account.equity
            if desired > ZERO:
                long_room = max(current, ZERO) + config.short_collateral_ratio * max(-current, ZERO) + available
                clip(min(desired, max(ZERO, long_room)),
                     "min_cash_weight", "cash buffer limit")
            elif desired < ZERO:
                short_room = max(-current, ZERO) + (max(current, ZERO) + available) / config.short_collateral_ratio
                clip(max(desired, -max(ZERO, short_room)),
                     "min_cash_weight", "short collateral cash buffer")
        approved = replace(target, weight=desired)
        cash = _cash_after(cash, account.equity, current, desired, config.short_collateral_ratio)
        turnover += abs(desired - current) * account.equity
        weights[target.symbol] = desired
        status: Literal["unchanged", "clipped", "rejected"] = "unchanged" if desired == target.weight else "clipped"
        reason = "; ".join(v.message for v in violations) or "Approved unchanged"
        results.append(RiskResult(target, approved, status, tuple(violations), reason))
    return tuple(results)
