"""Tokenized stock 历史 K 线下载器（**Binance Vision，官方数据源**）+ 代币/标的跟踪误差检验。

为什么是 Binance 而不是 Yahoo
----------------------------
Roostoo 的 `exchangeInfo` 把 21 个交易对标成 `AssetType: "stock"`（NVDAB/USD 这些）。
一开始以为只能去股票市场找数据（Yahoo），**这是错的**：

    Roostoo `NVDAB/USD`  ↔  Binance 现货 `NVDABUSDT`

命名规则完全一样（`{COIN}B` + `USDT`），21 个**全部**能在 Binance 现货上找到、状态都是 TRADING。
这正好对上 FAQ Q17「Roostoo real-time pricing is streamed from Binance」—— 它不只是说 crypto。

2026-10-04 实测（Roostoo ticker vs Binance 最新 1h 收盘）:

    TSLAB  371.43 vs 371.43   完全相等          NVDAB  234.69 vs 234.57
    MUB   1069.02 vs 1069.32                     SKHYB  195.25 vs 194.96
    CBRSB  180.64 vs 180.25                      CRCLB   83.28 vs  83.09

之前用 Yahoo 时看到的"CBRSB 差 831bp、SKHYB 差 7 倍"**根本不是代币脱钩, 是参照物选错了**:
Yahoo 报的是标的股票（SK 海力士在韩交所报 KRW）, 而 Roostoo 跟的是 Binance 的代币。
换到 Binance 之后 SKHYB 的 7 倍差距直接消失（194.96 vs 195.25）。

⚠️ 但 Binance 有一个致命限制：**这些代币全是 2026 年 6–7 月才上市的**
---------------------------------------------------------------
    NVDAB/MUB/SNDKB/TSLAB/CRCLB   2026-06-11      SPCXB  2026-06-12
    AMDB/INTCB/MSTRB              2026-06-23      LITEB/METAB/MSFTB/PLTRB  2026-06-30
    CBRSB/COINB/GLWB/GOOGLB/NBISB/QCOMB/WDCB  2026-07-07      SKHYB  2026-07-13

最长的只有 **116 天**（截至 2026-10-04）。而 crypto 的评估口径是 IS 2024-10 ~ 2025-12
+ OOS 2026-01 起、还要按季度做 walk-forward。**21 个里 0 个够 IS 窗口。**

所以这批数据的定位是:
  ✓ 实盘 bar 源（`BarFeed` 直接能用, 见下）
  ✓ 短窗口的描述性研究（2026-06 以来这一段）
  ✗ **不能**用来跑和 crypto 同口径的因子选择 —— 没有样本外, 任何"挖出来的规律"都无从验证

顺带解决了一个悬案: **实盘怎么拿股票的 bar**。之前以为不行（BarFeed 的源是 Binance,
而"Binance 不上市股票"是错的）。实际上 `roostoo_to_binance("NVDAB/USD")` → `NVDABUSDT`,
BarFeed 的 warmup / poll / 缓存恢复 / 质量校验 / 价差监控**原样可用**, 一行代码都不用改。

`--track` 模式: 代币到底跟不跟标的？
---------------------------------
既然 Binance 的代币只有 116 天, 而标的股票（Yahoo）有两三年, 那这段重叠期就是**唯一的窗口**
来回答"代币和标的是不是同一个东西"。`--track` 用两边**同一时刻**（美股收盘, 20:00 UTC）的价格算:

  - `div_bp`      代币价 / 标的价 − 1, 单位 bp。看的是**水平差**（有没有固定折价）
  - `ret_corr`    两边日收益的相关系数。看的是**同不同步**
  - `te_bp`       日收益差的年化标准差(bp)。看的是**跟踪误差**, 这才是真正致命的量
  - `offhours_%`  代币在美股休市时段贡献的收益占比。Yahoo 永远看不到这部分

判定逻辑: `ret_corr` 高 + `te_bp` 小 → 代币就是标的的镜像, 那么用标的的长历史去构建
收益率型因子是站得住的（因子只关心收益率, 不关心水平）。反之则不行。

存储
----
  partB/cache/stocks_binance_1h_all.parquet   B0 长表, 与 bars_1h_all.parquet 同构, 已过 validate()
  partB/reports/stock_tracking_binance_vs_underlying.csv   --track 的结果

用法
----
  python -m partB.data.stock_binance            # 下 21 个代币的 1h 全历史
  python -m partB.data.stock_binance --track    # 再跑代币 vs 标的的跟踪误差检验
  python -m partB.data.stock_binance --list     # 只看 Binance 符号映射
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from .binance_vision import fetch
from .schema import validate

HERE = pathlib.Path(__file__).resolve().parent.parent       # partB/
CACHE = HERE / "cache"
REPORTS = HERE / "reports"
EXCHANGE_INFO = HERE / "exchangeInfo.json"
OUT = CACHE / "stocks_binance_1h_all.parquet"
TRACK_CSV = REPORTS / "stock_tracking_binance_vs_underlying.csv"

# 这些代币最早 2026-06-11 上市, 从 2026-05 开始扫就够（多扫的月份只会 404, 不会出错）
START = "2026-05"
US_CLOSE_UTC_H = 20      # 美股 16:00 EDT = 20:00 UTC（冬令时是 21:00, 检验时两边都会命中其一）

# 参照物**不在美股时段**的交易对。检验方法是"取 20:00/21:00 UTC 那一刻两边对比",
# 这对美股是同一个瞬间, 对韩股不是 —— 韩交所 15:30 KST = 06:30 UTC 收盘, 差了 13.5 小时。
# 实测 SKHYB/USD: 比值 0.1365（代币 ≈ Yahoo 报价的 1/7.3）, 日收益相关只有 0.16。
# 这**不能**解读成"SKHYB 代币脱钩了":
#   · 代币价 194.96 vs Roostoo 报价 195.25 —— 差 15bp, 完全跟得上;
#   · 比值 0.1365 说明 Yahoo 那条 000660.KS 的 USD 换算口径和代币不是 1:1（可能是份额比例,
#     也可能是 FX/复权处理不一致）, 而 0.16 的相关系数主要来自 13.5 小时的时点错配。
# 所以这些行照常算出来, 但打上 ref_us_hours=False, 汇总时单列, 不参与结论。
NON_US_REF = {"SKHYB/USD"}


def stock_pairs() -> list[str]:
    """从 exchangeInfo 读出 AssetType == "stock" 的交易对。

    不在代码里硬编码 21 个名字: 主办方随时可能加标的, 而硬编码的清单会**静默**漏掉新的。
    """
    d = json.loads(EXCHANGE_INFO.read_text(encoding="utf-8"))
    return sorted(k for k, v in d["TradePairs"].items() if v.get("AssetType") == "stock")


def roostoo_to_binance_stock(pair: str) -> str:
    """`NVDAB/USD` → `NVDABUSDT`。和 crypto 是同一条规则, 单列出来只是为了读起来明确。"""
    return pair.split("/")[0] + "USDT"


def _one(pair: str) -> tuple[str, pd.DataFrame | str]:
    try:
        df = fetch(roostoo_to_binance_stock(pair), "1h", START)
        return pair, df
    except Exception as e:                                    # noqa: BLE001
        return pair, f"ERR {e}"


def download(pairs: list[str] | None = None, workers: int = 4) -> tuple[pd.DataFrame, list[str]]:
    """并行下载 → 合并成一张 B0 长表 → validate → 落盘。

    落盘是**合并**而不是整表覆盖: 网络抖动导致某几个标的失败时, 覆盖写会把上一轮
    已经下好的标的抹掉, 而脚本仍然打印"写入成功"。重跑一次就能补齐缺的那些。
    """
    pairs = pairs or stock_pairs()
    frames = []
    with ThreadPoolExecutor(workers) as ex:
        for pair, res in ex.map(_one, pairs):
            if isinstance(res, str):
                print(f"  ✗ {pair:12s} {res}")
                continue
            if res.empty:
                print(f"  ✗ {pair:12s} Vision 上没有数据")
                continue
            frames.append(res)
            d0 = pd.to_datetime(res.ts.min(), unit="ms", utc=True).date()
            d1 = pd.to_datetime(res.ts.max(), unit="ms", utc=True).date()
            days = (res.ts.max() - res.ts.min()) / 86_400_000
            # 24/7 交易 → 每天应该有 24 根 1h bar。少太多说明有停牌/缺口, 要喊出来。
            expect = days * 24
            fill = len(res) / expect if expect else 0
            warn = "" if fill > 0.95 else f"  ⚠️ 只有应有根数的 {fill:.0%}"
            print(f"  ✓ {pair:12s} {len(res):5d} 根 {d0} → {d1}（{days:.0f} 天）{warn}")
    if not frames:
        raise SystemExit("一个都没下下来")
    new = pd.concat(frames, ignore_index=True)
    if OUT.exists():
        old = pd.read_parquet(OUT)
        new = pd.concat([old[~old.symbol.isin(set(new.symbol))], new], ignore_index=True)
    out = validate(new.sort_values(["ts", "symbol"]).reset_index(drop=True), "1h")
    CACHE.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUT, index=False)
    missing = sorted(set(pairs) - set(out.symbol))
    print(f"\n写入 {OUT.name}: {len(out)} 行, {out.symbol.nunique()} 个标的")
    if missing:
        print(f"⚠️ 还缺 {len(missing)} 个: {', '.join(missing)} —— 再跑一次同一条命令即可补齐")
    # 不 raise 是为了让已经下好的部分能落盘（合并写）; 但必须让调用方看见。
    return out, missing
    return out


# ---------- 跟踪误差检验 ----------
def _underlying_close(pairs: list[str]) -> pd.DataFrame:
    """从已下好的 Yahoo 缓存里取标的股票的收盘价, 返回长表 (date, pair, close)。

    ⚠️ Yahoo 在这里的角色**只是参照物**, 不是数据源（数据源是 Binance）。
    之所以还要用它, 是因为只有它有 2026-06 之前的历史 —— 但本检验只用重叠期, 所以不影响结论。
    """
    from .stock_yahoo import MAPPING
    f = CACHE / "stocks_1d_all.parquet"
    if not f.exists():
        raise SystemExit(f"缺 {f.name}: 先跑 `python -m partB.data.stock_yahoo --intervals 1d`")
    y = pd.read_parquet(f)
    y = y[y.symbol.isin(pairs)]
    y = y.assign(date=pd.to_datetime(y.ts, unit="ms", utc=True).dt.date)
    return y[["date", "symbol", "close"]].rename(columns={"symbol": "pair", "close": "y_close"})


def track(pairs: list[str] | None = None) -> pd.DataFrame:
    """代币（Binance）vs 标的（Yahoo）在**同一时刻**的价格对比。

    取美股收盘那一刻（20:00 或 21:00 UTC）两边的价格: 代币取那根 1h bar 的 close,
    标的取当天的日线 close（Yahoo 日线的时间戳就是美股收盘）。
    这样比的是同一个瞬间, 不会像之前那样拿"周五收盘"对"周日实时"。
    """
    pairs = pairs or stock_pairs()
    if not OUT.exists():
        raise SystemExit(f"缺 {OUT.name}: 先跑 `python -m partB.data.stock_binance`")
    b = pd.read_parquet(OUT)
    b = b.assign(t=pd.to_datetime(b.ts, unit="ms", utc=True))
    # 美股收盘时刻的那根 1h bar（EDT=20:00, EST=21:00, 取当天最接近收盘的一根）
    b = b[b.t.dt.hour.isin([US_CLOSE_UTC_H, US_CLOSE_UTC_H + 1])]
    b = (b.sort_values("ts").groupby([b.t.dt.date, "symbol"]).tail(1)
         .assign(date=lambda x: x.t.dt.date)[["date", "symbol", "close"]]
         .rename(columns={"symbol": "pair", "close": "b_close"}))
    y = _underlying_close(pairs)
    m = b.merge(y, on=["date", "pair"], how="inner").sort_values(["pair", "date"])
    if m.empty:
        raise SystemExit("两边没有重叠日期 —— 检查 Yahoo 缓存和 Binance 缓存是否都下好了")

    rows = []
    for pair, g in m.groupby("pair"):
        g = g.set_index("date")
        div = (g.b_close / g.y_close - 1.0) * 1e4
        rb, ry = g.b_close.pct_change(), g.y_close.pct_change()
        d = (rb - ry).dropna()
        rows.append({
            "pair": pair,
            "n_days": len(g),
            "first": str(g.index.min()), "last": str(g.index.max()),
            "div_bp_median": div.median(),
            "div_bp_absmax": div.abs().max(),
            # ⚠️ div_bp_std 是"比值"的绝对标准差。比值≈1 时（美股）100bp 就是 1% 相对偏离;
            # 比值≈0.14 时（SKHYB, 见下）82bp 其实是 6% 相对偏离。要除以比值才可比。
            "div_bp_std": div.std(),
            "ratio_median": (g.b_close / g.y_close).median(),
            "ret_corr": rb.corr(ry),
            "te_bp_daily": d.std() * 1e4,
            "te_bp_annual": d.std() * 1e4 * (252 ** 0.5),
            "beta_to_underlying": (rb.cov(ry) / ry.var()) if ry.var() else float("nan"),
            "ref_us_hours": pair not in NON_US_REF,
        })
    r = pd.DataFrame(rows).set_index("pair")

    # 非美股时段的收益贡献（单独算, 需要完整的 1h 序列）
    full = pd.read_parquet(OUT).assign(t=lambda x: pd.to_datetime(x.ts, unit="ms", utc=True))
    full["hour"] = full.t.dt.hour
    # 美股常规时段 = 13:30–20:00 UTC(EDT) / 14:30–21:00 UTC(EST), 这里粗取 14–21
    ing = full[full.hour.between(14, 20)].groupby("symbol").close.apply(lambda s: s.pct_change().abs().sum())
    tot = full.groupby("symbol").close.apply(lambda s: s.pct_change().abs().sum())
    r["offhours_absret_%"] = ((1 - ing / tot) * 100).reindex(r.index)

    REPORTS.mkdir(parents=True, exist_ok=True)
    r.round(4).to_csv(TRACK_CSV, encoding="utf-8-sig")
    pd.set_option("display.width", 250)
    ok = r[r.ref_us_hours]
    bad = r[~r.ref_us_hours]
    print(f"\n=== 代币(Binance) vs 标的(Yahoo) 跟踪检验, 美股收盘时刻对齐 ===\n{r.round(3).to_string()}")
    if len(ok):
        print(f"\n可解释的 {len(ok)} 个（参照物也在美股时段）:"
              f"\n  收益相关  中位 {ok.ret_corr.median():.3f}（最低 {ok.ret_corr.min():.3f} / {ok.ret_corr.idxmin()}）"
              f"\n  水平偏离  中位 {ok.div_bp_median.abs().median():.0f}bp（最大 {ok.div_bp_median.abs().max():.0f}bp）"
              f"\n  日跟踪误差 中位 {ok.te_bp_daily.median():.0f}bp（最大 {ok.te_bp_daily.max():.0f}bp）"
              f"\n  对标的 beta 中位 {ok.beta_to_underlying.median():.2f}"
              f"\n  非美股时段贡献了 {ok['offhours_absret_%'].median():.0f}% 的绝对收益 → 代币确实 24/7 在动")
    if len(bad):
        print(f"\n⚠️ 参照物不在美股时段, 数字不可解读: {list(bad.index)}（见 NON_US_REF 注释）")
    print(f"写入 {TRACK_CSV.name}")
    return r


def main() -> None:
    ap = argparse.ArgumentParser(description="下载 21 个 tokenized stock 的 Binance 历史 + 跟踪误差检验")
    ap.add_argument("--only", nargs="+", default=None, help="只处理这些 Roostoo 交易对")
    ap.add_argument("--track", action="store_true", help="跑代币 vs 标的的跟踪误差检验（需要先有 Yahoo 1d 缓存）")
    ap.add_argument("--list", action="store_true", help="只打印映射表")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    pairs = a.only or stock_pairs()
    if a.list:
        for p in pairs:
            print(f"{p:12s} → {roostoo_to_binance_stock(p)}")
        return
    if not a.track:
        print(f"从 Binance Vision 下载 {len(pairs)} 个 tokenized stock（1h, {START} 起）")
        _, missing = download(pairs, a.workers)
        if missing:
            raise SystemExit(1)      # 非零退出: cron/CI 里必须能看见"没下全", 不能假装成功
    if a.track:
        track(pairs)


if __name__ == "__main__":
    main()
