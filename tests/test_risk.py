import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from d_layer.contracts import AccountState, PriceQuote, Target
from d_layer.risk import RiskConfig, evaluate_targets
from d_layer.monitoring import risk_snapshot


NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


def config(**overrides):
    # Deliberately test-only values; no production limits are implied.
    values = dict(max_position_weight=D("0.5"), max_gross_weight=D("1"),
                  max_net_weight=D("1"), min_cash_weight=D("0.1"),
                  max_order_notional=D("10000"), max_turnover_weight=D("1"),
                  max_drawdown=D("0.5"), max_daily_loss=D("0.5"),
                  max_market_age_seconds=60, allowed_symbols=frozenset({"BTC", "ETH"}),
                  allow_short=True, short_collateral_ratio=D("1"), emergency_halt=False)
    values.update(overrides)
    return RiskConfig(**values)


def target(symbol="BTC", weight="0.2", decision_id="d1"):
    return Target(symbol, D(weight), decision_id, "fixture signal", NOW)


def quote(symbol="BTC", price="100", age=0):
    return PriceQuote(symbol, D(price), NOW - timedelta(seconds=age))


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.account = AccountState(D("10000"), D("10000"), {},
                                    equity_peak=D("10000"), session_start_equity=D("10000"),
                                    turnover_notional=D("0"))
        self.prices = {"BTC": quote(), "ETH": quote("ETH", "50")}

    def assess(self, targets, cfg=None, account=None, prices=None):
        return evaluate_targets(targets, account or self.account,
                                prices if prices is not None else self.prices,
                                cfg or config(), NOW)

    def test_target_passes_unchanged(self):
        result, = self.assess([target()])
        self.assertEqual(result.status, "unchanged")
        self.assertEqual(result.approved.weight, D("0.2"))
        self.assertEqual(result.violations, ())

    def test_position_cap_clips_with_structured_violation(self):
        result, = self.assess([target(weight="0.8")])
        self.assertEqual(result.status, "clipped")
        self.assertEqual(result.approved.weight, D("0.5"))
        self.assertIn("max_position_weight", [v.code for v in result.violations])

    def test_multiple_limits_apply_in_stable_order(self):
        cfg = config(max_position_weight=D("0.7"), max_gross_weight=D("0.4"),
                     max_order_notional=D("3000"))
        result, = self.assess([target(weight="0.9")], cfg)
        self.assertEqual(result.approved.weight, D("0.3"))
        self.assertEqual([v.code for v in result.violations],
                         ["max_position_weight", "max_gross_weight", "max_order_notional"])

    def test_batch_gross_cap_uses_earlier_approved_targets(self):
        cfg = config(max_position_weight=D("0.7"), max_gross_weight=D("0.8"))
        results = self.assess([target("BTC", "0.6"), target("ETH", "0.6", "d2")], cfg)
        self.assertEqual([r.approved.weight for r in results], [D("0.6"), D("0.2")])

    def test_missing_or_nonfinite_price_rejects(self):
        self.assertEqual(self.assess([target()], prices={})[0].status, "rejected")
        bad = {"BTC": quote(price="NaN")}
        self.assertEqual(self.assess([target()], prices=bad)[0].status, "rejected")

    def test_stale_quote_rejects(self):
        result, = self.assess([target()], prices={"BTC": quote(age=61)})
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.violations[0].code, "stale_market_data")

    def test_halt_blocks_increase_but_allows_reduction(self):
        halted = config(emergency_halt=True)
        result, = self.assess([target()], halted)
        self.assertEqual(result.approved.weight, D("0"))
        self.assertIn("emergency_halt", [v.code for v in result.violations])
        held = AccountState(D("10000"), D("8000"), {"BTC": D("20")},
                            equity_peak=D("10000"), session_start_equity=D("10000"),
                            turnover_notional=D("0"))
        result, = self.assess([target(weight="0.1")], halted, held)
        self.assertEqual(result.approved.weight, D("0.1"))

    def test_cash_and_loss_cutoffs_block_new_exposure(self):
        result, = self.assess([target(weight="0.5")], config(min_cash_weight=D("0.8")))
        self.assertEqual(result.approved.weight, D("0.2"))
        self.assertIn("min_cash_weight", [v.code for v in result.violations])
        losing = AccountState(D("9000"), D("9000"), {}, equity_peak=D("10000"),
                              session_start_equity=D("10000"), turnover_notional=D("0"))
        result, = self.assess([target()], config(max_drawdown=D("0.05")), losing)
        self.assertEqual(result.approved.weight, D("0"))
        self.assertIn("max_drawdown", [v.code for v in result.violations])

    def test_missing_loss_state_fails_closed(self):
        account = AccountState(D("10000"), D("10000"), {})
        result, = self.assess([target()], account=account)
        self.assertEqual(result.status, "rejected")
        self.assertIn("missing_risk_state", [v.code for v in result.violations])

    def test_invalid_config_is_obvious(self):
        with self.assertRaises(ValueError):
            config(max_gross_weight=D("NaN"))

    def test_net_turnover_and_short_policy_clip(self):
        result, = self.assess([target(weight="-0.4")], config(allow_short=False))
        self.assertEqual(result.approved.weight, D("0"))
        self.assertIn("short_disabled", [v.code for v in result.violations])
        result, = self.assess([target(weight="-0.4")], config(max_net_weight=D("0.2")))
        self.assertEqual(result.approved.weight, D("-0.2"))
        self.assertIn("max_net_weight", [v.code for v in result.violations])
        used = AccountState(D("10000"), D("10000"), {}, equity_peak=D("10000"),
                            session_start_equity=D("10000"), turnover_notional=D("1900"))
        result, = self.assess([target(weight="0.2")],
                              config(max_turnover_weight=D("0.2")), used)
        self.assertEqual(result.approved.weight, D("0.01"))
        self.assertIn("max_turnover_weight", [v.code for v in result.violations])

    def test_symbol_validation_and_short_collateral_buffer(self):
        result, = self.assess([target(symbol="DOGE")])
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.violations[0].code, "symbol_not_allowed")
        cfg = config(max_position_weight=D("2"), max_gross_weight=D("2"),
                     max_net_weight=D("2"), short_collateral_ratio=D("2"))
        result, = self.assess([target(weight="-1")], cfg)
        self.assertEqual(result.approved.weight, D("-0.45"))
        self.assertIn("min_cash_weight", [v.code for v in result.violations])
        result, = self.assess([target(weight="-1")],
                              config(max_position_weight=D("2"), max_gross_weight=D("2"),
                                     max_net_weight=D("2"), short_collateral_ratio=D("1")))
        self.assertEqual(result.approved.weight, D("-0.9"))
        self.assertIn("min_cash_weight", [v.code for v in result.violations])

    def test_daily_loss_and_bad_target_timestamp_fail_closed(self):
        losing = AccountState(D("9000"), D("9000"), {}, equity_peak=D("9000"),
                              session_start_equity=D("10000"), turnover_notional=D("0"))
        result, = self.assess([target()], config(max_daily_loss=D("0.05")), losing)
        self.assertEqual(result.approved.weight, D("0"))
        self.assertIn("max_daily_loss", [v.code for v in result.violations])
        bad = Target("BTC", D("0.2"), "d1", "reason", datetime(2026, 10, 3))
        result, = self.assess([bad])
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.violations[0].code, "invalid_target_timestamp")

    def test_existing_net_breach_never_reverses_requested_direction(self):
        account = AccountState(D("10000"), D("0"), {"ETH": D("400")},
                               equity_peak=D("10000"), session_start_equity=D("10000"),
                               turnover_notional=D("0"))
        result, = self.assess([target(weight="0.2")], account=account)
        self.assertEqual(result.approved.weight, D("0"))
        self.assertIn("max_net_weight", [v.code for v in result.violations])

    def test_snapshot_has_exposures_and_serializable_values(self):
        account = AccountState(D("10000"), D("8000"), {"BTC": D("20"), "ETH": D("-10")},
                               equity_peak=D("11000"), session_start_equity=D("9500"),
                               turnover_notional=D("500"))
        snapshot = risk_snapshot(account, self.prices, config(), NOW)
        self.assertEqual(snapshot.gross_exposure, D("2500"))
        self.assertEqual(snapshot.net_exposure, D("1500"))
        self.assertEqual(snapshot.largest_concentration, D("0.2"))
        self.assertEqual(snapshot.to_dict()["per_symbol_weights"]["ETH"], "-0.05")

    def test_snapshot_flags_held_symbol_outside_universe(self):
        account = AccountState(D("10000"), D("9500"), {"DOGE": D("10")},
                               equity_peak=D("10000"), session_start_equity=D("10000"),
                               turnover_notional=D("0"))
        prices = {"DOGE": quote("DOGE", "50")}
        snapshot = risk_snapshot(account, prices, config(), NOW)
        self.assertIn("symbol_not_allowed", snapshot.active_violations)


if __name__ == "__main__":
    unittest.main()
