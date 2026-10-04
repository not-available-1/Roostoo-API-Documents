"""Offline score CLI. Example for 4h samples: python3 score.py equity.csv --periods-per-year 2190."""

import argparse
import csv
import json
from decimal import Decimal
from pathlib import Path

from d_layer.performance import MetricConvention, calculate_metrics


def main() -> None:
    """Read an equity CSV and print metrics with explicit team conventions."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("equity_csv", type=Path, help="CSV with an equity column")
    parser.add_argument("--periods-per-year", type=int, required=True,
                        help="Samples per year; choose to match the supplied series")
    parser.add_argument("--risk-free-rate", type=Decimal, default=Decimal("0"),
                        help="Annual simple risk-free rate (team convention, default 0)")
    parser.add_argument("--sortino-target-per-period", type=Decimal, default=Decimal("0"),
                        help="Minimum return per sample (team convention, default 0)")
    args = parser.parse_args()
    with args.equity_csv.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "equity" not in reader.fieldnames:
            parser.error("CSV must have an equity header")
        try:
            equity = [Decimal(row["equity"]) for row in reader]
            convention = MetricConvention(args.periods_per_year, args.risk_free_rate,
                                          args.sortino_target_per_period)
            output = calculate_metrics(equity, convention).to_dict()
        except (ValueError, TypeError, ArithmeticError) as exc:
            parser.error(str(exc))
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
