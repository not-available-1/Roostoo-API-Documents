"""Account-only risk snapshots for logs and later monitoring."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Mapping

from .contracts import AccountState, PriceQuote
from .risk import RiskConfig, _finite, _valid_quote


ZERO = Decimal("0")


@dataclass(frozen=True)
class RiskSnapshot:
    """Machine-readable account exposure and current safety status."""

    timestamp: datetime
    equity: Decimal
    cash: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    long_exposure: Decimal
    short_exposure: Decimal
    per_symbol_weights: Mapping[str, Decimal]
    largest_concentration: Decimal
    current_drawdown: Decimal | None
    session_pnl: Decimal | None
    turnover_notional: Decimal | None
    active_violations: tuple[str, ...]
    trading_halted: bool

    def to_dict(self) -> dict[str, object]:
        """Return JSON-ready values, preserving money as decimal strings."""

        return {
            "timestamp": self.timestamp.isoformat(), "equity": str(self.equity),
            "cash": str(self.cash), "gross_exposure": str(self.gross_exposure),
            "net_exposure": str(self.net_exposure), "long_exposure": str(self.long_exposure),
            "short_exposure": str(self.short_exposure),
            "per_symbol_weights": {k: str(v) for k, v in self.per_symbol_weights.items()},
            "largest_concentration": str(self.largest_concentration),
            "current_drawdown": None if self.current_drawdown is None else str(self.current_drawdown),
            "session_pnl": None if self.session_pnl is None else str(self.session_pnl),
            "turnover_notional": None if self.turnover_notional is None else str(self.turnover_notional),
            "active_violations": list(self.active_violations),
            "trading_halted": self.trading_halted,
        }


def risk_snapshot(account: AccountState, prices: Mapping[str, PriceQuote],
                  config: RiskConfig, now: datetime) -> RiskSnapshot:
    """Value current positions; missing/invalid held prices fail closed."""

    if not _finite(account.equity) or account.equity <= ZERO or not _finite(account.cash):
        raise ValueError("invalid account equity or cash")
    notionals: dict[str, Decimal] = {}
    for symbol, quantity in account.positions.items():
        if not _finite(quantity):
            raise ValueError("invalid position quantity")
        error = _valid_quote(symbol, prices, now, config.max_market_age_seconds)
        if error:
            raise ValueError(f"{symbol}: {error}")
        notionals[symbol] = quantity * prices[symbol].price
    long_exposure = sum((max(n, ZERO) for n in notionals.values()), ZERO)
    short_exposure = sum((-min(n, ZERO) for n in notionals.values()), ZERO)
    gross = long_exposure + short_exposure
    net = long_exposure - short_exposure
    weights = {symbol: notional / account.equity for symbol, notional in notionals.items()}
    concentration = max((abs(w) for w in weights.values()), default=ZERO)
    peak = account.equity_peak
    drawdown = (max(ZERO, (peak - account.equity) / peak)
                if _finite(peak) and peak > ZERO else None)
    start = account.session_start_equity
    pnl = account.equity - start if _finite(start) else None
    violations: list[str] = []
    if any(symbol not in config.allowed_symbols for symbol in weights):
        violations.append("symbol_not_allowed")
    if concentration > config.max_position_weight:
        violations.append("max_position_weight")
    if gross / account.equity > config.max_gross_weight:
        violations.append("max_gross_weight")
    if abs(net) / account.equity > config.max_net_weight:
        violations.append("max_net_weight")
    if account.cash / account.equity < config.min_cash_weight:
        violations.append("min_cash_weight")
    if any(weight < ZERO for weight in weights.values()) and not config.allow_short:
        violations.append("short_disabled")
    if drawdown is None or pnl is None or not _finite(account.turnover_notional):
        violations.append("missing_risk_state")
    else:
        if drawdown >= config.max_drawdown:
            violations.append("max_drawdown")
        if start > ZERO and -pnl / start >= config.max_daily_loss:
            violations.append("max_daily_loss")
        if account.turnover_notional / account.equity > config.max_turnover_weight:
            violations.append("max_turnover_weight")
    if config.emergency_halt:
        violations.append("emergency_halt")
    halted = any(code in violations for code in ("missing_risk_state", "max_drawdown",
                                                 "max_daily_loss", "emergency_halt"))
    return RiskSnapshot(now, account.equity, account.cash, gross, net,
                        long_exposure, short_exposure, weights, concentration,
                        drawdown, pnl, account.turnover_notional,
                        tuple(violations), halted)
