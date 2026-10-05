# partB → partC 接入说明

> 读者: C(策略)。作者: B(数据/因子)。
> 配套可运行示例: **`partB/example_for_c.py`** —— 先跑一遍再读本文, 边看边对照。
>
> ```bash
> export PYTHONPATH=$PWD
> python -m partB.example_for_c            # 离线, 用本地缓存(默认)
> python -m partB.example_for_c --live     # 在线, 走 BarFeed 拉 Binance 公开数据
> python -m partB.example_for_c --set is   # 换用 IS 那组因子做对比
> ```

---

## 0. 一句话总览

B 只交两样东西给 C:

| 交付物 | 是什么 | 怎么用 |
|---|---|---|
| `partB/reports/selected_IS.json` | **选哪些因子 + 每个因子的权重和方向** | `json.loads()` 读, 别把因子名硬编码进策略 |
| `partB.factors.compute_all(bars)` | **因子宽表**(一个函数算出全部 7 个因子) | 传 bars 长表进去, 拿 `{因子名: DataFrame}` 出来 |

bars 从哪来:

| 场景 | 来源 |
|---|---|
| 实盘 | `partB.live.barfeed.BarFeed`(Binance 公开 REST, 免 key, 不占 Roostoo 限频) |
| 回测/研究 | `partB/cache/bars_1h_all.parquet` → `resample(..., "4h")` |

**B 不写策略、不写回测、不写打分。C 不改 `partB/factors.py`、不改 `partB/data/`。**
要新因子 → 找 B 提, B 加进 `compute_all` 并重跑 `run_factors.py`, json 自动更新。

---

## 1. `selected_IS.json` 逐字段

当前内容(2026-10-04 生成, 已加入经济门槛):

```json
{
  "universe_symbols": 65,
  "interval": "4h",
  "horizon_bars": 6,
  "split_iso": "2026-01-01",
  "min_usd": 1000000,
  "rule": "IS-only: |IC_IR_IS|>=0.05, IS quarter hit>=0.67, LS alpha>0.0%/y with t>=2.0, corr<=0.5",
  "selected": ["lowvol_14d", "rangepos_14d", "mom_30d_skip1d"],
  "ic_ir_weights": {"lowvol_14d": 0.6362, "rangepos_14d": 0.2064, "mom_30d_skip1d": 0.1574},
  "ic_sign_is": {"lowvol_14d": 1, "rangepos_14d": 1, "mom_30d_skip1d": 1},
  "economic_is": {
    "lowvol_14d":     {"alpha_%/y": 72.7,  "t_alpha": 4.09, "beta_to_market": -0.56},
    "rangepos_14d":   {"alpha_%/y": 139.2, "t_alpha": 7.82, "beta_to_market": -0.16},
    "mom_30d_skip1d": {"alpha_%/y": 93.1,  "t_alpha": 4.98, "beta_to_market": -0.21}
  },
  "economic_oos_blind": {
    "note": "事后诊断, 不要拿它回头改 selected ...",
    "lowvol_14d":     {"alpha_%/y": 101.6, "t_alpha": 4.44,  "pass": true},
    "rangepos_14d":   {"alpha_%/y": 93.9,  "t_alpha": 4.68,  "pass": true},
    "mom_30d_skip1d": {"alpha_%/y": -68.7, "t_alpha": -3.17, "pass": false}
  },
  "walk_forward": {
    "note": "...",
    "live_quarter": "2026Q4",
    "live_selected": ["lowvol_14d", "rangepos_14d"]
  }
}
```

| 字段 | 含义(人话) | C 要注意 |
|---|---|---|
| `universe_symbols` | 65 个可交易币。Roostoo 挂 67 个, 但 **OMNI/TON 在 ticker 里查不到 → 不能交易**, 已剔除 | 你的回测宇宙必须也是这 65 个, 否则 IC 对不上 |
| `interval` | 因子在 **4h bar** 上算 | BarFeed 要传 `interval="4h"` |
| `horizon_bars` | 6 根 4h = **1 天**。B 评估时假设"持有 1 天" | 你的再平衡频率最好接近 1 天; 差太远 B 的 IC 就不能代表你的收益 |
| `split_iso` | 样本内(IS)/样本外(OOS)分界。**IS = 2024-10~2025-12**(用来选因子), **OOS = 2026-01 至今**(盲验, 选因子时完全没看过) | OOS 数字是裁判视角的"真实"表现, 别拿 IS 数字去汇报 |
| `min_usd` | 流动性门槛: 过去 7 天日均成交额 ≥ 100 万美元才纳入 | 低于门槛的币因子值是 **NaN**, 不是 0 |
| `rule` | 选因子的四条硬规则(见下) | — |
| `selected` | **IS 期内**选出的 3 个因子 | 只用于 OOS 盲验对照 |
| `ic_ir_weights` | 权重 = 各因子 \|IC_IR\| 归一化, 和为 1 | ⚠️ **是绝对值, 不含方向** |
| `ic_sign_is` | 每个因子 IS 期 IC 的正负号(+1/−1) | ⚠️ **必须和 weights 相乘**, 见 §4 陷阱 1 |
| `economic_is` | 每个入选因子在 IS 期的**真实赚钱能力**(见 §1.1) | `beta_to_market` 是净暴露的关键输入, 见 §4 陷阱 5 |
| `economic_oos_blind` | 同一批因子在 OOS 期(2026 年)的表现 | ⚠️ **只是诊断, 不许反过来改选择**, 否则盲验作废 |
| `walk_forward.live_quarter` | 当前实盘所属季度(2026Q4) | — |
| `walk_forward.live_selected` | **实盘该用的因子组**: 只用该季之前的数据选出来的 | ⚠️ **和 `selected` 不一样**, 见 §4 陷阱 2 |

