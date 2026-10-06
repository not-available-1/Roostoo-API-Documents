"""ML 挖因子 v1 —— 在 62 个原始特征里用机器学习找组合, 然后用同一套四道门槛审判。

和 `run_factors.py` 的分工
------------------------
`run_factors.py` 评估的是**人想出来的** 6 个因子; 这里评估的是**机器配出来的**组合。
两者共用完全相同的审判标准（`run_factors` 里的 `quarterly_ic` / `economic_report` /
`select_on` 直接 import 过来, 不另写一套, 免得两边标准悄悄跑偏）:

    1. |IC_IR_IS| >= 0.05
    2. IS 内季度 IC>0 占比 >= 2/3
    3. 按 **IS** 的 IC 方向做多空, 对市场等权回归后 alpha > 0 且 t_alpha >= 2
    4. 与已入选因子 |corr| <= 0.5

⚠️ 和 C 的边界: 这里输出的**每一个模型都是一个"因子"**, 走的是和手工因子一样的宽表接口。
   选哪几个、各占多少权重、怎么变成下单比例, 仍然是 C 的事（见 `docs/partB_to_C_integration.md`）。
   B 不越界去做组合优化。

四个模型（全部 numpy, **不装任何新依赖**）
--------------------------------------
    ridge    带 L2 的线性回归。62 个特征高度共线（r_42/r_90/rs_42 之间相关 0.8+）,
             普通最小二乘会给出巨大且乱跳的权重, L2 把它们压平。用 SVD 一次算出所有 λ。
    lasso    带 L1 的线性回归。会**把权重压成 0** → 直接告诉你"哪几个特征有用", 可解释性最好。
             用 Gram 矩阵做坐标下降, 62 维, 秒级。
    pca      对特征做主成分, 取前 k 个。这是"无监督"的: 不看收益率, 只找特征里最主要的
             几个独立方向。作用是**对照** —— 如果监督学习（ridge/lasso）打不过无监督的 PCA,
             说明收益率里根本没有可被这 62 个特征线性解释的东西。
    bag      随机子空间 + bagging 的 ridge 集成。每个模型只看随机一半的特征, 再平均。
             这是唯一带一点"非线性/交互"味道的做法, 且不需要新库。

为什么不用 LightGBM / sklearn
--------------------------
不是不想, 是**没得到装依赖的许可**（项目规矩: 每加一个库先问）。而且以现在的样本
（65 个币 × 2 年 4h bar, 有效观测约 23 万但**独立**的信息量远小于此 —— 币与币高度相关,
时间上又是重叠标签）, 树模型最容易干的事就是把噪声背下来。线性模型先跑, 打不过再说。

三重防自欺
--------
(a) **walk-forward**: 每个季度只用"该季度开始之前"的数据重新拟合, 预测该季度。
    拼起来的因子序列里, 每一个点都是模型没见过的。这比"全样本拟合再看 IC"诚实得多。
(b) **标签泄漏**: 预测期是未来 6 根 bar。训练窗口末尾的 6 根必须丢掉 ——
    它们的标签用到了窗口之外的数据。
(c) **多重检验**: 试了 m 个模型, 就算全是噪声也会有几个 t>2。所以除了普通 t 值,
    还算 Newey-West t（重叠标签会让普通 t 虚高）和 Bonferroni 校正后的临界值。
    过不了校正的, 报告里明说"可能是运气"。

用法
----
  python -m partB.ml_mine                    # 全流程, 输出报告 + ml_selected.json
  python -m partB.ml_mine --models ridge     # 只跑一个模型（调试用）
  python -m partB.ml_mine --no-walkforward   # 一次性 IS 拟合（快, 但只能看趋势不能信）
"""
from __future__ import annotations

import argparse
import json
import pathlib

import numpy as np
import pandas as pd

from partB import factors as F
from partB import ml_features as MF
from partB.data.binance_vision import resample
from partB.run_factors import (BARS_PER_DAY, SPLIT_MS, economic_report, ic_sign,
                               quarter_hit, quarterly_ic, select_on)

HERE = pathlib.Path(__file__).resolve().parent
CACHE = HERE / "cache"
REPORTS = HERE / "reports"
HORIZON = BARS_PER_DAY           # 6 根 4h = 1 天
# 一折至少要这么多训练数据才拟合。两个条件都要:
#   MIN_TRAIN_ROWS 防的是"总格子太少"（早期只有十几个币达标时, 62 个特征的回归会直接过拟合）
#   MIN_TRAIN_BARS 防的是"币很多但时间很短"（横截面多不等于信息多, 时间维度才是独立观测的来源）
MIN_TRAIN_ROWS = 8000
MIN_TRAIN_BARS = 250             # 250 根 4h ≈ 42 天


# ============================ 数据装配 ============================
def cs_standardize(feats: dict[str, pd.DataFrame], clip: float = 3.0) -> tuple[pd.DataFrame, list[str]]:
    """把每个特征做**横截面** z-score（同一个 ts 行内, 65 个币互相比）。

    为什么必须横截面而不是时间序列:
      - 不同币的量纲差几个数量级（amihud_42 对 BTC 是 0.01, 对小币是 500）, 不标准化没法进同一个回归。
      - 时间序列全样本标准化会**用到未来的均值和方差** → 直接是未来函数。
      - 横截面标准化只在同一时刻比, 天然不跨时间。
    代价: 丢掉"绝对水平"信息（一个币比自己历史高还是低）。这部分信息由特征本身承载
    （qv_z_18、rangepos_90 这些已经是"相对自己历史"的量）。
    """
    names = sorted(feats)
    out = {}
    for n in names:
        v = feats[n]
        # 退化截面（所有币在这一行取值相同, 比如 tsm 符号特征在普涨日全为 +1）没有排名信息。
        # 不挡的话 x/0 会出 inf, nanmean 等后续统计被整行污染, 而且 inf 不是 NaN,
        # 下游 nan_to_num 填不掉 → 直接炸回归。改成 NaN = "该截面无信息"。
        #
        # 判据必须是**相对**容差, 不能是 `sd == 0`: 一整列取值完全相同时, pandas 算出的
        # sd 并不是精确的 0 而是 ~1e-14 的浮点噪声（均值自身有舍入, x - mean 就不为 0）。
        # 于是 噪声/噪声 被放大成 O(1) 的数, 再被 clip 到 ±3 —— 变成"看起来很强的假信号",
        # 比 inf 更难发现。实测 age_days 在合成数据上就是这样, 一个截面把 nanmean 顶到 0.028。
        sd = v.std(axis=1)
        scale = v.abs().mean(axis=1)                 # 该截面的量级, 用来做相对化
        sd = sd.where(sd > 1e-10 * scale, np.nan)    # sd 为 0/NaN/相对可忽略 → 一律退化
        out[n] = v.sub(v.mean(axis=1), axis=0).div(sd, axis=0).clip(-clip, clip)
    return pd.concat({n: out[n].stack(future_stack=True) for n in names}, axis=1), names


