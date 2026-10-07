import pandas as pd
import numpy as np
from data.schema import validate
from c_contracts import StrategyABC, Target


def run_backtest(
    bars: pd.DataFrame,
    strategy: StrategyABC,
    interval: str,
    initial_equity: float = 50_000.0,
    fee_rate_taker: float = 0.001,
    fee_rate_maker: float = 0.0005,
):
    bars = validate(bars, interval=interval)
    bars = bars[bars.is_final].sort_values(["ts", "symbol"]).reset_index(drop=True)

    all_ts = sorted(bars.ts.unique())
    close = bars.pivot(index="ts", columns="symbol", values="close").sort_index()

    equity = initial_equity
    weights = {}  # symbol -> target weight
    records = []

    for i, ts in enumerate(all_ts):
        if i < strategy.warmup_bars:
            continue
        if i > 0:
            prev_ts = all_ts[i - 1]
            if prev_ts in close.index and ts in close.index:
                rets = close.loc[ts] / close.loc[prev_ts] - 1
                pnl = 0.0
                for sym, w in weights.items():
                    r = rets.get(sym, np.nan)
                    if pd.notna(r):
                        pnl += w * r
                equity *= (1.0 + pnl)

        history = bars[bars.ts < ts]
        if history.empty:
            continue

        targets = strategy.on_bar(history)
        target_weights = {t.symbol: t.target_weight for t in targets}

        all_syms = set(weights) | set(target_weights)
        turnover = 0.0
        for sym in all_syms:
            old_w = weights.get(sym, 0.0)
            new_w = target_weights.get(sym, 0.0)
            turnover += abs(new_w - old_w)

        fee = turnover * equity * fee_rate_taker
        equity -= fee
        weights = target_weights

        records.append({
            "ts": ts,
            "equity": equity,
            "turnover": turnover,
            "fee": fee,
            "fee_drag_pct": fee / equity if equity > 0 else 0.0,
        })

    return pd.DataFrame(records)