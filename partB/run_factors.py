"""B 侧因子评估流水线（P0 口径修正版）。只评估与选因子，不做任何回测（回测是 partC 的范围）。

口径规则:
1. 宇宙 = 65 可交易币（67 去掉 ticker 不返回的 OMNI/TON），4h bar，预测期 = 1 天（horizon=6 根）。
2. 切分固定 2026-01-01：IS=2024-10~2025-12，OOS=2026-01~。OOS 只出盲验报告，**不参与选因子**。
3. 选因子只在 IS 内做，四道门槛全过才进池：
   统计门槛  |IC_IR_IS| >= min_ic_ir 且 IS 内季度 IC>0 占比 >= min_q_hit；
   经济门槛  按 IC 方向做多空、对市场等权回归后 alpha > min_alpha 且 t_alpha >= min_t_alpha。
   进池后再按 |IC_IR_IS| 贪心去相关（IS 相关 <= max_corr）。
   为什么要有经济门槛: IC 只回答"信号有没有方向", 不回答"照这个方向下注赚不赚钱"。
   rev_1d 的 IC_IR_IS=0.133 全场第二, 多空组合却亏 37%/y —— 它的 IC 来自中间分位,
   头部/尾部根本不单调, 而实盘只交易头尾。详见 reports/rev_smooth_and_economic_gate.md。
4. walk-forward：每个季度 q 只用 q 之前的数据重跑同一套门槛，输出"当季实盘会选哪些因子"，
   用于看选择稳定性（而不是全样本选完再切 IS/OOS 自欺）。
5. 流动性: 过去 7 天平均日成交额 >= min_usd 才纳入（v0 口径 1e6）。

输出（partB/reports/）:
  factor_report_4h_full.csv   全样本 IC + IC_IS + IC_OOS + 换手 + 费用拖累（只读参考）
  factor_report_4h_IS.csv     IS 内报告（选择依据）
  factor_report_4h_OOS.csv    OOS 盲验报告
  factor_economic_IS.csv      IS 内多空 alpha 拆解（选择依据）
  factor_economic_OOS.csv     OOS 盲验的同一张表
  factor_corr_4h_IS.csv       IS 内因子相关阵
  walkforward_quarterly_IC.csv  每因子逐季度 IC
  walkforward_selection.csv     逐季度 walk-forward 选择结果
  selected_IS.json              交给 partC 的最终选择 + IC_IR 权重

用法: python -m partB.run_factors [--min-usd 1000000] [--min-ic-ir 0.05]
                                  [--min-alpha 0] [--min-t-alpha 2] [--max-corr 0.5] [--k 5]
"""
import argparse
import json
import pathlib

import numpy as np
import pandas as pd

from partB import factors as F
from partB.data.binance_vision import resample

HERE = pathlib.Path(__file__).resolve().parent
REPORTS = HERE / "reports"
SPLIT_MS = pd.Timestamp("2026-01-01", tz="UTC").value // 10**6
BARS_PER_DAY = 6  # 4h
TAKER = 0.001


def quarter_of(idx: pd.Index) -> pd.Series:
    return pd.Series(pd.to_datetime(idx, unit="ms", utc=True).tz_localize(None).to_period("Q"), index=idx)


def quarterly_ic(factor: pd.DataFrame, fwd: pd.DataFrame) -> pd.Series:
    q = quarter_of(factor.index)
    out = {}
    for name, grp in q.groupby(q):
        ic = F.ic_series(factor.loc[grp.index], fwd.loc[grp.index]).dropna()
        if len(ic) >= 20:
            out[str(name)] = ic.mean()
    return pd.Series(out)


def fee_drag_pct_per_day(rep: pd.DataFrame) -> pd.Series:
    return rep.turnover * TAKER * BARS_PER_DAY * 100


def ic_sign(rep: pd.DataFrame) -> pd.Series:
    """IC_IR 的符号 = 这个因子该怎么下注(正 IC 做多高分, 负 IC 反过来)。IC_IR 恰好为 0 时取 +1。"""
    return np.sign(rep.IC_IR).where(rep.IC_IR != 0, 1.0)


