# partB — 数据与因子（Team92-Laplace+ / Roostoo APAC Quant Hackathon）

> **这个分支（`b_data-&-factors`）只有 B 的东西**：数据管道、因子库、评估流水线、以及 B 写给
> A / C / 研究员的交接文档。A（执行）、C（策略回测）、D（风控审计）在各自的分支里，
> 本地磁盘上四个 part 是齐的，只是不提交到这里。
>
> `contracts/` 是四人冻结的接口层，B 自己要用（`example_for_c.py` 依赖 `contracts.targets.Target`），
> 所以整个保留。改动需四人同意。

## 1. B 的职责边界

**B 只交付四样东西**，不写策略、不写回测、不写评分：

1. 因子宽表 —— `partB.factors.compute_all(bars, interval)`
2. 选因子结果 —— `partB/reports/selected_IS.json`
3. 报告与判决记录 —— `partB/reports/*.md|csv`
4. 实盘 BarFeed —— `partB.live.barfeed`（A 的主循环、D 的风控都从这里拿 bar）

**C 只消费**：通过 partB 的公开函数 + 读 json。C 不改 `partB/factors.py`、不改 `partB/data/`。
选因子的规则与阈值只写在 `partB/run_factors.py`；怎么组合成仓位只在 partC。

C 接入前先读 `docs/partB_to_C_integration.md`，再跑 `python -m partB.example_for_c`。
该示例是**接口演示**（`to_targets()` 里的 K / 权重 / 阈值全是占位值），不是策略，C 自己重写组合逻辑。
json 每次进程启动都要重读，**不要把因子名硬编码进策略**——B 每月重跑后 json 会变。

## 2. 数据流

```
partB/dl.py            ──> partB/cache/bars_1h_all.parquet      65 币 1h, 无缺口
partB/data/binance_deriv.py ──> partB/cache/deriv/               funding / OI / 基差（免密钥）
partB/run_factors.py   ──> partB/reports/* + selected_IS.json    P0 评估流水线（B 的最终交付）
partB/ml_mine.py       ──> partB/reports/ml_selected.json        ML 挖因子（walk-forward + 四道门槛）
partB/live/barfeed.py  ──> 实盘 bar（warmup / 缓存恢复 / 质量监控）  → A 主循环、D 风控
```

评估口径（全部在 IS 内判定，OOS 只做盲验）：
固定切分 **2026-01-01** 之前为 IS；**四道门槛**依次是
`|IC_IR| ≥ 0.05` → 季度 IC>0 占比 ≥ 2/3 → **经济门槛**（多空组合对市场等权回归后 `alpha > 0` 且 `t_alpha ≥ 2`）
→ 与已选因子相关 ≤ 0.5。

经济门槛是 2026-10-04 加的：`rev_1d` 的 IC_IR_IS=0.133 排全场第二，但多空组合 IS 净亏 37%/年 ——
统计门槛过了、经济门槛没过 → 已剔除。推导见 `partB/reports/rev_smooth_and_economic_gate.md`。

## 3. 文件地图

### partB/ 根