选因子的四条规则(`rule` 字段):
1. `|IC_IR_IS| ≥ 0.05` —— IC_IR = IC 均值 ÷ IC 标准差, 相当于"这个因子的夏普"。0.05 是很低的门槛, 意思是"至少别是纯噪声"。
2. IS 内**季度 IC > 0 的占比 ≥ 2/3** —— 不能靠某一个季度暴赚撑起来的, 要季度间稳定。
3. **经济门槛**: 按 IC 方向做多空组合, 对市场等权收益回归后的年化 `alpha > 0` 且 `t_alpha ≥ 2`(见 §1.1)。
4. 与已选因子的**相关系数 ≤ 0.5** —— 按 \|IC_IR\| 从大到小贪心挑, 太像的不要(避免"看起来几个因子其实赌同一件事")。

### 1.1 为什么加了第 3 条(经济门槛)

前两条只回答一个问题: **"这个信号有没有方向感?"** 答案用 IC 衡量 —— IC 是"因子打分排名"
和"未来收益排名"的相关系数, 0.03 就算有点用, 0.1 已经很好。

但 IC 高**不等于**能赚钱。打个比方: IC 衡量的是"你能不能把全班同学按身高排对序",
而实盘只交易**排头和最矮的那几个**。如果中间 60% 排得很准、头尾却排错了, IC 依然好看,
但你按头尾下单就是亏的。

`rev_1d`(1 天反转)正是这个情况: 它的 `IC_IR_IS = 0.133`, 在 7 个因子里排**第二**,
按老规则稳稳入选、还拿到 0.24 的权重。可是把它折成"做多打分最高的 20% / 做空最低的 20%":

| | IS(2024-10~2025-12) | OOS(2026-01~) |
|---|---|---|
| 多空毛收益 | **−17.6%/年** | **−42.2%/年** |
| 扣掉市场暴露后的 alpha | −14.9%/年, t = −0.7 | −43.7%/年, t = −1.9 |

**它是亏的, 而且亏得很确定。** 分位收益也不单调(五个分位的日均收益挤在 −15bp 到 −22bp 之间,
而全宇宙均值是 −21.7bp), 说明 IC 那点方向感全在中间分位, 头尾根本没有信息。

所以经济门槛的做法是: 每期做多因子打分前 20%、做空后 20%, 得到一条多空收益序列;
再拿它对"全宇宙等权收益"做一元线性回归

```
多空收益 = alpha + beta × 市场收益
```

- `beta` = 这个策略顺带押了多少**市场方向**。样本期山寨等权是 **−62.7%/年**(IS)、
  **−19.7%/年**(OOS), 是个大熊市 —— 任何"系统性做空高波动币"的因子都会自动赚钱,
  那部分是**赌对了方向**, 不是选股本事。
- `alpha` = 扣掉方向后剩下的、真正来自"选了哪些币"的收益。**只有它才算因子质量。**
- `t_alpha ≥ 2` = 这个 alpha 不是运气(约等于 95% 置信)。t 值是"估计值 ÷ 它自己的标准误",
  绝对值越大越不像噪声。

完整推导、被否掉的候选、以及 B 在这一步犯过又修掉的两个计算错误, 都记在审计文件里:
**`partB/reports/rev_smooth_and_economic_gate.md`**。

---

## 2. 调用链(5 步)

完整可运行版本在 `partB/example_for_c.py`, 这里是骨架:

