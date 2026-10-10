# Part D handoff

Scope: offline D risk, reconciliation, audit, monitoring, performance and C/A boundary adapters. Detailed A/C handoff: `docs/D_TO_A_C_HANDOFF.md`.

This package intentionally contains no API credentials, no Roostoo HTTP client, no AWS code and no live-order path.

## Test locally

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q d_layer score.py tests
```

## Current integration semantics

- C `Target(symbol, target_weight, reason, ts)` -> `adapt_c_targets`.
- Empty C list = HOLD / no rebalance.
- Non-empty C list = complete desired portfolio; omitted held symbols are reconciled toward zero.
- D internal intents retain `intent_id`, `decision_id`, phase and reduce-only metadata.
- `to_a_order_intent` converts to current A `OrderIntent(pair, side, quantity, price, order_type, reason)`.
- `plan_trading_bot_decision` is the safe D entrypoint for the current A/C shapes: it checks the complete target portfolio as one batch, stops on a rejection, requires Free sellable balances and exchange minimums, and emits at most one order before account refresh.
- D never submits orders itself.

Before live use, A/B/C still need to supply normalized live account state, current prices/freshness, exchange precision/minimum metadata, durable order/fill state and agreed production risk limits. The current `trading-bot` main loop is not wired to D; this handoff is offline only.
