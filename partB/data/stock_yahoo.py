"""Tokenized stock 历史 K 线下载器（Yahoo Finance，免密钥）。

为什么要这个文件
----------------
Roostoo 上 88 个交易对里有 21 个是 tokenized stock（NVDAB/USD 之类），它们 7×24 小时可交易。
但:
- Roostoo **完全不提供 OHLCV**（FAQ Q18: 只有 ticker 快照, 没有 K 线）;
- 官方数据包 `docs/refs/DATA_SOURCES.md` 三个源全是 crypto, 没有股票;
- FAQ Q16 明确允许用别的行情 API（"Can we use other APIs for market data? Ans: Yes."）。

所以只能自己找股票源。实测（2026-10-04）:
- Stooq: 返回一段 JS 工作量证明挑战页, 拿不到 CSV → 不可用;
- Yahoo Finance chart API: 免密钥可用, 但**必须带 User-Agent**, 且**必须带 `events=div,split`**,
  少了后者会稳定 HTTP 422（这个坑很容易误判成"Yahoo 封了我们"）。

⚠️ 这份数据有三个先天缺陷，用之前先读 `partB/reports/stock_coverage.csv`
--------------------------------------------------------------------------------
1. **只有美股盘中 bar**。Yahoo 的 1h 线只在美东 09:30–16:00 有（约 5–7 根/天, 一周 5 天）。
   而 Roostoo 的 tokenized stock 7×24 交易。铺到 4h 网格上覆盖率只有约 25%,
   剩下 75% 只能前向填充 —— 周五收盘到周一开盘之间有长达 65 小时的陈旧窗口。
   → 结论: 这批数据**撑不起 4h 横截面因子**, 只适合日频。
2. **历史长度不齐**。21 个里只有 16 个能覆盖 IS 窗口（2024-10 起）;
   CBRS（2026-05-14 起）/ SPCX（2026-06-12 起）/ CRCL（2025-06-05 起）基本只有 OOS 段,
   SNDK（2025-02-24）/ NBIS（2024-10-21）也只覆盖一部分。做不了 walk-forward 的季度门槛。
3. **代理质量未验证**。Yahoo 报的是**标的股票**的价, C 实际下单的是 Roostoo 上的**代币化凭证**。
   2026-10-04 的一次抽查里, 两者价位对不齐的幅度差很多: 大部分 ±50bp 内,
   CBRSB 差 +824bp、CRCLB +256bp、MSTRB +245bp、COINB +128bp, SKHYB 更是差了 7 倍。

   ⚠️ 但"价位对不齐"和"数据不能用"**不是一回事**, 别把这两件事混起来判:
   我们的因子（动量、低波、区间位置）全都是按**收益率**算的, 收益率对常数倍缩放不敏感。
   代币价 = 标的价 × 7 这种固定的比例差, 对因子值毫无影响; 真正致命的只有
   **跟踪漂移** —— 代币和标的各走各的, 涨跌幅都对不上。
   所以下面那几个大偏差里, 至少有三类完全不同的可能, 而且现有数据分不开:
     (a) 时间差: 抽查是拿 Yahoo **周五收盘**对 Roostoo **周日实时**, 中间隔了整个周末,
         标的根本没在更新 —— 这是噪声, 不是脱钩;
     (b) 固定的比例/折价: 代币按某个固定系数锚定 —— 对收益率型因子无害;
     (c) 真脱钩: 代币自己漂 —— 致命, 算出来的因子描述的不是 C 下单的那个东西。
   要把三者分开, 只能在**标的开盘时段**同时采两边、连着采几天看偏离是否稳定。
   `--sample-live` 就是干这个的, 见下面。

4. **SKHYB 还多一层汇率**。Yahoo 只有韩交所的 000660.KS, 报 KRW; 这里按 USDKRW=X 折成 USD。
   折算本身在概念上是对的（USD 计价的 SK 海力士敞口 = KRW 价 × 汇率）,
   但实测折出来是 $1371, 而 Roostoo 报 $195.25, 差 7.02 倍 —— 这个倍数目前**解释不了**。
   按上面第 3 条, 固定倍数不影响收益率型因子; 但在搞清楚之前别把它当已验证。

时间戳对齐（无未来函数）
------------------------
Yahoo 的 bar 时间戳是**美东开盘时刻**, 不是整点: 1h 线落在 XX:30, 日线落在 13:30/14:30 UTC
（KSE 的 000660.KS 落在 00:00 UTC）。B0 schema 规定 ts = bar 开盘时间, 且 bar 要等 ts+interval
之后才可用。直接把 XX:30 抹到 XX:00 会造成最多 30 分钟的未来函数（提前看到还没收盘的 bar）。

所以统一用:  ts_grid = ceil_to_grid(t0 + close_lag) - step
即"把这根 bar 放到第一个『按 schema 规则算出来已经收盘』的格子里"。
- 1h: bar [13:30,14:30) → close_lag=1h → ceil(14:30)=15:00 → ts=14:00。schema 说 15:00 可用,
  实际 14:30 就收盘了 → 保守 30 分钟, 安全。
- 1d: bar 开于 13:30 UTC, 美股 20:00 UTC 收盘 → close_lag=8h → ceil(21:30)=次日00:00 → ts=当日00:00。
  schema 说次日 00:00 可用, 实际当日 20:00 收盘 → 保守 4 小时, 安全。
宁可贵一点也不能偷看未来。

复权
----
因子是按收益率算的, 不复权的话拆股会被当成一次 -90% 的暴跌（NVDA 2024-06 十拆一）。
所以 open/high/low/close 一律乘 adjclose/close 的比例（同时含拆股和分红）。
`volume` 保持 Yahoo 原值（股数, 拆股后口径会变）, `quote_volume` 用**未复权**收盘价 × volume
（这才是真实成交额）。两个量的口径不同是故意的, 别拿它们相除当均价。

存储
----
  partB/cache/raw/stocks_yahoo_{SYM}_{1h,1d}.parquet  Yahoo 原样（未复权 + adjclose 列, ts 未对齐）
  partB/cache/stocks_1h_all.parquet                   B0 长表, 已复权已对齐, symbol 用 Roostoo 格式
  partB/cache/stocks_1d_all.parquet                   同上, 日频（推荐用这个）
  partB/reports/stock_coverage.csv                    每对的覆盖率 / 起止 / 是否够 IS 窗口
  partB/reports/stock_mapping_probe.json              Roostoo 报价 vs Yahoo 收盘的偏差抽查

用法
----
  python -m partB.data.stock_yahoo                  # 下全部 21 个, 1h + 1d
  python -m partB.data.stock_yahoo --only NVDAB/USD TSLAB/USD
  python -m partB.data.stock_yahoo --list           # 只看映射表, 不下载
  python -m partB.data.stock_yahoo --sample-live    # 采一次"代币 vs 标的"跟踪误差（开盘时段跑）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import time
import urllib.parse
import urllib.request

import pandas as pd

from .schema import BAR_COLUMNS, INTERVAL_MS, validate

HERE = pathlib.Path(__file__).resolve().parent.parent      # partB/
CACHE = HERE / "cache"
RAW = CACHE / "raw"
REPORTS = HERE / "reports"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
FX = "USDKRW=X"          # 000660.KS 是 KRW 计价, 需要换汇
POLITE = 0.4             # 秒; Yahoo 没有公开限频, 客气一点不容易被 429
RETRIES = 3

# Roostoo 交易对 → (Yahoo 代码, 备注)。21 个里 19 个就是去掉尾巴的 "B";
# 两个特例: MUB→MU（Micron 的 Roostoo 代码只留了两个字母）, SKHYB→000660.KS（SK 海力士在韩交所）。
MAPPING: dict[str, tuple[str, str]] = {
    "AMDB/USD":   ("AMD",       "AMD"),
    "CBRSB/USD":  ("CBRS",      "Cerebras; Yahoo 只有 2026-05-14 起 → 不够 IS"),
    "COINB/USD":  ("COIN",      "Coinbase"),
    "CRCLB/USD":  ("CRCL",      "Circle; Yahoo 只有 2025-06-05 起 → 不够 IS"),
    "GLWB/USD":   ("GLW",       "Corning"),
    "GOOGLB/USD": ("GOOGL",     "Alphabet A"),
    "INTCB/USD":  ("INTC",      "Intel"),
    "LITEB/USD":  ("LITE",      "Lumentum"),
    "METAB/USD":  ("META",      "Meta"),
    "MSFTB/USD":  ("MSFT",      "Microsoft"),
    "MSTRB/USD":  ("MSTR",      "MicroStrategy"),
    "MUB/USD":    ("MU",        "Micron —— 特例, 不是去掉 B"),
    "NBISB/USD":  ("NBIS",      "Nebius; Yahoo 只有 2024-10-21 起 → IS 缺头一个月"),
    "NVDAB/USD":  ("NVDA",      "Nvidia"),
    "PLTRB/USD":  ("PLTR",      "Palantir"),
    "QCOMB/USD":  ("QCOM",      "Qualcomm"),
    "SKHYB/USD":  ("000660.KS", "SK 海力士 —— Yahoo 报 KRW, 已按 USDKRW 折成 USD; "
                                "折出来 $1371 而 Roostoo 报 $195, 差 7 倍且解释不了, 用前先看 docstring 第 3/4 条"),
    "SNDKB/USD":  ("SNDK",      "SanDisk; Yahoo 只有 2025-02-24 起 → IS 缺前 5 个月"),
    "SPCXB/USD":  ("SPCX",      "SpaceX; Yahoo 只有 2026-06-12 起 → 不够 IS"),
    "TSLAB/USD":  ("TSLA",      "Tesla"),
    "WDCB/USD":   ("WDC",       "Western Digital"),
}
# IS 窗口起点（与 run_factors.py 的评估口径一致）, 用来判断历史够不够
IS_START = pd.Timestamp("2024-10-01", tz="UTC")
WARMUP_D = 20            # 最长因子回看（lowvol_14d / mom_30d 需要约 30 天, 留点余量）

# 见模块 docstring "时间戳对齐": bar 真正收盘相对 Yahoo 时间戳的滞后
CLOSE_LAG_MS = {"1h": 3_600_000, "1d": 8 * 3_600_000}


def _ceil(ts: pd.Series | int, step: int) -> pd.Series | int:
    return -((-ts) // step) * step


def _get_json(url: str) -> dict:
    """带重试的 GET。Yahoo 偶发 429/5xx, 也可能因为漏了 events 参数返回 422（那是配置错, 重试没用）。"""
    last = None
    for i in range(RETRIES):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 422:          # 参数错, 重试也没用
                raise
            last = e
        except Exception as e:         # noqa: BLE001 - 网络抖动一律重试
            last = e
        time.sleep(POLITE * (i + 2))
    raise RuntimeError(f"{url} 连续 {RETRIES} 次失败: {last}")


def fetch(symbol: str, interval: str, start: dt.datetime | None = None) -> pd.DataFrame:
    """拉一个 Yahoo 代码的 K 线, 返回未对齐的原始表 + adjclose 列。

    两个必须踩过的坑:
    - `events=div,split` 不能省: 省了 Yahoo 直接 422, 而且这个错看起来像被封号, 很好误导人。
    - 1h 和 1d 要用**不同**的取数方式。period1/period2 在 1h 上跨度正好 730 天就 422（729 天可以）,
      而且同样窗口只给 3484 根; 换成 `range=730d` 反而给 5097 根（Yahoo 的 range 数的是**交易日**
      不是自然日, 730d ≈ 1064 个自然日）。所以 1h 走 range, 1d 走 period（range=max 会莫名只返回 334 根）。
    """
    step = INTERVAL_MS[interval]
    now = dt.datetime.now(dt.timezone.utc)
    # 1h 顶多只能拿 730 天（Yahoo 的硬上限）; 日线不受这个限制, 默认多拿一点,
    # 否则 period1 = now-730d = 2024-10-04, 刚好卡在 IS 起点上, 减去 20 天预热就会被判"不够 IS"。
    default_days = 730 if interval == "1h" else 1200
    begin = start or (now - dt.timedelta(days=default_days))
    if isinstance(begin, dt.datetime) and begin.tzinfo is None:
        begin = begin.replace(tzinfo=dt.timezone.utc)
    if interval == "1h":
        days = min(max((now - begin).days, 1), 730)
        qs = urllib.parse.urlencode({"interval": interval, "range": f"{days}d", "events": "div,split"})
    else:
        qs = urllib.parse.urlencode({"interval": interval, "period1": int(begin.timestamp()),
                                     "period2": int(now.timestamp()) + 86_400, "events": "div,split"})
    res = _get_json(CHART + urllib.parse.quote(symbol) + "?" + qs)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    adj = res["indicators"].get("adjclose", [{}])[0].get("adjclose")
    df = pd.DataFrame({
        "ts_raw": [t * 1000 for t in res["timestamp"]],
        "open": q["open"], "high": q["high"], "low": q["low"],
        "close": q["close"], "volume": q["volume"],
        "adjclose": adj if adj else q["close"],
        "currency": res["meta"].get("currency"),
    })
    df = df.dropna(subset=["open", "high", "low", "close", "adjclose"])
    df = df[df.close > 0].reset_index(drop=True)
    if df.empty:
        return df
    # 复权因子: 拆股 + 分红一起吸收。个别行 close 为 0 已在上面剔掉, 不会除零。
    r = (df.adjclose / df.close).clip(lower=1e-6, upper=1e6)
    for c in ("open", "high", "low", "close"):
        df[c + "_adj"] = df[c] * r
    # Yahoo 自己偶尔吐出自相矛盾的 bar（实测 000660.KS 2024-10-14: low=186900 但 close=186000）。
    # 这类行 B0 的 validate() 会直接拒绝入库 —— 一条脏数据不该让整批 21 个标的都存不下来。
    # 修法取最小干预: 只把 high/low 夹到 open/close 的包络里, 不动 open/close（因子基本只用 close）。
    oc_lo = df[["open_adj", "close_adj"]].min(axis=1)
    oc_hi = df[["open_adj", "close_adj"]].max(axis=1)
    n_fix = int((df.low_adj > oc_lo).sum() + (df.high_adj < oc_hi).sum())
    if n_fix:
        df.attrs["ohlc_clamped"] = n_fix
    df["high_adj"] = df.high_adj.where(df.high_adj >= oc_hi, oc_hi)
    df["low_adj"] = df.low_adj.where(df.low_adj <= oc_lo, oc_lo)
    df["ts"] = _ceil(df.ts_raw + CLOSE_LAG_MS[interval], step) - step
    # Yahoo 偶尔在盘尾多吐一根零头 bar（例: 2026-10-02 同时给了 19:30 和 20:00 两根, 对齐后撞进
    # 同一个 20:00 格子）。B0 主键是 (ts, symbol), 撞了 validate() 会直接拒绝入库。
    # 保留 ts_raw 更大的那根 = 更晚、信息更全的一根; 不能保留第一根, 否则等于把收盘价丢掉。
    n0 = len(df)
    df = df.sort_values("ts_raw").drop_duplicates("ts", keep="last").reset_index(drop=True)
    if len(df) < n0:
        df.attrs["dedup"] = n0 - len(df)
    return df


def fetch_fx(interval: str, start: dt.datetime | None = None) -> pd.DataFrame:
    """USDKRW 汇率（给 000660.KS 换汇用）。Yahoo 的汇率序列有洞, 前向填充后再用。"""
    df = fetch(FX, interval, start)
    if df.empty:
        raise RuntimeError("拿不到 USDKRW=X, 无法给 SKHYB 换汇")
    return df[["ts", "close"]].rename(columns={"close": "usdkrw"}).sort_values("ts").reset_index(drop=True)


def to_bar_table(df: pd.DataFrame, pair: str) -> pd.DataFrame:
    """原始表 → B0 契约长表。quote_volume 用**未复权**价 × 原始股数 = 真实成交额。"""
    out = pd.DataFrame({
        "ts": df.ts.astype("int64"),
        "symbol": pair,
        "open": df.open_adj, "high": df.high_adj, "low": df.low_adj, "close": df.close_adj,
        "volume": df.volume.fillna(0.0),
        "quote_volume": (df.close * df.volume).fillna(0.0),
        "n_trades": 0,                    # Yahoo 不提供成交笔数; 0 表示"未知", 不是"没有成交"
        "is_final": True,
    })
    return out[list(BAR_COLUMNS)]


def download(pairs: list[str], intervals: tuple[str, ...] = ("1h", "1d"),
             start: dt.datetime | None = None) -> dict[str, pd.DataFrame]:
    """下载 pairs 的所有 interval, 返回 {interval: 合并后的长表}。SKHYB 在这里换汇。"""
    RAW.mkdir(parents=True, exist_ok=True)
    need_fx = any(MAPPING[p][0] == "000660.KS" for p in pairs)
    out: dict[str, pd.DataFrame] = {}
    for iv in intervals:
        fx = fetch_fx(iv, start) if need_fx else None
        frames = []
        for pair in pairs:
            ysym, note = MAPPING[pair]
            try:
                df = fetch(ysym, iv, start)
            except Exception as e:                          # noqa: BLE001
                print(f"  ✗ {pair:12s} {ysym:10s} 失败: {e}")
                continue
            if df.empty:
                print(f"  ✗ {pair:12s} {ysym:10s} 返回空")
                continue
            df.to_parquet(RAW / f"stocks_yahoo_{ysym.replace('=','_').replace('.','_')}_{iv}.parquet", index=False)
            if ysym == "000660.KS":
                # 汇率序列比股票序列洞多, 先 merge_asof 到股票时间轴上再 ffill。
                # 直接逐行相除会因为两边时间戳对不齐而静默产生 NaN, 然后被 dropna 吃掉整段历史。
                # 按 ts_raw 做索引对齐, 不用 .values —— 位置对齐一旦排序变了就会静默错配。
                m = pd.merge_asof(pd.DataFrame({"ts_raw": df.ts_raw, "ts": df.ts}).sort_values("ts"),
                                  fx.sort_values("ts"), on="ts", direction="backward")
                rate = (m.set_index("ts_raw").usdkrw.ffill().bfill()
                        .reindex(df.ts_raw.values).to_numpy())
                if pd.isna(rate).any():
                    print(f"  ! {pair}: USDKRW 有 {int(pd.isna(rate).sum())} 个缺口无法填充")
                for c in ("open_adj", "high_adj", "low_adj", "close_adj"):
                    df[c] = df[c] / rate
                df["close"] = df["close"] / rate
            frames.append(to_bar_table(df, pair))
            d0 = pd.to_datetime(df.ts.min(), unit="ms", utc=True).date()
            d1 = pd.to_datetime(df.ts.max(), unit="ms", utc=True).date()
            flag = "" if pd.Timestamp(df.ts.min(), unit="ms", tz="UTC") <= IS_START else "  ⚠️ 不够 IS"
            fix = (f"  [修了 {df.attrs['ohlc_clamped']} 根 high/low]" if df.attrs.get("ohlc_clamped") else "")
            ded = (f"  [去了 {df.attrs['dedup']} 根重复]" if df.attrs.get("dedup") else "")
            print(f"  ✓ {pair:12s} {ysym:10s} {len(df):5d} 根 {d0} → {d1}{flag}{fix}{ded}")
            time.sleep(POLITE)
        if frames:
            out[iv] = validate(pd.concat(frames, ignore_index=True), iv)
    return out


ROOSTOO_TICKER = "https://mock-api.roostoo.com/v3/ticker"
TRACK_CSV = CACHE / "live" / "stock_tracking.csv"


def _yahoo_last(symbol: str) -> tuple[float, str, int]:
    """Yahoo 最近一根 1m bar 的收盘价（拿它当"现价"用）。返回 (price, currency, ts_ms)。"""
    qs = urllib.parse.urlencode({"interval": "1m", "range": "1d", "events": "div,split"})
    res = _get_json(CHART + urllib.parse.quote(symbol) + "?" + qs)["chart"]["result"][0]
    c = res["indicators"]["quote"][0]["close"]
    ts = res["timestamp"]
    for i in range(len(c) - 1, -1, -1):          # 尾部常有 None（还没成交的分钟）
        if c[i] is not None:
            return float(c[i]), res["meta"].get("currency", ""), int(ts[i]) * 1000
    raise RuntimeError(f"{symbol}: 今天一根 1m bar 都没有")


def _roostoo_ticker() -> dict:
    d = _get_json(ROOSTOO_TICKER + f"?timestamp={int(time.time() * 1000)}")
    if not d.get("Success"):                     # Roostoo 失败也返回 HTTP 200
        raise RuntimeError(d.get("ErrMsg"))
    return d["Data"]


def sample_live(pairs: list[str] | None = None) -> pd.DataFrame:
    """同时采 Roostoo 报价和 Yahoo 现价, 量"代币 vs 标的"的真实偏离, 追加进 stock_tracking.csv。

    为什么必须有这个: 模块 docstring 第 3 条里那次抽查（CBRSB +824bp 之类）是拿 Yahoo **周五收盘**
    对 Roostoo **周日实时**价, 中间隔了整个周末 —— 分不清是代币真脱钩了, 还是标的压根没在更新。
    只有在**标的开盘时段**同时采两边, 得到的 bp 才是真正的跟踪误差。
    这个函数就是把这件事做成可重复的采样: 美股开盘时（UTC 13:30–20:00, 周一到周五）挂个 cron
    每 15 分钟跑一次, 攒几天就有分布了, 那时再决定哪些对能用。
    """
    pairs = pairs or sorted(MAPPING)
    tick = _roostoo_ticker()
    rows, fx = [], None
    for pair in pairs:
        ysym, _ = MAPPING[pair]
        if pair not in tick:
            print(f"  ! {pair}: Roostoo ticker 里没有, 跳过")
            continue
        r = tick[pair]
        try:
            yp, ycur, yts = _yahoo_last(ysym)
        except Exception as e:                              # noqa: BLE001
            print(f"  ! {pair} ({ysym}): Yahoo 取价失败 {e}")
            continue
        if ycur and ycur != "USD":                          # 只有 000660.KS 会走到这
            if fx is None:
                fx = _yahoo_last(FX)[0]
            yp_usd, note = yp / fx, f"KRW/{fx:.2f}"
        else:
            yp_usd, note = yp, ""
        rp = float(r.get("LastPrice") or 0.0)
        now = dt.datetime.now(dt.timezone.utc)
        rows.append({
            "sampled_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pair": pair, "yahoo": ysym, "roostoo_last": rp,
            "yahoo_last_usd": yp_usd, "yahoo_currency": ycur, "fx_note": note,
            "div_bp": (rp / yp_usd - 1.0) * 1e4 if yp_usd else float("nan"),
            "yahoo_age_min": (now.timestamp() * 1000 - yts) / 60_000,
            # 粗略的开盘判定, 只为给样本打标: 美股 13:30–21:00 UTC, 韩股 00:00–06:30 UTC, 都只在工作日。
            # 不精确（没算美国节假日/夏令时切换的那一小时）, 但足够把"周末样本"和"盘中样本"分开 ——
            # 而这两类样本的偏离完全不可比, 混在一起算中位数是没有意义的。
            "market_open": bool(now.weekday() < 5 and (
                (13 <= now.hour < 21 and ysym != "000660.KS") or (0 <= now.hour < 7 and ysym == "000660.KS"))),
            "roostoo_bid": r.get("MaxBid"), "roostoo_ask": r.get("MinAsk"),
            "roostoo_unit_trade_value": r.get("UnitTradeValue"),
        })
        time.sleep(POLITE)
    if not rows:
        raise SystemExit("一个样本都没采到")
    df = pd.DataFrame(rows)
    TRACK_CSV.parent.mkdir(parents=True, exist_ok=True)
    old = pd.read_csv(TRACK_CSV) if TRACK_CSV.exists() else None
    df.to_csv(TRACK_CSV, mode="a", header=old is None, index=False, encoding="utf-8-sig")
    both = (old is not None and len(old)) or 0
    print(f"追加 {len(df)} 行到 {TRACK_CSV}（此前已有 {both} 行）")
    op = df[df.market_open]
    print(f"\n本次样本: 开盘时段 {len(op)} 个 / 休市 {len(df) - len(op)} 个")
    for tag, g in [("开盘", op), ("休市", df[~df.market_open])]:
        if len(g):
            print(f"  {tag}: |偏离| 中位 {g.div_bp.abs().median():7.1f}bp  最大 {g.div_bp.abs().max():8.1f}bp"
                  f"  >50bp 的 {int((g.div_bp.abs() > 50).sum())} 个")
    print("\n偏离最大的 6 个:")
    print(df.reindex(df.div_bp.abs().sort_values(ascending=False).index)
          .head(6)[["pair", "yahoo", "roostoo_last", "yahoo_last_usd", "div_bp", "market_open"]]
          .to_string(index=False))
    return df


def coverage_report(bars: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """每对一行: 各频率的行数/起止/跨度, 以及"够不够 IS 窗口"的判定。

    `is_enough` 是**硬结论**, C 拿到 False 的行就不该拿它去跑 walk-forward ——
    历史比 IS 起点还晚, 季度命中率的门槛会因为没有样本而假装通过。
    """
    base = bars.get("1d", bars.get("1h"))
    rows = []
    for pair in sorted(MAPPING):
        ysym, note = MAPPING[pair]
        r = {"pair": pair, "yahoo": ysym, "note": note}
        for iv, df in bars.items():
            s = df[df.symbol == pair]
            r[f"n_{iv}"] = len(s)
            if len(s):
                r[f"first_{iv}"] = str(pd.to_datetime(s.ts.min(), unit="ms", utc=True).date())
                r[f"last_{iv}"] = str(pd.to_datetime(s.ts.max(), unit="ms", utc=True).date())
        if base is None or not len(base[base.symbol == pair]):
            r.update(is_enough=False, need_from=str(IS_START.date()), reason="下载失败")
        else:
            s = base[base.symbol == pair]
            first = pd.Timestamp(s.ts.min(), unit="ms", tz="UTC")
            need = IS_START - pd.Timedelta(days=WARMUP_D)
            r.update(is_enough=bool(first <= need), need_from=str(need.date()),
                     reason="" if first <= need else f"历史从 {first.date()} 才开始, 晚于 {need.date()}")
        rows.append(r)
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="下载 21 个 tokenized stock 的历史 K 线（Yahoo Finance）")
    ap.add_argument("--only", nargs="+", default=None, help="只下这些 Roostoo 交易对, 例如 NVDAB/USD")
    ap.add_argument("--start", default=None, help="起始日 YYYY-MM-DD, 默认往前 730 天（Yahoo 1h 的上限）")
    ap.add_argument("--intervals", nargs="+", default=["1h", "1d"])
    ap.add_argument("--list", action="store_true", help="只打印映射表就退出")
    ap.add_argument("--sample-live", action="store_true",
                    help="不下历史; 同时采 Roostoo 报价和 Yahoo 现价, 量代币对标的的跟踪误差并追加到 CSV。"
                         "开盘时段跑才有意义（见 sample_live 的 docstring）")
    a = ap.parse_args()

    if a.list:
        for p, (y, n) in sorted(MAPPING.items()):
            print(f"{p:12s} → {y:10s} {n}")
        return

    pairs = a.only or sorted(MAPPING)
    bad = [p for p in pairs if p not in MAPPING]
    if bad:
        raise SystemExit(f"不认识的交易对 {bad}。可用的见 --list")
    if a.sample_live:
        sample_live(pairs)
        return
    start = dt.datetime.strptime(a.start, "%Y-%m-%d") if a.start else None

    print(f"下载 {len(pairs)} 个 tokenized stock × {a.intervals}（Yahoo Finance）")
    bars = download(pairs, tuple(a.intervals), start)
    if not bars:
        raise SystemExit("一个都没下下来")

    for iv, df in bars.items():
        p = CACHE / f"stocks_{iv}_all.parquet"
        df.to_parquet(p, index=False)
        print(f"\n写入 {p.name}: {len(df)} 行, {df.symbol.nunique()} 个标的")

    rep = coverage_report(bars)
    REPORTS.mkdir(parents=True, exist_ok=True)
    rep.to_csv(REPORTS / "stock_coverage.csv", index=False, encoding="utf-8-sig")
    pd.set_option("display.width", 250)
    print("\n=== 覆盖率 ===\n", rep.drop(columns=["note"]).to_string(index=False))
    print("\n够 IS 窗口的:", rep.pair[rep.is_enough].tolist())
    print("不够的（别拿去做因子）:", rep.pair[~rep.is_enough].tolist())
    print("\n⚠️ 提醒: Yahoo 只有美股盘中 bar, 铺到 4h 网格覆盖率约 25%; 日频才可用。"
          "\n⚠️ 提醒: 这是**标的股票**的价, 不是 Roostoo 代币的价, 两者可能脱钩 —— 见模块 docstring 第 3 条。")


if __name__ == "__main__":
    main()
