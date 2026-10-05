"""ML 侧单元测试。跑法: `python -m partB.run_tests partB.test_ml`（不需要 pytest）。

这里测的不是"模型准不准"（那是评估报告的事）, 而是**四类会静默毁掉结论的错**:
  1. 未来函数 —— 特征或标签用到了未来数据, IC 会好看但实盘归零
  2. 形状/对齐错 —— 典型是 `DataFrame <op> Series` 忘了 axis=0, 结果是列数暴涨 + 整表 NaN,
     **不报异常**, 一路传到报告里表现为"这个特征没用"
  3. 模型实现错 —— ridge/lasso/PCA 手写实现, 没有库帮忙兜底, 必须用已知答案的合成数据验
  4. 门槛判据错 —— 比如季度一致性忘了按 IC 方向摆正, 会把稳定为负的好因子判死（真踩过）
"""
import numpy as np
import pandas as pd

import partB.ml_features as MF
import partB.ml_mine as M
from partB.data.binance_vision import resample
from partB.run_factors import quarter_hit, select_on
from partB.test_partB import synth


# ---------- 1. 特征层 ----------
def synth_ml(n_sym=10, n=24 * 120):
    """synth() 的 n_trades 是常数 100 → 滚动 std 恒为 0 → nt_z_18 整表 NaN。

    这不是特征的 bug, 是测试数据太假: 真实成交笔数一直在抖。这里补上抖动,
    否则"整表 NaN"这条断言会被一个数据缺陷触发, 而真正的形状/对齐 bug 反而被淹没。
    """
    b = synth(n_sym, n).copy()
    rng = np.random.default_rng(7)
    for s in b.symbol.unique():
        m = (b.symbol == s).to_numpy()
        b.loc[m, "n_trades"] = rng.integers(40, 400, int(m.sum()))
    return b


def test_feature_shapes_and_themes():
    """每个特征都必须是 (n_bars, n_symbols), 且都在 THEME 里登记过。"""
    b = resample(synth_ml(), "4h")
    feats = MF.build(b, "4h", min_usd=0, with_funding=False)
    n_t, n_s = b.ts.nunique(), b.symbol.nunique()
    assert len(feats) >= 50, f"特征太少({len(feats)}), 是不是哪一组没算出来"
    for k, v in feats.items():
        assert v.shape == (n_t, n_s), f"{k} 形状 {v.shape} != {(n_t, n_s)}"
        assert k in MF.THEME, f"{k} 没在 THEME 里登记"
        assert not v.isna().all().all(), f"{k} 整表 NaN（算出来了但全是空的）"


def test_features_no_lookahead():
    """截断重算 + 篡改未来, 两条都要过（单独一条抓不全, 见 ml_features.selftest 的注释）。"""
    b = resample(synth_ml(), "4h")
    cut = b.ts.unique()[400]
    f1 = MF.build(b, "4h", min_usd=0, with_funding=False)
    f2 = MF.build(b[b.ts <= cut], "4h", min_usd=0, with_funding=False)
    for k in f1:
        a = f1[k].loc[:cut]
        c = f2[k].reindex(index=a.index, columns=a.columns)
        assert np.allclose(a.values, c.values, equal_nan=True, rtol=1e-12, atol=1e-12), f"{k} 截断不一致"

    fut = b.copy()
    m = fut.ts > cut
    fut.loc[m, ["open", "high", "low", "close"]] *= 3.0
    fut.loc[m, ["volume", "quote_volume", "n_trades"]] *= 7.0
    f3 = MF.build(fut, "4h", min_usd=0, with_funding=False)
    for k in f1:
        a = f1[k].loc[:cut]
        c = f3[k].reindex(index=a.index, columns=a.columns).loc[:cut]
        assert np.allclose(a.values, c.values, equal_nan=True, rtol=1e-12, atol=1e-12), f"{k} 未来泄漏"


