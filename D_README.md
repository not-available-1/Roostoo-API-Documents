# Team92 Laplace+ — D Risk / Reconciliation Layer

D sits between C's target portfolio and A's execution broker. It does **not** generate alpha and it does **not** call Roostoo directly.

```text
C Target(s)
    -> adapt_c_targets
    -> evaluate_targets
    -> reconcile
    -> internal D OrderIntent
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

1. C produces targets.
2. D adapts and risk-checks them.
3. D reads a normalized account/price snapshot from A/B adapters.
4. D reconciles approved targets against actual positions.
5. D maps internal intents to A's current `OrderIntent`.
6. A executes and returns broker/fill state.
7. D logs the lifecycle and replans from refreshed account state.

Before live integration, the team still must agree the symbol naming/mapping, A's normalized balance/position fields, precision metadata, and actual risk-limit values. See `docs/D_INTEGRATION_CHECKLIST.md`.
