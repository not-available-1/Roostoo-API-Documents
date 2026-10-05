"""B→C 接入示例 —— 接口演示, **不是策略, 不要直接上线**。

C 从 B 拿到的只有两样东西:
  1. `partB/reports/selected_IS.json` —— 选哪些因子、权重多少、方向是正是负、实盘该用哪一组
  2. `partB.factors.compute_all(bars)` —— 因子宽表(已横截面 rank, 已按流动性打 NaN)
     bars 的来源: 实盘走 `partB.live.barfeed.BarFeed`, 研究/回测走本地 parquet 缓存

本文件把这条链完整跑一遍: json → bars → 因子 → 合成分数 → 当期横截面 → (示意) C0 Target。
组合逻辑、仓位权重、择时、风控、回测**全部归 C**; 末尾的 `to_targets()` 只演示
"分数怎么变成 C0 契约的形状", 里面的 top-K / 等权 / 阈值都是随手写的占位值。

运行:
    PYTHONPATH=. python -m partB.example_for_c                  # 离线: 用 cache/bars_1h_all.parquet
    PYTHONPATH=. python -m partB.example_for_c --live           # 在线: 走 BarFeed(Binance 公开 REST, 免 key)
    PYTHONPATH=. python -m partB.example_for_c --set is         # 用 IS 选出的那组(含 mom_30d_skip1d)做对比

三条不可违反的时序规则(违反 = 未来函数 = 提交无效):
  R1 只用已收盘 bar。`BarFeed.history()` 内部已丢弃 `close_time >= now` 的 bar, 所以它返回的
     最后一行就是"最近一根已收盘 4h bar", 可以直接用, 不需要再 shift。
  R2 因子在 ts 行的值只用 <= ts 那根 bar 的收盘信息 → 实际可用时刻是 ts + interval。
     回测里 C 必须 `factor.shift(1)` 去对齐"下一根 bar 的收益", 或等价地只在 bar 收盘后下单。
     B 的评估口径是"t 收盘进、t+horizon 收盘出"; 实盘是"t 收盘后几秒下单", 两者一致但成交价会漂,
     C 自己决定加滑点还是改用 t+1 open 成交(保守版)。
  R3 分数为 NaN 的币 = 流动性不足或历史不够窗口 → 必须 dropna, **绝不能用 0 填**
     (cs_rank 的 0.5 附近是中位数, 填 0 会凭空造出"极端看空"的仓位)。
"""
from __future__ import annotations

import argparse
import json
import pathlib

import pandas as pd

from partB import factors as F
from partB.data.binance_deriv import default_symbols
from partB.data.binance_vision import resample

HERE = pathlib.Path(__file__).resolve().parent
SELECTION = HERE / "reports" / "selected_IS.json"
CACHE_BARS = HERE / "cache" / "bars_1h_all.parquet"


# ---------------------------------------------------------------- 1. 读选择结果
def load_selection(path: pathlib.Path = SELECTION, which: str = "live") -> dict:
    """把 json 解析成 C 真正需要的四元组: 因子名 / 带符号权重 / interval / horizon。

    which="live" → 用 walk_forward.live_selected(只用该季之前数据选出, 实盘用这个)
    which="is"   → 用 selected(IS 全期选出, 只用于 OOS 盲验对照)

    ⚠️ `ic_ir_weights` 存的是 |IC_IR| 归一化的**幅度**, 不含方向;
       方向在 `ic_sign_is` 里。两者必须相乘, 否则负 IC 因子会被反向下单。
    """
    d = json.loads(path.read_text(encoding="utf-8"))
    names = d["walk_forward"]["live_selected"] if which == "live" else d["selected"]

    signs = d.get("ic_sign_is", {})
    raw_w = d["ic_ir_weights"]
    # live 组可能是 IS 组的子集 → 重新归一, 让权重和为 1
    w = {f: raw_w[f] * signs.get(f, 1) for f in names}
    tot = sum(abs(v) for v in w.values())
    w = {k: v / tot for k, v in w.items()} if tot else w

    return {"factors": names, "weights": w, "interval": d["interval"],
            "horizon": d["horizon_bars"], "min_usd": d["min_usd"], "raw": d}