```python
import json, pathlib
from partB import factors as F
from partB.live.barfeed import BarFeed
from partB.data.binance_deriv import default_symbols   # 65 个币的清单(免 key)

# 1) 读 B 的选择结果
d = json.loads(pathlib.Path("partB/reports/selected_IS.json").read_text(encoding="utf-8"))
names = d["walk_forward"]["live_selected"]              # 实盘用这组
w = {f: d["ic_ir_weights"][f] * d["ic_sign_is"][f] for f in names}
tot = sum(abs(v) for v in w.values()); w = {k: v / tot for k, v in w.items()}

# 2) 拿 bars(实盘)
feed = BarFeed(default_symbols(), interval=d["interval"])   # 缓存走默认 partB/cache/live/
assert feed.warmup(), "数据没就绪 → 不许出信号"
feed.poll()
bars = feed.history()          # schema 长表, 只含已收盘 bar

# 3) 一次算出全部因子(每个都是横截面 rank 后的宽表)
all_f = F.compute_all(bars, d["interval"], d["min_usd"])

# 4) 合成: 带符号加权求和
score = sum(all_f[f] * w[f] for f in names)     # 值域约 [-0.5, +0.5], 0 = 中位数

# 5) 取最近一根已收盘 bar 的横截面 → 这就是 C 此刻能用的全部信息
cs = score.iloc[-1].dropna().sort_values(ascending=False)
```

第 5 步之后**全是 C 的地盘**: 选几个、多空比例、权重怎么给、要不要 BTC 择时闸门、
多久再平衡、怎么含费回测、怎么映射成 `contracts.targets.Target`。

`Target` 的形状(C0 契约, 已冻结):

```python
from contracts.targets import Target
Target(pair="BTC/USD", side="LONG", weight=0.07,
       reason="composite +0.256 rank #2/58 (B live_selected)", urgency="MAKER")
```

- `weight` = 占总权益的比例, 必须在 `[0, 1]`; `side="FLAT"` 时 `weight` 必须为 0。
- `reason` **必填**, 会原样写进审计日志 —— Screen 1 查的就是"每笔单都能说清为什么下"。
- 策略输出的是**目标持仓**, 不是订单。D 负责把目标 diff 成 OrderIntent, A 负责签名下单。

---

## 3. 三条时序红线(违反 = 未来函数 = 提交无效)

**R1 — 只用已收盘的 bar。**
`BarFeed.history()` 内部已经丢掉了 `close_time >= now` 的 bar, 所以它返回的最后一行
就是"最近一根已收盘 4h bar", 可以直接用, **不需要再 shift**。
一根 4h bar 的 `ts` 是**开盘**时间, 覆盖 `[ts, ts+4h)`; 只有过了 `ts+4h` 它才算完成。

**R2 — 回测里因子必须 `shift(1)`。**
`partB/factors.py` 的约定: 因子在 `ts` 行的值只用 `<= ts` 那根 bar 的收盘信息,
所以**实际可用时刻是 `ts + interval`**。B 的评估口径是"t 收盘进、t+6 根收盘出"
(`forward_returns = log(close.shift(-6)/close)` 对齐在 t 行)。
实盘是"t 收盘后几秒下单", 与评估口径一致, 但成交价会漂 —— C 自己决定是加滑点假设,
还是改用 t+1 open 成交(更保守)。

**R3 — NaN 不许填 0。**
`compute_all` 在两种情况下给 NaN: ①流动性不足 `min_usd`; ②历史长度不够因子窗口
(例如新上市币算不出 `lowvol_14d`)。NaN 的意思是"**这个币这期没信息**", 必须 `dropna` 剔除。
填 0 = 把它当成"横截面中位数", 会凭空造出仓位。
当前 65 个币里通常约 58 个有分数, 7 个被剔掉。

---

## 4. 六个已知陷阱(B 踩过或差点踩的)

**陷阱 1 — 权重是绝对值, 方向在另一个字段。**
`ic_ir_weights` 存的是 `|IC_IR|` 归一化后的幅度, **不带符号**。方向在 `ic_sign_is` 里。
两者必须相乘。当前 3 个因子 IS 期 IC 全为正(所以符号全是 +1, 乘不乘一样), 但
下次重跑如果选中一个负 IC 因子, 漏乘符号就会**把它反向下单** —— 而且是静默的, 回测照样能跑。

**陷阱 2 — `selected` ≠ `live_selected`, 实盘用后者。**
`selected` 是整个 IS 期(2024-10~2025-12)选出来的, 只用于 OOS 盲验对照。
`walk_forward.live_selected` 是"只用 2026Q4 之前的数据"选出来的, **这才是实盘该用的**。
两者现在差一个因子: `mom_30d_skip1d`。它在 IS 期表现很好(alpha **+93.1%/年**, t = 4.98,
所以进了 `selected`), 但 2026 年三个季度的 IC 是 **−0.011 / −0.061 / −0.012**,
OOS 按 IS 当初给的方向做多空是 **alpha −68.7%/年, t = −3.17**(`economic_oos_blind.pass = false`)。
walk-forward 从 2026Q2 起就把它踢掉了 —— 这个决定和 OOS 盲验的结论**互相印证**。
> 人话: 30 天动量这个因子 2025 年能赚钱, 2026 年反着走了。B 不建议实盘用它。
> 如果 C 坚持, 请在 deck 里自己解释为什么 —— B 的数据不支持。

