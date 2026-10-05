# 交接给 A：BarFeed 还没接进主循环

> 写的人: B(数据/因子)。收的人: A(执行/主循环)。抄送: C(策略)、D(风控)。
> 日期: 2026-10-04。**B 不动 `partA/` 的代码**, 本文只说明现状、给出接法建议, 改不改、怎么改由 A 定。

---

## 1. 问题：bot 现在是"瞎"的

`partA/main.py` 的 `run_cycle()` 里，喂给策略的 `Context.bars` 是**硬编码的空字典**：

```python
ctx = Context(
    ts_ms=book.ts_ms or int(time.time() * 1000),
    tickers=tickers,
    bars={},                      # ← 这里
    equity_usd=book.equity_usd,
    positions={p.pair: p.qty for p in book.positions},
)
```

`main()` 里装的策略是 `MvpTickerMomentum()` —— 它只看 `ctx.tickers`，不看 `ctx.bars`，
所以**现在这样跑是通的，不会报错**。这正是危险的地方：问题被保底策略掩盖了。

`build_broker()` 在 live 分支返回 `(broker, None)`，`feed` 恒为 `None`；
`run_cycle` 里 `if feed is not None:` 那段是 paper 模式专用的 `SyntheticFeed`，
和 B 的 `partB.live.barfeed.BarFeed` **没有任何关系，两者不是同一个东西**。

**后果**: C 一旦把策略换成多因子(读 `selected_IS.json` + `partB.factors.compute_all`)，
它拿到的 `ctx.bars` 是空的 → 所有因子算不出来 → 要么全 NaN 不下单，要么直接抛异常。
而且是**静默的**：paper 模式下有 SyntheticFeed 兜着，本地测试发现不了。

---

## 2. B 这边已经就绪的部分

`partB/live/barfeed.py`，2026-10-04 全 65 币自检通过（`partB/reports/barfeed_healthcheck.log`）：

```
[1] warmup:    ready=True  123.9s  HTTP 65 次
[2] 重启恢复:   124.2s      HTTP 65 次   → 缓存命中, 没重下 40 天
[3] poll:      正常
[4] 数据质量:   62335 rows / 65 币, 缺口 0 / NaN 0 / 非正价格 0 / 重复键 0
[5] history(): 15535 rows, 最后一根已收盘 4h bar = 2026-10-04 04:00 UTC
[6] 价格偏离:   65/65 可比, 中位 0.0bp, 最大 6.9bp, 超阈值的 0 个
```

### 公开 API（A 只需要这四个 + 一个模块函数）

```python
from partB.live.barfeed import BarFeed, fetch_binance_prices, DRIFT_MAX_BP
from partB.data.binance_deriv import default_symbols

feed = BarFeed(default_symbols(), interval="4h")   # 缓存走默认 partB/cache/live/(绝对路径)

feed.warmup()          # -> bool  阻塞, 首次约 124s(65 币); 有缓存时也是这个量级
                       #          False = BTC/USD 基准缺失或数据不合格 → **不许出信号**
feed.poll()            # -> bool  增量补; True 表示出现了新的已收盘 4h bar
feed.history()         # -> DataFrame  schema 长表, 只含**已收盘** bar
feed.price_drift(tickers, binance_px=bn)   # -> {symbol: bp}  只返回超阈值的
```

关键约定：

- **`history()` 内部已经丢掉未收盘的 bar**，返回的最后一行就是"最近一根已收盘 4h bar"，
  C 直接用，**不需要再 shift**。（这是 B/C 文档里的红线 R1。）
- `poll()` 每币发 1 次 HTTP，65 币 = 65 次，走的是 **Binance 公开 REST，不占 Roostoo 的 30 次/分配额**。
- 脏数据（NaN / 非正价格 / high<low / 重复键）在 `fetch_klines` 内部就被 `validate()` 拒掉，
  **不会写进缓存**，只打一条 error 日志。所以一次网络抖动不会污染后续所有计算。

---

## 3. 建议的接法（A 决定）

最小改动，三处：

```python
# main() 里, live 分支
def build_broker(cfg):
    if cfg.env == "paper":
        return PaperBroker(initial_cash=50_000.0), SyntheticFeed(), None
    broker = LiveBroker(cfg.api_key, cfg.secret_key, cfg.base_url)
    feed = BarFeed(default_symbols(), interval="4h")
    if not feed.warmup():
        raise SystemExit("BarFeed warmup 失败: 数据不就绪, 拒绝启动")   # 别带病上线
    return broker, None, feed
```

