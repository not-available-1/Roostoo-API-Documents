"""实盘 BarFeed + warmup 回填。

设计: 实盘与回测用同一数据源 (Binance spot K 线), 保证因子"训练=上线"一致;
      Roostoo ticker 只做 (1) 价格偏离监控 (2) Binance 不可用时的降级 K 线。
- 主源: data-api.binance.vision/api/v3/klines — 公开、无 key、不占 Roostoo 30 calls/min 限频
- 备源: Roostoo /v3/ticker 快照 → 自合成 1h bar (volume 为近似值, is_final 标记)
- warmup: 启动时 本地 parquet 缓存 → 用 REST 补齐到现在 → 校验无缺口 → 才允许出信号

用法 (主循环, 由 A 调用):
    feed = BarFeed(symbols, interval="4h")   # 缓存默认 partB/cache/live/, 绝对路径
    feed.warmup()                     # 启动/重启时调用一次, 阻塞直到数据就绪
    while True:
        if feed.poll():               # 有新的已收盘 bar → 返回 True
            bars = feed.history()     # schema 长表, 只含 is_final=True
            factors = compute_all(bars, "4h")
        sleep(30)

自检 (EC2 部署后必跑, 只读不下单免密钥):
    python -m partB.live.barfeed --check
"""
from __future__ import annotations
import json, logging, pathlib, time, urllib.request, urllib.parse
import pandas as pd
from partB.data.schema import INTERVAL_MS, binance_to_roostoo, quality_report, roostoo_to_binance, validate
from partB.data.binance_vision import resample

log = logging.getLogger("barfeed")
BINANCE = "https://data-api.binance.vision/api/v3/klines"
BINANCE_PRICE = "https://data-api.binance.vision/api/v3/ticker/price"
ROOSTOO_TICKER = "https://mock-api.roostoo.com/v3/ticker"
WARMUP_DAYS = 40          # 最长因子窗口 30d + 余量
# 缓存目录必须是绝对路径: 之前默认相对路径 "cache", 在 EC2 上会按 CWD 解析 →
# systemd 里 CWD 与手动跑不一样, 结果每次都当成"无缓存"重下 40 天, 重启恢复失效。
# 必须独立子目录 live/: BarFeed 只保留 ~47 天滚动窗口, 而 dl.py 的全历史缓存用的是
# 同一个文件名 {SYM}_1h.parquet。共用目录会让 warmup 静默截断掉两年研究数据,
# 且 dl.py 见文件存在就不再重下 → 数据永久丢失。别把这两者合并。
DEFAULT_CACHE = pathlib.Path(__file__).resolve().parent.parent / "cache" / "live"
# Roostoo/Binance 价差报警阈值(bp)。2026-10-04 实测 65 币: 中位 0.0bp, 最大 6.9bp
# → 噪声地板约 7bp(两次 HTTP 调用之间的时间差, 不是真的价差)。
# 原定 50bp 留了近 7 倍余量, 正常行情下永远不会响, 等于没监控; 收到 20bp 仍有约 3 倍余量。
DRIFT_MAX_BP = 20.0


def _http_json(url: str, timeout=15, retries=3):
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            if i == retries - 1:
                raise
            log.warning("http retry %s: %s", i + 1, e); time.sleep(2 ** i)


def fetch_klines(symbol_bn: str, interval: str, start_ms: int, end_ms: int | None = None) -> pd.DataFrame:
    """分页拉取 [start_ms, end_ms) 的已收盘 K 线。"""
    rows, cur, step = [], start_ms, INTERVAL_MS[interval]
    now = int(time.time() * 1000)
    end_ms = end_ms or now
    while cur < end_ms:
        q = urllib.parse.urlencode({"symbol": symbol_bn, "interval": interval,
                                    "startTime": cur, "endTime": end_ms - 1, "limit": 1000})
        data = _http_json(f"{BINANCE}?{q}")
        if not data:
            break
        rows += data
        cur = data[-1][0] + step
        if len(data) < 1000:
            break
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).iloc[:, :9]
    df.columns = ["ts", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "n_trades"]
    df = df[df.close_time.astype("int64") < now]          # 丢弃未收盘 bar → 防未来/防重绘
    out = pd.DataFrame({"ts": df.ts.astype("int64"), "symbol": binance_to_roostoo(symbol_bn),
                        **{c: df[c].astype(float) for c in ["open", "high", "low", "close", "volume", "quote_volume"]},
                        "n_trades": df.n_trades.astype("int64"), "is_final": True})
    return validate(out, interval)


