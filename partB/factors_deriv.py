"""衍生品因子（B 侧扩展库）。日频、横截面 rank 输出，防未来函数规则同 factors.py。

可用时间约定: 日 bar ts = D 00:00 UTC。funding 在 00/08/16 UTC 结算,
D 00:00 那笔结算在 ts 当下才落地 → 因子只用 fundingTime < ts 的结算(即 D-1 及更早)。

因子与金融逻辑:
- funding_carry_3d  = -mean(最近 3 笔结算费率)
  逻辑: 费率是多头付空头的成本。持续高正费率 = 多头拥挤 + 持有成本高 →
  ① carry: 收负费率的一方赚钱; ② 拥挤卸载: 高费率币后续收益偏低。截面取负 → 分高=做多。
- funding_chg_3d_7d = -(mean3 - mean(前 7 笔))
  逻辑: 费率快速抬升 = 杠杆多头正在涌入(拥挤形成中) → 反向信号; 费率回落 = 去拥挤完成。
- basis_level_1d    = -(perp/spot - 1), 取 D 00:00 前最后一根已收盘 4h bar 的值
  逻辑: 永续基差 = 边际杠杆需求温度计; 高基差=过热→截面负向。
  数据: vision futures klines(免密钥)。此前记的"沙箱访问不了、要队友机器跑"是**错的**,
  真因是下载器把 URL 写成了 data/binance/futures/um/... (多一段 /binance/) → 稳定 404。
  已在 partB/data/binance_deriv.py 修正, 2026-10-04 实测可下 2024-10 起的全历史。
  传入的 fut_close_w / spot_close_w 必须是 **4h** 网格、index 为 bar 开盘时刻(ms)。
"""
import pathlib

import numpy as np
import pandas as pd

from partB import factors as F

CACHE = pathlib.Path(__file__).resolve().parent / "cache" / "deriv"
SETTLE_PER_DAY = 3


def load_funding_wide() -> pd.DataFrame:
    d = pd.read_parquet(CACHE / "funding.parquet")
    return d.pivot(index="ts", columns="symbol", values="funding_rate").sort_index()


def load_basis_panels(spot_1h: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """构造 (perp_close_4h, spot_close_4h) 两张宽表, 供 compute_deriv_factors 算基差。

    perp 来自 `python -m partB.data.binance_deriv --what basis` 落的 cache/deriv/fut4h_*.parquet;
    spot 由本地 1h 缓存 resample 到 4h。

    ⚠️ 必须把 perp 重索引到 spot 的网格上再返回: 两者是两条独立的时间序列,
    直接 div() 会按各自的 index 对齐 —— 只要有一边缺一根 bar, 后面全部错位一格,
    算出来的"基差"其实是相邻两根的比值, 而且不会报错。
    没有 perp 的币整列为 NaN → 因子值 NaN → 走 R3 剔除, 不许填 0。
    """
    from partB.data.binance_vision import resample

    files = sorted(CACHE.glob("fut4h_*.parquet"))
    if not files:
        raise SystemExit(
            f"{CACHE} 下没有 fut4h_*.parquet。\n"
            f"先跑: python -m partB.data.binance_deriv --what basis --start 2024-10")
    fut = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    spot_w = F.to_wide(resample(spot_1h, "4h"), "close")
    fut_w = F.to_wide(fut, "close").reindex(index=spot_w.index, columns=spot_w.columns)
    return fut_w, spot_w


def _align_daily(fw: pd.DataFrame, daily_ts: pd.Index) -> pd.DataFrame:
    """把 8h 结算序列重采样到日 bar: 第 D 行 = fundingTime < ts_D 的最近 SETTLE_PER_DAY 笔均值等。"""
    out_mean3, out_chg = {}, {}
    for sym in fw.columns:
        s = fw[sym].dropna()
        if s.empty:
            continue
        m3, chg = [], []
        pos = s.index.searchsorted(daily_ts, side="left")  # 第一笔 >= ts 的位置 → 之前都可用
        for i, p in zip(daily_ts, pos):
            hist = s.iloc[max(0, p - 10):p]
            if len(hist) >= SETTLE_PER_DAY:
                m3.append(hist.iloc[-SETTLE_PER_DAY:].mean())
            else:
                m3.append(np.nan)
            if len(hist) >= SETTLE_PER_DAY + 7:
                chg.append(hist.iloc[-SETTLE_PER_DAY:].mean() - hist.iloc[-10:-SETTLE_PER_DAY].mean())
            else:
                chg.append(np.nan)
        out_mean3[sym] = pd.Series(m3, index=daily_ts)
        out_chg[sym] = pd.Series(chg, index=daily_ts)
    return (pd.DataFrame(out_mean3), pd.DataFrame(out_chg))


def compute_deriv_factors(daily_ts: pd.Index, fut_close_w: pd.DataFrame | None = None,
                          spot_close_w: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
    fw = load_funding_wide()
    m3, chg = _align_daily(fw, daily_ts)
    raw = {
        "funding_carry_3d": -m3,
        "funding_chg_3d_7d": -chg,
    }
    if fut_close_w is not None and spot_close_w is not None:
        basis = fut_close_w.div(spot_close_w) - 1.0
        # ⚠️ 不能直接 basis.reindex(daily_ts)。basis 的 index 是 4h bar 的**开盘**时刻,
        # 而一根 4h bar 的 close 要到 ts+4h 才知道。直接取 D 00:00 那行 = 用上了
        # [D 00:00, D 04:00) 这根的收盘价, 比因子标称的可用时刻晚 4 小时 → 未来函数,
        # 而且方向上一定是"帮忙"的(提前看到了当天基差), 评估结果会系统性虚高。
        # 正确做法: 取日 bar 开盘前**最后一根已收盘**的 4h bar, 即 daily_ts - step。
        dif = basis.index.to_series().diff().dropna()
        step = int(dif.min()) if len(dif) else 14_400_000
        basis_d = basis.reindex(pd.Index(daily_ts - step)).ffill(limit=1)
        basis_d.index = daily_ts
        raw["basis_level_1d"] = -basis_d
    return {k: F.cs_rank(v) for k, v in raw.items()}