```python
# run_cycle() 里, 拿 ticker 之后、建 Context 之前
if barfeed is not None:
    barfeed.poll()                       # 便宜的, 每周期调一次没问题
    bars = barfeed.history()
else:
    bars = {}                            # paper 保底策略不需要

# 价差监控: A 已经有 tickers, 顺手比一次
if barfeed is not None:
    bad = barfeed.price_drift(tickers, binance_px=fetch_binance_prices())
    if bad:
        log.error("价差异常 %s → 交 D 暂停这些 pair", bad)
        # 建议把 bad 放进 Context.meta, 让 D 的闸门读它, 而不是在这里直接改仓位

ctx = Context(ts_ms=..., tickers=tickers, bars=bars, equity_usd=..., positions=..., meta={...})
```

几个 A 需要注意的点：

1. **`warmup()` 是阻塞的，约 2 分钟。** `deploy/roostoo-bot.service` 是 `Type=simple`，
   systemd 不会因为启动慢判超时，但**这段时间内不要进 `run_cycle`**（现在按上面的写法自然不会进）。
2. **4h bar 只在 00/04/08/12/16/20 UTC 收盘。** 主循环如果每分钟跑一次，
   `poll()` 绝大多数时候返回 `False`（没有新 bar），这是正常的，不是故障。
   想省 HTTP 可以只在整点后调，但 65 次请求很便宜，不值得为它加状态机。
3. **`price_drift()` 必须跟 Binance 的实时价比**，不要图省事拿 `history()` 里最近一根的
   `close` 比 —— 那根 bar 最多可能是 59 分钟前的，山寨币这段时间动 70bp 属于正常波动。
   B 第一版就是这么写的，结果对 ENA/FET/NEAR/LISTA 报出 51~89bp 的**假偏离**，
   而它们与实时价的真实价差是 0.0/−4.3/+2.0/0.0bp。`fetch_binance_prices()` 一次调用拿全市场，
   已经处理好了符号映射的坑（必须只取 USDT 对，见该函数 docstring）。
4. **阈值现在是 `DRIFT_MAX_BP = 20.0`**（2026-10-04 从 50 收紧）。
   实测噪声地板约 7bp（两次 HTTP 调用之间的时间差，不是真价差），20bp 留了约 3 倍余量。
   D 如果要拿它做熔断，建议**连续两次超阈值才动作**，单次可能是抓取时差。
5. **重启恢复依赖 `partB/cache/live/`**。这个目录是 gitignore 的，EC2 上第一次部署会重新下载
   （约 2 分钟），之后每次重启都走缓存。别把 `cache_dir` 指到 `partB/cache/` 根目录 ——
   那里是 `dl.py` 的两年全历史研究缓存，**文件名相同**，BarFeed 的 47 天滚动窗口会把它截断，
   而 `dl.py` 见文件存在就不再重下 → 数据永久丢失。

---

## 4. 部署后请跑一次自检

```bash
cd ~/roostoo-bot && export PYTHONPATH=$PWD
.venv/bin/python -m partB.live.barfeed --check
```

六项全过会打印 `=== 自检结果: 全部通过 ✅ ===` 并 exit 0，否则 exit 1。
只读、不下单、不需要密钥，可以随便跑。日志参考: `partB/reports/barfeed_healthcheck.log`。

---

## 5. 归属

| | 归谁 |
|---|---|
| `partB/live/barfeed.py` 内部实现、数据质量、缓存策略、自检 | **B** |
| `partA/main.py` 里怎么构造 BarFeed、多久 poll 一次、bars 怎么进 Context | **A** |
| 拿到 bars 之后算因子、合成、出 Target | **C** |
| 价差异常要不要暂停交易、怎么暂停 | **D** |

B 不改 `partA/`。上面第 3 节的代码是**建议**，不是已提交的改动 ——
A 如果觉得 `build_broker` 返回值从二元组变三元组太脏，换成挂个属性或单独一个
`build_feed(cfg)` 都行，B 这边接口不变。

有问题找 B，或者直接看 `docs/partB_to_C_integration.md`（那份是给 C 的，但 §3 三条时序红线对 A 同样适用）。
