"""B 因子库 v0。输入: schema 长表; 输出: 宽表 (index=ts, columns=symbol)。

防未来函数规则:
1. 因子在 ts 行的值只用 <= ts 那根 bar 的收盘信息 → 实际可用时间 = ts + interval。
   回测时 C 必须用 factor.shift(1) 对齐下一根 bar 的收益, 或在 bar 收盘后才下单。
2. 横截面操作只在同一 ts 行内做, 不跨时间。
3. 所有 rolling 都是 trailing window, 禁止 center=True / 全样本标准化。
"""
import numpy as np
import pandas as pd

BARS_PER_DAY = {"1h": 24, "4h": 6, "1d": 1}


def to_wide(df: pd.DataFrame, field: str = "close") -> pd.DataFrame:
    return df.pivot(index="ts", columns="symbol", values=field).sort_index()


# ---------- 横截面工具 ----------
def cs_rank(x: pd.DataFrame) -> pd.DataFrame:
    """横截面百分位排名, 映射到 [-0.5, 0.5]。"""
    return x.rank(axis=1, pct=True) - 0.5


def cs_zscore(x: pd.DataFrame, clip: float = 3.0) -> pd.DataFrame:
    z = x.sub(x.mean(axis=1), axis=0).div(x.std(axis=1), axis=0)
    return z.clip(-clip, clip)


def liquidity_mask(qv: pd.DataFrame, window: int, min_usd: float) -> pd.DataFrame:
    """过去 window 根平均成交额 >= min_usd 才纳入。"""
    return qv.rolling(window, min_periods=window).mean() >= min_usd