**陷阱 3 — 分数值域是 [−0.5, +0.5], 不是 [0, 1]。**
`F.cs_rank` 的实现是 `rank(pct=True) - 0.5`, 所以单个因子和合成分数都以 0 为中心。
写阈值时如果按"百分位"直觉写 `score >= 0.5`, 结果会是**一条多单都选不出来、
却把所有币判成做空** —— 静默变成单边裸空。B 的示例里第一版就犯了这个错。
门槛要对称 + 带符号闸门(`s > 0 and s >= min_abs` / `s < 0 and -s >= min_abs`)。

**陷阱 4 — 宇宙必须正好是那 65 个币, 多一个少一个都不行。**
`cs_rank` 是"这个币在当期所有币里排第几"。所以因子值**取决于当期参与排名的币集合**：

- **少币**: 只有 3 个币时排名只有 3 种取值, 因子基本变成噪声(这也是 B 否掉 partB2 早期版本的
  原因之一: N=3 的横截面 IC 没有统计意义)。
- **多币**: 本地 1h 缓存 `bars_1h_all.parquet` 里其实有 **67** 个币 —— 多出来的 OMNI/TON
  在 Binance 和 Roostoo 都已退市(OMNI 数据止于 2025-09-29, TON 止于 2026-06-30),
  Roostoo 的 ticker 查不到 → **下不了单**。如果不剔, 会有两个后果:
  ①横截面从 65 变 67, 你算出的因子值和 B 的 IC 报告对不上;
  ②退市币价格是冻结的, 因子值长期卡在某个极端分位, 回测里会被反复选进组合,
  造出一堆实盘根本发不出去的"幽灵交易", 收益虚高。

`run_factors.py`、`example_for_c.py`、`default_symbols()` 三处都用同一个
`partB.data.binance_deriv.EXCLUDED` 常量做剔除, 别自己另写一份清单。
`example_for_c.py` 在拿到 bars 后会断言 `symbol.nunique() == json.universe_symbols`, 对不上直接退出。

**回测和实盘都必须用完整的 65 币宇宙。** 想快速验证代码可以只用几个币跑通流程,
但那个 IC/收益数字不能拿去汇报。

**陷阱 5 — `lowvol_14d` 自带 −0.56 的市场空头, 别和 70-30 多空比例叠加两次。**
`economic_is.beta_to_market` 是"照这个因子做多空时, 顺带押了多少市场方向":

| 因子 | beta | 含义 |
|---|---|---|
| `lowvol_14d` | **−0.56** | 一半以上的收益来自"做空了整个山寨市场", 不是选股 |
| `rangepos_14d` | −0.16 | 接近市场中性, alpha 基本是纯选股 |
| `mom_30d_skip1d` | −0.21 | (已不建议实盘用) |

`lowvol_14d` 的逻辑是"做多低波动币、做空高波动币", 而熊市里高波动币跌得更狠 ——
所以它天然带空头暴露。这在 IS/OOS 两段山寨熊市里都帮了大忙(alpha 扣完还剩 +72.7 / +101.6),
但 C 如果再套一层"70% 多 / 30% 空"的目标净暴露, **两层会叠加**, 实际净空头可能远超 30%。
> B 的建议(仅供参考, 决定权在 C): 先按 beta 折算组合层面的净暴露, 再决定要不要额外的方向性倾斜。
> ⚠️ 反过来, 如果 2026Q4 之后市场转牛, `beta = −0.56` 会从"帮忙"变成"拖累"。
> 这是当前策略最大的未知风险 —— 见下面 §4.1。

**陷阱 6 — `fee_drag_%/day` 是"每 4h 调一次仓"的口径, 和你的持有期未必一致。**
`factor_report_4h_*.csv` 里那一列 = `turnover × 0.001 × 6 × 100`, 假设每根 4h bar 都换一次手。
但 B 的评估持有期是 **1 天**(horizon=6), 对应的调仓频率是每天一次:

| 你的回测调仓频率 | rev_1d 的费用拖累 |
|---|---|
| 每 4h 一次 | 0.32%/天 ≈ **117%/年** |
| 每 1 天一次(与 B 的 horizon 一致) | 0.054%/天 ≈ **19.6%/年** |