def assemble(interval: str = "4h", min_usd: float = 1e6, max_missing: float = 0.3) -> dict:
    """装配训练用的一切: 特征矩阵 X、标签 y、流动性掩码、时间索引。

    标签 = 未来 6 根 bar 的对数收益, 再做**横截面去均值 + 标准化**。
    去均值的理由: 我们要的是"选哪个币", 不是"该不该持有币"。后者（择时/净敞口）是 C 的决定,
    而且比赛规则已经把多空比例定死在 70-30, 模型再去学市场方向只会浪费自由度。
    """
    h = pd.read_parquet(CACHE / "bars_1h_all.parquet")
    h = h[~h.symbol.isin(["OMNI/USD", "TON/USD"])]
    b = resample(h, interval)
    feats = MF.build(b, interval, min_usd)
    close = F.to_wide(b, "close")
    mask = F.liquidity_mask(F.to_wide(b, "quote_volume"), 7 * F.BARS_PER_DAY[interval],
                            min_usd / 24 * (24 // F.BARS_PER_DAY[interval]))

    fwd = F.forward_returns(close, HORIZON)
    lab = fwd.sub(fwd.mean(axis=1), axis=0).div(fwd.std(axis=1), axis=0).clip(-3, 3)

    Xw, names = cs_standardize(feats)
    # 长表化: (ts, symbol) 为主键
    # ⚠️ `lab.where(mask)` 不是 `lab.mask(mask)`: pandas 里 where = "条件为真则保留",
    # mask = "条件为真则替换成 NaN", 正好相反。写反的后果是整列标签变 NaN,
    # 后面所有观测被 `~isnan(y)` 过滤掉 → "观测 0 行", 但不报错。
    Xl = Xw.reset_index()
    yl = lab.where(mask).stack(future_stack=True).rename("y").reset_index()
    ml = mask.stack(future_stack=True).rename("m").reset_index()
    p = Xl.merge(yl, on=["ts", "symbol"]).merge(ml, on=["ts", "symbol"])
    p = p[p.m]                                     # 只保留流动性达标的格子
    X = p[names].to_numpy(dtype=np.float64)
    miss = np.isnan(X).mean(axis=1)
    p = p[miss <= max_missing].copy()              # 缺太多特征的观测丢掉（新上市的币头几天）
    X = p[names].to_numpy(dtype=np.float64)
    X = np.nan_to_num(X, nan=0.0)                  # 剩下的零星缺失填 0 = "横截面中位数", 中性先验
    y = p["y"].to_numpy(dtype=np.float64)
    ok = ~np.isnan(y)
    return {"X": X[ok], "y": y[ok], "ts": p.ts.to_numpy()[ok], "sym": p.symbol.to_numpy()[ok],
            "names": names, "close": close, "mask": mask, "bars": b,
            "index": pd.MultiIndex.from_arrays([p.ts.to_numpy()[ok], p.symbol.to_numpy()[ok],],
                                               names=["ts", "symbol"])}


def _to_wide(vals: np.ndarray, index: pd.MultiIndex, columns) -> pd.DataFrame:
    """把长向量摊回宽表 (index=ts, columns=symbol)。"""
    s = pd.Series(vals, index=index)
    return s.unstack("symbol").reindex(columns=columns)


# ============================ 模型（纯 numpy） ============================
def ridge_path(X: np.ndarray, y: np.ndarray, lams: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """一次 SVD 算出所有 λ 的 ridge 权重。

    ridge 的闭式解 w = (X'X + λI)^-1 X'y。逐个 λ 求逆是 O(p^3) × len(lams);
    先做 X = U S V', 则 w(λ) = V · diag(s/(s²+λ)) · U'y —— 一次分解, 所有 λ 共用。
    p=62 时差别不大, 但这样写 λ 网格可以随便加密而不担心耗时。

    返回 (权重矩阵 p×len(lams), 每个 λ 在给定验证集上的 IC) —— 这里只返回权重, IC 由调用方算。
    """
    xm, ym = X.mean(0), y.mean()
    Xc, yc = X - xm, y - ym
    U, s, Vt = np.linalg.svd(Xc, full_matrices=False)
    Uty = U.T @ yc
    s2 = s ** 2
    W = np.empty((len(s), len(lams)))
    for j, lam in enumerate(lams):
        W[:, j] = Vt.T @ (s / (s2 + lam) * Uty)
    return W, (xm, ym)


def ridge_predict(X: np.ndarray, W: np.ndarray, center: tuple, j: int) -> np.ndarray:
    xm, _ = center
    return (X - xm) @ W[:, j]


def lasso_cd(G: np.ndarray, Xy: np.ndarray, lam: float, n_iter: int = 300) -> np.ndarray:
    """L1 正则的坐标下降。

    用 Gram 矩阵 G = X'X 和 Xy 而不是原始 X: 坐标下降每步只需要 G 的一列（p 维）,
    所以复杂度从 O(n·p) 每步降到 O(p) 每步。n=23 万、p=62 时差三个数量级。

    软阈值 soft(v, t) = sign(v)·max(|v|-t, 0) 就是 L1 的全部秘密:
    贡献小于 t 的特征被**精确**压到 0（L2 只会压小不会压到 0）, 所以 lasso 天然做特征选择。
    """
    p = len(Xy)
    w = np.zeros(p)
    Gd = np.diag(G).copy()
    Gd[Gd <= 0] = 1e-12
    for _ in range(n_iter):
        delta = 0.0
        for j in range(p):
            r = Xy[j] - G[j] @ w + G[j, j] * w[j]
            new = np.sign(r) * max(abs(r) - lam, 0.0) / Gd[j]
            delta = max(delta, abs(new - w[j]))
            w[j] = new
        if delta < 1e-8:
            break
    return w


def pca_loadings(X: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """在**训练窗口**上求主成分方向, 之后每个时刻都用这套固定方向去投影。

    为什么不能每个横截面单独做 PCA: 主成分的**符号是任意的**（v 和 -v 都是解）,
    逐期做的话第 1 主成分这一期是"动量方向"、下一期翻成"反动量方向", 拼起来的时间序列
    毫无意义。固定在训练窗口上求一次, 符号就锁死了。
    """
    xm = X.mean(0)
    Xc = X - xm
    C = Xc.T @ Xc / max(len(Xc) - 1, 1)
    evals, evecs = np.linalg.eigh(C)
    order = np.argsort(evals)[::-1][:k]
    return evecs[:, order], evals[order], xm


def bagged_ridge(X: np.ndarray, y: np.ndarray, lam: float, n_bags: int = 40,
                 frac: float = 0.5, seed: int = 0) -> list[tuple[np.ndarray, np.ndarray]]:
    """随机子空间 + bagging 的 ridge 集成。

    每个 bag 随机抽 frac 的**特征**（不是样本）+ 随机抽 80% 的样本, 各拟合一个 ridge, 最后平均。
    作用是降方差: 单个 ridge 的权重对共线特征很敏感, 平均之后稳定得多。
    抽特征而不是抽样本, 是因为我们的样本本来就高度相关（同一时刻 65 个币、相邻 bar 重叠标签）,
    抽样带不来多少独立性, 而 62 个特征里确实有很多冗余可以打散。
    """
    rng = np.random.default_rng(seed)
    n, p = X.shape
    out = []
    for _ in range(n_bags):
        cols = rng.choice(p, max(int(p * frac), 2), replace=False)
        rows = rng.choice(n, int(n * 0.8), replace=False)
        w, ctr = ridge_path(X[np.ix_(rows, cols)], y[rows], np.array([lam]))
        out.append((cols, (w[:, 0], ctr)))
    return out


def bag_predict(X: np.ndarray, model: list) -> np.ndarray:
    acc = np.zeros(len(X))
    for cols, (w, ctr) in model:
        acc += (X[:, cols] - ctr[0]) @ w
    return acc / len(model)


# ============================ walk-forward ============================
def _quarters(ts: np.ndarray) -> list[int]:
    """返回每个季度的起始时间戳（只保留有足够训练数据的）。"""
    q = pd.Series(pd.to_datetime(pd.unique(ts), unit="ms", utc=True).tz_localize(None).to_period("Q"))
    starts = sorted({int(pd.Timestamp(x.start_time, tz="UTC").value // 10**6) for x in q})
    return starts


def _cs_ic(pred: np.ndarray, y: np.ndarray, ts: np.ndarray) -> float:
    """逐时间截面算 Spearman rank IC, 再对所有截面取平均。选 λ / 选模型都用它。

    为什么不用 MSE: 最终评价因子用的是 rank IC 和多空价差, 两者都只关心**排序**。
    MSE 会被少数几个极端收益的币主导（加密里单日 +80% 的币不稀奇）, 按 MSE 选出来的 λ
    未必是排序最准的那个 —— 选错标准等于朝错的方向优化。

    为什么用 numpy 而不是 `factor.rank(axis=1).corrwith(...)`: 这里是长向量（23 万行）,
    pandas 的 groupby+rank+corr 在这个规模上要几十秒, 而且每折、每个 λ 都要算一次。
    """
    o = np.argsort(ts, kind="stable")
    p, yy, t = pred[o], y[o], ts[o]
    bounds = np.flatnonzero(np.r_[True, t[1:] != t[:-1], True])
    out = []
    for i in range(len(bounds) - 1):
        a, b_ = bounds[i], bounds[i + 1]
        if b_ - a < 8:
            continue
        rp = pd.Series(p[a:b_]).rank().to_numpy()
        ry = pd.Series(yy[a:b_]).rank().to_numpy()
        if rp.std() == 0 or ry.std() == 0:
            continue
        c = np.corrcoef(rp, ry)[0, 1]
        if np.isfinite(c):
            out.append(c)
    return float(np.mean(out)) if out else -np.inf


def fit_fold(Xtr, ytr, ts_tr, cfg) -> tuple[dict, dict]:
    """在一折训练数据上拟合所有模型。

    返回 (models, attribution):
      models      {名字: (类型, payload)}
      attribution {名字: 该模型的权重/载荷, 用于事后解释}

    ⚠️ 超参（ridge 的 λ、lasso 的 λ）在**训练窗口内部再切一刀**做验证来选。
    绝不能拿待预测的那个季度去选超参 —— 那等于用答案挑模型。
    """
    models, attr = {}, {}
    n = len(Xtr)
    cut = int(n * 0.8)                              # 数据已按时间排序, 直接前 80% 训练 / 后 20% 验证
    # 验证集按 stride 抽稀: 选超参不需要全部数据, 而 _cs_ic 要逐截面算 rank, 是整条流水线最慢的一步。
    # 隔 3 个取 1 个截面, 结果几乎不变（相邻截面本来就高度重叠）, 耗时降到 1/3。
    vs = np.arange(cut, n, 3)
    Xv, yv, tsv = Xtr[vs], ytr[vs], ts_tr[vs]

    if "ridge" in cfg["models"]:
        lams = cfg["lams"]
        W, ctr = ridge_path(Xtr[:cut], ytr[:cut], lams)
        ics = [_cs_ic(ridge_predict(Xv, W, ctr, j), yv, tsv) for j in range(len(lams))]
        best = int(np.argmax(ics))
        W_full, ctr_full = ridge_path(Xtr, ytr, lams)
        models["ml_ridge"] = ("ridge", (W_full[:, best], ctr_full))
        attr["ml_ridge"] = pd.Series(W_full[:, best], index=cfg["names"]).sort_values(key=abs, ascending=False)
        attr["ml_ridge_lambda"] = float(lams[best])

    if "lasso" in cfg["models"]:
        xm, ym = Xtr.mean(0), ytr.mean()
        Xc = Xtr - xm
        G = Xc.T @ Xc / n                            # 用 Gram 矩阵: 坐标下降每步只需 p 维运算
        Xy = Xc.T @ (ytr - ym) / n
        cands = []
        for lam in cfg["lasso_lams"]:
            w = lasso_cd(G, Xy, lam)
            nz = int((w != 0).sum())
            if nz == 0:
                continue
            ics = _cs_ic((Xv - xm) @ w, yv - ym, tsv)
            cands.append((lam, w, nz, ics))
        # 在"非零个数不超过 max_nz"的约束下, 取验证 IC 最高的 λ
        elig = [c for c in cands if c[2] <= cfg["max_nz"]] or cands
        pick = max(elig, key=lambda c: c[3]) if elig else cands[0]
        models["ml_lasso"] = ("lasso", (pick[1], (xm, ym)))
        attr["ml_lasso"] = pd.Series(pick[1], index=cfg["names"])
        attr["ml_lasso"] = attr["ml_lasso"][attr["ml_lasso"] != 0].sort_values(key=abs, ascending=False)
        attr["ml_lasso_lambda"] = float(pick[0])
        attr["ml_lasso_n_nonzero"] = int(pick[2])

    if "pca" in cfg["models"]:
        L, ev, xm = pca_loadings(Xtr, cfg["n_pc"])
        tot = float(np.nansum(ev)) or 1.0
        for i in range(cfg["n_pc"]):
            models[f"ml_pc{i + 1}"] = ("pca", (L[:, i], xm))
            s = pd.Series(L[:, i], index=cfg["names"]).sort_values(key=abs, ascending=False)
            attr[f"ml_pc{i + 1}"] = s
            attr[f"ml_pc{i + 1}_var_explained"] = float(ev[i]) / tot
        # PCA 的载荷符号是任意的: 让每个 PC 的最大绝对载荷为正, 否则不同折之间会翻号,
        # 拼起来的因子序列会出现莫名其妙的跳变。
        for k, (kind, payload) in list(models.items()):
            if kind != "pca":
                continue
            v, xm = payload
            if v[int(np.argmax(np.abs(v)))] < 0:
                models[k] = ("pca", (-v, xm))
                attr[k] = -attr[k]

    if "bag" in cfg["models"]:
        mdl = bagged_ridge(Xtr, ytr, cfg["bag_lam"], cfg["n_bags"], seed=cfg.get("seed", 0))
        models["ml_bag"] = ("bag", (mdl, None))
        acc = np.zeros(Xtr.shape[1])
        for cols, (w, _) in mdl:
            acc[cols] += w / len(mdl)
        attr["ml_bag"] = pd.Series(acc, index=cfg["names"]).sort_values(key=abs, ascending=False)

    return models, attr


def apply_models(Xte: np.ndarray, models: dict) -> dict[str, np.ndarray]:
    out = {}
    for name, (kind, payload) in models.items():
        if kind in ("ridge", "lasso"):
            w, ctr = payload
            out[name] = (Xte - ctr[0]) @ w
        elif kind == "pca":
            v, xm = payload
            out[name] = (Xte - xm) @ v
        elif kind == "bag":
            out[name] = bag_predict(Xte, payload[0])
    return out


def fold_masks(ts: np.ndarray, qs: int, qe: int, gap_ms: int) -> tuple[np.ndarray, np.ndarray]:
    """切出一折的训练/测试掩码。单列成函数是为了能单独测 —— 这是整条流水线最容易出泄漏的地方。

    训练集必须在 `qs - gap_ms` 之前结束。gap = HORIZON 根 bar 的时长:
    最后一根训练 bar 的标签用的是它**之后 6 根**的价格, 如果不留出这个间隔,
    标签里就含着测试季度的信息 → 模型"提前看过答案", IC 虚高且无法察觉。
    """
    return ts < qs - gap_ms, (ts >= qs) & (ts < qe)


def walk_forward(D: dict, cfg) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, dict]:
    """逐季度重拟合 → 拼出一条"每个点都没被模型见过"的因子序列。"""
    X, y, ts, sym = D["X"], D["y"], D["ts"], D["sym"]
    cols = D["close"].columns
    order = np.argsort(ts, kind="stable")
    X, y, ts, sym = X[order], y[order], ts[order], sym[order]
    starts = _quarters(ts)
    gap = HORIZON * 14_400_000
    preds: dict[str, list] = {}
    log, attrs = [], {}
    for qi, qs in enumerate(starts):
        qe = starts[qi + 1] if qi + 1 < len(starts) else ts.max() + 1
        tr, te = fold_masks(ts, qs, qe, gap)
        if tr.sum() < MIN_TRAIN_ROWS or len(np.unique(ts[tr])) < MIN_TRAIN_BARS or te.sum() == 0:
            continue
        models, attr = fit_fold(X[tr], y[tr], ts[tr], cfg)
        qname = str(pd.Timestamp(qs, unit="ms", tz="UTC").to_period("Q"))
        for k, v in attr.items():
            attrs.setdefault(k, {})[qname] = v
        p = apply_models(X[te], models)
        for k, v in p.items():
            preds.setdefault(k, []).append(pd.Series(v, index=pd.MultiIndex.from_arrays(
                [ts[te], sym[te]], names=["ts", "symbol"])))
        nz = attr.get("ml_lasso_n_nonzero", "")
        lam = attr.get("ml_ridge_lambda", "")
        log.append({"quarter": qname, "n_train": int(tr.sum()), "n_test": int(te.sum()),
                    "ridge_lambda": lam, "lasso_nz": nz, "models": ",".join(sorted(models))})
        print(f"  {qname}  训练 {log[-1]['n_train']:6d} 行 → 预测 {log[-1]['n_test']:5d} 行"
              f"  ridge λ={lam:g}  lasso 非零={nz}")
    out = {}
    for k, parts in preds.items():
        s = pd.concat(parts).sort_index()
        w = s.unstack("symbol").reindex(columns=cols)
        # 输出前套上流动性掩码 + 转成横截面 rank, 和手工因子完全同构（C 那边一套代码就能吃）
        out[k] = F.cs_rank(w.where(D["mask"].reindex_like(w)))
    return out, pd.DataFrame(log), attrs


def fit_once_is(D: dict, cfg) -> tuple[dict[str, pd.DataFrame], dict]:
    """不 walk-forward: 只用 IS 段拟合一次, 然后对全期打分（快, 用于调试和看趋势）。

    ⚠️ 这条路产出的 IS 段因子是**样本内**的, IC 会明显虚高, 不能拿去和 walk-forward 的结果比,
    更不能当成"这个因子能用"的证据。只有 OOS 段（模型没见过）的部分有参考价值。
    """
    X, y, ts, sym = D["X"], D["y"], D["ts"], D["sym"]
    cols, mask = D["close"].columns, D["mask"]
    o = np.argsort(ts, kind="stable")
    X, y, ts, sym = X[o], y[o], ts[o], sym[o]
    tr = ts < SPLIT_MS - HORIZON * 14_400_000
    models, attr = fit_fold(X[tr], y[tr], ts[tr], cfg)
    p = apply_models(X, models)
    out = {}
    for k, v in p.items():
        idx = pd.MultiIndex.from_arrays([ts, sym], names=["ts", "symbol"])
        w = pd.Series(v, index=idx).unstack("symbol").reindex(columns=cols)
        out[k] = F.cs_rank(w.where(mask.reindex_like(w)))
    return out, attr


# ============================ 统计检验 ============================
def _norm_ppf(p: float) -> float:
    """标准正态分位数（Acklam 近似, 相对误差 < 1.15e-9）。numpy 没有这个函数,
    而 Bonferroni 校正需要它 —— 不想为了一个 ppf 去装 scipy。"""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = np.sqrt(-2 * np.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = np.sqrt(-2 * np.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def regress_nw(s: pd.Series, m: pd.Series, horizon: int = HORIZON, ppy: int = 365,
               nw_lags: int | None = None) -> dict:
    """对已算好的多空序列 s 和市场序列 m 做回归, 同时给 OLS 和 Newey-West 两套 t。

    单列出来是为了能单独测 —— NW 修正是否真的把 t 压下去, 用合成数据一验就知道;
    埋在 ls_alpha_nw 里就得先造出"残差强自相关的因子+价格", 反而测不干净。
    """
    d = pd.DataFrame({"s": s, "m": m}).dropna()
    n = len(d)
    if n < 30:
        return {"gross": np.nan, "beta": np.nan, "alpha": np.nan, "t_alpha": np.nan,
                "t_alpha_nw": np.nan, "n": n}
    Xm = np.c_[np.ones(n), d.m.values]
    b = np.linalg.lstsq(Xm, d.s.values, rcond=None)[0]
    e = d.s.values - Xm @ b
    L = nw_lags if nw_lags is not None else horizon - 1
    XtX_inv = np.linalg.inv(Xm.T @ Xm)
    sig = np.sqrt(e @ e / (n - 2))
    t_ols = b[0] / (sig * np.sqrt(XtX_inv[0, 0])) if sig > 0 else np.nan
    Xe = Xm * e[:, None]
    S = Xe.T @ Xe
    for l in range(1, L + 1):
        w = 1 - l / (L + 1)                       # Bartlett 核: 越远的滞后权重越小
        S += w * (Xe[l:].T @ Xe[:-l] + Xe[:-l].T @ Xe[l:])
    se_nw = np.sqrt(np.diag(XtX_inv @ S @ XtX_inv))
    return {"gross": d.s.mean() * ppy * 100, "beta": b[1], "alpha": b[0] * ppy * 100,
            "t_alpha": t_ols,
            "t_alpha_nw": b[0] / se_nw[0] if se_nw[0] else np.nan, "n": n}


def ls_alpha_nw(factor, close, horizon=HORIZON, ppy=365, q=0.2, nw_lags=None):
    """和 `F.ls_alpha` 同一套回归, 但 t 值用 **Newey-West** 修正。

    为什么必须修: 预测期是 6 根 bar, 而我们每根 bar 都出一个观测 → 相邻 6 个观测的标签
    **重叠**, 残差正相关。普通 OLS 的标准误假设残差独立, 于是 t 值系统性偏大
    （大约 sqrt(6) ≈ 2.4 倍的量级）。用一个明显虚高的 t 去做"t>=2 才要"的门槛, 等于没有门槛。
    nw_lags 默认取 horizon-1（重叠长度）。
    """
    fwd = F.forward_returns(close, horizon)
    idx = factor.index.intersection(fwd.index)
    s = F.ls_spread(factor.loc[idx], fwd.loc[idx], q)
    mkt = fwd.loc[idx].mean(axis=1).reindex(s.index)
    return regress_nw(s, mkt, horizon, ppy, nw_lags)


# ret 主题特征 → 单根对数增量的窗口 [(起, 止, 符号)]。
# 依据 ml_features._feat_ret 的定义: r_K = lc.diff(K) = Σ_{k=1..K} e_k,
# r_K_skip6 = lc.shift(6) - lc.shift(K) = Σ_{k=7..K} e_k, 其中 e_k 是往回数第 k 根的单根增量。
# 这是**精确恒等式**(对数收益可加), 不是近似 —— 所以权重能被无损地摊到 horizon 上。
_RET_SPANS: dict[str, list[tuple[int, int, float]]] = {}
for _k in (1, 2, 3, 6, 12, 18, 42, 90, 180):
    _RET_SPANS[f"r_{_k}"] = [(1, _k, 1.0)]
for _k in (42, 90, 180):
    _RET_SPANS[f"r_{_k}_skip6"] = [(7, _k, 1.0)]
_RET_SPANS["r_6_minus_r_42"] = [(1, 6, 1.0), (1, 42, -1.0)]
_RET_SPANS["r_18_minus_r_90"] = [(1, 18, 1.0), (1, 90, -1.0)]

# rel 主题里的 rs_K 也是收益窗口特征: rs_K = log(c).diff(K) - log(btc).diff(K)
# = Σ_{k=1..K} (e_k - eb_k)。同一个恒等式成立, 只是增量换成"相对 BTC 的超额"。
# 漏掉它就会重犯刚修的那个错: rs_42 的权重 -0.0262 是第四大, 不能不算进 horizon 归因。
_RS_SPANS: dict[str, list[tuple[int, int, float]]] = {f"rs_{_k}": [(1, _k, 1.0)] for _k in (6, 18, 42, 90)}


def increment_exposure(w: pd.Series, max_lag: int = 180,
                       spans: dict[str, list[tuple[int, int, float]]] | None = None) -> pd.Series:
    """把收益窗口类特征的权重摊成 **每个 lag 上单根增量的净系数** c_k。

    为什么必须做这一步(而不是直接读权重表): **逐特征权重不是归因的正确单位**。
    r_180 和 r_180_skip6 相关接近 1, 共线组里的单个权重可以任意平移而预测值不变。
    实测: w(r_180) = -0.0328、w(r_180_skip6) = +0.0325 —— 只读后者会以为模型在买
    30 天动量, 但按 r_180 = r_180_skip6 + r_6 展开, 净动量是 -0.0003 ≈ 0,
    真正的押注是 -0.0328 的 **1 天反转**。归因叙事曾经就此把量级排反。

    spans 默认 _RET_SPANS(本币对数收益); 传 _RS_SPANS 得到"相对 BTC 超额收益"那条通道。
    两条通道要分开读: 本币通道是总方向押注, 超额通道额外隐含一个反向的 BTC 头寸。

    返回 index = lag k(根), value = c_k。读法: 同一段 k 上 c_k 近似恒定就是一个"horizon 带",
    例如 c_1..c_6 是 1 天带、c_7..c_42 是周~月带。负 = 该 horizon 涨得多就做空它。
    """
    spans = _RET_SPANS if spans is None else spans
    c = np.zeros(max_lag + 2)
    for f, wt in w.items():
        if not wt or f not in spans:
            continue
        for lo, hi, sgn in spans[f]:
            hi2 = min(hi, max_lag)
            if lo <= hi2:
                c[lo:hi2 + 1] += sgn * wt
    return pd.Series(c[1:max_lag + 1], index=pd.RangeIndex(1, max_lag + 1, name="lag"))


def band_exposure(c: pd.Series, bands: dict[str, tuple[int, int]] | None = None) -> pd.Series:
    """把 c_k 按 horizon 带汇总成一张能读的表(带内取均值, 因为共线展开后带内本就接近恒定)。"""
    bands = bands or {"1d(1-6)": (1, 6), "2-7d(7-42)": (7, 42), "1-2w(43-90)": (43, 90),
                      "2w-30d(91-180)": (91, 180)}
    return pd.Series({name: c.loc[lo:hi].mean() for name, (lo, hi) in bands.items()})


def main():
    ap = argparse.ArgumentParser(description="ML 挖因子")
    ap.add_argument("--models", nargs="+", default=["ridge", "lasso", "pca", "bag"],
                    choices=["ridge", "lasso", "pca", "bag"])
    ap.add_argument("--no-walkforward", action="store_true", help="只用 IS 拟合一次（调试用, 结果偏乐观）")
    ap.add_argument("--n-pc", type=int, default=5)
    ap.add_argument("--n-bags", type=int, default=40)
    ap.add_argument("--max-nz", type=int, default=25, help="lasso 最多保留多少个非零特征")
    ap.add_argument("--min-usd", type=float, default=1_000_000)
    ap.add_argument("--min-ic-ir", type=float, default=0.05)
    ap.add_argument("--min-q-hit", type=float, default=2 / 3)
    ap.add_argument("--min-t-alpha", type=float, default=2.0)
    ap.add_argument("--max-corr", type=float, default=0.5)
    ap.add_argument("--k", type=int, default=5)
    a = ap.parse_args()
    REPORTS.mkdir(exist_ok=True)

    print("装配特征面板…")
    D = assemble(min_usd=a.min_usd)
    print(f"  观测 {D['X'].shape[0]:,} 行 × {D['X'].shape[1]} 特征; "
          f"{len(np.unique(D['ts']))} 个时间截面; 标签非空 {np.isfinite(D['y']).mean():.1%}")

    cfg = {"models": set(a.models), "n_pc": a.n_pc, "n_bags": a.n_bags, "max_nz": a.max_nz,
           "names": D["names"], "seed": 0,
                      # λ 上界要够大: 实测 25 个 λ 的最优解顶到了 1e5（网格上限）, 说明验证集想要更强的收缩。
           # 网格边界被顶到是"网格设错了"的信号, 不是"λ 就是这么大"的结论 —— 必须放宽重跑。
           "lams": np.logspace(1, 7, 25), "lasso_lams": np.logspace(-4, 0, 25),
           "bag_lam": 300.0}

    if a.no_walkforward:
        print("\n⚠️ 一次性 IS 拟合（样本内, IC 虚高, 仅调试）")
        facs, attrs = fit_once_is(D, cfg)
        wf_log = pd.DataFrame()
    else:
        print("\nwalk-forward 逐季度重拟合…")
        facs, wf_log, attrs = walk_forward(D, cfg)

    close = D["close"]
    names = sorted(facs)
    fs_is = {k: v[v.index < SPLIT_MS] for k, v in facs.items()}
    fs_oos = {k: v[v.index >= SPLIT_MS] for k, v in facs.items()}
    close_is, close_oos = close[close.index < SPLIT_MS], close[close.index >= SPLIT_MS]

    rep_is = F.factor_report(fs_is, close_is, HORIZON)
    rep_oos = F.factor_report(fs_oos, close_oos, HORIZON)
    qic = pd.DataFrame({k: quarterly_ic(v, F.forward_returns(close, HORIZON)) for k, v in facs.items()}).T
    qic_is = qic.loc[:, [c for c in qic.columns if c < "2026Q1"]]
    q_hit = quarter_hit(qic_is, rep_is.IC_mean)

    sign_is = ic_sign(rep_is)
    eco_is = economic_report(fs_is, close_is, HORIZON, sign_is, 0.0, a.min_t_alpha)
    eco_oos = economic_report(fs_oos, close_oos, HORIZON, sign_is, 0.0, a.min_t_alpha)

    # Newey-West 修正后的 alpha t 值（重叠标签会让 eco_* 里的 t 虚高）
    nw = {k: ls_alpha_nw(fs_is[k], close_is) for k in names}
    eco_is["t_alpha_nw"] = pd.Series({k: v["t_alpha_nw"] * float(sign_is.get(k, 1.0)) for k, v in nw.items()})
    eco_is["alpha_nw_%/y"] = pd.Series({k: v["alpha"] * float(sign_is.get(k, 1.0)) for k, v in nw.items()})

    corr_is = F.factor_corr(fs_is)
    # 和现有手工入选因子的相关: ML 因子如果只是 lowvol_14d 的马甲, 对 C 就没有增量价值。
    # 这是**最关键的一张表** —— 它决定 ML 值不值得上, 比 IC 高不高更重要。
    # 而且它不只是"看看": 下面 select_on 的 seed 参数会拿它去**挡掉**重复的马甲。
    old_is, cross = {}, pd.DataFrame()
    try:
        sel_old = json.loads((REPORTS / "selected_IS.json").read_text(encoding="utf-8"))["selected"]
        old = F.compute_all(D["bars"], "4h", a.min_usd)
        old_is = {k: v[v.index < SPLIT_MS] for k, v in old.items() if k in sel_old}
    except Exception as e:                                          # noqa: BLE001
        print(f"（跳过与手工因子的相关对比: {e}）")
    if old_is:
        # 联合相关阵: ML×ML、ML×手工、手工×手工 用同一个估计量一次算完,
        # 免得 cross 和 corr_is 两套口径对不上。
        corr_joint = F.factor_corr({**fs_is, **old_is})
        cross = corr_joint.loc[list(old_is), names]
    else:
        corr_joint = corr_is

    m_tests = max(len(names), 1)
    t_crit_bonf = _norm_ppf(1 - 0.05 / (2 * m_tests))

    # ---------- OOS 一票否决 ----------
    # 用 OOS 去**挑**因子是偷看答案; 用 OOS 去**毙**因子正是留出盲验段的全部意义。
    # 两者的区别在于: 挑是"看着 OOS 调参直到它变好看"(可以无限次, 必然过拟合);
    # 毙是"它自己崩了就承认它不是真的"(一次性, 不看别的)。
    # 实例: ml_pc3 的 IS alpha +41.7%/y、t=2.14 勉强过线, OOS 却是 **-85%/y, t=-3.88**
    # —— 方向整个反过来。这种因子交给 C, 就是在给实盘埋雷。
    # 代价要说清楚: 这一刀下去, OOS 窗口就"用掉"了。之后再挖出新因子,
    # 不能拿同一段 OOS 当证据(它已经被看过), 只能靠 IS + 经济逻辑判断。
    oos_dead = eco_oos["alpha_%/y"].reindex(names).fillna(0.0) <= 0

    ok = ((rep_is.IC_IR.abs() >= a.min_ic_ir)
          & (q_hit.reindex(rep_is.index) >= a.min_q_hit)
          & eco_is["pass"].reindex(rep_is.index).fillna(False))
    ok_live = ok & ~oos_dead.reindex(ok.index).fillna(False)
    pool_rep = rep_is[ok_live.reindex(rep_is.index).fillna(False)]
    chosen = select_on(pool_rep, corr_joint, q_hit, eco_is,
                       a.min_ic_ir, a.min_q_hit, a.max_corr, a.k, seed=list(old_is))

    # ---------- 输出 ----------
    tab = pd.DataFrame({
        "IC_IS": rep_is.IC_mean, "IC_IR_IS": rep_is.IC_IR, "IC_OOS": rep_oos.IC_mean,
        "IC_IR_OOS": rep_oos.IC_IR, "q_hit_IS": q_hit,
        "alpha_IS_%/y": eco_is["alpha_%/y"], "t_alpha_IS": eco_is["t_alpha"],
        "t_alpha_IS_nw": eco_is["t_alpha_nw"], "beta_IS": eco_is["beta"],
        "alpha_OOS_%/y": eco_oos["alpha_%/y"], "t_alpha_OOS": eco_oos["t_alpha"],
        "turnover": rep_is.turnover,
        "gate12_3_pass": ok,
        # OOS 崩掉的一票否决: IS 过了但盲验段方向反转, 不许交出去
        "oos_veto": oos_dead.reindex(rep_is.index).fillna(False),
        # 与**已交给 C 的手工因子** |相关| 的最大值; > max_corr 就是马甲, 没有增量
        "max_corr_vs_hand": (cross.abs().max(axis=0).reindex(rep_is.index)
                             if len(cross) else pd.Series(np.nan, index=rep_is.index)),
        "selected": [n in chosen for n in rep_is.index],
        # Bonferroni: 试了 m 个模型, 临界 t 要提高。过不了就是"可能只是运气"。
        "pass_bonferroni": eco_is["t_alpha_nw"].abs() >= t_crit_bonf,
    })
    tab.round(4).to_csv(REPORTS / "ml_factor_report.csv", encoding="utf-8-sig")
    corr_is.round(3).to_csv(REPORTS / "ml_factor_corr_IS.csv", encoding="utf-8-sig")
    qic.round(4).to_csv(REPORTS / "ml_quarterly_IC.csv", encoding="utf-8-sig")
    if len(wf_log):
        wf_log.to_csv(REPORTS / "ml_walkforward_log.csv", index=False, encoding="utf-8-sig")
    if len(cross):
        cross.round(3).to_csv(REPORTS / "ml_vs_handcrafted_corr.csv", encoding="utf-8-sig")

    pd.set_option("display.width", 260)
    print(f"\n=== ML 因子报告（IS 选择 / OOS 盲验, t_alpha_IS_nw 已做 Newey-West 重叠修正）==="
          f"\n{tab.round(3).to_string()}")
    print(f"\nIS 三门槛全过: {list(tab.index[tab.gate12_3_pass])}")
    if tab.oos_veto.any():
        print(f"OOS 一票否决(盲验段 alpha<=0, 不许交给 C): "
              f"{list(tab.index[tab.oos_veto & tab.gate12_3_pass])}")
    if len(cross):
        dup = tab.index[(tab.max_corr_vs_hand > a.max_corr)]
        print(f"与手工入选因子重复(|相关|>{a.max_corr}, 被 seed 挡掉): {list(dup)}")
    print(f"去相关后入选(交给 C 的候选): {chosen}")
    print(f"Bonferroni 临界 t（{m_tests} 个模型, 5% 双尾）= {t_crit_bonf:.2f}; "
          f"过线: {list(tab.index[tab.pass_bonferroni])}")
    if len(cross):
        print(f"\n=== ML 因子 vs 现有手工入选因子（IS 平均横截面相关, 行=手工 列=ML）===\n"
              f"{cross.round(3).to_string()}")
        print(f"\n每个 ML 因子与手工因子的最大 |相关|（>{a.max_corr} 即视为马甲）:\n"
              f"{cross.abs().max(axis=0).round(3).to_string()}")

    # ---------- 归因: 模型到底在用什么特征 ----------
    # 这一步是"先挖后解释"里的**解释**。没有它, ML 因子就是个黑箱, 出了问题没人知道为什么,
    # 也没法判断它是真的经济机制还是过拟合。看的时候按 MF.THEME 分组读, 别逐个特征编故事。
    print("\n=== 模型归因（最后一折的权重, |w| 前 10）===")
    attr_rows = {}
    for k, per_q in attrs.items():
        last = list(per_q.values())[-1] if isinstance(per_q, dict) else per_q
        if isinstance(last, pd.Series):
            attr_rows[k] = last
            top = last.reindex(last.abs().sort_values(ascending=False).index).head(10)
            top = top[top != 0]
            if not len(top):
                continue
            body = "\n".join(f"    {f:20s} {w:+.4f}   [{MF.THEME.get(f, '?')}]" for f, w in top.items())
            print(f"\n  {k}\n{body}")
        else:
            print(f"  {k} = {last}")
    if attr_rows:
        pd.DataFrame(attr_rows).round(5).to_csv(REPORTS / "ml_attribution_last_fold.csv",
                                                encoding="utf-8-sig")

    (REPORTS / "ml_selected.json").write_text(json.dumps({
        "note": ("B 侧 ML 挖因子结果。**每个条目都是一个因子**, 走和手工因子一样的宽表接口; "
                 "选哪些、怎么加权、怎么变成下单比例仍是 C 的决定。"),
        "universe_symbols": int(D["close"].shape[1]),
        "interval": "4h", "horizon_bars": HORIZON, "split_iso": "2026-01-01",
        "min_usd": a.min_usd,
        "n_features": int(D["X"].shape[1]),
        "models_tried": sorted(a.models),
        "walk_forward": bool(not a.no_walkforward),
        "label": "未来 6 根 4h bar 的对数收益, 横截面去均值+标准化（只学选币, 不学择时）",
        "gate": (f"|IC_IR_IS|>={a.min_ic_ir}, IS 季度命中>={a.min_q_hit:.2f}, "
                 f"LS alpha>0 且 t>={a.min_t_alpha}(按 IS 方向), |corr|<={a.max_corr}"),
        "veto": {
            "oos_alpha_le_0": sorted(tab.index[tab.oos_veto & tab.gate12_3_pass].tolist()),
            "dup_of_handcrafted": sorted(tab.index[tab.max_corr_vs_hand > a.max_corr].tolist()),
        },
        "dedup_seed": list(old_is),
        "t_crit_bonferroni": round(float(t_crit_bonf), 3),
        "selected": chosen,
        "ic_ir_weights": (lambda w: (w / w.sum()).round(4).to_dict() if len(w) else {})(
            rep_is.loc[chosen, "IC_IR"].abs()) if chosen else {},
        "ic_sign_is": {f: int(sign_is[f]) for f in chosen},
        "stats": {f: {"IC_IR_IS": round(float(rep_is.loc[f, "IC_IR"]), 4),
                      "IC_IR_OOS": round(float(rep_oos.loc[f, "IC_IR"]), 4),
                      "alpha_IS_%/y": round(float(eco_is.loc[f, "alpha_%/y"]), 1),
                      "t_alpha_IS": round(float(eco_is.loc[f, "t_alpha"]), 2),
                      "t_alpha_IS_nw": round(float(eco_is.loc[f, "t_alpha_nw"]), 2),
                      "alpha_OOS_%/y": round(float(eco_oos.loc[f, "alpha_%/y"]), 1),
                      "t_alpha_OOS": round(float(eco_oos.loc[f, "t_alpha"]), 2),
                      "turnover": round(float(rep_is.loc[f, "turnover"]), 4),
                      "max_corr_vs_hand": (round(float(tab.loc[f, "max_corr_vs_hand"]), 3)
                                           if pd.notna(tab.loc[f, "max_corr_vs_hand"]) else None),
                      "oos_veto": bool(tab.loc[f, "oos_veto"]),
                      "pass_bonferroni": bool(tab.loc[f, "pass_bonferroni"])}
                 for f in names},
        "oos_blind_note": ("OOS 只用来**毙**, 不用来**挑**: 拿它挑因子等于偷看答案, "
                           "可以看无限次直到好看, 必然过拟合; 拿它毙因子是一次性的, "
                           "崩了就承认它不是真的。代价是这一段 OOS 已经用掉了 —— "
                           "此后再挖的新因子不能拿同一段 OOS 当证据, 只能靠 IS + 经济逻辑。"),
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n写入 reports/ml_selected.json + ml_factor_report.csv")


if __name__ == "__main__":
    main()
