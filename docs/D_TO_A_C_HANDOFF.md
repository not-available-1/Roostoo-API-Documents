# D to A/C handoff (offline integration)

Reference code: GitHub `trading-bot` at `f51f3ea214a449985369c3402b09a91e18a3d6c7`. This document describes the current A/C classes, not the different proposed `contracts/` shapes on `b_data-&-factors`. No live broker is wired in this branch.

## One decision, one order at a time

Call `d_layer.handoff.plan_trading_bot_decision` with C's actual `c_contracts.Target` list and A-normalized, read-only snapshots. It returns `DecisionPlan(decision_id, status, risk_results, intents, reason)` and never makes a network call. A/C modules are not imported by D.

```text
C Strategy.on_bar(final bars) -> C Target list
  -> D plan_trading_bot_decision(targets, account, prices, steps,
       minimum_notionals, free_positions, risk_config, now,
       order_state, last_settled_decision_id)
  -> at most one D intent -> to_a_order_intent(intent, base.OrderIntent)
  -> A submits once -> A confirms fill and refreshes account -> replan
```

`status=hold` means C returned `[]`; no order is planned. `status=already_settled` means the same C decision was completed earlier. `status=blocked` means no order may be submitted; inspect `reason` and `risk_results`. `status=ready` returns zero or one intent. With zero intents, A may persist this `decision_id` as settled after confirming no outstanding order. With one intent, A must persist the decision/intent and submission state before calling `place_order`, then mark the order pending or unknown until its outcome is confirmed. A must not mark a decision settled on an order acknowledgment alone. A confirmed partial fill requires a fresh account snapshot and another D plan with the same decision ID. A must not send any other planned order before this cycle completes.

The C decision must carry one explicit millisecond `ts` shared by all targets. C's `MultiFactorStrategy` returns its previous targets during `min_hold_bars`; their unchanged timestamp and D's deterministic decision ID allow A to recognize a settled decision and avoid repeated 4-hour rebalances. `last_settled_decision_id` and order state must survive process restarts. When C returns a non-empty list, it represents the complete desired portfolio; D adds explicit zero targets for held symbols omitted from that list **before** risk evaluation. Any rejected target blocks the whole batch. D's empty-list rule is HOLD. Current C `backtest.py` instead converts `[]` to an empty portfolio, so C must align its backtest semantics or emit explicit zero targets for a deliberate flatten before live integration.

## A inputs and execution responsibility

| A-supplied input | Required meaning |
|---|---|
| `AccountState.equity`, `positions` | Marked account equity and total signed base quantities for valuation. Current A execution is restricted to nonnegative spot positions. |
| `AccountState.cash`, `free_positions` | Free USD and per-pair **Free sellable base quantity**; never use Free+Lock as sellable. Missing or insufficient Free blocks the whole plan. |
| `PriceQuote` | Positive timestamped price, with agreed bid/ask/last convention and freshness. |
| `quantity_steps`, `minimum_notionals` | Per-pair executable quantity step and strict minimum order value from exchange metadata. Missing or subminimum orders block the plan. |
| `order_state` | `clear` only after all prior submissions have a confirmed terminal outcome; otherwise `pending` or `unknown`. |
| `last_settled_decision_id` | Durable identifier for the last fully completed C decision, not merely the last submitted one. |

Use `RiskConfig(allow_short=False, ...)` for this A boundary. Current `base.OrderIntent` and `LiveBroker.place_order` only express BUY/SELL spot-style orders, so D refuses short-enabled planning. A retains responsibility for exact response parsing, rate limits, order lookup, fills, cancellation, free-balance refresh, fee/slippage accounting, and durable submission tracking. D's `intent_id` is local audit/dedup metadata in `reason`; current A does not transmit it as a server idempotency key. An uncertain POST result therefore blocks further orders until A independently resolves it. D's `AuditLog` can record each decision, rejection, submission, raw response after redaction, confirmed fill, and final account snapshot using the same IDs.

## Before any live use

- Supply an A-owned normalization of `get_balance()` and exchange metadata; current `LiveBroker` returns raw balance and does not provide order/fill status or `get_exchange_info()`.
- Align C's empty-list semantics with its backtest. The current `strategies.py` imports a `weights.py` that is absent from `trading-bot`; C must provide the runnable strategy module.
- Use a dedicated integration entrypoint. The current `main.py` is a separate four-factor prototype: it does not call C or D and references undefined `price` and omits A `OrderIntent.reason`.
- Agree production risk thresholds, executable price convention, fees/slippage buffer, persisted order state, and a broker fill-confirmation route. Run offline A/C/D fixtures and paper execution before enabling live orders.

The D package and ZIP are an offline handoff. Passing D tests does not certify the current `trading-bot` branch as a working live bot.