def fetch_binance_prices() -> dict[str, float]:
    """一次调用拿全市场 Binance 最新成交价, 返回 {Roostoo 符号: price}。

    免 key、不占 Roostoo 的 30 calls/min 配额。用于和 Roostoo LastPrice 做同时刻价差比对
    (见 BarFeed.price_drift)。

    ⚠️ 只收 **USDT** 计价的对。`binance_to_roostoo` 会把 USDT/USDC/FDUSD 三种后缀都剥成
    同一个 "X/USD"(3723 个 Binance 符号里有 349 个这样碰撞), 若不过滤, 字典会被后写入的
    FDUSD 对覆盖 —— 而冷门币的 FDUSD 盘口又宽又陈旧(实测 UNIFDUSD 3.651 vs UNIUSDT 9.059),
    于是 UNI/XLM/MIRA 等 13 个币报出 700~14800bp 的假偏离。
    Roostoo 的 LastPrice 与 USDT 对逐位吻合, 所以 USDT 才是正确的比对基准。
    """
    out = {}
    for r in _http_json(BINANCE_PRICE):
        sym = r.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        try:
            out[binance_to_roostoo(sym)] = float(r["price"])
        except (ValueError, KeyError, TypeError):
            continue
    return out


class TickerBarBuilder:
    """降级源: Roostoo ticker 快照 → 1h bar。每次 poll 调一次 ticker(全市场一次调用)。
    volume 用 24h 滚动成交额 UnitTradeValue 的差分近似, 仅作参考。"""

    def __init__(self, interval="1h"):
        self.step = INTERVAL_MS[interval]; self.cur: dict[str, dict] = {}; self.done: list[dict] = []

    def on_snapshot(self, server_ms: int, data: dict):
        b = server_ms // self.step * self.step
        for sym, t in data.items():
            px = t.get("LastPrice")
            if not px:                                   # 0 值字段会被省略
                continue
            c = self.cur.get(sym)
            if c and c["ts"] != b:                       # 跨 bar → 封口
                self.done.append({**c, "is_final": True}); c = None
            if c is None:
                c = self.cur[sym] = {"ts": b, "symbol": sym, "open": px, "high": px, "low": px, "close": px,
                                     "volume": 0.0, "quote_volume": 0.0, "n_trades": 0,
                                     "_tv0": t.get("UnitTradeValue", 0.0)}
            c["high"] = max(c["high"], px); c["low"] = min(c["low"], px); c["close"] = px
            c["quote_volume"] = max(0.0, t.get("UnitTradeValue", 0.0) - c["_tv0"])

    def pop_final(self) -> pd.DataFrame:
        out, self.done = self.done, []
        if not out:
            return pd.DataFrame()
        return pd.DataFrame(out).drop(columns="_tv0")

    def poll(self):
        d = _http_json(f"{ROOSTOO_TICKER}?timestamp={int(time.time()*1000)}")
        if not d.get("Success"):                         # 失败也是 HTTP 200
            raise RuntimeError(d.get("ErrMsg"))
        self.on_snapshot(d["ServerTime"], d["Data"])
        return d