def test_cs_standardize_no_time_leak():
    """横截面标准化只许在同一 ts 行内做: 改未来的行不能动过去的行。"""
    b = resample(synth_ml(), "4h")
    feats = MF.build(b, "4h", min_usd=0, with_funding=False)
    X1, names = M.cs_standardize(feats)
    cut = b.ts.unique()[400]
    dirty = {k: v.copy() for k, v in feats.items()}
    for k in dirty:                                   # 把 cut 之后的值全部乘 5
        d = dirty[k]
        d[d.index > cut] = d[d.index > cut] * 5.0
    X2, _ = M.cs_standardize(dirty)
    a = X1[X1.index.get_level_values("ts") <= cut]
    c = X2[X2.index.get_level_values("ts") <= cut].reindex(index=a.index, columns=a.columns)
    assert np.allclose(a.to_numpy(), c.to_numpy(), equal_nan=True, rtol=1e-12), "横截面标准化跨时间泄漏了"
    # 标准化后**每个**截面的均值都应≈0。别只挑一行: 挑哪一行是按位置取的,
    # 特征数量一变就落到别的 bar 上, 测试会时过时不过（真踩过）。
    g = X1.groupby(level="ts", sort=False)
    mu = g.mean().abs()                              # (n_ts, n_feat) 每个截面的均值
    assert np.nanmax(mu.to_numpy()) < 1e-9, \
        f"{int((mu.to_numpy() >= 1e-9).sum())} 个(截面,特征)均值不为 0, 最大 {np.nanmax(mu.to_numpy()):.3g}"
    sd = g.std()
    sdv = sd.to_numpy()[np.isfinite(sd.to_numpy())]
    assert np.abs(sdv - 1.0).max() < 1e-6, f"截面标准差不是 1, 最大偏离 {np.abs(sdv - 1.0).max():.3g}"
    # 注意不能用 isfinite: NaN 也算"非有限", 而退化截面本来就该是 NaN。要挡的只是 inf。
    assert not np.isinf(X1.to_numpy()).any(), "标准化后出现 inf（退化截面没被挡掉）"


def test_cs_standardize_kills_degenerate_cross_section():
    """整列取值相同的特征必须变成 NaN, 不能被浮点噪声放大成 ±clip 的假信号。

    为什么单独立一个测试: `sd == 0` 这个判据看着够用了, 实际不够。
    10 个完全相同的浮点数, pandas 算出的 std 是 ~1e-14 而不是 0（均值自身有舍入）,
    于是 噪声/噪声 = O(1), 再被 clip 到 ±3 —— 一个"没有任何信息"的特征
    变成了满格强信号, 而且是**静默**的。age_days 在合成数据上正好触发。
    """
    idx = pd.RangeIndex(5)
    cols = ["A", "B", "C"]
    flat = pd.DataFrame(66.66666666666667, index=idx, columns=cols)   # 横截面上完全相同
    good = pd.DataFrame(np.arange(15, dtype=float).reshape(5, 3), index=idx, columns=cols)
    X, names = M.cs_standardize({"flat": flat, "good": good})
    f = X["flat"].unstack()
    assert f.isna().all().all(), f"退化截面没被挡掉, 得到了 {f.iloc[0].tolist()}"
    assert np.isfinite(X["good"].to_numpy()).all(), "正常特征被误杀了"
    # 全零列同样是退化, 不能因为 scale=0 就漏掉
    X0, _ = M.cs_standardize({"z": pd.DataFrame(0.0, index=idx, columns=cols)})
    assert X0["z"].isna().all().all()


# ---------- 2. 折切分 ----------
def test_fold_masks_leaves_horizon_gap():
    """训练集最后一根 bar 的标签会用到之后 HORIZON 根 → 必须留出间隔, 否则标签泄漏。"""
    step = 14_400_000
    ts = np.arange(1000) * step
    qs, qe = 700 * step, 900 * step
    tr, te = M.fold_masks(ts, qs, qe, M.HORIZON * step)
    assert ts[tr].max() + M.HORIZON * step < qs, "训练集吃进了测试季度的价格"
    assert ts[te].min() == qs and ts[te].max() < qe
    assert not (tr & te).any(), "训练/测试重叠"
    assert tr.sum() + te.sum() < len(ts), "gap 那段必须被丢掉, 不能算进任何一边"


# ---------- 3. 模型实现 ----------
def test_ridge_recovers_known_signal():
    """合成 y = Xw + 噪声, ridge 应该把方向找回来（不要求系数相等, 只要求同向）。"""
    rng = np.random.default_rng(3)
    n, p = 4000, 12
    X = rng.normal(size=(n, p))
    w = np.zeros(p)
    w[[0, 3, 7]] = [2.0, -1.5, 1.0]
    y = X @ w + rng.normal(scale=1.0, size=n)
    W, _ = M.ridge_path(X, y, np.array([1.0]))
    est = W[:, 0]
    assert np.corrcoef(est, w)[0, 1] > 0.99, f"ridge 方向不对: {est.round(3)}"
    assert np.argmax(np.abs(est)) in (0, 3, 7)
    # λ 越大收缩越强: 权重范数必须单调下降
    norms = [np.linalg.norm(M.ridge_path(X, y, np.array([l]))[0][:, 0]) for l in (1.0, 100.0, 1e4)]
    assert norms[0] > norms[1] > norms[2], f"λ 没有起到收缩作用: {norms}"


