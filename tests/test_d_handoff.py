import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal as D

from d_layer.contracts import AccountState, PriceQuote
from d_layer.handoff import plan_trading_bot_decision
from d_layer.risk import RiskConfig


NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)


@dataclass(frozen=True)
class CTarget:
    symbol: str
    target_weight: float
    reason: str
    ts: int


def config(allowed=frozenset({"BTC/USD", "ETH/USD"}), allow_short=False):
    return RiskConfig(
        max_position_weight=D("1"), max_gross_weight=D("2"),
        max_net_weight=D("2"), min_cash_weight=D("0"),
        max_order_notional=D("1000"), max_turnover_weight=D("10"),
        max_drawdown=D("1"), max_daily_loss=D("1"),
        max_market_age_seconds=60, allowed_symbols=allowed,
        allow_short=allow_short, short_collateral_ratio=D("1"),
    )


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.account = AccountState(
            equity=D("100"), cash=D("50"),
            positions={"BTC/USD": D("0.5")},
            equity_peak=D("100"), session_start_equity=D("100"),
            turnover_notional=D("0"),
        )
        self.prices = {
            "BTC/USD": PriceQuote("BTC/USD", D("100"), NOW),
            "ETH/USD": PriceQuote("ETH/USD", D("10"), NOW),
        }
        self.steps = {"BTC/USD": D("0.1"), "ETH/USD": D("0.1")}
        self.minimums = {"BTC/USD": D("1"), "ETH/USD": D("1")}
        self.free = {"BTC/USD": D("0.5")}

    def plan(self, targets=None, **overrides):
        args = dict(
            c_targets=targets if targets is not None else [CTarget("ETH/USD", 0.1, "new", NOW_MS)],
            account=self.account, prices=self.prices,
            quantity_steps=self.steps, minimum_notionals=self.minimums,
            free_positions=self.free, config=config(), now=NOW,
            order_state="clear", last_settled_decision_id=None,
        )
        args.update(overrides)
        return plan_trading_bot_decision(**args)

    def test_rejected_target_stops_whole_batch_instead_of_selling_it(self):
        targets = [CTarget("BTC/USD", 0.5, "keep", NOW_MS),
                   CTarget("ETH/USD", 0.1, "new", NOW_MS)]
        plan = self.plan(targets, config=config(frozenset({"ETH/USD"})))
        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.intents, ())
        self.assertIn("rejected", [result.status for result in plan.risk_results])

    def test_omitted_held_position_is_risk_checked_before_flatten(self):
        plan = self.plan(config=config(frozenset({"ETH/USD"})))
        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.intents, ())
        self.assertEqual({result.requested.symbol for result in plan.risk_results},
                         {"BTC/USD", "ETH/USD"})

    def test_complete_portfolio_submits_one_reduction_before_any_purchase(self):
        plan = self.plan()
        self.assertEqual(plan.status, "ready")
        self.assertEqual([(i.symbol, i.side, i.quantity) for i in plan.intents],
                         [("BTC/USD", "SELL", D("0.5"))])

        after_sell = AccountState(D("100"), D("100"), {},
                                  D("100"), D("100"), D("50"))
        next_plan = self.plan(account=after_sell, free_positions={},
                              order_state="clear")
        self.assertEqual([(i.symbol, i.side, i.quantity) for i in next_plan.intents],
                         [("ETH/USD", "BUY", D("1.0"))])

    def test_fully_invested_rotation_uses_sale_before_purchase_in_risk_projection(self):
        invested = AccountState(D("100"), D("0"), {"BTC/USD": D("1")},
                                D("100"), D("100"), D("0"))
        plan = self.plan([CTarget("ETH/USD", 1.0, "rotate", NOW_MS)],
                         account=invested, free_positions={"BTC/USD": D("1")})
        self.assertEqual([(i.symbol, i.side, i.quantity) for i in plan.intents],
                         [("BTC/USD", "SELL", D("1"))])

    def test_repeated_settled_c_decision_does_not_rebalance_on_price_drift(self):
        first = self.plan()
        changed_prices = dict(self.prices)
        changed_prices["ETH/USD"] = PriceQuote("ETH/USD", D("12"), NOW)
        repeated = self.plan(prices=changed_prices,
                             last_settled_decision_id=first.decision_id)
        self.assertEqual(repeated.status, "already_settled")
        self.assertEqual(repeated.intents, ())

    def test_pending_or_unknown_order_blocks_new_submissions(self):
        for state in ("pending", "unknown"):
            with self.subTest(state=state):
                plan = self.plan(order_state=state)
                self.assertEqual(plan.status, "blocked")
                self.assertEqual(plan.intents, ())

    def test_empty_c_batch_is_hold_not_implicit_flatten(self):
        plan = self.plan([])
        self.assertEqual(plan.status, "hold")
        self.assertEqual(plan.intents, ())

    def test_sell_exceeding_free_quantity_blocks_whole_batch(self):
        plan = self.plan(free_positions={"BTC/USD": D("0.3")})
        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.intents, ())

    def test_subminimum_order_blocks_whole_batch(self):
        plan = self.plan(minimum_notionals={"BTC/USD": D("1"),
                                             "ETH/USD": D("10")})
        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.intents, ())

    def test_current_a_boundary_rejects_short_enabled_config(self):
        with self.assertRaises(ValueError):
            self.plan(config=config(allow_short=True))

    def test_missing_free_position_or_minimum_is_not_assumed_zero(self):
        self.assertEqual(self.plan(free_positions={}).status, "blocked")
        self.assertEqual(self.plan(minimum_notionals={"ETH/USD": D("1")}).status,
                         "blocked")
        self.assertEqual(self.plan(minimum_notionals={"BTC/USD": D("0"),
                                                      "ETH/USD": D("0")}).status,
                         "blocked")

    def test_invalid_account_position_blocks_without_decimal_comparison_error(self):
        account = AccountState(D("100"), D("50"), {"BTC/USD": D("NaN")},
                               D("100"), D("100"), D("0"))
        self.assertEqual(self.plan(account=account).status, "blocked")

    def test_mixed_or_missing_c_timestamps_do_not_create_live_decision(self):
        with self.assertRaises(ValueError):
            self.plan([CTarget("BTC/USD", 0.5, "keep", NOW_MS - 1),
                       CTarget("ETH/USD", 0.1, "new", NOW_MS)])
        with self.assertRaises(ValueError):
            self.plan([CTarget("ETH/USD", 0.1, "new", None)])


if __name__ == "__main__":
    unittest.main()