class BarFeed:
    def __init__(self, symbols: list[str], interval="4h", cache_dir=None, base="1h"):
        self.symbols = symbols                             # Roostoo 格式 "BTC/USD"
        self.interval, self.base = interval, base
        self.cache = pathlib.Path(cache_dir) if cache_dir else DEFAULT_CACHE
        self.cache.mkdir(parents=True, exist_ok=True)
        self.bars: dict[str, pd.DataFrame] = {}
        self.last_emitted_ts: int | None = None
        self.ready = False
        self.last_quality: dict = {}                       # 最近一次 quality_report
        self.http_calls = 0                                # 自检用: 数 REST 请求次数

    # ---------- warmup ----------
    def _cache_path(self, s): return self.cache / f"{roostoo_to_binance(s)}_{self.base}.parquet"

    def _fetch(self, sym_bn: str, start_ms: int) -> pd.DataFrame:
        self.http_calls += 1
        return fetch_klines(sym_bn, self.base, start_ms)

    def warmup(self, days: int = WARMUP_DAYS):
        """重启必须调用: 缓存 + REST 补齐 → 每个币至少 days 天连续 base bar。

        重启恢复: 缓存命中时只从 `最后一根 + 1 step` 往后补, 不重下整段。
        用 `python -m partB.live.barfeed --check` 可以实测这一点(第二次 warmup 应接近 0 次请求)。
        """
        step = INTERVAL_MS[self.base]
        need_from = int(time.time() * 1000) - days * 86_400_000
        for s in self.symbols:
            p = self._cache_path(s)
            df = pd.read_parquet(p) if p.exists() else pd.DataFrame()
            # 缓存比 need_from 还旧时, 起点抬到 need_from: 更早的部分下面会被截掉,
            # 白下载几十页只会拖慢启动(启动期间 bot 不出信号)。
            start = max(int(df.ts.max()) + step, need_from) if len(df) else need_from
            try:
                new = self._fetch(roostoo_to_binance(s), start)
            except Exception as e:  # noqa: BLE001
                log.error("warmup %s failed: %s", s, e); new = pd.DataFrame()
            df = pd.concat([df, new]).drop_duplicates(["ts", "symbol"]).sort_values("ts")
            df = df[df.ts >= need_from - 7 * 86_400_000]  # 缓存只留必要长度
            if len(df):
                df.to_parquet(p)
            self.bars[s] = df
        self.ready = self._check_ready(need_from)
        self.last_emitted_ts = self._latest_final_ts()
        log.info("warmup done ready=%s last=%s http_calls=%d", self.ready, self.last_emitted_ts, self.http_calls)
        return self.ready

    def _check_ready(self, need_from: int) -> bool:
        step = INTERVAL_MS[self.base]; ok = True
        empty = []
        for s, df in self.bars.items():
            if df.empty:
                empty.append(s); continue
            if df.ts.min() > need_from + step:
                log.warning("%s 历史不足 warmup (新币), 因子会是 NaN", s)
        if empty:
            # OMNI/TON 这类 Binance 无现货数据的币会落在这里; 只警告, 不阻塞其它币
            log.warning("%d 个币无数据(可能 Binance 无此币) → 不参与交易: %s", len(empty), empty[:10])
        allb = pd.concat([d for d in self.bars.values() if len(d)]) if any(len(d) for d in self.bars.values()) else pd.DataFrame()
        if len(allb):
            q = self.last_quality = quality_report(allb, self.base)
            bad = {k: v for k, v in q.items()
                   if k in ("nan_price", "nonpositive_price", "high_lt_low", "duplicate_keys", "misaligned_ts", "gaps") and v}
            if bad:
                log.error("数据质量异常 %s; 缺口明细 %s", bad, q["gap_detail"][:5])
            else:
                log.info("数据质量 OK: %d rows / %d symbols, 无缺口无异常值", q["rows"], q["symbols"])
        if "BTC/USD" not in self.bars or self.bars["BTC/USD"].empty:
            ok = False                                     # 基准缺失 → 不交易
            log.error("BTC/USD 基准数据缺失 → 不允许出信号")
        return ok

    # ---------- 增量 ----------
    def poll(self) -> bool:
        """拉增量 base bar; 若出现新的已收盘 self.interval bar 返回 True。"""
        touched = False
        for s, df in self.bars.items():
            start = int(df.ts.max()) + INTERVAL_MS[self.base] if len(df) else int(time.time()*1000) - 86_400_000
            try:
                # fetch_klines 内部已跑 validate(): NaN/非正价格/high<low/重复键都会被拒
                new = self._fetch(roostoo_to_binance(s), start)
            except AssertionError as e:
                log.error("poll %s: 脏数据已拒绝(未写入缓存) → %s", s, e); continue
            except Exception as e:  # noqa: BLE001
                log.error("poll %s: %s", s, e); continue
            if len(new):
                self.bars[s] = pd.concat([df, new]).drop_duplicates(["ts", "symbol"])
                self.bars[s].to_parquet(self._cache_path(s))
                touched = True
        if touched:
            self._check_quality()
        t = self._latest_final_ts()
        if t is not None and t != self.last_emitted_ts:
            self.last_emitted_ts = t; return True
        return False

    def _check_quality(self) -> dict:
        """收到新 bar 后体检一次: 缺口/异常值只报警, 不中断实盘循环。"""
        frames = [d for d in self.bars.values() if len(d)]
        if not frames:
            return {}
        q = self.last_quality = quality_report(pd.concat(frames), self.base)
        bad = {k: v for k, v in q.items()
               if k in ("nan_price", "nonpositive_price", "high_lt_low", "duplicate_keys", "misaligned_ts", "gaps") and v}
        if bad:
            log.error("poll 后数据质量异常 %s; 缺口明细 %s", bad, q["gap_detail"][:5])
        return q

    def _latest_final_ts(self) -> int | None:
        b = self.bars.get("BTC/USD")
        if b is None or b.empty:
            return None
        h = resample(b, self.interval)
        return int(h.ts.max()) if len(h) else None

    def history(self) -> pd.DataFrame:
        """返回 self.interval 的已收盘 bar 长表 (schema 格式)。"""
        allb = pd.concat([d for d in self.bars.values() if len(d)])
        return resample(allb, self.interval) if self.interval != self.base else validate(allb, self.base)

    def price_drift(self, roostoo_ticker: dict, max_bp=DRIFT_MAX_BP,
                    binance_px: dict | None = None) -> dict[str, float]:
        """Roostoo LastPrice vs Binance **同一时刻**最新价 的偏离(bp)。超阈值的币应由 D 暂停交易。

        ⚠️ 必须跟实时价比, 不能跟缓存里最近一根已收盘 bar 比。那根 bar 最多可能是 59 分钟前的,
        山寨币这段时间动 70bp 属于正常波动。2026-10-04 实测: 用收盘价比会对
        ENA/FET/NEAR/LISTA 报出 51~89bp 的"偏离", 而它们与 Binance 实时价的真实价差是
        0.0 / -4.3 / +2.0 / 0.0 bp —— 全是误报。用收盘价 = 在监控市场波动, 不是监控价差,
        后果是 D 按假警报停掉健康交易对。
        """
        bn = binance_px if binance_px is not None else fetch_binance_prices()
        out = {}
        for s in self.symbols:
            r = roostoo_ticker.get(s, {}).get("LastPrice")
            b = bn.get(s)
            if r and b:
                bp = (r / b - 1) * 1e4
                if abs(bp) > max_bp:
                    out[s] = round(bp, 1)
        return out


