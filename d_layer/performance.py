"""Transparent team metric conventions for the stated composite weights."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence


ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True)
class MetricConvention:
    """Team convention to confirm against the official judging implementation.

    Returns are simple returns sampled evenly. Sharpe uses sample standard
    deviation. Sortino uses RMS of negative deviations over all periods.
    Calmar divides annualized geometric return by peak-to-trough drawdown.
    """

    periods_per_year: int
    annual_risk_free_rate: Decimal = ZERO
    sortino_target_per_period: Decimal = ZERO

    def __post_init__(self) -> None:
        if type(self.periods_per_year) is not int or self.periods_per_year <= 0:
            raise ValueError("periods_per_year must be a positive integer")
        for value in (self.annual_risk_free_rate, self.sortino_target_per_period):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError("rate and target must be finite Decimals")


@dataclass(frozen=True)
class PerformanceMetrics:
    """All returns and ratios are dimensionless; undefined ratios are None."""

    total_return: Decimal
    running_peaks: tuple[Decimal, ...]
    drawdowns: tuple[Decimal, ...]
    max_drawdown: Decimal
    sharpe: Decimal | None
    sortino: Decimal | None
    calmar: Decimal | None
    composite: Decimal | None
    convention: MetricConvention

    def to_dict(self) -> dict[str, object]:
        """Render exact decimal strings and JSON null for undefined ratios."""

        optional = lambda value: None if value is None else str(value)
        return {
            "total_return": str(self.total_return),
            "running_peaks": [str(x) for x in self.running_peaks],
            "drawdowns": [str(x) for x in self.drawdowns],
            "max_drawdown": str(self.max_drawdown),
            "sharpe": optional(self.sharpe), "sortino": optional(self.sortino),
            "calmar": optional(self.calmar), "composite": optional(self.composite),
            "convention": {
                "label": "team metric convention — to confirm",
                "periods_per_year": self.convention.periods_per_year,
                "annual_risk_free_rate": str(self.convention.annual_risk_free_rate),
                "sortino_target_per_period": str(self.convention.sortino_target_per_period),
                "sharpe_deviation": "sample",
                "sortino_downside": "RMS over all returns",
                "calmar_return": "geometric annualized",
            },
        }


def composite_score(sortino: Decimal | None, sharpe: Decimal | None,
                    calmar: Decimal | None) -> Decimal | None:
    """Apply only the supplied hackathon weights; undefined inputs stay null."""

    if any(value is None or not isinstance(value, Decimal) or not value.is_finite()
           for value in (sortino, sharpe, calmar)):
        return None
    return Decimal("0.4") * sortino + Decimal("0.3") * sharpe + Decimal("0.3") * calmar


def calculate_metrics(equity: Sequence[Decimal],
                      convention: MetricConvention) -> PerformanceMetrics:
    """Calculate returns and ratios from an evenly sampled positive equity series."""

    if not equity or any(not isinstance(x, Decimal) or not x.is_finite() or x <= ZERO
                         for x in equity):
        raise ValueError("equity series must contain finite positive Decimals")
    returns = tuple(equity[i] / equity[i - 1] - ONE for i in range(1, len(equity)))
    peaks: list[Decimal] = []
    drawdowns: list[Decimal] = []
    peak = equity[0]
    for value in equity:
        peak = max(peak, value)
        peaks.append(peak)
        drawdowns.append((peak - value) / peak)
    max_drawdown = max(drawdowns)
    total_return = equity[-1] / equity[0] - ONE
    sharpe: Decimal | None = None
    sortino: Decimal | None = None
    calmar: Decimal | None = None
    if len(returns) >= 2:
        count = Decimal(len(returns))
        mean = sum(returns, ZERO) / count
        variance = sum(((value - mean) ** 2 for value in returns), ZERO) / (count - ONE)
        if variance > ZERO:
            per_period_rf = convention.annual_risk_free_rate / convention.periods_per_year
            sharpe = (mean - per_period_rf) * Decimal(convention.periods_per_year).sqrt() / variance.sqrt()
        downside = sum((min(value - convention.sortino_target_per_period, ZERO) ** 2
                        for value in returns), ZERO) / count
        if downside > ZERO:
            sortino = ((mean - convention.sortino_target_per_period) *
                       Decimal(convention.periods_per_year).sqrt() / downside.sqrt())
        if max_drawdown > ZERO:
            annualized = (equity[-1] / equity[0]) ** (Decimal(convention.periods_per_year) / count) - ONE
            calmar = annualized / max_drawdown
    composite = composite_score(sortino, sharpe, calmar)
    return PerformanceMetrics(total_return, tuple(peaks), tuple(drawdowns), max_drawdown,
                              sharpe, sortino, calmar, composite, convention)