**差 6 倍。** 本报告里所有"净 edge"数字用的都是第二种(1 天调仓)。
C 的成本模型自己算的时候, 记得先确认口径 —— 用错会让所有因子的净收益同时偏低, 而且不会报错。

### 4.1 一个必须写进 deck 的样本局限

IS(2024-10~2025-12)山寨等权 **−62.7%/年**, OOS(2026-01~) **−19.7%/年**,
同期 BTC 分别是 **+27.1%/年** 和 **−4.2%/年**。

**两段都是山寨熊市, 样本里没有一个牛市。** 所以"OOS 也通过了"只能证明
"这套选择规则不是对 IS 噪声过拟合", **不能证明"它在各种行情下都有效"**。
低波动类因子在山寨牛市里历史上是跑输的(牛市里垃圾币涨得最凶)。
这不是 B 能修的 —— 数据就这么多。但汇报时别说成"经过了牛熊验证"。

---

## 5. 谁负责什么 / 多久重跑一次

| 事项 | 归属 | 频率 |
|---|---|---|
| 下载 bar、维护 `cache/` | B | `python -m partB.dl`(需要时) |
| 算因子、选因子、出 `selected_IS.json` | B | **每月一次**(或季度切换时); 命令 `python -m partB.run_factors` |
| 实盘 BarFeed 维护 + 自检 | B | `python -m partB.live.barfeed --check`(部署后跑一次, 之后按需) |
| OI 增量收集(Binance 只留 30 天, 不存就没了) | B, 已进 cron | 每 4h 自动, 见 `deploy/setup.sh` |
| 衍生品(资金费率/基差)数据与评估 | B | 已跑完, **结论是不通过**, 见 `deriv_basis_verdict.md` |
| Tokenized stock 历史(**Binance**) | B | 已下完 21/21, **不建议做因子**(历史仅 83~115 天), 见 `tokenized_stock_data.md` |
| 代币 vs 标的跟踪检验 | B | **已完成**(重叠期 56~79 个交易日): 20/21 水平偏离中位 8bp、收益相关 0.94 |
| ML 挖因子(62 特征 → ridge/lasso/PCA/bagging) | B | **已跑完, 严格口径下 0 入选**; 判决与取舍表见 `ml_factor_verdict.md` |
| 合成分数 → 组合 → `Target` | **C** | 每个决策周期 |
| 含费回测、参数选择、Sharpe/Sortino/Calmar | **C** | 持续 |
| 仓位上限、单笔上限、回撤熔断、对账 | D | 每周期 |
| 签名、限频、下单、重试 | A | 每周期 |

`run_factors.py` 每次重跑会在 `partB/reports/` 刷新这些文件, C 想自己核数可以直接读:

| 文件 | 内容 |
|---|---|
| `factor_report_4h_{full,IS,OOS}.csv` | IC 均值 / IC_IR / 命中率 / 换手 / 费用拖累 |
| `factor_economic_IS.csv` | **选择依据**: 多空毛收益、beta、alpha、t 值、是否通过 |
| `factor_economic_OOS.csv` | 同一张表的 OOS 盲验版(按 IS 的方向定向) |
| `factor_corr_4h_IS.csv` | 因子两两相关阵 |
| `walkforward_quarterly_IC.csv` | 每因子逐季度 IC(看衰减用) |
| `walkforward_selection.csv` | 逐季度 walk-forward 选出的因子组 |
| `rev_smooth_and_economic_gate.md` | rev_1d 平滑方案的完整否决定论 + 经济门槛推导 |

下面这两份不是 `run_factors.py` 产出的, 是 B 单独跑的专题结论(C 不需要重跑, 看结论就行):

| 文件 | 内容 |
|---|---|
| `deriv_basis_verdict.md` | 资金费率/基差因子的**否决报告**(含"信号比手续费还小"的量化推导) |
| `tokenized_stock_data.md` | 21 个 tokenized stock 的数据来源(**Binance**)、跟踪检验、历史硬伤 |
| `stock_coverage.csv` | 每个股票对的覆盖率与 `is_enough` 硬结论(不够的别拿去做因子) |
| `stock_tracking_binance_vs_underlying.csv` | 代币 vs 真实标的的逐对跟踪检验数据 |
| `ml_factor_verdict.md` | **ML 挖因子判决**: 8 个模型各自的死法、ml_bag 的金融逻辑与取舍表 |
| `ml_factor_report.csv` | ML 因子的 IC/alpha/t/换手/各道门槛通过情况(机器可读版) |
| `ml_attribution_last_fold.csv` | ML 模型最后一折的逐特征权重(归因原始数据) |
| `ml_vs_handcrafted_corr.csv` | ML 因子 × 手工入选因子的相关阵 |
| `ml_selected.json` | ML 侧的交付 json; **当前 `selected` 是空列表**(见 §6) |

