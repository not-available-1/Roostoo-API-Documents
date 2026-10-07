import pandas as pd
from data.binance_vision import fetch, check_against_official

bars_4h = pd.read_parquet("bars/all_4h.parquet")
mine = bars_4h[bars_4h.symbol == "BTC/USD"]
official = fetch("BTCUSDT", "4h", "2024-10")
bad = check_against_official(mine, official)
print("Error", len(bad))
if len(bad):
    print(bad.head())

from backtest import run_backtest
from strategies import CrossSectionalMomentum
from score import composite

bars_4h = pd.read_parquet("bars/all_4h.parquet")

strategy = CrossSectionalMomentum(lookback=24, top_k=1, bottom_k=1)
result = run_backtest(bars_4h, strategy, interval="4h")
print(result.head())
print(result.tail())
print("费用拖累 mean:", result.fee_drag_pct.mean())
print("最终 equity:", result.equity.iloc[-1])

all_ts = sorted(bars_4h.ts.unique())
split = all_ts[int(len(all_ts) * 0.7)]
is_bars = bars_4h[bars_4h.ts <= split]
oos_bars = bars_4h[bars_4h.ts > split]

is_res = run_backtest(is_bars, strategy, "4h")
oos_res = run_backtest(oos_bars, strategy, "4h")

is_ret = is_res.equity.pct_change().dropna()
oos_ret = oos_res.equity.pct_change().dropna()
is_score = composite(is_ret)
oos_score = composite(oos_ret)
print("IS:", is_score, "OOS:", oos_score)
print("OOS/IS:", oos_score / is_score if is_score else None)