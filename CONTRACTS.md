# Team interfaces

This is the integration contract for B → C, C → D, D → A, and A → everyone. D now has the current C `Target` field names and the current A `OrderIntent` shape from the team code. D still keeps its own richer internal dataclasses and adapts at the boundaries so A/C do not need to adopt D's types.

All monetary values and quantities use `Decimal`. A weight is a signed fraction of equity (`0.2` means 20%, not 0.2%). Timestamps are timezone-aware UTC. Symbols must use one consistent naming scheme across all teams.

## B → C: Bar / market data — integration contract to confirm

Minimum fields D eventually needs from the same market data stream: symbol, positive quote-currency price per base unit, timestamp for freshness, and symbol universe. C additionally needs the agreed bar interval and OHLCV definitions; B and C own that schema. D currently accepts `PriceQuote(symbol, price, timestamp)` as a temporary adapter. A missing, nonfinite, nonpositive, future, or stale quote causes risk rejection. Confirm timestamp source, timezone, bar close semantics, and whether prices are mid, last, bid, or ask.

## C → D: Target — current team contract

C currently emits `Target(symbol, target_weight, reason="", ts=None)`. `target_weight` is a signed fraction of account equity (`0.5` means 50%). D converts it with `Decimal(str(target_weight))`. `ts` is currently treated as a millisecond Unix timestamp; `None` uses an explicitly supplied decision time. A seconds timestamp is rejected rather than silently misinterpreted.

An **empty C target list means HOLD / no rebalance** in D phase 1. It is never interpreted as flatten. A **non-empty** target list is treated as the complete desired portfolio, matching C's current backtest semantics; held symbols omitted from that list receive an implicit zero target during reconciliation. D assigns a deterministic batch `decision_id` and retains C's `reason` for audit.

`evaluate_targets` processes a batch in input order. Each symbol may appear once; duplicate symbols raise an error. `RiskResult` retains the requested target, an approved target or `None`, status (`unchanged`, `clipped`, `rejected`), ordered rule violations, and a human reason. Rejected results must never be passed to reconciliation.

## D → A: OrderIntent — D internal + current A boundary

D's internal `OrderIntent` carries `intent_id`, `decision_id`, `symbol`, `side` (`BUY` or `SELL`), positive `quantity` in base asset units, `reduce_only`, `phase` (`CLOSE`, `OPEN`, `ADJUST`), and `reason`. It contains no Roostoo signing material or API credential. `intent_id` is deterministic for the decision ID and timestamp, symbol, phase, side, quantity, and starting position.

A currently accepts `base.OrderIntent(pair, side, quantity, price, order_type, reason)`. `d_layer.adapters.to_a_order_intent` maps D's internal intent to that exact shape without importing or modifying A's module. Decimal quantity is converted to float only at that boundary. D metadata (`intent_id`, `decision_id`, phase, reduce-only state) is appended to `reason` so it is not lost while A's current contract remains minimal.

For a flip, D emits a `CLOSE` intent followed by an `OPEN` intent. All risk-reducing intents in a batch are ordered before risk-increasing intents. **A must wait for confirmed close fill and refresh account state before submitting the open.** Partial fills, rejects, or changed equity require replanning and possibly another D risk check; array order alone is not an execution guarantee. A must enforce exchange quantity step, minimum size/notional, and any actual reduce-only mechanism. D requires A's precision metadata and floors desired quantity toward zero to the supplied step. Fees and slippage are A/C integration topics; D never formats an exchange request.

## A → everyone: Broker protocol — integration contract to confirm

D requires read-only account equity, available cash, signed position quantities, timestamped executable/current prices, equity peak, session start equity, and accumulated turnover. It also requires order submission/acknowledgment/fill/reject/cancel status, order IDs, fill price/quantity, raw response after safe redaction, and exchange symbol/precision metadata. `AccountState` and `PriceQuote` are temporary adapters; they are not a Broker implementation. A owns API calls, signing, execution, persistence of submitted `intent_id`s, and mapping broker responses into the audit lifecycle. D code makes no network calls.

## Safety and metric conventions

`RiskConfig` has no production defaults for numeric limits. Startup cannot construct it without each required limit, allowed symbols, short policy, and collateral ratio. Missing equity peak, session start equity, turnover, or needed market price causes rejection. All available limits are applied in a stable order: symbol/position, gross, net, halt/loss cutoffs, order notional, turnover, cash/collateral. A target may be clipped more than once. A current breach can remain after a partial risk-reducing order; monitor the resulting account again.

The only stated hackathon score rule available here is `0.4 × Sortino + 0.3 × Sharpe + 0.3 × Calmar`, provided in the task request. Annualization, risk-free rate, Sortino target, return frequency, and exact Calmar definition were absent from repository materials. `d_layer.performance` uses a **team metric convention — to confirm against official judging implementation**: evenly sampled simple returns, sample standard deviation for Sharpe, downside RMS over all return observations for Sortino, and geometric annualized return divided by maximum peak-to-trough drawdown for Calmar. Undefined ratios are `null`; the composite is `null` if any component is undefined.