# ---------- 因子 ----------
def mom_vol_adj(close, lookback, skip=0):
    """波动调整动量: 过去 lookback 根收益 / 同期波动, 跳过最近 skip 根(避开短期反转)。"""
    r = np.log(close).diff()
    ret = np.log(close.shift(skip) / close.shift(skip + lookback))
    vol = r.rolling(lookback, min_periods=lookback // 2).std() * np.sqrt(lookback)
    return ret / vol


def short_reversal(close, lookback):
    """短期反转: 最近收益取负。"""
    return -np.log(close / close.shift(lookback))


def low_vol(close, lookback):
    """低波动异象: 波动越低分越高。"""
    return -np.log(close).diff().rolling(lookback, min_periods=lookback // 2).std()


def volume_shock(qv, short, long):
    """成交额异动: 短期均值 / 长期均值 (log)。"""
    return np.log(qv.rolling(short).mean() / qv.rolling(long).mean())


def beta_to_btc(close, lookback, btc="BTC/USD"):
    """对 BTC beta, 取负 → 偏好低 beta(控回撤)。"""
    r = np.log(close).diff()
    cov = r.rolling(lookback).cov(r[btc])
    return -(cov.div(r[btc].rolling(lookback).var(), axis=0))


def range_position(high, low, close, lookback):
    """收盘在 N 根高低区间中的位置 (0~1)。"""
    hh, ll = high.rolling(lookback).max(), low.rolling(lookback).min()
    return (close - ll) / (hh - ll)


def compute_all(df: pd.DataFrame, interval: str = "4h", min_usd: float = 1e6) -> dict[str, pd.DataFrame]:
    """返回 {因子名: 横截面 rank 后的宽表}; 不满足流动性的格子为 NaN。"""
    d = BARS_PER_DAY[interval]
    c, h, l, qv = (to_wide(df, f) for f in ["close", "high", "low", "quote_volume"])
    mask = liquidity_mask(qv, 7 * d, min_usd / 24 * (24 // d))  # 换算到每根 bar
    raw = {
        "mom_30d_skip1d": mom_vol_adj(c, 30 * d, skip=d),
        "mom_7d": mom_vol_adj(c, 7 * d),
        "rev_1d": short_reversal(c, d),
        "lowvol_14d": low_vol(c, 14 * d),
        "volshock_1d_14d": volume_shock(qv, d, 14 * d),
        "rangepos_14d": range_position(h, l, c, 14 * d),
    }
    if "BTC/USD" in c:
        raw["lowbeta_30d"] = beta_to_btc(c, 30 * d)
    return {k: cs_rank(v.where(mask)) for k, v in raw.items()}


# ---------- 评估: 交 C 的 top-5 低相关清单 ----------
def forward_returns(close: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """t → t+horizon 收益, 对齐在 t 行(仅用于评估, 不可作为特征)。"""
    return np.log(close.shift(-horizon) / close)


def ic_series(factor, fwd):
    """每期 Spearman IC。"""
    return factor.rank(axis=1).corrwith(fwd.rank(axis=1), axis=1)


def factor_report(factors: dict, close: pd.DataFrame, horizon: int) -> pd.DataFrame:
    fwd = forward_returns(close, horizon)
    rows = {}
    for k, f in factors.items():
        ic = ic_series(f, fwd).dropna()
        turnover = f.diff().abs().sum(axis=1).mean() / f.abs().sum(axis=1).mean()
        rows[k] = {"IC_mean": ic.mean(), "IC_IR": ic.mean() / ic.std() if ic.std() else np.nan,
                   "hit": (ic > 0).mean(), "turnover": turnover, "n": len(ic)}
    return pd.DataFrame(rows).T.sort_values("IC_IR", ascending=False)


def factor_corr(factors: dict) -> pd.DataFrame:
    """因子间平均横截面相关(按期计算再取均值)。"""
    ks = list(factors)
    m = pd.DataFrame(np.eye(len(ks)), ks, ks)
    for i, a in enumerate(ks):
        for b in ks[i + 1:]:
            m.loc[a, b] = m.loc[b, a] = factors[a].corrwith(factors[b], axis=1).mean()
    return m


def ls_spread(factor: pd.DataFrame, fwd: pd.DataFrame, q: float = 0.2) -> pd.Series:
    """每期 做多 top-q / 做空 bottom-q 的收益(log)。按因子横截面百分位分组。"""
    fw = fwd.reindex(index=factor.index, columns=factor.columns)
    r = factor.rank(axis=1, pct=True)
    return (fw.where(r >= 1 - q).mean(axis=1) - fw.where(r <= q).mean(axis=1)).dropna()


def ls_alpha(factor: pd.DataFrame, close: pd.DataFrame, horizon: int,
             ppy: int = 365, q: float = 0.2) -> dict:
    """**经济门槛**: 多空组合收益对市场等权收益回归, 拆成 方向性暴露 + 选股 alpha。

    为什么 IC 不够: IC 是统计门槛(信号有没有方向), 但 IC=0.03 这种量级折成"做多头部/
    做空尾部"的实际价差可能是负的 —— rev_1d 的 IC_IR_IS=0.133 排全场第二, 多空却亏 37%/y。
    为什么还要对市场回归: 样本期(2024-10~2025-12)山寨等权是 -62.7%/y, 任何系统性做空
    高波动币的因子都会自动赚钱, 那部分是赌方向不是选股能力, 必须扣掉再评判。

    返回年化 %(按 ppy 期/年折算, horizon=1 天时 ppy=365):
      gross 毛edge · beta 对市场的暴露 · alpha 扣掉暴露后的截距 · t_alpha 截距 t 值 · n 期数
    判据建议: alpha > 0 且 |t_alpha| >= 2。
    """
    fwd = forward_returns(close, horizon)
    idx = factor.index.intersection(fwd.index)
    s = ls_spread(factor.loc[idx], fwd.loc[idx], q)
    mkt = fwd.loc[idx].mean(axis=1).reindex(s.index)
    d = pd.DataFrame({"s": s, "m": mkt}).dropna()
    if len(d) < 30:
        return {"gross": np.nan, "beta": np.nan, "alpha": np.nan,
                "t_alpha": np.nan, "t_beta": np.nan, "n": len(d)}
    X = np.c_[np.ones(len(d)), d.m.values]
    b = np.linalg.lstsq(X, d.s.values, rcond=None)[0]
    resid = d.s.values - X @ b
    sig = np.sqrt(resid @ resid / (len(d) - 2))
    se = sig * np.sqrt(np.diag(np.linalg.inv(X.T @ X)))
    return {"gross": d.s.mean() * ppy * 100, "beta": b[1], "alpha": b[0] * ppy * 100,
            "t_alpha": b[0] / se[0] if se[0] else np.nan,
            "t_beta": b[1] / se[1] if se[1] else np.nan, "n": len(d)}


def select_low_corr(report: pd.DataFrame, corr: pd.DataFrame, k=5, max_corr=0.5) -> list[str]:
    """按 |IC_IR| 贪心选, 与已选因子相关 > max_corr 的跳过。"""
    chosen = []
    for f in report.IC_IR.abs().sort_values(ascending=False).index:
        if all(abs(corr.loc[f, g]) <= max_corr for g in chosen):
            chosen.append(f)
        if len(chosen) == k:
            break
    return chosen
