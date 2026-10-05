# Part B 交接说明（数据与因子）— v1, 2026-10-04

## 1. 目录
| 路径 | 内容 | 谁用 |
|---|---|---|
| `data/schema.py` | Bar schema 契约 + `validate()` + symbol 映射 | 全员 |
| `data/binance_vision.py` | 历史数据下载 / 1h→4h/1d 聚合 / 与官方逐根校验 | B, C |
| `factors.py` | 7 个横截面因子 + IC / 相关 / 选因子工具 | C |
| `live/barfeed.py` | 实盘 BarFeed + warmup 回填 + Roostoo ticker 降级源 + 价格偏离监控 | A（主循环）, D（偏离风控） |
| `evaluate.py`, `evaluate_extra.py` | 真实数据评估, 结果在 `reports/` | C, D |
| `bars_1h_all.parquet` | 65 币 × 1h, 2024-10-01 ~ 2026-10-02, 102.6 万行, 无缺口（不进 git, 用 dl.py 重建） | C |

安装: `pip install -r requirements.txt`；测试: `pytest -q`（6 项全过）。

## 2. Bar schema（B→C 契约）
长表，主键 `(ts, symbol)`，按 `ts, symbol` 排序。

| 列 | dtype | 说明 |
|---|---|---|
| ts | int64 | **bar 开盘时间**, UTC 毫秒。bar 覆盖 `[ts, ts+interval)` |
| symbol | string | Roostoo 格式 `BTC/USD` |
| open/high/low/close | float64 | |
| volume | float64 | 基础币成交量 |
| quote_volume | float64 | USD(T) 成交额 |
| n_trades | int64 | |
| is_final | bool | 已收盘=True。策略只能用 True |

例: 4h bar `ts=2026-10-03 08:00 UTC`（=HKT 16:00）覆盖 08:00–11:59:59 UTC，**12:00 UTC 之后**才可用。

## 3. 给 C：因子清单（4h bar，预测 1 天，IS=2024-10~2025-12，OOS=2026-01~）
| # | 因子 | 方向 | IC | IC_IR | IC_IS → IC_OOS | 季度 IC>0 | 每根调仓费用拖累 | 状态 |
|---|---|---|---|---|---|---|---|---|
| 1 | lowvol_14d | +1 | 0.085 | 0.29 | 0.084 → 0.088 | 9/9 | 0.02%/天 | ✅ 核心 |
| 2 | rev_1d | +1 | 0.027 | 0.12 | 0.031 → 0.020 | 8/9 | **0.32%/天** | ✅ 但必须降换手 |
| 3 | rangepos_14d | +1 | 0.015 | 0.08 | 0.018 → 0.010 | 6/9 | 0.15%/天 | ✅ 弱 |
| 4 | lowbeta_30d | +1 | 0.036 | 0.11 | 0.051 → 0.013 | 9/9 | 0.02%/天 | ⚠️ 备选：与 #1 相关 0.55，OOS 衰减 |
| 5 | mom_30d_skip1d | — | ≈0 | ≈0 | +0.015 → −0.029 | 5/9 | | ❌ 不建议，方向翻转 |

严格规则（方向 IS/OOS 一致 + |IC_IR|≥0.05 + 两两相关≤0.5）只有 **3 个**通过；不凑数。
淘汰: mom_7d、volshock_1d_14d（IC≈0）。完整表: `reports/factor_report_{1h,4h,1d}.csv`, `factor_corr_4h.csv`。

**C 必读**
1. 因子在 `ts` 行 → 只能用于预测 `ts+interval` 之后的收益：回测 `weights = f.shift(1)` 或收盘后下单。
2. rev_1d 费用拖累远超 0.05%/天预算：用 EMA 平滑信号 / 日频调仓 / 设调仓阈值（权重变化 < x 不交易）。
3. lowvol 部分来自"新币上市后高波动下跌"；只用老币(41 只)仍 IC=0.069，效应成立但会弱一些。
4. rev_1d 与 rangepos 负相关(−0.38) → 合成时会部分抵消，建议各自单独回测再加权。

## 4. 给 A：BarFeed 接入
```python
from live.barfeed import BarFeed
feed = BarFeed(symbols, interval="4h", cache_dir="/opt/bot/cache")
assert feed.warmup()            # 启动/重启必调; False → 不出信号
while True:
    if feed.poll():             # 有新收盘 4h bar
        bars = feed.history()   # schema 长表 → 交给 C 的 Strategy
    time.sleep(60)
```
- 主源是 Binance 公共 K 线 (`data-api.binance.vision`)，**不占 Roostoo 30 calls/min**；与回测数据逐字段一致（已测）。
- warmup: 读本地 parquet 缓存 → REST 补齐至今 → 校验缺口；首启 40 天约 1 次请求/币，重启只补增量（秒级）。
- poll 频率: 每 60s 一次就够（4h 策略），每次 ~65 个请求打 Binance，不打 Roostoo。
- Roostoo 不在 Binance 的币: OMNI、TON 无数据 → 自动跳过。

## 5. 给 D：风控接口
- `feed.price_drift(roostoo_ticker, max_bp=50)` → Binance 收盘价与 Roostoo LastPrice 偏离 >50bp 的币，建议暂停该币下单。
- 流动性过滤: 过去 7 天平均日成交额 < $1M 的币因子为 NaN（当前 58/65 通过；被滤: 1000CHEEMS, BMT, EDEN, LISTA, MIRA, OPEN, STO）。
- `warmup()` 返回 False（BTC 无数据）时必须不交易。

## 6. 验收状态
| 项 | 结果 |
|---|---|
| 自建 4h vs 官方 4h 逐根一致 | ✅ 0 差异 |
| 无未来函数（截断数据 / 篡改未来两种测试，真实数据） | ✅ |
| 实盘源 = 回测源 | ✅ 逐字段一致 |
| warmup 后立刻有因子值、重启走缓存结果一致 | ✅ |
| top-5 低相关清单 | ✅ 3 个合格 + 2 个标注备选 |

## 7. 已知问题 / 下一步
- Binance 月度包每月初几天才发布 → 下载器已自动回退为逐日下载（之前曾漏掉 9 月, 已修）。
- 21 只 tokenized stocks 无历史，未纳入因子。
- 下一步: 加更多候选（资金费率、BTC 趋势过滤做净敞口开关）；与 C 一起做因子合成 + 降换手。
