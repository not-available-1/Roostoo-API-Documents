# D integration checklist for A/B/C

This checklist distinguishes what is **already known from team code** from what still needs agreement before live wiring.

The concrete offline handoff for `trading-bot` A/C is `D_TO_A_C_HANDOFF.md`. Use `plan_trading_bot_decision` rather than filtering `RiskResult.approved` and calling `reconcile` directly: a rejected target must stop the whole complete-portfolio decision.

## Known current contracts

### C -> D

Current C dataclass:

```python
Target(symbol: str, target_weight: float, reason: str = "", ts: int | None = None)
```

D adapter: `d_layer.adapters.adapt_c_targets`.

Current D interpretation:
- `target_weight` = final desired fraction of account equity;
- `[]` = HOLD / no rebalance;
- a non-empty list is the complete desired portfolio; held symbols omitted from it are flattened during D reconciliation;
- `ts` = Unix milliseconds when supplied; `None` uses an explicitly supplied decision time;
- C's `reason` is preserved for audit.

### D -> A

Current A dataclass:

```python
OrderIntent(pair, side, quantity, price, order_type, reason)
```

D adapter: `d_layer.adapters.to_a_order_intent`.

D retains richer internal fields (`intent_id`, `decision_id`, `reduce_only`, `phase`) and adds them to the boundary reason string so they are not lost while A's public contract is minimal.

## Still required before live integration

| Owner | Needed | Why | D consumer |
|---|---|---|---|
| B/C | Preserve B's current Roostoo symbol convention (`BTC/USD`); confirm live feed uses the same mapping | D must never silently trade the wrong pair | C adapter, allowed-symbol config, reconciliation |
| C | Confirm `[] = HOLD` special case while non-empty lists remain complete portfolios | Avoid live/backtest disagreement or stale held positions | C adapter / reconciliation |
| C | Confirm rebalance cadence and decision-time semantics | Define target lifetime and audit grouping | intake/audit |
| A | Normalized account snapshot: equity, free cash, signed positions, and per-pair Free sellable quantity | Required to compute actual delta without selling locked holdings | `AccountState`, handoff, risk, reconciliation |
| A | Equity peak, session-start equity, accumulated turnover or agreed source | Required for drawdown/daily-loss/turnover rules | risk, monitoring |
| A/B | Timestamped executable/current prices | Required for sizing and stale-data protection | `PriceQuote`, risk, reconciliation |
| A | Exchange quantity step / amount precision and minimum notional per pair | Required to generate valid executable quantities | reconciliation / later execution validation |
| A | Order/fill statuses, partial-fill, rejection and cancel semantics | Replan from actual fills and avoid duplicate exposure | audit/replan loop |
| A | Durable order state and last fully settled C decision ID | Block pending/unknown submissions and repeated hold-period targets across restarts | `plan_trading_bot_decision` |
| A | Persistent handling of D `intent_id` metadata | Avoid duplicate submissions on retry/restart | execution/audit |
| A | Mapping of D reduce/close phase to broker behavior | Ensure reductions happen before any opposite exposure | execution |
| A | Raw broker response shape after safe redaction | Complete audit chain without credential leakage | `AuditLog` |
| Team | Production risk thresholds | Tests intentionally contain only test values | `RiskConfig` |
| Team | Official metric conventions, if organizers publish them | D currently exposes team-configurable conventions only | `MetricConvention`, `score.py` |

## D-only assumptions

- Account position is signed base quantity; price is quote currency per base unit.
- Account cash means free/available cash.
- The current A boundary has no distinct short actions; its D handoff requires `allow_short=False` and blocks existing short positions.
- C `MultiFactorStrategy` can return previous targets during its hold period. A must persist the last settled `decision_id`; a matching repeated decision does not trigger rebalancing.
- D interprets an empty C batch as HOLD. Current C `backtest.py` interprets it as a flat portfolio; C must align the semantics before live wiring.
- The D handoff emits at most one checked intent, then requires a confirmed fill and refreshed account snapshot before the next plan.
- D evaluates approved target changes deterministically in input order.
- Current broker quantities may contain sub-step residuals. D floors each **new order** toward zero rather than rejecting the account state.
- If a position flip has a residual that cannot be fully flattened at the exchange step, D emits only the executable CLOSE and waits for a refreshed account state before any OPEN.
- A must refresh account state after fills and re-run D reconciliation.
- D itself makes no network calls and never formats a Roostoo signed request.

## Performance convention

The composite weights used in D are:

```text
0.4 * Sortino + 0.3 * Sharpe + 0.3 * Calmar
```

Annualization, risk-free rate, Sortino target and exact judging implementation remain configurable. For a **4-hour evenly sampled equity series**, the team's current annualization input is `periods_per_year = 365 * 6 = 2190`. This is a team convention, not an assertion about the organizer's hidden scorer.

## Audit lifecycle to wire

Use the same `run_id` and `decision_id` across signal/target, risk decision, approved target, D intent, A submission, broker response, fill and portfolio/PnL events. Add `intent_id` once D plans an order and `order_id` once A supplies it. Append events; do not rewrite prior lifecycle records.

`AuditLog` uses SQLite WAL with `synchronous=FULL` and redacts recognized credential keys/patterns. Close/reopen and committed-event survival across child `os._exit(0)` are tested. SIGKILL/power-loss durability has not been claimed.