**C 每次进程启动都要重新读 json**, 不要 copy 因子名到策略代码里 ——
B 重跑之后 json 会变, 硬编码就会静默用旧因子。

---

## 6. B 目前**没有**交给 C 的东西(避免误会)

- **`rev_1d`(1 天反转)已被剔除**, 2026-10-04 起不再出现在 json 里。
  它以前是入选因子(权重 0.24), 现在过不了经济门槛: IS 多空**毛**收益 −17.6%/年,
  按 1 天持有期扣掉费用拖累 19.6%/年 → **净 −37.2%/年**; OOS 毛 −42.2%/年。
  C 提的"用 EWM 平滑降换手"方案 B 也试过并**否掉了**: 平滑确实是单调地"用 IC 换换手率",
  span=2 时净 edge 只从 −37.2%/年 改善到 −32.0%/年, 还是负的;
  而且 IS 和 OOS 两段都变差。**问题不是换手贵, 是这个因子在头尾分位没有 alpha。**
  完整数据和推导: `partB/reports/rev_smooth_and_economic_gate.md`。
  (代码仍然算得出 `rev_1d` —— `compute_all` 保留它, C 想做研究随时可用, 只是 B 不建议进组合。)
- **资金费率 / 基差因子: 数据已下全, 结论是"不通过", 不进 json。**
  2026-10-04 把永续 4h K 线（65/65 个币, 2024-10 → 2026-10, 25.5 万根）和资金费率
  （65/65, 18.8 万条结算）都下齐了, 按同一套四道门槛跑完:
  `funding_carry_3d` / `funding_chg_3d_7d` / `basis_level_1d` **三个全部卡在第一道统计门槛**
  （\|IC_IR_IS\| 都 < 0.05, `basis_level_1d` 的 IC_IS 只有 −0.0019, 基本是零）,
  经济门槛的 t 值也全在 ±1.4 以内。
  **失败是结构性的, 不是样本不够**: 基差在币与币之间的横截面离散度只有 std 0.072%（IQR 0.059%）,
  而同期 1 天收益率的横截面 std 是 3.39% —— 差 47 倍。换成钱: 一次完整多空换手 ≈ 0.2% 成本,
  而全体币全体日的 basis 分布 1%/99% 分位只有 −0.354% / +0.154%。
  **就算你能百分百确定宽基差会收敛, 能抓到的空间也比手续费小。**
  另外还有两个更硬的理由: ①Roostoo 不上市永续, "多现货空永续"那条腿根本不存在;
  ②比赛规则禁止套利, 基差收敛本质就是套利。
  完整推导、数据自检、以及下载时踩到的两个坑（Vision URL 多了 `/binance/`;
  BONK/FLOKI/PEPE/SHIB 的永续是 1000 倍合约, 不除乘数会让 basis 算成 +99900% 且**不报错**）:
  **`partB/reports/deriv_basis_verdict.md`**。
  代码和缓存都保留, C 想自己研究随时能跑 —— B 的结论是"不建议进组合", 不是"数据不许用"。
- **持仓量(OI)因子**: 历史只能从"开始收集那天"算起(cron 2026-10-04 上线,
  Binance 官方只保留最近 30 天), 短期内做不了回测。