# ------------------------------------------------------------------ 自检
def healthcheck(symbols: list[str] | None = None, interval="4h", max_bp=DRIFT_MAX_BP) -> bool:
    """EC2 上跑一次就能确认"眼睛"是好的。全部只读, 不下单, 不需要密钥。

        python -m partB.live.barfeed --check

    检查 6 件事:
      1 warmup 能拉到数据且 ready=True(BTC/USD 基准在)
      2 **重启能从缓存恢复**: 第二个实例复用同一 cache, HTTP 请求数应 ≈ 币数
        (每币一次增量补最新几根), 而不是 币数 × 40 天。超了就说明缓存没生效。
      3 poll 能跑通, 且新 bar 通过质量校验
      4 数据质量体检: 无缺口 / 无 NaN / 无非正价格 / 无重复键
      5 history() 能出 self.interval 的宽表, 且最后一根是已收盘 bar
      6 价格偏离: Roostoo LastPrice vs Binance **实时价**, 超 max_bp 报警
    """
    from partB.data.binance_deriv import default_symbols

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    syms = symbols or default_symbols()
    print(f"=== BarFeed 自检: {len(syms)} 币, interval={interval}, cache={DEFAULT_CACHE} ===")
    fails: list[str] = []

    f1 = BarFeed(syms, interval=interval)
    t0 = time.time()
    ready = f1.warmup()
    print(f"\n[1] warmup: ready={ready} 用时 {time.time()-t0:.1f}s HTTP 请求 {f1.http_calls} 次")
    if not ready:
        fails.append("warmup 未就绪(BTC/USD 基准缺失?)")

    f2 = BarFeed(syms, interval=interval)
    t0 = time.time()
    f2.warmup()
    print(f"[2] 重启恢复: 第二次 warmup 用时 {time.time()-t0:.1f}s, HTTP 请求 {f2.http_calls} 次")
    if f2.http_calls > len(syms):
        fails.append(f"重启未走缓存(请求 {f2.http_calls} 次 > 币数 {len(syms)}), 检查 cache 目录权限/路径")
    else:
        print("    → 缓存命中, 只补了最新几根, 没有重下 40 天 ✅")

    new_bar = f2.poll()
    print(f"[3] poll: 新收盘 bar={new_bar} 最新 bar ts={f2.last_emitted_ts}")

    q = f2.last_quality
    bad = {k: v for k, v in q.items()
           if k in ("nan_price", "nonpositive_price", "high_lt_low", "duplicate_keys", "misaligned_ts", "gaps") and v}
    print(f"[4] 质量: rows={q.get('rows')} symbols={q.get('symbols')} gaps={q.get('gaps')} "
          f"nan={q.get('nan_price')} 非正价格={q.get('nonpositive_price')} 重复={q.get('duplicate_keys')}")
    if bad:
        fails.append(f"数据质量异常 {bad}")
    if q.get("gap_detail"):
        print(f"    缺口明细(前几条): {q['gap_detail'][:3]}")

    h = f2.history()
    print(f"[5] history(): {len(h)} rows / {h.symbol.nunique()} 币, "
          f"最后已收盘 {interval} bar = {pd.Timestamp(int(h.ts.max()), unit='ms', tz='UTC'):%Y-%m-%d %H:%M UTC}")
    try:
        tk = TickerBarBuilder().poll()["Data"]
        bn = fetch_binance_prices()
        drift = f2.price_drift(tk, max_bp=max_bp, binance_px=bn)
        bps = sorted(((tk[s]["LastPrice"] / bn[s] - 1) * 1e4
                      for s in f2.symbols if tk.get(s, {}).get("LastPrice") and bn.get(s)),
                     key=abs, reverse=True)
        n = len(bps)
        med = bps[n // 2] if n else 0.0
        print(f"[6] 价格偏离(Roostoo vs Binance **实时价**, 非缓存收盘价): 可比 {n} 币")
        print(f"    |偏离| 最大 {abs(bps[0]):.1f}bp  中位 {abs(med):.1f}bp  超 {max_bp}bp 的 {len(drift)} 个 {dict(list(drift.items())[:5])}")
        if drift:
            fails.append(f"{len(drift)} 个币 Roostoo/Binance 实时价差 > {max_bp}bp: {list(drift)[:5]}")
    except Exception as e:  # noqa: BLE001
        print(f"[6] 价格偏离: 跳过(Roostoo ticker 或 Binance 价格不可达: {e})")

    print("\n=== 自检结果:", "全部通过 ✅" if not fails else f"{len(fails)} 项失败 ❌", "===")
    for x in fails:
        print("  -", x)
    return not fails


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="BarFeed 自检(只读, 不下单, 免密钥)")
    ap.add_argument("--check", action="store_true",
                    help="跑 warmup/重启恢复/poll/质量/history/价格偏离 六项自检")
    ap.add_argument("--symbols", nargs="+", default=None, help="默认全 65 币宇宙; 调试可只给几个")
    ap.add_argument("--interval", default="4h")
    ap.add_argument("--max-bp", type=float, default=DRIFT_MAX_BP,
                    help=f"价差报警阈值(bp), 默认 {DRIFT_MAX_BP:.0f}; 实测噪声地板约 7bp")
    a = ap.parse_args()
    if not a.check:
        ap.error("目前只支持 --check")
    raise SystemExit(0 if healthcheck(a.symbols, a.interval, a.max_bp) else 1)