| 文件 | 内容 |
|---|---|
| `data/schema.py` | Bar schema 契约 + validate + symbol 映射 |
| `data/binance_vision.py` | 下载 / 1h→4h 聚合 / 与官方逐根校验（含月度包缺失→逐日回退修复） |
| `data/binance_deriv.py` | funding / OI / 基差下载器（免密钥；OI 仅 30 天 → EC2 cron 增量收集；1000 倍合约已处理） |
| `data/stock_binance.py` | **21 个 tokenized stock 的数据源**（Binance Vision，免密钥）。`Roostoo NVDAB/USD ↔ Binance NVDABUSDT` 一一对应，价格差 0~23bp；含 `--track` 代币 vs 标的跟踪检验。**结论是不建议做因子**（历史仅 83~115 天），见 `reports/tokenized_stock_data.md` |
| `data/stock_yahoo.py` | 标的股票历史（Yahoo）。**已降级为参照物**，只给 `stock_binance --track` 当对比基准，不是数据源 |
| `dl.py` | 全量重建 65 币 1h parquet |
| `live/barfeed.py` | 实盘 BarFeed + warmup + 缓存恢复 + 数据质量 + 价格偏离监控；`--check` 跑六项自检 |
| `factors.py` | 7 个手工因子 + IC / 相关 / 选因子工具 + `ls_alpha` 经济门槛（trailing-only，防未来函数） |
| `factors_deriv.py` | 衍生品因子（funding carry / 拥挤变化 / 基差），日频、结算时刻防未来 |
| `evaluate_deriv.py` | 衍生品因子评估（同 P0 口径；准入判定 + OOS 盲验）。**结论：三个全部不通过** |
| `evaluate_extra.py` | 稳健性（老币子样本 / 未来函数篡改测试） |
| `run_factors.py` | **P0 评估流水线**：65 币、IS 内选因子（四道门槛）、固定切分 2026-01-01、walk-forward；不含回测 |
| `ml_features.py` | **ML 特征池**：75 个原始特征 / 9 个经济主题（ret·vol·liq·shape·auto·rel·trend·age·fund），全部 trailing；`--selftest` 做未来函数自检。**改了这个文件必须跑 selftest** |
| `ml_mine.py` | **ML 挖因子**：ridge / lasso / PCA / bagging 四个模型，纯 numpy **不装新依赖**；逐季度 walk-forward 重拟合，Newey-West 修正重叠标签的 t 值，Bonferroni 校正多重检验；**OOS 一票否决**（盲验段 alpha≤0 不许交出）+ **与手工入选因子联合去相关**（seed 挡马甲）；`increment_exposure()` 把逐特征权重按恒等式 `r_K − r_K_skip6 = r_6` 摊成逐 horizon 净暴露（**归因必须用它，不能直接读权重表**） |
| `example_for_c.py` | **B→C 接入示例**（接口演示，非策略）：json→bars→因子→合成分数→Target 形状 |
| `sample_3sym.py` | 3 币样本 4h 构建（引擎冒烟用） |
| `run_tests.py` | B 的测试入口（`test_partB.py` / `test_live.py` / `test_ml.py`） |
| `test_ml.py` | ML 侧单元测试（特征无未来函数、模型可复现、标签无泄漏、退化截面不被放大成假信号、归因恒等式） |

### partB/reports/（交付物 + 判决记录）

| 文件 | 内容 |
|---|---|
| `selected_IS.json` | **B 给 C 的主交付**：ic_ir_weights / ic_sign_is / economic_is / economic_oos_blind / walk_forward.live_selected |
| `factor_report_4h{,_IS,_OOS,_full}.csv` | 手工因子 IC 报告（全样本 / IS / OOS 盲验） |
| `factor_economic_{IS,OOS}.csv` | 经济门槛明细（多空组合对市场等权回归的 alpha 与 t） |
| `factor_corr_4h{,_IS}.csv` | 因子相关阵（第四道门槛用） |
| `walkforward_{selection,quarterly_IC}.csv` | 逐季度 walk-forward 选择与 IC |
| `rev_smooth_and_economic_gate.md` | **经济门槛的推导记录**：rev_1d 为什么被否 |
| `deriv_basis_verdict.md` | **衍生品因子否决记录**（funding carry / 拥挤变化 / 基差） |
| `tokenized_stock_data.md` | **股票代币数据结论**：源改 Binance，但不建议做因子 |
| `ml_selected.json` + `ml_factor_report.csv` | ML 挖因子结果与四道门槛明细 |
| `ml_factor_verdict.md` | **ML 判决**：8 个模型逐个的死法 + ml_bag 的金融逻辑与 horizon 归因 |
| `ml_attribution_last_fold.csv` | 模型在用哪些特征（⚠️ 只持久化了最后一折） |
| `ml_walkforward_log.csv` / `ml_quarterly_IC.csv` / `ml_vs_handcrafted_corr.csv` | ML 的逐折日志、季度 IC、与手工因子的相关 |

### partB/cache/（parquet，gitignore，不入库）

`bars_1h_all.parquet`（65 币 1h，主数据）· 3 币样本 · `deriv/`（funding、65 币永续 4h）
· `stocks_binance_1h_all.parquet`（**21 只代币，主数据源**）· `stocks_{1h,1d}_all.parquet`（Yahoo 标的，仅作参照）