def test_lasso_zeros_out_noise_features():
    """62 个特征里只有 3 个真的有用, lasso 应该把其余的压成**精确的 0**。"""
    rng = np.random.default_rng(11)
    n, p = 4000, 30
    X = rng.normal(size=(n, p))
    w = np.zeros(p)
    w[[2, 5, 9]] = [1.5, -1.2, 0.8]
    y = X @ w + rng.normal(scale=0.7, size=n)
    xm, ym = X.mean(0), y.mean()
    Xc = X - xm
    G = Xc.T @ Xc / n
    Xy = Xc.T @ (y - ym) / n
    est = M.lasso_cd(G, Xy, lam=0.05)
    nz = set(np.flatnonzero(est))
    assert nz <= {2, 5, 9}, f"lasso 留下了噪声特征: {sorted(nz)}"
    assert nz, "lasso 把所有特征都压成 0 了（λ 太大）"


def test_pca_sign_is_stable():
    """PCA 载荷符号必须锁定: 同一份数据反复算, 符号不能翻。

    不锁符号的话, walk-forward 每折的 PC1 可能反向, 拼起来的因子序列会出现无意义的大跳变。
    """
    rng = np.random.default_rng(5)
    X = rng.normal(size=(3000, 10))
    X[:, 0] += X[:, 1] * 2                      # 造一个明显的主方向
    L1, ev1, _ = M.pca_loadings(X, 3)
    L2, ev2, _ = M.pca_loadings(X, 3)
    assert np.allclose(L1, L2), "同数据两次 PCA 结果不同"
    assert ev1[0] > ev1[1] > ev1[2], "特征值没有降序"
    proj = (X - X.mean(0)) @ L1[:, 0]
    assert abs(np.corrcoef(proj, X[:, 0] + 2 * X[:, 1])[0, 1]) > 0.9


def test_bagged_ridge_reproducible():
    """集成模型必须可复现: 同 seed → 同结果。评委重跑要能对得上数。"""
    rng = np.random.default_rng(1)
    X, y = rng.normal(size=(2000, 10)), rng.normal(size=2000)
    a = M.bagged_ridge(X, y, lam=10.0, n_bags=8, seed=42)
    b = M.bagged_ridge(X, y, lam=10.0, n_bags=8, seed=42)
    assert np.allclose(M.bag_predict(X, a), M.bag_predict(X, b))
    c = M.bagged_ridge(X, y, lam=10.0, n_bags=8, seed=7)
    assert not np.allclose(M.bag_predict(X, a), M.bag_predict(X, c)), "seed 没有起作用"


# ---------- 4. 门槛判据 ----------
def test_quarter_hit_orients_by_ic_sign():
    """季度一致性必须按 IC 方向摆正 —— 这是修掉的一个真 bug。

    原来写的是 `(qic > 0).mean()`: 一个**稳定为负**的因子（IC_IR = -0.24, 每个季度都是负的）
    会算出 q_hit = 0.00 而被第 2 道门槛判死, 但它其实是最一致的那一个。
    ML 挖出的 ml_pc2 就是这么被误杀的, 所以这里钉一个回归测试。
    """
    idx = ["f_pos", "f_neg", "f_flip"]
    qic = pd.DataFrame({"2025Q1": [0.05, -0.05, 0.05],
                        "2025Q2": [0.04, -0.06, -0.05],
                        "2025Q3": [0.06, -0.04, 0.04]}, index=idx)
    ic = pd.Series({"f_pos": 0.05, "f_neg": -0.05, "f_flip": 0.013}, index=idx)
    got = quarter_hit(qic, ic)
    assert got["f_pos"] == 1.0, "正 IC 因子应全部命中"
    assert got["f_neg"] == 1.0, f"稳定负 IC 因子必须也算 1.0, 实际 {got['f_neg']}"
    assert abs(got["f_flip"] - 2 / 3) < 1e-9, "方向不稳的因子应该只有 2/3"