# ---------------------------------------------------------------- 2. 拿 bars
def bars_offline(interval: str) -> pd.DataFrame:
    """研究/回测路径: 本地 1h 缓存 → 重采样到 interval。schema 长表, 全部 is_final=True。

    ⚠️ 必须剔掉 EXCLUDED(OMNI/TON)。缓存里有它们的 Binance 历史(OMNI 8715 根, 止于 2025-09-29;
    TON 15291 根, 止于 2026-06-30 —— 两个都已在 Binance 和 Roostoo 退市), 但 Roostoo 的 ticker
    查不到它们 → **不能下单**。留着会有两个后果: ①横截面从 65 变 67, C 的因子值和 B 评估的
    对不上; ②退市币价格是冻结的, 因子值长期停在某个极端分位, 回测里会被反复选进组合,
    造出一笔笔实盘根本发不出去的"幽灵交易"。run_factors.py 做的是同一件事, 两边口径必须一致。
    """
    if not CACHE_BARS.exists():
        raise SystemExit(f"缺 {CACHE_BARS.name}: 先跑 `python -m partB.dl` 下载历史 bar")
    from partB.data.binance_deriv import EXCLUDED

    h = pd.read_parquet(CACHE_BARS)
    h = h[~h.symbol.isin(EXCLUDED)]
    return resample(h, interval)


def bars_live(interval: str, symbols: list[str] | None = None, cache_dir: str | None = None) -> pd.DataFrame:
    """实盘路径: BarFeed 自己管缓存 + REST 补洞 + 缺口检查, 只返回已收盘 bar(R1)。

    cache_dir 留空 = 用 BarFeed 默认的 partB/cache/live/(绝对路径)。
    别指向 partB/cache/ 根目录: 那里是 dl.py 的两年全历史缓存, 文件名相同会被截断覆盖。
    """
    from partB.live.barfeed import BarFeed

    feed = BarFeed(symbols or default_symbols(), interval=interval, cache_dir=cache_dir)
    if not feed.warmup():                 # 阻塞直到每个币都有 >=40 天连续 1h bar
        raise SystemExit("warmup 失败: BTC/USD 基准数据缺失 → 不允许出信号")
    feed.poll()                           # 补到最新一根已收盘 bar
    return feed.history()


# ---------------------------------------------------------------- 3. 因子 → 合成分数
def composite_score(bars: pd.DataFrame, sel: dict) -> pd.DataFrame:
    """返回合成分数宽表: index=bar 开盘 ts(ms), columns=symbol。

    值域约 **[-0.5, +0.5]**, 0 = 当期横截面中位数, 越大越看多。
    (因为 `F.cs_rank` 是 `rank(pct=True) - 0.5`, 权重绝对值和为 1 → 加权平均仍落在 [-0.5,0.5]。
     C 如果要接自己的回测/优化器, 想要 [0,1] 就 `+0.5`, 想要 z 分数就自己再标准化一次。)

    步骤:
      compute_all 一次算出全部 7 个因子(每个都已是横截面 rank, 且流动性不足的格子是 NaN)
      → 只挑 json 里选中的 → 乘带符号权重 → 求和。
    """
    all_f = F.compute_all(bars, sel["interval"], sel["min_usd"])
    missing = [f for f in sel["factors"] if f not in all_f]
    if missing:
        raise SystemExit(f"json 选的因子 B 算不出来: {missing}; 可用: {sorted(all_f)}")

    score = None
    for name in sel["factors"]:
        term = all_f[name] * sel["weights"][name]
        score = term if score is None else score + term
    # 不做任何 fillna: pandas 加法天然让 NaN 传染, 缺任一因子的币该期直接出局(R3)
    return score


def latest_cross_section(score: pd.DataFrame) -> pd.Series:
    """最近一根已收盘 bar 的横截面分数, 降序。这一行就是 C 此刻能用的全部信息。"""
    row = score.iloc[-1].dropna().sort_values(ascending=False)
    row.index.name = None
    return row


