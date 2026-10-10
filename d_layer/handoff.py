"""Offline handoff from trading-bot C targets to A-ready D order intents."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Mapping, Sequence

from .adapters import adapt_c_targets
from .contracts import AccountState, OrderIntent, PriceQuote, Target
from .reconciliation import reconcile
from .risk import RiskConfig, RiskResult, evaluate_targets


@dataclass(frozen=True)
class DecisionPlan:
    decision_id: str | None
    status: Literal["hold", "already_settled", "blocked", "ready"]
    risk_results: tuple[RiskResult, ...]
    intents: tuple[OrderIntent, ...]
    reason: str


def plan_trading_bot_decision(
    c_targets: Sequence[object],
    account: AccountState,
    prices: Mapping[str, PriceQuote],
    quantity_steps: Mapping[str, Decimal],
    minimum_notionals: Mapping[str, Decimal],
    free_positions: Mapping[str, Decimal],
    config: RiskConfig,
    now: datetime,
    *,
    order_state: Literal["clear", "pending", "unknown"],
    last_settled_decision_id: str | None,
) -> DecisionPlan:
    """Plan one C decision without placing orders or reading the exchange.

    A must supply verified free balances and order state. Only a confirmed
    fill/account refresh can settle a submitted decision; an acknowledgment
    alone cannot. Replanning a partially filled decision is allowed once A
    reports order_state='clear' and has not marked the decision settled.
    """
    if config.allow_short:
        raise ValueError("trading-bot A has no distinct short open/close actions")
    if order_state not in ("clear", "pending", "unknown"):
        raise ValueError("order_state must be clear, pending, or unknown")
    if order_state != "clear":
        return DecisionPlan(None, "blocked", (), (), f"order state is {order_state}")
    if any(not isinstance(qty, Decimal) or not qty.is_finite() or qty < 0
           for qty in free_positions.values()):
        return DecisionPlan(None, "blocked", (), (), "invalid free position quantity")
    if any(not isinstance(qty, Decimal) or not qty.is_finite() or qty <= 0
           for qty in minimum_notionals.values()):
        return DecisionPlan(None, "blocked", (), (), "invalid minimum notional")
    if any(not isinstance(qty, Decimal) or not qty.is_finite()
           for qty in account.positions.values()):
        return DecisionPlan(None, "blocked", (), (), "invalid account position")
    if any(qty < 0 for qty in account.positions.values()):
        return DecisionPlan(None, "blocked", (), (), "current A boundary cannot reconcile short positions")
    if not c_targets:
        return DecisionPlan(None, "hold", (), (), "empty C target batch means no trade")

    timestamps = {getattr(target, "ts", None) for target in c_targets}
    if None in timestamps or len(timestamps) != 1:
        raise ValueError("live C targets must share one explicit millisecond timestamp")
    targets = adapt_c_targets(c_targets, now)
    decision_id = targets[0].decision_id
    if decision_id == last_settled_decision_id:
        return DecisionPlan(decision_id, "already_settled", (), (), "decision was already settled")

    # Risk must see every intended reduction; otherwise filtering a rejected
    # C target can make reconciliation mistake it for an omitted holding.
    mentioned = {target.symbol for target in targets}
    full_targets: list[Target] = []
    for symbol in sorted(account.positions):
        if account.positions[symbol] != 0 and symbol not in mentioned:
            full_targets.append(Target(symbol, Decimal("0"), decision_id,
                                       "implicit_flatten: omitted from complete C target portfolio",
                                       targets[0].timestamp))
    full_targets.extend(targets)

    risk_results = evaluate_targets(full_targets, account, prices, config, now)
    if any(result.approved is None for result in risk_results):
        return DecisionPlan(decision_id, "blocked", risk_results, (), "one or more targets rejected")
    approved = tuple(result.approved for result in risk_results if result.approved is not None)
    try:
        intents = reconcile(approved, account, prices, quantity_steps)
    except ValueError as exc:
        return DecisionPlan(decision_id, "blocked", risk_results, (), f"reconciliation failed: {exc}")

    for intent in intents:
        quote = prices[intent.symbol]
        minimum = minimum_notionals.get(intent.symbol)
        if minimum is None or intent.quantity * quote.price <= minimum:
            return DecisionPlan(decision_id, "blocked", risk_results, (),
                                f"missing or subminimum order for {intent.symbol}")
        if intent.side == "SELL":
            free = free_positions.get(intent.symbol)
            if free is None or intent.quantity > free:
                return DecisionPlan(decision_id, "blocked", risk_results, (),
                                    f"sell quantity exceeds verified free balance for {intent.symbol}")
    # Submit one order, then require an acknowledged fill and fresh account
    # snapshot before sizing the next order from this same C decision.
    return DecisionPlan(decision_id, "ready", risk_results, intents[:1],
                        "risk and execution checks passed")
