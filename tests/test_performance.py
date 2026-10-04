import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from d_layer.performance import MetricConvention, calculate_metrics, composite_score


class PerformanceTests(unittest.TestCase):
    def test_equity_returns_peaks_and_drawdown(self):
        metrics = calculate_metrics([D("100"), D("110"), D("121"), D("108.9")],
                                    MetricConvention(1))
        self.assertEqual(metrics.total_return, D("0.089"))
        self.assertEqual(metrics.running_peaks, (D("100"), D("110"), D("121"), D("121")))
        self.assertEqual(metrics.drawdowns, (D("0"), D("0"), D("0"), D("0.1")))
        self.assertEqual(metrics.max_drawdown, D("0.1"))

    def test_known_sharpe_uses_sample_deviation(self):
        metrics = calculate_metrics([D("100"), D("110"), D("132"), D("171.6")],
                                    MetricConvention(1))
        self.assertAlmostEqual(float(metrics.sharpe), 2.0)

    def test_known_sortino_uses_all_observations_in_downside_deviation(self):
        # Returns: +20%, -10%, +20%, -10%; mean 5%, downside RMS sqrt(.005).
        metrics = calculate_metrics([D("100"), D("120"), D("108"), D("129.6"), D("116.64")],
                                    MetricConvention(1))
        self.assertAlmostEqual(float(metrics.sortino), 0.05 / (0.005 ** 0.5))

    def test_known_calmar_and_composite(self):
        metrics = calculate_metrics([D("100"), D("110"), D("99")],
                                    MetricConvention(2))
        self.assertEqual(metrics.calmar, D("-0.1"))
        self.assertEqual(composite_score(D("1"), D("2"), D("3")), D("1.9"))

    def test_undefined_ratios_are_null_not_infinite(self):
        flat = calculate_metrics([D("100"), D("100"), D("100")], MetricConvention(365))
        self.assertIsNone(flat.sharpe)
        self.assertIsNone(flat.sortino)
        self.assertIsNone(flat.calmar)
        self.assertIsNone(flat.composite)
        self.assertIsNone(calculate_metrics([D("100")], MetricConvention(365)).sharpe)
        one_return = calculate_metrics([D("100"), D("90")], MetricConvention(365))
        self.assertIsNone(one_return.sortino)
        self.assertIsNone(one_return.calmar)
        with self.assertRaises(ValueError):
            calculate_metrics([], MetricConvention(365))
        with self.assertRaises(ValueError):
            calculate_metrics([D("100"), D("NaN")], MetricConvention(365))

    def test_cli_requires_explicit_sampling_convention_and_emits_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "equity.csv"
            path.write_text("equity\n100\n110\n99\n", encoding="utf-8")
            process = subprocess.run([sys.executable, "score.py", str(path),
                                      "--periods-per-year", "2"],
                                     capture_output=True, text=True, check=True)
            output = json.loads(process.stdout)
            self.assertEqual(output["total_return"], "-0.01")
            self.assertEqual(output["calmar"], "-0.1")
            self.assertEqual(output["convention"]["label"], "team metric convention — to confirm")


if __name__ == "__main__":
    unittest.main()