def quarter_hit(qic: pd.DataFrame, ic_mean: pd.Series) -> pd.Series:
    """季度一致性: 有多少比例的季度, IC 和**全期 IC 的方向**一致。

    ⚠️ 修掉的一个真 bug: 原来写的是 `(qic > 0).mean(axis=1)`, 即"IC 为正的季度占比"。
    这对正 IC 因子没问题, 但对**负 IC 因子完全反了** —— 一个稳定为负的因子（比如
    ML 挖出的 ml_pc2, IS 的 IC 全是负的、IC_IR = -0.239）会算出 q_hit = 0.00,
    被第 2 道门槛判死, 而它其实是**最一致**的那一个。
    门槛的本意是"信号方向稳不稳", 不是"信号是不是正的"。方向由 `ic_sign` 单独负责,
    一致性这一关必须先按方向摆正了再数。

    方向用**同一段数据**的全期 IC 均值定（在 walk-forward 里就是当折训练段的 IC）,
    不引入任何未来信息。
    """
    sgn = np.sign(ic_mean).where(ic_mean != 0, 1.0)
    return qic.mul(sgn.reindex(qic.index), axis=0).gt(0).mean(axis=1)


def economic_report(factors: dict, close: pd.DataFrame, horizon: int, sign: pd.Series,
                    min_alpha: float, min_t_alpha: float) -> pd.DataFrame:
    """经济门槛表: 每个因子按 sign 指定的方向做多空, 对市场等权收益回归, 拆出 暴露 + 选股 alpha。

    sign 必须由**调用方**给, 而不是从本段数据的 IC 现算 —— 盲验段(OOS)如果用自己的 IC 翻号,
    等于先偷看答案再决定方向: mom_30d_skip1d 在 OOS 的 IC 转负, 按 OOS 号定向会显示
    alpha +68.7%/y "通过", 而按 IS 当初给的方向真实交易是 -68.7%/y。所以 OOS 一律用 IS 的 sign。

    所有列都已乘 sign → 判据可以统一写成 "alpha > 0 且 t_alpha >= 2"。
    beta 也一起翻号: 它表示"照这个方向下注时, 你顺带做多了多少市场"。
    beta 显著为负(如 lowvol_14d 的 -0.56)意味着因子本身已经在做空山寨, C 叠加 70-30
    多空比例时必须把这部分算进去, 否则净暴露会跑到目标之外。
    """
    rows = {}
    for k, f in factors.items():
        d = F.ls_alpha(f, close, horizon)
        s = float(sign.get(k, 1.0))
        rows[k] = {"sign": s, "gross_%/y": d["gross"] * s, "beta": d["beta"] * s,
                   "alpha_%/y": d["alpha"] * s, "t_alpha": d["t_alpha"] * s, "n": d["n"]}
    e = pd.DataFrame(rows).T
    e["pass"] = (e["alpha_%/y"] > min_alpha) & (e["t_alpha"] >= min_t_alpha)
    return e