### docs/（B 写的交接文档）

| 文件 | 读者 |
|---|---|
| `partB_to_C_integration.md` | **C 必读**：接口、成本口径陷阱、因子现状 |
| `handoff_to_A_barfeed.md` | **A 必读**：BarFeed 怎么接进主循环（目前还没接） |
| `handoff_to_researcher.md` | 研究员：ML 流水线现状 + 待审问题 |
| `partB_handoff_v1.md` | B 的第一版交接 |

### contracts/（四人冻结，不属于 B 但要一起编译）

| 文件 | 内容 |
|---|---|
| `bars.py` (B0) | Bar 列契约 + `Ticker`（实时盘口：mid/spread/买一卖一） |
| `targets.py` (C0) | `Target(pair, side, weight, reason, urgency)`、`Context`、`Strategy` ABC |
| `broker.py` (A0) / `orders.py` (D0) | `Broker` Protocol；`OrderIntent/OrderResult/Position/BookState` |

`contracts/__init__.py` 一次性 import 四个模块，**不能只留 `bars.py`** —— 拆开会让所有
`import contracts` 的代码（含本地 A/C/D）直接失败。

## 4. 常用命令

```bash
export PYTHONPATH=$PWD                    # Windows: set PYTHONPATH=%CD%

python -m partB.run_tests                 # B 的全部测试（pytest 未装，用自带入口）
python -m partB.run_factors               # P0 评估流水线（65 币，约 2 分钟）→ 重写 selected_IS.json
python -m partB.example_for_c             # B→C 接入示例（离线；--live 走 BarFeed）
python -m partB.live.barfeed --check      # BarFeed 六项自检（warmup/缓存恢复/poll/质量/history/价差）
python -m partB.data.binance_deriv --what oi          # 手动补一次 OI（EC2 上 cron 每 4h 自动跑）
python -m partB.data.binance_deriv --what basis --start 2024-10   # 补永续 4h（断点续传，--force 重下）
python -m partB.evaluate_deriv            # 衍生品因子评估（结论：三个全部不通过）
python -m partB.data.stock_binance        # 下 21 只 tokenized stock（约 2 分钟；合并写，重跑即补齐）
python -m partB.data.stock_binance --track  # 代币 vs 标的跟踪检验（20/21 水平偏离中位 8bp）
python -m partB.ml_features --selftest    # ML 特征池的未来函数自检（改了 ml_features.py 必须跑）
python -m partB.ml_mine                   # ML 挖因子全流程（walk-forward，约 10~20 分钟）→ ml_selected.json
```

依赖：`requests / pandas / numpy / python-dotenv / pyarrow / polars`（见 `requirements.txt`）。
**没有 sklearn、没有 lightgbm** —— `ml_mine.py` 的四个模型全是 numpy 手写的，这是刻意的：
项目规矩是每加一个库先问，而且以现在的样本量（65 币 × 2 年 4h，独立信息量远小于行数）
树模型最容易干的事就是把噪声背下来。

## 5. 红线

- 密钥只进 `.env`（本仓库开源，`git status` 必查；`docs/refs/` 下的凭据邮件已 gitignore）。
- 因子只用 `is_final=True` 的 bar；信号 shift 一根再用。
- 所有特征必须 trailing —— 改了 `ml_features.py` 就跑 `--selftest`。
- 选因子只在 IS 内判定，OOS 只做一次盲验；**OOS 用过就是用过，不许拿它调参**。
- 门槛的值不看结果改：要改必须给出一个"在看到结果之前就成立"的论证。

## 6. 已知瑕疵（别被报告里的数字骗到）

- `ml_factor_verdict.md` 等 ML 报告里的数字是用 **62 个特征**跑出来的；当前代码已是 **75 个**
  （新增 trend / age / max_ret / rs_6 / rs_18）。要新数字必须重跑 `python -m partB.ml_mine`。
  **手工三因子不受影响**（它们走 `run_factors.py`，与 ML 特征池无关）。
- `ml_attribution_last_fold.csv` 只有最后一折的权重，跨折符号一致性目前**无法验证**。
- `partB/run_cache.py` 里有一个硬编码的 Windows 绝对路径，换机器要改。
