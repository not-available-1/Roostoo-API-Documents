"""ML 特征池 v1 —— 把"人来想因子"换成"机器在一大堆原料里找组合"。

为什么要这一步
------------
`factors.py` 里只有 6 个手工因子, 过完四道门槛只剩 3 个（OOS 盲验后剩 2 个）。
手工因子的产能上限就是"人能想到多少种", 而比赛只剩十几天。所以改成:

    先铺一大堆**原始特征**（本文件, ~75 个）
    → 再用 ML 在里面找线性组合（`ml_mine.py`）
    → 挖出来的东西**事后**才问"金融逻辑讲不讲得通"

顺序很重要: 先挖后解释, 而不是先想好故事再去凑数据。但"事后解释"这一步不能省 ——
过不了门槛的直接扔, 过了门槛却讲不出为什么会有人持续付钱给你的, 要标注成可疑
（大概率是数据挖掘偏差, 见 `ml_mine.py` 的报告部分）。

铁律: 没有未来函数
----------------
每个特征在 ts 这一行的值**只用 <= ts 的 bar**。三条硬规则:

1. 所有 rolling 都是 trailing（pandas 默认）, 禁 `center=True`。
2. **不做时间序列上的全样本标准化**。原始特征直接输出（比如 rv_90 就是波动率本身, 单位是
   每根 bar 的收益率标准差）。标准化留到 `ml_mine.py` 里做, 而且是**横截面**标准化
   （同一个 ts 行内, 65 个币互相比较）—— 横截面操作天然不跨时间, 不可能泄漏未来。
   如果在这里用 `(x - x.mean()) / x.std()` 全样本归一, 2024 年的特征值就会含 2026 年的信息。
3. `--selftest` 会真的去验证上面两条（截断重算 + 篡改未来后过去不变）。

特征分组（`THEME`）
------------------
分组不是装饰: 事后做金融逻辑验证时, 是**按组**问"这类信号为什么该有 alpha",
而不是逐个特征编故事。挖出来的东西如果集中在某一组, 说明是真的经济机制;
如果东一个西一个, 更像噪声。

    ret    动量/反转          vol   波动率与高阶矩
    liq    成交量/流动性       shape 价格形态与位置
    auto   自相关（微观结构）  rel   相对 BTC 的系统性/特质性
    trend  趋势方向与质量      age   成熟度/上架时长
    fund   资金费（衍生品情绪, 只有 65 个有 Binance 永续的币有）

用法
----
  python -m partB.ml_features --selftest          # 未来函数自检（改了这个文件必须跑）
  python -m partB.ml_features --dump              # 存 partB/cache/ml_features_4h.parquet
"""
from __future__ import annotations

import argparse
import pathlib

import numpy as np
import pandas as pd

from partB import factors as F
from partB.data.binance_vision import resample

HERE = pathlib.Path(__file__).resolve().parent
CACHE = HERE / "cache"
OUT = CACHE / "ml_features_4h.parquet"

# 特征 → 经济主题。新增特征必须在这里登记, 否则 --selftest 会报"未归组"。
THEME: dict[str, str] = {}


def _reg(name: str, theme: str) -> str:
    THEME[name] = theme
    return name


# ---------- 基础量 ----------
def _logret(c: pd.DataFrame) -> pd.DataFrame:
    return np.log(c).diff()


def _rv(r: pd.DataFrame, k: int) -> pd.DataFrame:
    """已实现波动率, 年化到"每 k 根 bar"的口径（乘 sqrt(k)）, 这样不同 k 之间可比。"""
    return r.rolling(k, min_periods=k).std() * np.sqrt(k)


def _zscore_trailing(x: pd.DataFrame, k: int) -> pd.DataFrame:
    """相对**自己过去 k 根**的 z 值。注意这是时间序列上的滚动统计, 不是全样本 → 不泄漏。"""
    return (x - x.rolling(k, min_periods=k).mean()) / x.rolling(k, min_periods=k).std()