- **Tokenized stock 因子(21 个): 数据下到了, 但没有一个因子基于它交付, 别拿去用。**
  Roostoo 上 NVDAB/USD、TSLAB/USD 这 21 对 7×24 可交易, 但 Roostoo **一根 K 线都不给**(FAQ Q18),
  官方数据包三个源全是 crypto。

  **数据源是 Binance, 不是 Yahoo。** 关键发现: `Roostoo NVDAB/USD ↔ Binance 现货 NVDABUSDT`,
  21 个**全部**一一对应(规则和 crypto 完全相同), 价格和 Roostoo ticker 差 0~23bp(TSLAB 完全相等)。
  这对上了 FAQ Q17「Roostoo real-time pricing is streamed from Binance」—— 那句话不只是说 crypto。
  已入库 `partB/cache/stocks_binance_1h_all.parquet`(50,315 行, 21/21, B0 同构, 过 `validate`)。
  Yahoo 那两份 `stocks_{1h,1d}_all.parquet` **降级为参照物**, 只用于下面第③点的检验, 不是数据源。

  仍然不建议做因子, 原因从"三个缺陷"收敛成**一个硬伤**:
  ①**历史太短: 最长 115 天, 最短 83 天**(这批代币 2026-06-11 ~ 07-13 才在 Binance 上市)。
  IS 窗口要求数据早于 2024-09-11, **21 个里 0 个够**。没有样本外, 任何"挖出来的规律"都无法验证,
  第 2、3 道门槛会因为"没有样本"而**假装通过**;
  ②因此也做不了季度 walk-forward —— 83~115 天连两个季度都凑不齐;
  ③代币跟不跟标的? **已实测: 跟。** 用 56~79 个交易日的重叠期, 取**同一时刻**(美股收盘 20:00/21:00 UTC)
  两边对比 —— 20 个可解释的交易对里, 水平偏离中位 **8bp**、日收益相关中位 **0.943**、对标的 beta 中位 0.92。
  日跟踪误差中位 153bp, 但其中**一半的绝对收益发生在美股休市时段**(代币真的 24/7 在动),
  这是 Yahoo 日线根本看不到的信息, 不是噪声。
  ⚠️ **SKHYB/USD 的检验数字不可解读**(比值 0.137、相关 0.16): 它的标的是韩股 `000660.KS`,
  06:30 UTC 收盘, 和美股收盘差 13.5 小时, 且 USD 换算口径不是 1:1。代币本身正常
  (194.96 vs Roostoo 195.25, 差 15bp)。已在代码里标 `ref_us_hours=False` 并从汇总剔除。

  **顺带解决了一个悬案**: 之前说"实盘怎么拿股票的 bar 没有方案(BarFeed 的源是 Binance,
  Binance 不上市这些股票)" —— **前提是错的**, Binance 上市了全部 21 个。
  `roostoo_to_binance("NVDAB/USD") → NVDABUSDT` 直接可用, BarFeed 的 warmup/轮询/重启恢复/
  质量校验/价差监控**一行都不用改**, 股票和 crypto 走完全相同的路径。
  所以如果 C 想把股票加进实盘宇宙, **管道是通的, 缺的只是研究依据**。

  完整分析、映射表、跟踪检验、SKHYB 说明: **`partB/reports/tokenized_stock_data.md`**;
  逐对检验数据: `partB/reports/stock_tracking_binance_vs_underlying.csv`;
  旧的 Yahoo 覆盖率表 `stock_coverage.csv` 已被取代, 留着做对比。
- **ML 挖出来的因子: 一个都没进 json, `ml_selected.json` 的 `selected` 是空列表 —— 这不是文件坏了。**
  62 个特征 × 4 个模型(ridge/lasso/PCA/bagging), walk-forward 逐季度重拟合, 出了 8 个 ML 因子,
  按**和手工因子完全相同的四道门槛**跑: 5 个过了 IS 三门槛, 然后全被最后两关挡下。
  新增了两道闸: **OOS 一票否决**(盲验段 alpha ≤ 0 就不许交) 和 **与手工入选因子联合去相关**
  (seed 挡马甲)。挡掉的典型: `ml_pc3` 的 IS alpha +41.7%/年 看着过线, OOS 却是 **−85%/年(t=−3.88)**,
  方向整个反过来 —— 这种因子进了组合就是埋雷。
  完整判决表、8 个模型各自的死法、以及 ml_bag 的取舍数据: **`partB/reports/ml_factor_verdict.md`**。
  ⚠️ **注意时效**: 那份报告里的数字是用 **62 个特征**跑出来的, 而特征池现在已经扩到 69/75 个
  (新增 `trend`、`age` 两个主题)。结论方向大概率不变, 但**具体数字要重跑 `ml_mine` 才作数**。
  你现在用的三个手工因子**不受影响** —— 它们不经过 ML 特征池。
- **1h / 1d 频率的因子选择**: 只做了 4h。想要别的频率找 B 重跑。
- **因子以外的信号**(链上、宏观、情绪): 没有。

---

## 7. 关于你现在手上这三个因子, B 必须说的三件事

`selected_IS.json` 现在是 `lowvol_14d` / `rangepos_14d` / `mom_30d_skip1d`。
选它们的时候只用了 IS, 这是规矩(不能拿 OOS 选, 那就不盲了)。但 OOS 跑完之后有三个事实
C 必须知道 —— **决定权在你, B 不替你改组合**:

### ① `mom_30d_skip1d` 在盲验段翻车了

| 因子 | alpha_IS | alpha_OOS(盲验) | beta_IS |
|---|---|---|---|
| `lowvol_14d` | +72.7%/年 (t 4.09) | +101.6%/年 (t 4.44) | −0.556 |
| `rangepos_14d` | +139.2%/年 (t 7.82) | +93.9%/年 (t 4.68) | −0.155 |
| `mom_30d_skip1d` | +93.1%/年 (t 4.98) | **−68.7%/年 (t −3.17)** | −0.214 |

按 IS 当初给的方向真实交易, 它在 OOS 是 **−68.7%/年**。数据在 `factor_economic_OOS.csv`。
砍掉、降权、还是留着赌它回归, 是你的决定 —— 但别当它还是 +93%/年 的那个因子。

