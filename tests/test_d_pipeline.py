import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal as D

from d_layer.adapters import adapt_c_targets, to_a_order_intent
from d_layer.contracts import AccountState, PriceQuote
from d_layer.reconciliation import reconcile
from d_layer.risk import RiskConfig, evaluate_targets


NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)


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


def cfg() -> RiskConfig:
    return RiskConfig(
        max_position_weight=D("0.6"),
        max_gross_weight=D("1"),
        max_net_weight=D("1"),
        min_cash_weight=D("0"),
        max_order_notional=D("50000"),
        max_turnover_weight=D("2"),
        max_drawdown=D("0.2"),
        max_daily_loss=D("0.2"),
        max_market_age_seconds=60,
        allowed_symbols=frozenset({"BTC/USD", "ETH/USD"}),
        allow_short=False,
        short_collateral_ratio=D("1"),
    )


class DPipelineTests(unittest.TestCase):
    def setUp(self):
        self.prices = {
            "BTC/USD": PriceQuote("BTC/USD", D("100000"), NOW),
            "ETH/USD": PriceQuote("ETH/USD", D("5000"), NOW),
        }
        self.steps = {"BTC/USD": D("0.000001"), "ETH/USD": D("0.0001")}
        self.c_targets = [
            CTarget("BTC/USD", 0.5, "lowvol top2", NOW_MS),
            CTarget("ETH/USD", 0.5, "lowvol top2", NOW_MS),
        ]

    def test_c_to_d_to_a_and_second_reconcile_is_idempotent(self):
        account = AccountState(
            equity=D("50000"),
            cash=D("5000"),
            positions={"BTC/USD": D("0.2"), "ETH/USD": D("5")},
            equity_peak=D("50000"),
            session_start_equity=D("50000"),
            turnover_notional=D("0"),
        )
        internal = adapt_c_targets(self.c_targets, NOW)
        risk_results = evaluate_targets(internal, account, self.prices, cfg(), NOW)
        approved = tuple(r.approved for r in risk_results if r.approved is not None)
        self.assertEqual([r.status for r in risk_results], ["unchanged", "unchanged"])

        orders = reconcile(approved, account, self.prices, self.steps)
        self.assertEqual(len(orders), 1)
        self.assertEqual((orders[0].symbol, orders[0].side, orders[0].quantity),
                         ("BTC/USD", "BUY", D("0.05")))

        a_order = to_a_order_intent(orders[0], AOrderIntent)
        self.assertEqual((a_order.pair, a_order.side, a_order.quantity, a_order.order_type),
                         ("BTC/USD", "BUY", 0.05, "MARKET"))

        filled = AccountState(
            equity=D("50000"),
            cash=D("0"),
            positions={"BTC/USD": D("0.25"), "ETH/USD": D("5")},
            equity_peak=D("50000"),
            session_start_equity=D("50000"),
            turnover_notional=D("5000"),
        )
        again_results = evaluate_targets(internal, filled, self.prices, cfg(), NOW)
        again_approved = tuple(r.approved for r in again_results if r.approved is not None)
        self.assertEqual(reconcile(again_approved, filled, self.prices, self.steps), ())

    def test_empty_c_targets_is_hold_and_never_flattens(self):
        account = AccountState(
            equity=D("50000"),
            cash=D("30000"),
            positions={"BTC/USD": D("0.2")},
            equity_peak=D("50000"),
            session_start_equity=D("50000"),
            turnover_notional=D("0"),
        )
        internal = adapt_c_targets([], NOW)
        self.assertEqual(internal, ())
        self.assertEqual(evaluate_targets(internal, account, self.prices, cfg(), NOW), ())
        self.assertEqual(reconcile(internal, account, self.prices, self.steps), ())


if __name__ == "__main__":
    unittest.main()
