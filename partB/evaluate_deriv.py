"""衍生品因子评估（B 侧）。口径与 run_factors.py 一致: 65 币、日频、固定切分 2026-01-01、
IS 内判定准入、OOS 只盲验。不含回测。

准入门槛与 run_factors.py 对齐(四道):
  1 |IC_IR_IS| >= 0.05            2 IS 季度 IC>0 占比 >= 2/3
  3 经济门槛: 多空对市场回归后 alpha > 0 且 t_alpha >= 2
  4 与 lowvol_14d 对照因子的相关 <= 0.5, 且 fee_drag <= 0.05%/天

用法:
  python -m partB.data.binance_deriv --what basis --start 2024-10   # 先下 perp klines
  python -m partB.evaluate_deriv
"""
import pathlib

import numpy as np
import pandas as pd

from partB import factors as F
from partB import factors_deriv as FD
from partB.data.binance_deriv import EXCLUDED
from partB.data.binance_vision import resample

HERE = pathlib.Path(__file__).resolve().parent
REPORTS = HERE / "reports"
SPLIT_MS = pd.Timestamp("2026-01-01", tz="UTC").value // 10**6
TAKER = 0.001
MIN_IC_IR, MIN_Q_HIT, MAX_CORR, MAX_FEE, MIN_T = 0.05, 2 / 3, 0.5, 0.05, 2.0


def quarter_of(idx):
    return pd.Series(pd.to_datetime(idx, unit="ms", utc=True).tz_localize(None).to_period("Q"), index=idx)


def main():
    h = pd.read_parquet(HERE / "cache" / "bars_1h_all.parquet")
    h = h[~h.symbol.isin(EXCLUDED)]
    d1 = resample(h, "1d")
    close_w = F.to_wide(d1, "close")
    daily_ts = close_w.index
    fwd = F.forward_returns(close_w, 1)

    fut_w, spot_w = FD.load_basis_panels(h)
    fs = FD.compute_deriv_factors(daily_ts, fut_w, spot_w)
    fs["lowvol_14d(对照)"] = F.cs_rank(F.low_vol(close_w, 14))
    n_perp = int(fs["basis_level_1d"].notna().sum(axis=1).iloc[-1]) if "basis_level_1d" in fs else 0
    print(f"factors: {list(fs.keys())} | 有 perp 的币: {n_perp} / {close_w.shape[1]}"
          f" | 有 funding 的币: {int(fs['funding_carry_3d'].notna().sum(axis=1).iloc[-1])}")

    rep = F.factor_report(fs, close_w, 1)
    fs_is = {k: v[v.index < SPLIT_MS] for k, v in fs.items()}
    close_is = close_w[close_w.index < SPLIT_MS]
    rep_is = F.factor_report(fs_is, close_is, 1)
    rep_oos = F.factor_report({k: v[v.index >= SPLIT_MS] for k, v in fs.items()},
                              close_w[close_w.index >= SPLIT_MS], 1)
    rep["IC_IS"], rep["IC_OOS"] = rep_is.IC_mean, rep_oos.IC_mean
    rep["fee_drag_%/day"] = rep.turnover * TAKER * 1 * 100  # 日频调仓

    # 经济门槛: 方向一律用 **IS** 的 IC 符号(盲验段也用它, 否则等于偷看答案)
    sign_is = np.sign(rep_is.IC_IR).where(rep_is.IC_IR != 0, 1.0)
    eco_is = {k: F.ls_alpha(v, close_is, 1) for k, v in fs_is.items()}
    eco_oos = {k: F.ls_alpha(v[v.index >= SPLIT_MS], close_w[close_w.index >= SPLIT_MS], 1)
               for k, v in fs.items()}
    for col, key in [("ls_alpha_IS_%/y", "alpha"), ("t_alpha_IS", "t_alpha"), ("beta_IS", "beta")]:
        rep[col] = [eco_is[k][key] * float(sign_is.get(k, 1.0)) for k in rep.index]
    rep["ls_alpha_OOS_%/y"] = [eco_oos[k]["alpha"] * float(sign_is.get(k, 1.0)) for k in rep.index]
    rep["t_alpha_OOS"] = [eco_oos[k]["t_alpha"] * float(sign_is.get(k, 1.0)) for k in rep.index]

    q = quarter_of(close_w.index)
    qic = pd.DataFrame({k: pd.Series({str(qn): F.ic_series(v.loc[g.index], fwd.loc[g.index]).dropna().mean()
                                      for qn, g in q.groupby(q) if len(g) >= 40})
                        for k, v in fs.items()}).T
    qic_is = qic.loc[:, [c for c in qic.columns if c < "2026Q1"]]
    q_hit_is = (qic_is > 0).mean(axis=1)

    corr = F.factor_corr(fs)
    rep["q_hit_IS"] = q_hit_is.reindex(rep.index)
    rep["corr_lowvol"] = corr["lowvol_14d(对照)"].reindex(rep.index)
    rep["pass"] = ((rep_is.IC_IR.abs().reindex(rep.index) >= MIN_IC_IR)
                   & (q_hit_is.reindex(rep.index) >= MIN_Q_HIT)
                   & (rep["ls_alpha_IS_%/y"] > 0) & (rep.t_alpha_IS >= MIN_T)
                   & (rep.corr_lowvol.abs() <= MAX_CORR)
                   & (rep["fee_drag_%/day"] <= MAX_FEE))

    rep.round(4).to_csv(REPORTS / "deriv_factor_report.csv")
    qic.round(4).to_csv(REPORTS / "deriv_quarterly_IC.csv")
    pd.set_option("display.width", 260)
    print("\n=== 衍生品因子报告 (日频, fwd=1d) ===\n", rep.round(4))
    print("\n=== 逐季度 IC ===\n", qic.round(3))
    print("\n=== 因子相关 ===\n", corr.round(2))
    print("\npass (IS 四道门槛):", rep.index[rep["pass"].fillna(False)].tolist())


if __name__ == "__main__":
    main()