# ---------- 各组特征 ----------
def _feat_ret(c: pd.DataFrame, d: int) -> dict[str, pd.DataFrame]:
    """动量 / 反转。多档回看期是必须的: 加密市场在 1 天上是反转、在 1~4 周上是动量,
    单一 horizon 会把两个方向相反的信号混成 0。"""
    lc = np.log(c)
    out = {}
    for k in (1, 2, 3, 6, 12, 18, 42, 90, 180):
        out[_reg(f"r_{k}", "ret")] = lc.diff(k)
    # skip 最近 1 天: 避开短期反转对动量的污染（学术动量因子的标准做法）
    for k in (42, 90, 180):
        out[_reg(f"r_{k}_skip{d}", "ret")] = lc.shift(d) - lc.shift(k)
    # 短长差: 动量的"加速度", 比单看短期动量对趋势拐点更敏感
    out[_reg("r_6_minus_r_42", "ret")] = lc.diff(6) - lc.diff(42)
    out[_reg("r_18_minus_r_90", "ret")] = lc.diff(18) - lc.diff(90)
    return out


def _feat_trend(c: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """趋势方向与质量。⚠️ 这一组是收益的**非线性**变换（sign / 比值 / 绝对值），
    摊不进 `ml_mine._RET_SPANS` 的线性 horizon 归因，所以单独成主题；归因时直接读原始权重。

    TSMOM = 时间序列动量：不跟别的币比，只看自己过去的方向。
    文献（Liu & Tsyvinski 2021）在加密上最强的是 3~4 周窗口 → 4h bar 上取 42/90/180 根。
    sign 版只留方向；vol 版 = 收益 / 自身波动，把"波动越大信号越不可信"交给模型自己权衡。
    """
    lc = np.log(c)
    r = lc.diff()
    out = {}
    for k in (42, 90, 180):
        out[_reg(f"tsm_{k}", "trend")] = np.sign(lc.diff(k))
        vol_k = (r.rolling(k, min_periods=k).std() * np.sqrt(k)).replace(0, np.nan)
        out[_reg(f"tsm_vol_{k}", "trend")] = lc.diff(k) / vol_k
    # 效率比 (Kaufman ER) = |净位移| / 路径总长 ∈ [0,1]。1 = 单边直线趋势, 0 = 原地震荡。
    # 同样涨 10%, 一路涨上去和震荡着涨上去, 后续的延续性完全不同。
    for k in (42, 90):
        path = r.abs().rolling(k, min_periods=k).sum().replace(0, np.nan)
        out[_reg(f"er_{k}", "trend")] = lc.diff(k).abs() / path
    return out


def _feat_vol(c: pd.DataFrame, h: pd.DataFrame, l: pd.DataFrame, d: int) -> dict[str, pd.DataFrame]:
    """波动率与高阶矩。低波动异象在加密上是已知最强的横截面规律之一
    （现有入选因子里 lowvol_14d 的 IS alpha = +72.7%/y, t = 4.09）, 所以这一组铺得比较密。"""
    r = _logret(c)
    out = {}
    for k in (6, 18, 42, 90, 180):
        out[_reg(f"rv_{k}", "vol")] = _rv(r, k)
    out[_reg("rv_ratio_18_90", "vol")] = _rv(r, 18) / _rv(r, 90)
    out[_reg("rv_ratio_6_42", "vol")] = _rv(r, 6) / _rv(r, 42)
    # 下行波动: 只数跌的那部分。同样波动率, "全是跌出来的"和"涨跌各半"风险完全不同。
    # 用 clip 而不是 where(r<0)+std: 后者一半格子是 NaN, rolling(min_periods=k) 永远凑不满 k 个
    # 有效值 → 整列全 NaN（这个坑真实踩过, 表现为"特征存在但覆盖率 0"）。
    out[_reg("dvol_90", "vol")] = np.sqrt((r.clip(upper=0.0) ** 2).rolling(90, min_periods=90).mean() * 90)
    # Parkinson: 用高低价而不是收盘价估波动, 信息效率高约 5 倍（同样窗口更准）
    hl = np.log(h / l)
    out[_reg("pk_42", "vol")] = np.sqrt((hl ** 2).rolling(42, min_periods=42).mean() / (4 * np.log(2)))
    # 波动率的波动率: 二阶不确定性。高 vol-of-vol 通常伴随做市商撤单、价差走阔。
    out[_reg("volofvol_90", "vol")] = _rv(r, 18).rolling(90, min_periods=90).std()
    out[_reg("rskew_90", "vol")] = r.rolling(90, min_periods=90).skew()
    out[_reg("rkurt_90", "vol")] = r.rolling(90, min_periods=90).kurt()
    # MAX 效应 (Bali et al.): 过去 N 根里最大的单根收益 = 彩票属性。追彩票 → 高估 → 预期回报低。
    for k in (42, 90):
        out[_reg(f"max_ret_{k}", "vol")] = r.rolling(k, min_periods=k).max()
    return out


def _feat_liq(qv: pd.DataFrame, nt: pd.DataFrame, c: pd.DataFrame, d: int) -> dict[str, pd.DataFrame]:
    """成交量 / 流动性。⚠️ 这里的量来自 Binance, 而 Roostoo 是模拟撮合 ——
    用它做**因子**没问题（它反映的是真实世界的关注度）, 但别拿它当"我们的成交量"去估冲击成本。"""
    r = _logret(c)
    lq = np.log(qv.clip(lower=1.0))
    out = {}
    for k in (6, 18, 90):
        out[_reg(f"qv_z_{k}", "liq")] = _zscore_trailing(lq, k)
    out[_reg("qv_trend_18_90", "liq")] = lq.rolling(18, min_periods=18).mean() - lq.rolling(90, min_periods=90).mean()
    # Amihud 非流动性: 每 1 美元成交额能推动多少价格。越大 = 越薄 = 要求越高的流动性溢价。
    out[_reg("amihud_42", "liq")] = (r.abs() / qv.clip(lower=1.0)).rolling(42, min_periods=42).mean() * 1e9
    out[_reg("nt_z_18", "liq")] = _zscore_trailing(np.log(nt.clip(lower=1.0).astype(float)), 18)
    # 单笔均额: 成交额/笔数。变大 = 大单进场（机构/巨鲸）, 变小 = 散户主导。
    ats = np.log((qv / nt.clip(lower=1.0)).clip(lower=1e-9))
    out[_reg("avg_trade_42", "liq")] = ats.rolling(42, min_periods=42).mean()
    out[_reg("avg_trade_trend", "liq")] = ats.rolling(18, min_periods=18).mean() - ats.rolling(90, min_periods=90).mean()
    # 量价相关: 涨的时候放量 = 趋势有承接; 涨的时候缩量 = 拉不动。
    out[_reg("pv_corr_90", "liq")] = r.rolling(90, min_periods=90).corr(lq.diff())
    return out


def _feat_shape(c: pd.DataFrame, h: pd.DataFrame, l: pd.DataFrame, o: pd.DataFrame, d: int) -> dict[str, pd.DataFrame]:
    """价格形态与位置。rangepos_14d 是现有入选因子里最强的（IS alpha +139.2%/y, t = 7.82）,
    所以这组同样铺密: 它捕捉的是"离区间上沿还有多远"这个非常朴素但反复有效的东西。"""
    out = {}
    for k in (6, 18, 42, 90, 180):
        out[_reg(f"rangepos_{k}", "shape")] = F.range_position(h, l, c, k)
    # 回撤深度: 距 N 根最高价跌了多少。和 rangepos 相关但不对称（rangepos 有下界, 回撤没有）
    for k in (90, 180):
        out[_reg(f"dd_{k}", "shape")] = c / c.rolling(k, min_periods=k).max() - 1.0
    out[_reg("up_from_low_180", "shape")] = c / c.rolling(180, min_periods=180).min() - 1.0
    # bar 内位置: 收在上影线还是下影线。(close-open)/(high-low) ∈ [-1,1]
    rng = (h - l).replace(0, np.nan)
    out[_reg("intrabar_18", "shape")] = ((c - o) / rng).rolling(18, min_periods=18).mean()
    out[_reg("gap_18", "shape")] = (o / c.shift(1) - 1.0).rolling(18, min_periods=18).mean()
    out[_reg("hl_range_18", "shape")] = (rng / c).rolling(18, min_periods=18).mean()
    return out


def _feat_auto(c: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """收益率自相关 = 微观结构摩擦的代理。加密里负自相关（来回打脸）通常意味着
    做市商在赚价差、信息含量低; 正自相关意味着有持续的资金流在推。"""
    r = _logret(c)
    out = {}
    for k in (42, 90):
        out[_reg(f"ac1_{k}", "auto")] = r.rolling(k, min_periods=k).corr(r.shift(1))
    # lag-6 = 4h bar 上的"日频季节性"（每天同一时段的行为重复）
    out[_reg("ac6_90", "auto")] = r.rolling(90, min_periods=90).corr(r.shift(6))
    return out


def _feat_rel(c: pd.DataFrame, btc: str = "BTC/USD") -> dict[str, pd.DataFrame]:
    """相对 BTC: 把收益拆成"跟着大盘"和"自己的"。

    为什么要拆: 山寨币收益的 60~90% 是 BTC 带的。不拆的话, 任何"选币"因子其实都在偷偷
    赌 beta。拆完之后 beta 本身是一个因子（低 beta 抗跌）, 残差动量是另一个（真有独立行情）。
    """
    r = _logret(c)
    out = {}
    if btc not in r.columns:
        return out
    rb = r[btc]
    # ⚠️ 所有 DataFrame <op> Series 都必须写 axis=0。
    # pandas 的默认对齐轴是 columns: `df - series` 会拿 series 的**索引**（这里是时间戳）
    # 去和 df 的**列名**（这里是币名）对齐, 结果是两边的并集、且整表 NaN。
    # 这个错不报异常, 只表现为"特征算出来了但全是空的", 极难发现（本文件真实踩过）。
    for k in (42, 90, 180):
        cov = r.rolling(k, min_periods=k).cov(rb)
        out[_reg(f"beta_{k}", "rel")] = cov.div(rb.rolling(k, min_periods=k).var(), axis=0)
    # 特质波动: 扣掉 BTC 之后还剩多少波动
    beta90 = out["beta_90"]
    # `beta90.mul(rb, axis=0)` 而不是 `rb.mul(beta90, axis=0)`: Series 当调用方时 axis 的语义
    # 和 DataFrame 当调用方时不一样, 后者会把 Series 的索引对到 DataFrame 的**列**上 → 4457 列。
    resid = r.sub(beta90.mul(rb, axis=0), axis=0)
    out[_reg("idiovol_90", "rel")] = resid.rolling(90, min_periods=90).std() * np.sqrt(90)
    out[_reg("resmom_42", "rel")] = resid.rolling(42, min_periods=42).sum()
    # 相对强弱: 跑赢/跑输大盘多少
    lb = np.log(c[btc])
    for k in (6, 18, 42, 90):
        out[_reg(f"rs_{k}", "rel")] = np.log(c).diff(k).sub(lb.diff(k), axis=0)
    return out


def _feat_age(c: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """成熟度: 该币截至 t 在我们数据里存在了多少天（= t 减该币第一根 bar 的时间戳）。

    这是"上架时长"在现有数据内的最大努力: 42 个老币的第一根 bar 都是下载起点
    (2024-10-01), 真实上架时间更早, 所以老币之间没有区分度; 它能区分的是
    "2024-10 之后才上架的新币"(24 个)。对应质量类里 CMA 的逻辑: 新币 = 高风险/高抛压。

    注意用时间戳而不是 notna().cumsum(): cumsum 会被个别缺失 bar 干扰, 造成
    "几乎相同但标准差不为 0"的伪截面 —— z 分数爆表后被 clip 截成有偏分布(测试真实踩过)。
    """
    first_ts = c.notna().idxmax()
    age_ms = c.index.to_numpy()[:, None] - first_ts.to_numpy()[None, :]
    age = pd.DataFrame(age_ms, index=c.index, columns=c.columns) / 86_400_000
    return {_reg("age_days", "age"): age.astype(float)}


def _feat_fund(fund_w: pd.DataFrame | None, rv90: pd.DataFrame | None, d: int) -> dict[str, pd.DataFrame]:
    """资金费: 永续合约多头付给空头的费率, 8 小时一次。

    ⚠️ 这批因子在**基差/资金费单独评估**里已经被否决过（见 `reports/deriv_basis_verdict.md`:
    |IC_IR_IS| 只有 0.028/0.038, 多空 alpha 的 t 值全在 ±1.4 内, 根本原因是横截面基差
    离散度只有 0.07% 而往返手续费就要 0.2%）。这里重新放进来是因为**语境变了**:
    单独用没用, 不代表和其他 50 个特征一起进线性模型时没贡献（它可能只在极端分位上有用）。
    结论仍要过四道门槛, 不过就还是不过。
    """
    out = {}
    if fund_w is None:
        return out
    for k in (3, 7, 21):
        out[_reg(f"fund_{k}d", "fund")] = fund_w.rolling(k * d, min_periods=k * d).mean()
    out[_reg("fund_chg_3d_7d", "fund")] = (fund_w.rolling(3 * d, min_periods=3 * d).mean()
                                          - fund_w.rolling(7 * d, min_periods=7 * d).mean())
    out[_reg("fund_std_7d", "fund")] = fund_w.rolling(7 * d, min_periods=7 * d).std()
    if rv90 is not None:
        # 交互项: 高资金费 + 高波动 才危险, 低波动的高资金费往往只是持仓拥挤
        out[_reg("fund_x_rv", "fund")] = fund_w.rolling(7 * d, min_periods=7 * d).mean() * rv90
    return out


# ---------- 汇总 ----------
def load_funding_wide(interval: str = "4h") -> pd.DataFrame | None:
    """把 8 小时一次的资金费摊到 bar 网格上（前向填充）。

    资金费在 ts 时刻**已经结算并公布**, 所以直接放在 ts 那一行不算泄漏（bar 在 ts+interval
    才收盘可用, 比公布时刻还晚）。8h 的结算点是 00/08/16 UTC, 恰好落在 4h 网格上,
    所以 reindex 之后只需要 ffill 一格空的（04/12/20 这些 bar 沿用上一个结算值）。
    """
    f = CACHE / "deriv" / "funding.parquet"
    if not f.exists():
        return None
    d = pd.read_parquet(f)
    w = d.pivot(index="ts", columns="symbol", values="funding_rate").sort_index()
    step = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[interval]
    grid = np.arange(w.index.min() // step * step, w.index.max() + step, step)
    both = np.union1d(w.index.to_numpy(), grid)
    return w.reindex(both).ffill().reindex(grid)


def build(df: pd.DataFrame, interval: str = "4h", min_usd: float = 1e6,
          with_funding: bool = True) -> dict[str, pd.DataFrame]:
    """输入 B0 长表, 输出 {特征名: 宽表(index=ts, columns=symbol)}。**原始值, 未标准化。**

    流动性不足的格子设 NaN: 一个币如果过去 7 天日均成交额不到 min_usd, 它在实盘上根本
    吃不下单, 让它参与横截面排名只会污染因子。
    """
    d = F.BARS_PER_DAY[interval]
    c, h, l, o, qv, nt = (F.to_wide(df, f) for f in
                          ["close", "high", "low", "open", "quote_volume", "n_trades"])
    mask = F.liquidity_mask(qv, 7 * d, min_usd / 24 * (24 // d))

    feats: dict[str, pd.DataFrame] = {}
    feats.update(_feat_ret(c, d))
    feats.update(_feat_trend(c))
    feats.update(_feat_vol(c, h, l, d))
    feats.update(_feat_liq(qv, nt, c, d))
    feats.update(_feat_shape(c, h, l, o, d))
    feats.update(_feat_auto(c))
    feats.update(_feat_rel(c))
    feats.update(_feat_age(c))
    if with_funding:
        fw = load_funding_wide(interval)
        if fw is not None:
            fw = fw.reindex(index=c.index, columns=c.columns)
            feats.update(_feat_fund(fw, feats.get("rv_90"), d))

    # 形状闸门: 宽表必须严格是 (n_bars, n_symbols)。
    # 专门用来抓 `df <op> series` 忘了写 axis=0 —— 那个错不抛异常, 只会把列数从 65 撑到 4457
    # 并且整表 NaN, 一路传到模型里表现为"这个特征没用", 排查成本极高。
    bad = {k: v.shape for k, v in feats.items() if v.shape != c.shape}
    assert not bad, f"特征形状不对(基本是 DataFrame<Series> 漏了 axis=0): {bad}"

    ungrouped = [k for k in feats if k not in THEME]
    assert not ungrouped, f"这些特征没在 THEME 里登记: {ungrouped}"
    return {k: v.where(mask) for k, v in feats.items()}


def build_panel(feats: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, list[str]]:
    """把 {特征: 宽表} 摊成一张长面板 (index=(ts,symbol), columns=特征名), 给 ML 用。"""
    names = sorted(feats)
    parts = [feats[n].stack(dropna=False).rename(n) for n in names]
    p = pd.concat(parts, axis=1)
    p.index.names = ["ts", "symbol"]
    return p, names


# ---------- 自检 ----------
def selftest() -> None:
    """未来函数自检。两件独立的事, 都要过:

    (a) **截断一致**: 只喂前 N 根 bar 重算, 得到的历史特征值必须和喂全量时逐格相等。
        抓的是"用了未来窗口"（比如 center=True、全样本标准化）。
    (b) **篡改未来不影响过去**: 把 N 之后的价格乘上随机数, 前 N 根的特征必须一字不变。
        抓的是"按全样本统计量归一化"这类更隐蔽的泄漏 —— (a) 抓不到它, 因为截断后
        它自己会重新算一个不同的归一化常数, 但历史格子看起来还是"一致"的。
    """
    h = pd.read_parquet(CACHE / "bars_1h_all.parquet")
    h = h[~h.symbol.isin(["OMNI/USD", "TON/USD"])]
    b = resample(h, "4h")
    full = build(b, "4h")
    names = sorted(full)
    print(f"特征数 {len(names)}; 主题分布 " +
          ", ".join(f"{t}={sum(v == t for v in THEME.values())}" for t in sorted(set(THEME.values()))))

    cut = int(b.ts.quantile(0.7))
    trunc = build(b[b.ts <= cut], "4h")
    bad_a = [k for k in names
             if not np.allclose(full[k].loc[:cut].values,
                                trunc[k].reindex(columns=full[k].columns).loc[:cut].values,
                                equal_nan=True, rtol=1e-12, atol=1e-12)]
    print(f"(a) 截断一致: {'PASS' if not bad_a else 'FAIL ' + str(bad_a)}")

    fut = b.copy()
    m = fut.ts > cut
    rng = np.random.default_rng(7)
    fut.loc[m, ["open", "high", "low", "close"]] *= rng.uniform(0.2, 5.0, (int(m.sum()), 4))
    corrupt = build(fut, "4h")
    bad_b = [k for k in names
             if not np.allclose(full[k].loc[:cut].values,
                                corrupt[k].reindex(columns=full[k].columns).loc[:cut].values,
                                equal_nan=True, rtol=1e-12, atol=1e-12)]
    print(f"(b) 篡改未来后过去不变: {'PASS' if not bad_b else 'FAIL ' + str(bad_b)}")

    # 覆盖率: 每个特征有多少非 NaN 格子。太低的特征对 ML 没贡献, 只会增加过拟合自由度。
    rows = b.ts.nunique() * b.symbol.nunique()
    cov = pd.Series({k: v.notna().sum().sum() / rows for k, v in full.items()}).sort_values()
    print(f"\n覆盖率最低的 8 个（总格子 {rows}）:\n{cov.head(8).round(3).to_string()}")
    print(f"覆盖率中位数 {cov.median():.3f}; < 0.3 的有 {(cov < 0.3).sum()} 个")
    # 覆盖率 0 = 这个特征根本没算出来（不是数据不够, 是代码有 bug）→ 硬失败。
    dead = cov[cov <= 0.001].index.tolist()
    assert not dead, f"这些特征全空, 等于没算: {dead}"
    if bad_a or bad_b:
        raise SystemExit("自检失败: 存在未来函数, 不要把这些特征交给模型")
    print("\n自检全部通过")


def main() -> None:
    ap = argparse.ArgumentParser(description="ML 特征池")
    ap.add_argument("--selftest", action="store_true", help="跑未来函数自检")
    ap.add_argument("--dump", action="store_true", help="算一遍并存 parquet")
    ap.add_argument("--min-usd", type=float, default=1_000_000)
    ap.add_argument("--no-funding", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        selftest()
        return
    if a.dump:
        h = pd.read_parquet(CACHE / "bars_1h_all.parquet")
        h = h[~h.symbol.isin(["OMNI/USD", "TON/USD"])]
        b = resample(h, "4h")
        feats = build(b, "4h", a.min_usd, with_funding=not a.no_funding)
        p, names = build_panel(feats)
        CACHE.mkdir(parents=True, exist_ok=True)
        p.astype("float32").to_parquet(OUT)
        print(f"写入 {OUT.name}: {len(p)} 行 × {len(names)} 特征")
        print("特征:", ", ".join(names))
        return
    print(__doc__)


if __name__ == "__main__":
    main()
