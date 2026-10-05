"""B0 — Bar / Ticker data contract.

Owner: B (Data). Consumers: C (Strategy), A (live feed), D (metrics).
FROZEN: column names, order and dtypes must not change without a 4-person PR review.
Both the historical loader (Binance Vision) and the live BarFeed MUST produce
frames that pass `validate_bars`, so backtests and live trading see identical data.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

BAR_COLUMNS = [
    "open_time",     # int64, epoch ms of bar OPEN
    "open",          # float64
    "high",          # float64
    "low",           # float64
    "close",         # float64
    "volume",        # float64, base-asset volume
    "quote_volume",  # float64, USD volume
    "n_trades",      # int64
]

BAR_DTYPES = {
    "open_time": "int64",
    "open": "float64",
    "high": "float64",
    "low": "float64",
    "close": "float64",
    "volume": "float64",
    "quote_volume": "float64",
    "n_trades": "int64",
}


@dataclass(frozen=True)
class Ticker:
    """One pair's live snapshot, mirroring Roostoo GET /v3/ticker fields."""

    pair: str
    max_bid: float
    min_ask: float
    last: float
    change: float        # 24h price % change, e.g. 0.0059 == +0.59%
    quote_volume: float  # 24h USD turnover, used for liquidity filtering

    @property
    def mid(self) -> float:
        return (self.max_bid + self.min_ask) / 2.0

    @property
    def spread_bps(self) -> float:
        m = self.mid
        return (self.min_ask - self.max_bid) / m * 1e4 if m else float("nan")

    def buy_price(self) -> float:
        """A market BUY crosses the ask."""
        return self.min_ask

    def sell_price(self) -> float:
        """A market SELL hits the bid."""
        return self.max_bid


def empty_bars() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=d) for c, d in BAR_DTYPES.items()})


def validate_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce + sanity-check a bar frame. Raises on anything a backtest must not see."""
    missing = [c for c in BAR_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"bars missing columns: {missing}")
    out = df[BAR_COLUMNS].copy()
    for c, d in BAR_DTYPES.items():
        out[c] = out[c].astype(d)
    if out["open_time"].duplicated().any():
        raise ValueError("duplicate open_time in bars")
    if not out["open_time"].is_monotonic_increasing:
        out = out.sort_values("open_time").reset_index(drop=True)
    if (out[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("non-positive price in bars")
    if (out["high"] < out[["open", "close"]].max(axis=1) - 1e-12).any():
        raise ValueError("high below open/close")
    if (out["low"] > out[["open", "close"]].min(axis=1) + 1e-12).any():
        raise ValueError("low above open/close")
    return out.reset_index(drop=True)


def resample_bars(df: pd.DataFrame, interval_ms: int) -> pd.DataFrame:
    """Aggregate validated bars to a coarser interval (crypto trades 24/7, no sessions)."""
    src = validate_bars(df)
    if src.empty:
        return empty_bars()
    g = src.groupby(src["open_time"] // interval_ms * interval_ms, sort=True)
    out = pd.DataFrame(
        {
            "open_time": g["open_time"].first(),
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
            "quote_volume": g["quote_volume"].sum(),
            "n_trades": g["n_trades"].sum(),
        }
    ).reset_index(drop=True)
    return validate_bars(out)
