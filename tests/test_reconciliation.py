import unittest
from datetime import datetime, timezone
from decimal import Decimal as D

from d_layer.contracts import AccountState, PriceQuote, Target
from d_layer.reconciliation import reconcile


NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)
PRICES = {"BTC": PriceQuote("BTC", D("100"), NOW)}
STEPS = {"BTC": D("0.001")}


def plan(current, desired):
    account = AccountState(D("10000"), D("10000"), {"BTC": D(current)})
    approved = [Target("BTC", D(desired), "decision-1", "fixture signal", NOW)]
    return reconcile(approved, account, PRICES, STEPS)


class ReconciliationTests(unittest.TestCase):
    def test_zero_difference_creates_no_orders(self):
        self.assertEqual(plan("20", "0.2"), ())

    def test_partial_increase(self):
        orders = plan("10", "0.2")
        self.assertEqual([(o.side, o.quantity, o.reduce_only) for o in orders],
                         [("BUY", D("10"), False)])

    def test_partial_decrease_and_close(self):
        self.assertEqual([(o.side, o.quantity, o.reduce_only) for o in plan("20", "0.1")],
                         [("SELL", D("10"), True)])
        self.assertEqual([(o.side, o.quantity, o.reduce_only) for o in plan("20", "0")],
                         [("SELL", D("20"), True)])

    def test_long_to_short_closes_before_opening(self):
        orders = plan("50", "-0.3")
        self.assertEqual([(o.phase, o.side, o.quantity, o.reduce_only) for o in orders],
                         [("CLOSE", "SELL", D("50"), True),
                          ("OPEN", "SELL", D("30"), False)])

    def test_short_to_long_closes_before_opening(self):
        orders = plan("-30", "0.2")
        self.assertEqual([(o.phase, o.side, o.quantity, o.reduce_only) for o in orders],
                         [("CLOSE", "BUY", D("30"), True),
                          ("OPEN", "BUY", D("20"), False)])

    def test_repeated_plan_is_stable_and_updated_state_is_idempotent(self):
        first = plan("10", "0.2")
        self.assertEqual(first, plan("10", "0.2"))
        self.assertEqual(plan("20", "0.2"), ())

    def test_intent_id_distinguishes_decisions_at_different_times(self):
        from datetime import timedelta

        account = AccountState(D("10000"), D("10000"), {"BTC": D("0")})
        first = Target("BTC", D("0.2"), "reused-id", "reason", NOW)
        later = Target("BTC", D("0.2"), "reused-id", "reason", NOW + timedelta(days=1))
        self.assertNotEqual(reconcile([first], account, PRICES, STEPS)[0].intent_id,
                            reconcile([later], account, PRICES, STEPS)[0].intent_id)

    def test_sub_step_difference_does_not_trade(self):
        self.assertEqual(plan("20", "0.200001"), ())

    def test_batch_closes_all_reductions_before_any_open(self):
        prices = {"BTC": PRICES["BTC"], "ETH": PriceQuote("ETH", D("50"), NOW)}
        steps = {"BTC": D("0.001"), "ETH": D("0.001")}
        account = AccountState(D("10000"), D("10000"), {"BTC": D("0"), "ETH": D("20")})
        targets = [Target("BTC", D("0.1"), "d1", "reason", NOW),
                   Target("ETH", D("0"), "d1", "reason", NOW)]
        orders = reconcile(targets, account, prices, steps)
        self.assertEqual([(o.symbol, o.reduce_only) for o in orders],
                         [("ETH", True), ("BTC", False)])

    def test_missing_precision_or_bad_price_fails_closed(self):
        account = AccountState(D("10000"), D("10000"), {})
        target = Target("BTC", D("0.1"), "d1", "reason", NOW)
        with self.assertRaises(ValueError):
            reconcile([target], account, PRICES, {})
        with self.assertRaises(ValueError):
            reconcile([target], account, {"BTC": PriceQuote("BTC", D("NaN"), NOW)}, STEPS)

    def test_unaligned_current_quantity_is_tolerated(self):
        account = AccountState(D("10000"), D("10000"), {"BTC": D("10.0005")})
        approved = [Target("BTC", D("0.2"), "d1", "reason", NOW)]
        orders = reconcile(approved, account, PRICES, STEPS)
        self.assertEqual([(o.side, o.quantity) for o in orders], [("BUY", D("9.999"))])

    def test_closing_residual_position_never_overshoots_past_zero(self):
        account = AccountState(D("10000"), D("10000"), {"BTC": D("20.0005")})
        approved = [Target("BTC", D("0"), "d1", "reason", NOW)]
        orders = reconcile(approved, account, PRICES, STEPS)
        self.assertEqual([(o.side, o.quantity, o.reduce_only) for o in orders],
                         [("SELL", D("20.000"), True)])

    def test_residual_within_one_step_of_target_does_not_trade(self):
        account = AccountState(D("10000"), D("10000"), {"BTC": D("20.0005")})
        approved = [Target("BTC", D("0.2"), "d1", "reason", NOW)]
        self.assertEqual(reconcile(approved, account, PRICES, STEPS), ())

    def test_flip_with_untradable_residual_does_not_open_opposite_risk(self):
        account = AccountState(D("10000"), D("10000"), {"BTC": D("20.0005")})
        approved = [Target("BTC", D("-0.2"), "d1", "reason", NOW)]
        orders = reconcile(approved, account, PRICES, STEPS)
        self.assertEqual([(o.phase, o.side, o.quantity, o.reduce_only) for o in orders],
                         [("CLOSE", "SELL", D("20.000"), True)])

    def test_nonempty_target_batch_flattens_held_symbols_omitted_by_complete_portfolio(self):
        prices = {"BTC": PriceQuote("BTC", D("100"), NOW),
                  "ETH": PriceQuote("ETH", D("50"), NOW)}
        steps = {"BTC": D("0.001"), "ETH": D("0.001")}
        account = AccountState(D("10000"), D("5000"), {"BTC": D("20"), "ETH": D("40")})
        targets = [Target("ETH", D("0.2"), "d1", "new complete portfolio", NOW)]
        orders = reconcile(targets, account, prices, steps)
        self.assertEqual([(o.symbol, o.side, o.quantity, o.reduce_only) for o in orders],
                         [("BTC", "SELL", D("20"), True)])

    def test_empty_target_batch_remains_hold_even_with_existing_positions(self):
        account = AccountState(D("10000"), D("8000"), {"BTC": D("20")})
        self.assertEqual(reconcile([], account, PRICES, STEPS), ())


if __name__ == "__main__":
    unittest.main()