def select_on(rep: pd.DataFrame, corr: pd.DataFrame, q_hit: pd.Series, eco: pd.DataFrame,
              min_ic_ir: float, min_q_hit: float, max_corr: float, k: int,
              seed: list | None = None) -> list:
    """seed = 已经交给 C 的因子名, 只参与去相关、不参与被选。

    ML 侧必须传 seed: 不传的话, 一个和 lowvol_14d 相关 -0.9 的 ml_pc2 会照样入选,
    C 拿到手等于同一个信号收了两遍, 却以为是两个独立来源。
    corr 必须是**联合**相关阵(含 seed 里的名字), 否则 seed 起不到作用。
    """
    seed = list(seed or [])
    ok = ((rep.IC_IR.abs() >= min_ic_ir) & (q_hit.reindex(rep.index) >= min_q_hit)
          & eco["pass"].reindex(rep.index).fillna(False))
    pool = rep[ok.fillna(False)]
    chosen, blocking = [], list(seed)
    for f in pool.IC_IR.abs().sort_values(ascending=False).index:
        if f in blocking:
            continue
        if all(abs(corr.loc[f, g]) <= max_corr for g in blocking if g in corr.columns):
            chosen.append(f)
            blocking.append(f)
        if len(chosen) == k:
            break
    return chosen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-usd", type=float, default=1_000_000)
    ap.add_argument("--min-ic-ir", type=float, default=0.05)
    ap.add_argument("--min-q-hit", type=float, default=2 / 3)
    ap.add_argument("--min-alpha", type=float, default=0.0,
                    help="经济门槛: IS 多空组合对市场回归后的年化 alpha%% 下限")
    ap.add_argument("--min-t-alpha", type=float, default=2.0,
                    help="经济门槛: alpha 的 t 值下限(<2 视为噪声, 不给 C 用)")
    ap.add_argument("--max-corr", type=float, default=0.5)
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()

    REPORTS.mkdir(exist_ok=True)
    h = pd.read_parquet(HERE / "cache" / "bars_1h_all.parquet")
    h = h[~h.symbol.isin(["OMNI/USD", "TON/USD"])]
    b4 = resample(h, "4h")
    print("bars 4h:", b4.shape, "symbols:", b4.symbol.nunique())

    fs = F.compute_all(b4, interval="4h", min_usd=args.min_usd)
    close_w = F.to_wide(b4, "close")
    fwd = F.forward_returns(close_w, BARS_PER_DAY)
    q_all = quarter_of(close_w.index)

    fs_is = {k: v[v.index < SPLIT_MS] for k, v in fs.items()}
    fs_oos = {k: v[v.index >= SPLIT_MS] for k, v in fs.items()}
    close_is, close_oos = close_w[close_w.index < SPLIT_MS], close_w[close_w.index >= SPLIT_MS]

    rep_full = F.factor_report(fs, close_w, BARS_PER_DAY)
    rep_is = F.factor_report(fs_is, close_is, BARS_PER_DAY)
    rep_oos = F.factor_report(fs_oos, close_oos, BARS_PER_DAY)
    rep_full["IC_IS"], rep_full["IC_OOS"] = rep_is.IC_mean, rep_oos.IC_mean
    rep_full["fee_drag_%/day"] = fee_drag_pct_per_day(rep_full)

    sign_is = ic_sign(rep_is)

    eco_is = economic_report(fs_is, close_is, BARS_PER_DAY, sign_is, args.min_alpha, args.min_t_alpha)
    # 盲验段用 IS 的方向, 不用 OOS 自己的 IC 翻号(见 economic_report docstring)
    eco_oos = economic_report(fs_oos, close_oos, BARS_PER_DAY, sign_is, args.min_alpha, args.min_t_alpha)

    corr_is = F.factor_corr(fs_is)

    qic = pd.DataFrame({k: quarterly_ic(v, fwd) for k, v in fs.items()}).T
    qic_is = qic.loc[:, [c for c in qic.columns if c < "2026Q1"]]
    q_hit_is = quarter_hit(qic_is, rep_is.IC_mean)

    selected = select_on(rep_is, corr_is, q_hit_is, eco_is,
                         args.min_ic_ir, args.min_q_hit, args.max_corr, args.k)
    ic_ir_is = rep_is.loc[selected, "IC_IR"]
    # 权重用 |IC_IR| 归一; 方向单独给 C, 免得负 IC 因子被反向下单(当前入选者全为正, 但别赌)
    signs = {f: int(sign_is[f]) for f in selected}
    weights = ic_ir_is.abs()
    weights = (weights / weights.sum()).round(4).to_dict() if selected else {}

    # walk-forward: 每季度只用该季之前的数据选(同一套四道门槛)
    wf_rows = []
    for q in sorted(q_all.unique()):
        if str(q) <= "2025Q1":  # 之前数据不足两个季度, 跳过
            continue
        cut = pd.Timestamp(q.start_time, tz="UTC").value // 10**6
        f_tr = {k: v[v.index < cut] for k, v in fs.items()}
        c_tr = close_w[close_w.index < cut]
        if len(c_tr) < 500:
            continue
        r_tr = F.factor_report(f_tr, c_tr, BARS_PER_DAY)
        cq = F.factor_corr(f_tr)
        eq = economic_report(f_tr, c_tr, BARS_PER_DAY, ic_sign(r_tr), args.min_alpha, args.min_t_alpha)
        qq = pd.DataFrame({k: quarterly_ic(v, F.forward_returns(c_tr, BARS_PER_DAY)) for k, v in f_tr.items()}).T
        qq = qq.loc[:, [c for c in qq.columns if c < str(q)]]
        sel = select_on(r_tr, cq, quarter_hit(qq, r_tr.IC_mean), eq,
                        args.min_ic_ir, args.min_q_hit, args.max_corr, args.k)
        wf_rows.append({"quarter": str(q), "selected": "|".join(sel)})
    wf = pd.DataFrame(wf_rows)

    rep_full.round(4).to_csv(REPORTS / "factor_report_4h_full.csv")
    rep_is.round(4).to_csv(REPORTS / "factor_report_4h_IS.csv")
    rep_oos.round(4).to_csv(REPORTS / "factor_report_4h_OOS.csv")
    eco_is.round(4).to_csv(REPORTS / "factor_economic_IS.csv")
    eco_oos.round(4).to_csv(REPORTS / "factor_economic_OOS.csv")
    corr_is.round(3).to_csv(REPORTS / "factor_corr_4h_IS.csv")
    qic.round(4).to_csv(REPORTS / "walkforward_quarterly_IC.csv")
    wf.to_csv(REPORTS / "walkforward_selection.csv", index=False)
    live_q = str(sorted(q_all.unique())[-1]) if len(q_all) else None
    live_sel = wf.loc[wf.quarter == live_q, "selected"]
    (REPORTS / "selected_IS.json").write_text(json.dumps({
        "universe_symbols": int(b4.symbol.nunique()),
        "interval": "4h", "horizon_bars": BARS_PER_DAY,
        "split_iso": "2026-01-01", "min_usd": args.min_usd,
        "rule": (f"IS-only: |IC_IR_IS|>={args.min_ic_ir}, IS quarter hit>={args.min_q_hit:.2f}, "
                 f"LS alpha>{args.min_alpha}%/y with t>={args.min_t_alpha}, corr<={args.max_corr}"),
        "selected": selected, "ic_ir_weights": weights, "ic_sign_is": signs,
        "economic_is": {f: {"alpha_%/y": round(float(eco_is.loc[f, "alpha_%/y"]), 1),
                            "t_alpha": round(float(eco_is.loc[f, "t_alpha"]), 2),
                            "beta_to_market": round(float(eco_is.loc[f, "beta"]), 2)}
                        for f in selected},
        "economic_oos_blind": {
            "note": "事后诊断, **不要**拿它回头改 selected(那就不盲了)。方向仍按 IS 的 sign。"
                    "看它只为一件事: 哪些 IS 通过的因子在 OOS 崩了 → 别在实盘用。",
            **{f: {"alpha_%/y": round(float(eco_oos.loc[f, "alpha_%/y"]), 1),
                   "t_alpha": round(float(eco_oos.loc[f, "t_alpha"]), 2),
                   "pass": bool(eco_oos.loc[f, "pass"])}
               for f in selected},
        },
        "walk_forward": {
            "note": "selected/ic_ir_weights 是 IS(2024-10~2025-12) 的选择, 用于 OOS 盲验; "
                    "实盘请用 live_selected(只用该季之前数据选出), 两者可能不同",
            "live_quarter": live_q,
            "live_selected": live_sel.iloc[0].split("|") if len(live_sel) and live_sel.iloc[0] else selected,
        },
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    pd.set_option("display.width", 200)
    print("\n=== full (IC_IS/IC_OOS 仅供盲验对照, 不参与选择) ===\n", rep_full.round(4))
    print("\n=== IS report (统计门槛) ===\n", rep_is.round(4))
    print("\n=== IS 经济门槛: 多空 alpha 拆解 (年化%, 已按 IS 的 IC 方向定向) ===\n", eco_is.round(2))
    print("\n=== OOS 经济门槛 (盲验: 仍按 IS 的方向定向, 不参与选择) ===\n", eco_oos.round(2))
    print("\n=== IS 季度 IC>0 占比 ===\n", q_hit_is.round(2))
    print("\n=== 逐季度 IC ===\n", qic.round(3))
    print("\n=== walk-forward 选择 ===\n", wf.to_string(index=False))
    print("\nselected (IS-only):", selected)
    print("ic_ir_weights:", weights)


if __name__ == "__main__":
    main()