def test_select_on_seed_blocks_duplicates():
    """去相关必须把**已经交给 C 的手工因子**算进去, 不能只在 ML 内部去重。

    真实事故: ml_pc2 和 lowvol_14d 的相关是 -0.899（几乎同一个信号）, 但原来的
    select_on 只看 ML×ML 的相关阵, 于是 ml_pc2 照样入选 —— C 收到的是同一个因子两遍,
    却以为是两个独立来源, 权重会给错。seed 参数就是为了挡这个。
    """
    names = ["ml_a", "ml_b", "hand"]
    rep = pd.DataFrame({"IC_IR": [0.30, 0.25, 0.20]}, index=names)
    q_hit = pd.Series(1.0, index=names)
    eco = pd.DataFrame({"pass": [True, True, True]}, index=names)
    corr = pd.DataFrame([[1.0, 0.10, 0.20],
                         [0.10, 1.0, -0.90],        # ml_b 是 hand 的马甲
                         [0.20, -0.90, 1.0]], index=names, columns=names)
    got = select_on(rep, corr, q_hit, eco, 0.05, 0.67, 0.5, 5, seed=["hand"])
    assert got == ["ml_a"], f"ml_b 与 seed 里的 hand 相关 -0.90, 必须被挡掉; 实际 {got}"
    # 不传 seed 就会放行 —— 钉住这个对比, 免得以后有人把 seed 默认值改没了
    assert select_on(rep, corr, q_hit, eco, 0.05, 0.67, 0.5, 5) == ["ml_a", "ml_b"]


# ---------- 5. 统计检验 ----------
def test_newey_west_shrinks_tstat():
    """重叠标签会让普通 OLS 的 t 值虚高, Newey-West 修正后必须**变小**。

    造一个残差强正相关（AR(1), ρ=0.8）的多空序列: 这正是 horizon=6 时相邻观测重叠的后果。
    如果 NW 的 t 没有明显小于 OLS 的 t, 说明修正没生效 —— 那"t>=2 才要"这道门槛就是假的。
    测的是 regress_nw（吃现成的 s/m 序列）而不是 ls_alpha_nw: 后者要先从因子和价格里
    算出多空价差, 想造出"残差恰好 AR(1)"的输入几乎不可能, 测出来也说不清是谁的错。
    """
    rng = np.random.default_rng(2)
    n = 2000
    e = np.zeros(n)
    for i in range(1, n):
        e[i] = 0.8 * e[i - 1] + rng.normal(scale=0.3)
    mkt = pd.Series(rng.normal(scale=0.01, size=n))
    s = pd.Series(0.0002 + e)                      # 真 alpha 很小, 但残差高度自相关
    out = M.regress_nw(s, mkt, horizon=6)
    assert out["n"] == n and np.isfinite(out["t_alpha_nw"])
    assert abs(out["t_alpha_nw"]) < abs(out["t_alpha"]), \
        f"NW({out['t_alpha_nw']:.2f}) 没有比 OLS({out['t_alpha']:.2f}) 更保守"
    # 无自相关的残差上, 两套 t 应该差不多 —— 否则说明 NW 无脑压 t, 那也不能用
    w = pd.Series(rng.normal(scale=0.3, size=n))
    o2 = M.regress_nw(pd.Series(0.0002 + w.values), mkt, horizon=6)
    assert abs(o2["t_alpha_nw"] - o2["t_alpha"]) < 0.5 * abs(o2["t_alpha"]) + 0.3, \
        f"独立残差下 NW({o2['t_alpha_nw']:.2f}) 和 OLS({o2['t_alpha']:.2f}) 差太远"


def test_norm_ppf_matches_known_values():
    """自己写的正态分位数必须对得上教科书数值（Bonferroni 校正全靠它）。"""
    for p, expect in [(0.975, 1.959964), (0.995, 2.575829), (0.95, 1.644854),
                      (0.9995, 3.290527), (0.90, 1.281552)]:
        got = M._norm_ppf(p)
        assert abs(got - expect) < 1e-4, f"ppf({p}) = {got}, 应为 {expect}"
    assert abs(M._norm_ppf(0.025) + 1.959964) < 1e-4, "左尾不对称"


