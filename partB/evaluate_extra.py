"""稳健性: (a) 只用 2024-10 已上市的老币 (b) 未来函数测试(真实数据) (c) 分年 IC"""
import pandas as pd, numpy as np
from partB.data.binance_vision import resample
from partB import factors as F
h = pd.read_parquet("bars_1h_all.parquet"); h = h[~h.symbol.isin(["OMNI/USD","TON/USD"])]
b = resample(h, "4h"); c = F.to_wide(b)
fs = F.compute_all(b, "4h")
old = c.columns[c.iloc[0].notna()]
fwd = F.forward_returns(c, 6)
print("老币数", len(old))
for k in ["lowvol_14d","lowbeta_30d","rev_1d","rangepos_14d","volshock_1d_14d","mom_30d_skip1d"]:
    ic_all = F.ic_series(fs[k], fwd); ic_old = F.ic_series(fs[k][old], fwd[old])
    yr = ic_all.groupby(pd.to_datetime(ic_all.index, unit="ms").to_period("Q")).mean()
    print(f"{k:16s} 全部 {ic_all.mean():+.4f} | 仅老币 {ic_old.mean():+.4f} | 季度IC>0占比 {(yr>0).mean():.0%} ({len(yr)}季)")
# 未来函数测试: 截断 vs 全量
cut = c.index[2000]
f2 = F.compute_all(b[b.ts <= cut], "4h")
ok = all(np.allclose(fs[k].loc[:cut].values, f2[k].reindex(columns=fs[k].columns).values, equal_nan=True) for k in fs)
fut = b.copy(); m = fut.ts > cut; fut.loc[m, ["open","high","low","close"]] *= np.random.default_rng(1).uniform(.2, 5, m.sum())[:,None]
f3 = F.compute_all(fut, "4h")
ok2 = all(np.allclose(fs[k].loc[:cut].values, f3[k].loc[:cut].values, equal_nan=True) for k in fs)
print("未来函数测试 截断一致:", ok, "| 篡改未来后过去不变:", ok2)
