import math
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal as D

from d_layer.adapters import adapt_c_targets, to_a_order_intent
from d_layer.contracts import OrderIntent


NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class CTarget:
    symbol: str
    target_weight: float
    reason: str = ""
    ts: int | None = None


@dataclass(frozen=True)
class AOrderIntent:
    pair: str
    side: str
    quantity: float
    price: float | None
    order_type: str
    reason: str


class AdapterTests(unittest.TestCase):
    def test_real_c_target_maps_to_decimal_internal_target(self):
        out = adapt_c_targets([CTarget("BTC/USD", 0.5, "lowvol", 1700000000000)], NOW)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].symbol, "BTC/USD")
        self.assertEqual(out[0].weight, D("0.5"))
        self.assertEqual(out[0].reason, "lowvol")
        self.assertEqual(out[0].timestamp.tzinfo, timezone.utc)

    def test_empty_c_batch_means_hold_and_returns_no_internal_targets(self):
        self.assertEqual(adapt_c_targets([], NOW), ())

    def test_nonfinite_c_weights_are_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                adapt_c_targets([CTarget("BTC/USD", value, "bad", 1700000000000)], NOW)

    def test_c_timestamp_is_milliseconds_and_seconds_are_rejected(self):
        out = adapt_c_targets([CTarget("BTC/USD", 0.5, "x", 1700000000000)], NOW)
        self.assertEqual(out[0].timestamp, datetime.fromtimestamp(1700000000, tz=timezone.utc))
        with self.assertRaises(ValueError):
            adapt_c_targets([CTarget("BTC/USD", 0.5, "x", 1700000000)], NOW)

    def test_none_timestamp_uses_supplied_now_not_wall_clock(self):
        out = adapt_c_targets([CTarget("BTC/USD", 0.5, "x", None)], NOW)
        self.assertEqual(out[0].timestamp, NOW)

    def test_same_batch_and_effective_timestamp_has_stable_decision_id(self):
        batch = [CTarget("BTC/USD", 0.5, "x", 1700000000000),
                 CTarget("ETH/USD", 0.5, "y", 1700000000000)]
        first = adapt_c_targets(batch, NOW)
        second = adapt_c_targets(batch, NOW)
        self.assertEqual({x.decision_id for x in first}, {x.decision_id for x in second})
        self.assertEqual(len({x.decision_id for x in first}), 1)

    def test_a_order_adapter_matches_current_a_shape(self):
        internal = OrderIntent(
            intent_id="intent123",
            decision_id="decision456",
            symbol="BTC/USD",
            side="BUY",
            quantity=D("0.025"),
            reduce_only=False,
            phase="ADJUST",
            reason="rebalance",
        )
        out = to_a_order_intent(internal, AOrderIntent)
        self.assertIsInstance(out, AOrderIntent)
        self.assertEqual(out.pair, "BTC/USD")
        self.assertEqual(out.side, "BUY")
        self.assertEqual(out.quantity, 0.025)
        self.assertIsNone(out.price)
        self.assertEqual(out.order_type, "MARKET")
        self.assertIn("rebalance", out.reason)
        self.assertIn("intent123", out.reason)
        self.assertIn("decision456", out.reason)
        self.assertIn("phase=ADJUST", out.reason)

    def test_a_order_adapter_rejects_nonpositive_quantity(self):
        internal = OrderIntent("i", "d", "BTC/USD", "BUY", D("0"), False, "ADJUST", "x")
        with self.assertRaises(ValueError):
            to_a_order_intent(internal, AOrderIntent)


if __name__ == "__main__":
    unittest.main()