# ---------- 5. 归因层：把逐特征权重摊成逐 horizon 净暴露 ----------
def test_ret_spans_covers_every_ret_feature():
    """`_RET_SPANS` 必须和 `ml_features` 的 ret 组一一对应。

    这张表是**手写的**，特征定义是**代码算的**，两边会各自漂移：
    加一个新的 ret 特征而忘了登记，`increment_exposure` 会静默漏掉它的权重，
    净暴露表看起来照样合理，归因结论就错了 —— 而且没有任何报错。
    另外 skip 的档距 d 来自 `BARS_PER_DAY[interval]`（4h → 6），
    `_RET_SPANS` 里的 "skip6" 只对 4h 成立，改 interval 必须同步改这里。
    """
    b = resample(synth_ml(), "4h")
    feats = MF.build(b, "4h", min_usd=0, with_funding=False)
    ret_names = {k for k, v in MF.THEME.items() if v == "ret"}
    assert ret_names == set(M._RET_SPANS), (
        f"两边对不上。特征里有但表里没有: {sorted(ret_names - set(M._RET_SPANS))}; "
        f"表里有但特征里没有: {sorted(set(M._RET_SPANS) - ret_names)}")
    # 恒等式 r_k - r_k_skip6 == r_6（对数收益可加，精确成立，不是近似）
    for k in (42, 90, 180):
        lhs = (feats[f"r_{k}"] - feats[f"r_{k}_skip6"]).stack()
        rhs = feats["r_6"].stack()
        both = lhs.index.intersection(rhs.index)
        assert np.allclose(lhs.loc[both], rhs.loc[both], atol=1e-12), \
            f"r_{k} - r_{k}_skip6 != r_6，_RET_SPANS 的展开前提是错的"
    # rs_K 走的是同一条恒等式，只是增量换成"相对 BTC 的超额"，必须也登记全
    rs_names = {k for k in MF.THEME if k.startswith("rs_")}
    assert rs_names == set(M._RS_SPANS), f"rs 特征对不上: {rs_names ^ set(M._RS_SPANS)}"


def test_increment_exposure_known_answers():
    """用能手算的权重验展开逻辑，特别是"共线对相互抵消"这个关键行为。"""
    # ① r_180 与 r_180_skip6 等权反号 → 7~180 根上净暴露为 0，只剩最近 6 根的反转。
    #    这正是 ml_bag 实测的情形，也是"逐特征权重不是归因单位"的实例。
    c = M.increment_exposure(pd.Series({"r_180": -1.0, "r_180_skip6": 1.0}))
    assert np.allclose(c.loc[1:6], -1.0), f"近 6 根应为 -1: {c.loc[1:6].tolist()}"
    assert np.allclose(c.loc[7:180], 0.0), "7 根以外必须完全抵消"

    # ② 短长差特征：r_6 - r_42 = -Σ_{k=7..42} e_k，近 6 根**互相抵消**，只在 7~42 上留 -1。
    #    这条容易被手算错（直觉以为是 "+1 在 1~6"），所以必须钉住。
    c = M.increment_exposure(pd.Series({"r_6_minus_r_42": 1.0}))
    assert np.allclose(c.loc[1:6], 0.0), f"近 6 根应抵消为 0: {c.loc[1:6].tolist()}"
    assert np.allclose(c.loc[7:42], -1.0) and np.allclose(c.loc[43:180], 0.0)

    # ③ 叠加：两个特征各自贡献相加
    c = M.increment_exposure(pd.Series({"r_6": 1.0, "r_6_minus_r_42": -1.0}))
    assert np.allclose(c.loc[1:6], 1.0) and np.allclose(c.loc[7:42], 1.0), \
        f"1~42 都应是 +1: {c.loc[1:42].unique()}"

    # ④ 非 ret 主题的权重不该被算进来（它们不是对数收益的线性组合，摊不到 horizon 上）
    c = M.increment_exposure(pd.Series({"rv_90": 5.0, "amihud_42": -3.0}))
    assert np.allclose(c, 0.0), "vol/liq 特征不属于 _RET_SPANS，必须整列为 0"

    # ⑤ rs 通道要显式传 spans 才生效——默认表里没有 rs，混用会静默算成 0
    w = pd.Series({"rs_42": -1.0})
    assert np.allclose(M.increment_exposure(w), 0.0), "默认通道不该吃 rs"
    c = M.increment_exposure(w, spans=M._RS_SPANS)
    assert np.allclose(c.loc[1:42], -1.0) and np.allclose(c.loc[43:180], 0.0)


def test_band_exposure_aggregates():
    """带内取均值。共线展开之后带内本就接近恒定，均值不会掩盖结构。"""
    c = M.increment_exposure(pd.Series({"r_180": -1.0, "r_180_skip6": 1.0}))
    bands = M.band_exposure(c)
    assert abs(bands["1d(1-6)"] + 1.0) < 1e-12
    assert abs(bands["2w-30d(91-180)"]) < 1e-12, "30 天动量应被抵消成 0"
