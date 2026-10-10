# Team92 Laplace+ — D Risk / Reconciliation Layer

D sits between C's target portfolio and A's execution broker. It does **not** generate alpha and it does **not** call Roostoo directly.

```text
C Target(s)
    -> plan_trading_bot_decision (adapt, complete portfolio, risk, reconcile)
    -> at most one internal D OrderIntent
    -> to_a_order_intent
    -> A Broker
```

## Current team interfaces

Current C target shape observed in the team research code:

```python
Target(symbol: str, target_weight: float, reason: str = "", ts: int | None = None)
```

D treats `target_weight` as a fraction of current equity. An empty target list means **HOLD / no rebalance** in this phase; it does not mean flatten. A **non-empty** list is treated as C's complete desired portfolio, matching the current backtest: held symbols omitted from it are assigned an implicit zero target for reconciliation. `ts` is expected to be Unix milliseconds when supplied.

Current A execution shape observed on the `trading-bot` branch:

```python
OrderIntent(
    pair: str,
    side: str,
    quantity: float,
    price: float | None,
    order_type: str,
    reason: str,
)
```

D keeps a richer internal intent (`intent_id`, `decision_id`, reduce-only flag, phase) and maps it to A's current shape only at the boundary. D does not import or modify A's module.

For the actual `trading-bot` A/C shapes, use `d_layer.handoff.plan_trading_bot_decision` as the integration entrypoint. It blocks the entire decision on any rejected target, validates Free sellable quantity and minimum notional, skips settled repeat decisions, and releases at most one intent before a confirmed fill and account refresh. See `docs/D_TO_A_C_HANDOFF.md` for exact inputs, state transitions, and live blockers.

## What D provides

- configurable target risk checks and clipping/rejection;
- long-only policy support for the current strategy;
- gross/net/concentration/cash/turnover/loss controls;
- stale and invalid market-data rejection;
- deterministic target-to-order reconciliation;
- conservative handling of sub-step broker position residuals;
- deterministic internal intent IDs;
- SQLite WAL audit events with credential redaction;
- exposure/risk snapshots;
- Sharpe, Sortino, Calmar and `0.4*Sortino + 0.3*Sharpe + 0.3*Calmar` under an explicit **team convention**.

No production risk thresholds are hard-coded. Tests use test-only values.

## Run offline tests

Python 3.10+; D uses only the standard library.

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q d_layer score.py tests
```

There are no network calls in the D package and no API credentials are required.

## Score CLI

```bash
python3 score.py equity.csv --periods-per-year 2190
```

`2190 = 365 * 6` is the team's current annualization convention **only when the input equity series has one observation every 4 hours**. It is not claimed to be the organizer's official judging implementation. If the equity input is daily, use the appropriate daily sampling convention instead.

The CLI requires `--periods-per-year` explicitly so a 4h series cannot silently be annualized as daily.

## Integration order

1. C produces targets with a shared explicit decision timestamp.
2. A supplies a normalized account/price snapshot, Free sellable balances, exchange steps/minimums, and durable order state.
3. D plans one risk-approved intent through `plan_trading_bot_decision`.
4. A maps that intent to its current `OrderIntent`, submits once, then confirms the outcome.
5. A refreshes account state and replans the same C decision until no intent remains; only then is the decision settled.

Before live integration, the team still must agree the symbol naming/mapping, A's normalized balance/position fields, precision metadata, fill confirmation, and actual risk-limit values. See `docs/D_TO_A_C_HANDOFF.md` and `docs/D_INTEGRATION_CHECKLIST.md`.