def ts_to_utc(ts_ms: int) -> str:
    return pd.Timestamp(ts_ms, unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------- 4. (示意) 映射到 C0
def to_targets(cs: pd.Series, top_k: int = 8, weight: float = 0.07,
               min_abs_score: float = 0.10, source: str = "B live_selected") -> list:
    """**接口形状演示, 不是策略**。C 要自己决定: K 取几、多空比例、权重怎么给、
    要不要加 BTC 择时闸门、要不要按波动率调仓、多久再平衡一次。

    这里只做最朴素的一件事: 分数最高且 > 0 的 K 个做多, 最低且 < 0 的 K 个做空
    (比赛允许方向性做空), 不够极端的不动。

    ⚠️ 门槛必须**对称**且带符号闸门: 分数值域是 [-0.5,+0.5](见 composite_score),
       写成 `s >= 0.5` 这种"看起来像百分位"的阈值会一条多单都选不出来、
       却把所有币判成做空 —— 静默变成单边裸空。改阈值前先确认值域。

    返回 contracts.targets.Target 列表: C0 契约要求策略输出"目标持仓", 绝不输出订单;
    reason 必填, 会原样进审计日志(Screen 1)。source 要写清用了哪组因子 ——
    事后复盘"这笔单为什么下"时, 光说"因子选的"没法查。
    """
    from contracts.targets import Target

    n = len(cs)
    if n < 2 * top_k:
        return []
    out = []
    for i, (pair, s) in enumerate(cs.head(top_k).items(), 1):
        if s > 0 and s >= min_abs_score:
            out.append(Target(pair=pair, side="LONG", weight=weight,
                              reason=f"composite {s:+.3f} rank #{i}/{n} ({source})",
                              urgency="MAKER"))
    for i, (pair, s) in enumerate(cs.tail(top_k).iloc[::-1].items(), 1):
        if s < 0 and -s >= min_abs_score:
            out.append(Target(pair=pair, side="SHORT", weight=weight,
                              reason=f"composite {s:+.3f} bottom #{i}/{n} ({source})",
                              urgency="MAKER"))
    return out


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description="B→C 接入示例(接口演示, 非策略)")
    ap.add_argument("--live", action="store_true", help="走 BarFeed 拉实时数据(默认用本地缓存)")
    ap.add_argument("--set", dest="which", choices=["live", "is"], default="live",
                    help="用 walk-forward 当期选择(live)还是 IS 选择(is)")
    ap.add_argument("--top", type=int, default=10, help="打印前/后 N 个币")
    a = ap.parse_args()

    sel = load_selection(which=a.which)
    wf = sel["raw"]["walk_forward"]
    print(f"=== selected_IS.json (which={a.which}) ===")
    print(f"interval={sel['interval']}  horizon={sel['horizon']} bars (=1 天)")
    print(f"live_quarter={wf['live_quarter']}  live_selected={wf['live_selected']}")
    print(f"IS selected   ={sel['raw']['selected']}")
    print(f"带符号权重     ={ {k: round(v, 4) for k, v in sel['weights'].items()} }")
    eco = sel["raw"].get("economic_is", {})
    if eco:
        print("IS 经济门槛    =" + "  ".join(
            f"{f}: alpha {v['alpha_%/y']:+.0f}%/y t={v['t_alpha']:.1f} beta={v['beta_to_market']:+.2f}"
            for f, v in eco.items()))
    blind = sel["raw"].get("economic_oos_blind", {})
    failed = [f for f, v in blind.items() if isinstance(v, dict) and not v.get("pass")]
    if failed:
        print(f"⚠️ OOS 盲验未通过(仅供诊断, 不影响上面的选择): {failed}")

    bars = bars_live(sel["interval"]) if a.live else bars_offline(sel["interval"])
    n_uni = sel["raw"].get("universe_symbols")
    got = bars.symbol.nunique()
    if n_uni and got != n_uni:
        raise SystemExit(
            f"宇宙对不上: bars 里有 {got} 个币, json 的评估口径是 {n_uni} 个。\n"
            f"横截面 rank 依赖'当期有哪些币参与排名', 差一个币所有因子值都会变 →\n"
            f"C 的回测结果没法和 B 的 IC 报告对照。检查是否漏剔退市币(见 bars_offline 的注释)。")
    print(f"\n=== bars ===  {len(bars)} rows, {got} symbols, "
          f"{ts_to_utc(int(bars.ts.min()))} → {ts_to_utc(int(bars.ts.max()))}")
    print(f"最后一根已收盘 bar = {ts_to_utc(int(bars.ts.max()))}  ← 实盘此刻能用的最新信息(R1)")

    score = composite_score(bars, sel)
    cs = latest_cross_section(score)
    print(f"\n=== 当期合成分数横截面 ({ts_to_utc(int(score.index.max()))}) ===")
    print(f"可交易 {len(cs)} 个币(其余因流动性/窗口不足被 NaN 剔除, R3)")
    print(f"\n做多候选(分数最高 {a.top}):")
    for i, (p, s) in enumerate(cs.head(a.top).items(), 1):
        print(f"  {i:2d}. {p:<16} {s:+.4f}")
    print(f"\n做空候选(分数最低 {a.top}):")
    for i, (p, s) in enumerate(cs.tail(a.top).iloc[::-1].items(), 1):
        print(f"  {i:2d}. {p:<16} {s:+.4f}")

    tg = to_targets(cs, source=f"B {a.which}_selected")
    n_long = sum(1 for t in tg if t.side == "LONG")
    n_short = sum(1 for t in tg if t.side == "SHORT")
    gross = sum(t.weight for t in tg)
    print(f"\n=== C0 Target 示意(占位逻辑, C 自己重写) === {len(tg)} 条 "
          f"| LONG {n_long} SHORT {n_short} | gross {gross:.2f}")
    if not n_long or not n_short:
        print("  ⚠️ 单边书! 检查阈值与分数值域是否匹配(见 to_targets 的注释)")
    for t in tg[:6]:
        print(f"  {t.side:<5} {t.pair:<16} w={t.weight:.3f} {t.urgency:<5} | {t.reason}")
    print("\n下一步归 C: 把 cs 喂进 partC/backtest.py 做含费回测 → 定 K/权重/闸门 → 出 Target。")


if __name__ == "__main__":
    main()
