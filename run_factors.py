import pandas as pd
import datetime as dt
import factors as F
from backtest import run_backtest
from strategies import MultiFactorStrategy
from score import composite
from data.binance_vision import resample
from data.schema import validate


BARS_PATH = r"C:\Users\weibin.WSSHAFP5BV7TX\Desktop\partB 2\bars\all_4h.parquet"

bars_4h = pd.read_parquet(BARS_PATH)
print("bars:", bars_4h.shape, bars_4h.symbol.nunique())

# 因子
fs = F.compute_all(bars_4h, interval="4h", min_usd=1_000_000)  # 改回 B 默认
close_w = F.to_wide(bars_4h, "close")
print("factors:", list(fs.keys()))

# 因子报告
rep = F.factor_report(fs, close_w, horizon=6)
corr = F.factor_corr(fs)
print(rep.round(3))

# 写死单因子
selected = ["lowvol_14d"]
print("selected:", selected)


def make_strategy(min_hold_bars=0, weight_threshold=0.0,
                  top_k=5, bottom_k=5, net="50-50"):
    return MultiFactorStrategy(
        factors=fs, selected=selected,
        top_k=top_k, bottom_k=bottom_k, net=net,
        warmup_bars=200,
        min_hold_bars=min_hold_bars,
        weight_threshold=weight_threshold,
    )


def run_and_report(label, min_hold_bars=0, weight_threshold=0.0,
                   top_k=5, bottom_k=5, net="50-50"):
    strategy = make_strategy(min_hold_bars, weight_threshold, top_k, bottom_k, net)
    result = run_backtest(bars_4h, strategy, "4h", initial_equity=100000)

    all_ts = sorted(bars_4h.ts.unique())
    split = all_ts[int(len(all_ts) * 0.7)]
    is_bars = bars_4h[bars_4h.ts <= split]
    oos_bars = bars_4h[bars_4h.ts > split]

    is_res = run_backtest(is_bars, make_strategy(min_hold_bars, weight_threshold, top_k, bottom_k, net),
                          "4h", initial_equity=100000)
    oos_res = run_backtest(oos_bars, make_strategy(min_hold_bars, weight_threshold, top_k, bottom_k, net),
                           "4h", initial_equity=100000)

    is_ret = is_res.equity.pct_change().dropna()
    oos_ret = oos_res.equity.pct_change().dropna()
    is_score = composite(is_ret)
    oos_score = composite(oos_ret)

    print(f"\n=== {label} ===")
    print(f"min_hold_bars={min_hold_bars} weight_threshold={weight_threshold} "
          f"top_k={top_k} bottom_k={bottom_k} net={net}")
    print(f"turnover mean: {result.turnover.mean():.4f}")
    print(f"Cost drag mean: {result.fee_drag_pct.mean():.6f}")
    print(f"Final equity: {result.equity.iloc[-1]:.2f}")
    print(f"IS: {is_score:.4f}  OOS: {oos_score:.4f}")
    print(f"OOS/IS: {oos_score / is_score if is_score else None:.4f}")
    return result


# 切分时间
all_ts = sorted(bars_4h.ts.unique())
split = all_ts[int(len(all_ts) * 0.7)]
print("split time:", dt.datetime.fromtimestamp(split / 1000, tz=dt.timezone.utc))

# 旋钮 2：hold 期
for hold in [0, 6, 12, 24]:
    run_and_report(f"hold={hold}", min_hold_bars=hold)

# 旋钮 3：权重阈值
for th in [0.0, 0.05, 0.1, 0.2]:
    run_and_report(f"threshold={th}", weight_threshold=th)

# 旋钮 4：top_k
for k in [3, 5, 10, 20]:
    run_and_report(f"top_k={k}", top_k=k, bottom_k=k)

# 旋钮 5：net
for net in ["50-50", "60-40", "70-30", "100-0"]:
    run_and_report(f"net={net}", net=net)