### ② 三个因子的 beta **全是负的**, 净敞口要合并算

−0.556 / −0.155 / −0.214。这意味着这套因子**本身就偏做空山寨市场**
(样本期 2024-10~2025-12 山寨等权是 −62.7%/年, 做空高波动币自动赚钱, 经济门槛已经把
这部分作为 beta 扣掉了, 所以 alpha 是干净的)。

但对你有实际影响: 你按 70-30 设净多头敞口时, 这三个因子叠起来的**隐含空头 beta**
比你单独看每一个要大。要合并算, 不要各算各的。

### ③ `ml_bag` 可以补 `mom_30d_skip1d` 的位, 但它**不是同一种敞口**——先读这段再决定

它没进 `selected` 的唯一原因是与 `lowvol_14d` 相关 **0.517**, 超去相关线(0.5)差 **0.017**。
其余全过: OOS alpha **+92.5%/年 (t 4.16)**、Bonferroni 校正后仍显著(NW t 4.01 > 临界 2.73)、
IS 四个季度 IC 方向全一致(q_hit = 1.00), 且全部 7 个季度的 IC 都是正的(0.041~0.106)。

| | alpha_IS | alpha_OOS | beta_IS | 换手(每 bar) | 与 mom 相关 | 与 lowvol 相关 |
|---|---|---|---|---|---|---|
| `mom_30d_skip1d`(现用) | +93.1%/年 | **−68.7%/年** | −0.214 | 0.115 | 1.0 | — |
| `ml_bag`(候选) | +129.5%/年 | **+92.5%/年** | −0.425 | 0.314 | 0.270 | 0.517 |

**⚠️ 最关键的一条(researcher 独立审阅后的结论)**: ml_bag 实质是
**低波动 + 强 1 天反转**的混合体, **动量含量很低**。

B 把它的权重按 horizon 精确摊开算过(`ml_mine.py: increment_exposure()`, 不是手推):

| horizon | 净暴露(每根 bar) | 含义 |
|---|---|---|
| 最近 1 天 | **−0.126** | 涨得多就做空 —— 反转, 这是它最大的收益押注 |
| 2-7 天 | −0.028 | 反转延伸, 弱 4 倍 |
| 7-15 天 | +0.017 | **唯一的动量段**, 弱 7 倍 |
| 15-30 天 | **−0.0003 ≈ 0** | 30 天动量**净为零**(两个大权重精确抵消) |

所以: **用它替换 `mom_30d_skip1d`, 等于用反转敞口换动量敞口, 与"替换"的初衷相反。**
它和 mom 相关只有 0.270, 看着像"干净的替换", 但那个低相关恰恰是因为**它做的是反方向的赌注**。
如果你的组合本来就想加一条反转腿, 它很合适; 如果你想补回失去的动量, 它补不上。

其他取舍要点:
- 换手高约 3 倍(0.314 vs 0.115), 每 bar 的交易成本更高。
- 和 `lowvol_14d` 有一半重叠(0.517² ≈ 27% 共享方差)。**但这个 0.517 的抽样标准误约
  ±0.02~0.04, 统计上和 0.5 分不开** —— 门槛照原样切了(理由见 verdict 文档), 不代表
  它真的有 27% 是重复的。想判准, 该做的是**残差 alpha 检验**(ml_bag 多空收益对 lowvol
  多空收益回归看截距), B 还没做, 需要先补 ML 因子面板存盘。
- 归因只来自**最后一折**, 跨折符号一致率目前无法核验。结论要打折看。

**超参稳定性**(此前文档写错了, 更正): `ml_bag` 的正则强度是**固定的** `bag_lam = 300`,
不做逐折 CV, **不存在 λ 抖动**; 它的训练样本逐折单调增长(19,150 → 229,527), 最后一折
反而最稳。逐折从 5.6e3 抖到 3.2e6 的是 `ml_ridge`/`ml_lasso`(lasso 非零特征 25 → 1),
这两个都没入选。最后一折测试段只有 350 行是**评估样本少**, 不是训练样本少。
实盘仍建议每月重跑 `python -m partB.ml_mine`, 但理由是特征分布会漂, 不是参数不稳。

**要用它的话, 别 copy 因子名** —— 走和手工因子一样的宽表接口, B 会把它加进 json。
完整的机制排序、三重证据、以及那个还没解释的矛盾(`rev_1d` 单独用年亏 37%, 在 ml_bag 里
反转却是主要收益来源)都在 `ml_factor_verdict.md` 的"ml_bag 的金融逻辑"一节,
权重原始数据在 `ml_attribution_last_fold.csv`。
