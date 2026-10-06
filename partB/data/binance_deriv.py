"""Binance 衍生品数据下载器: funding rate / open interest / 永续基差。免费、免密钥。

数据结构（2026-10 实测）:
1. 资金费率  GET https://fapi.binance.com/fapi/v1/fundingRate?symbol=&startTime=&endTime=&limit=1000
   行: {symbol, fundingTime(ms 结算时刻), fundingRate(str), markPrice(str), rateType}
   - 每 8h 结算一次(00:00/08:00/16:00 UTC); 全历史可回溯到上线; 分页 1000/请求
   - fundingRate = 该期多头付空头的费率, 0.0001 = 0.01%/8h
2. 持仓量  GET https://fapi.binance.com/futures/data/openInterestHist?symbol=&period=4h&limit=500
   行: {symbol, sumOpenInterest(基础币), sumOpenInterestValue(USD), CMCCirculatingSupply, timestamp}
   - ⚠️ 官方只保留最近 30 天 → 做不了两年回测; 用 collect_oi() 在 EC2 上 cron 每 4h 增量收集,
     几周后才够回测窗口。当前只作实盘信号候选。
3. 永续基差 basis = perp close / spot close - 1
   - perp klines: https://data.binance.vision/data/futures/um/{monthly,daily}/klines/...
     格式与 spot 完全一致(12 列 csv zip); 长历史 OK, 与 spot 同基础设施
   - 备选(免下载, 但只有最近 1500 根): GET https://fapi.binance.com/fapi/v1/klines?symbol=&interval=4h&limit=1500
     4h×1500 ≈ 250 天, 不够覆盖 2024-10 起的 IS 段 → 长历史仍走 vision zip
   - 没有 perp 的币拿不到数据(返回空), 那些币的 basis 因子值为 NaN → 走 R3 剔除, 不许填 0

存储 (partB/cache/deriv/), 全部长表, symbol 用 Roostoo 格式:
   funding.parquet : ts(fundingTime), symbol, funding_rate
   oi.parquet      : ts, symbol, open_interest, open_interest_usd   (增量收集)
   basis 不单独存: 由 futures klines 与 spot klines 现算

用法:
  python -m partB.data.binance_deriv --what funding            # 全宇宙 funding 历史
  python -m partB.data.binance_deriv --what oi                 # 增量收集 OI(30 天窗口)
  python -m partB.data.binance_deriv --what basis --start 2024-10   # 需 vision 可达的网络
"""
import argparse
import datetime as dt
import json
import pathlib
import time
import urllib.parse
import urllib.request

import pandas as pd

from .binance_vision import _get, _parse
from .schema import binance_to_roostoo, roostoo_to_binance

REST = "https://fapi.binance.com"
# *** 路径里没有 "/binance/" 这一段。Vision 的布局是 data/{spot,futures/um,futures/cm}/...,
#   写成 data/binance/futures/um/... 会稳定 404。这个 bug 曾被误判成"沙箱网络访问不了 vision",
#   结论差点变成"basis 历史只能队友机器跑" —— 实际只是 URL 拼错。
#   2026-10-04 实测: monthly/daily 两种 futures klines zip 均返回 200。
VISION_FUT = "https://data.binance.vision/data/futures/um"
CACHE = pathlib.Path(__file__).resolve().parent.parent / "cache" / "deriv"
OI_MAX_WINDOW_MS = 30 * 86_400_000  # 官方 OI 历史只保留 30 天
POLITE = 0.1


def _get_json(path: str, params: dict) -> list:
    url = REST + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


# ---------- funding ----------
def fetch_funding(symbol_bin: str, start_ms: int | None = None, end_ms: int | None = None,
                  roostoo: str | None = None) -> pd.DataFrame:
    """`roostoo` 给 1000 倍合约用: binance_to_roostoo("1000PEPEUSDT") 会得到 "1000PEPE/USD",
    一个 Roostoo 上不存在的交易对, 后面按 symbol 过滤就永远匹配不上。"""
    rows, cur = [], start_ms or 0
    while True:
        p = {"symbol": symbol_bin, "limit": 1000, "startTime": cur}
        if end_ms:
            p["endTime"] = end_ms
        batch = _get_json("/fapi/v1/fundingRate", p)
        if not batch:
            break
        rows += batch
        if len(batch) < 1000:
            break
        cur = batch[-1]["fundingTime"] + 1
        time.sleep(POLITE)
    if not rows:
        return pd.DataFrame(columns=["ts", "symbol", "funding_rate"])
    df = pd.DataFrame(rows)
    df = df.assign(ts=df.fundingTime.astype("int64"),
                   funding_rate=df.fundingRate.astype(float),
                   symbol=roostoo or binance_to_roostoo(symbol_bin))
    return df[["ts", "symbol", "funding_rate"]].drop_duplicates("ts").sort_values("ts")


# ---------- open interest (30 天窗口) ----------
def fetch_oi(symbol_bin: str, start_ms: int, end_ms: int, period: str = "4h",
             roostoo: str | None = None, mult: int = 1) -> pd.DataFrame:
    if end_ms - start_ms > OI_MAX_WINDOW_MS:
        raise ValueError("openInterestHist 只保留最近 30 天, 窗口超限")
    rows, cur = [], start_ms
    while True:
        batch = _get_json("/futures/data/openInterestHist",
                          {"symbol": symbol_bin, "period": period, "limit": 500, "startTime": cur})
        if not batch:
            break
        rows += batch
        if len(batch) < 500:
            break
        cur = batch[-1]["timestamp"] + 1
        time.sleep(POLITE)
    if not rows:
        return pd.DataFrame(columns=["ts", "symbol", "open_interest", "open_interest_usd"])
    df = pd.DataFrame(rows)
    # sumOpenInterest 的单位是"合约张数×乘数"的基础币数量: 1000 倍合约报的是 1000 个币为一单位,
    # 要乘回 mult 才是真的币数。sumOpenInterestValue 是 USD, 不受乘数影响, 保持原样。
    df = df.assign(ts=df.timestamp.astype("int64"),
                   open_interest=df.sumOpenInterest.astype(float) * mult,
                   open_interest_usd=df.sumOpenInterestValue.astype(float),
                   symbol=roostoo or binance_to_roostoo(symbol_bin))
    return df[["ts", "symbol", "open_interest", "open_interest_usd"]].drop_duplicates("ts").sort_values("ts")


def collect_oi(symbols_roostoo: list[str]) -> pd.DataFrame:
    """增量收集: 读旧 parquet → 每币从 max(旧末根, now-29d) 补到 now → 合并落盘。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / "oi.parquet"
    old = pd.read_parquet(path) if path.exists() else pd.DataFrame(
        columns=["ts", "symbol", "open_interest", "open_interest_usd"])
    now = int(time.time() * 1000)
    parts = [old] if len(old) else []
    for sym in symbols_roostoo:
        last = old.ts[old.symbol == sym].max() if len(old) and (old.symbol == sym).any() else None
        start = int(max(last + 1 if last is not None else 0, now - 29 * 86_400_000))
        try:
            perp, mult = roostoo_to_perp(sym)
            df = fetch_oi(perp, start, now, roostoo=sym, mult=mult)
        except Exception as e:  # 无 perp 的币 / 网络抖动: 跳过不中断
            print(f"[skip] {sym}: {e}")
            continue
        if len(df):
            parts.append(df)
            print(f"[ok] {sym}: +{len(df)} rows")
    if not parts:
        return old
    out = pd.concat(parts).drop_duplicates(["ts", "symbol"]).sort_values(["ts", "symbol"])
    out.to_parquet(path, index=False)
    return out


# ---------- 永续 klines (basis 的分子) ----------
def fetch_futures_klines(symbol_bin: str, interval: str, start: str, end: str | None = None,
                         roostoo: str | None = None, mult: int = 1) -> pd.DataFrame:
    """下载 Binance USD-M 永续的 K 线, 返回 B0 风格的长表。

    `roostoo` / `mult` 必须一起给 1000 倍合约用（见 PERP_MULT）:
    - 不传 roostoo 的话会用 binance_to_roostoo("1000PEPEUSDT") → 得到 "1000PEPE/USD", 一个不存在的交易对;
    - 不除 mult 的话 perp close 是现货的 1000 倍, basis = perp/spot - 1 会算出 +99900%,
      而且**不会报错** —— 因子照样出数, 只是全是垃圾。所以这里就把价格折回单个币的量纲。
    """
    today = dt.date.today()
    cur = dt.date.fromisoformat(start + "-01")
    stop = dt.date.fromisoformat((end or today.strftime("%Y-%m")) + "-01")
    parts = []
    while cur <= stop:
        ym = cur.strftime("%Y-%m")
        b = None
        if (cur.year, cur.month) < (today.year, today.month):
            b = _get(f"{VISION_FUT}/monthly/klines/{symbol_bin}/{interval}/{symbol_bin}-{interval}-{ym}.zip")
            if b:
                parts.append(_parse(b))
        if b is None:
            d = cur
            nxt = (cur.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
            while d < min(today, nxt):
                b = _get(f"{VISION_FUT}/daily/klines/{symbol_bin}/{interval}/{symbol_bin}-{interval}-{d}.zip")
                if b:
                    parts.append(_parse(b))
                d += dt.timedelta(days=1)
        cur = (cur.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts)
    px = {c: df[c].astype(float) for c in ["open", "high", "low", "close"]}
    out = pd.DataFrame({
        "ts": df.ts, "symbol": roostoo or binance_to_roostoo(symbol_bin),
        **{c: v / mult for c, v in px.items()},
        "volume": df.volume.astype(float) * mult,        # 币数要乘回去, 成交额才对得上
        "quote_volume": df.quote_volume.astype(float),
    }).drop_duplicates(["ts", "symbol"]).sort_values(["ts", "symbol"])
    return out.reset_index(drop=True)


# ---------- panels ----------
EXCLUDED = ("OMNI/USD", "TON/USD")   # Roostoo ticker 无这两个币 → 不可交易, 不进宇宙

# Binance 永续对"单价极低、数量极大"的币用 1000 倍合约: 现货是 PEPEUSDT, 合约是 1000PEPEUSDT,
# 一根 K 线的 close 是"1000 个 PEPE 值多少 USDT"。
# 直接用 roostoo_to_binance() 会得到 PEPEUSDT → 合约市场上没这个符号 → 全年 404 →
# 表现成"这个币没有 perp", 而实际上有。2026-10-04 实测漏掉了 BONK/FLOKI/PEPE/SHIB 四个。
PERP_MULT = {"BONK/USD": 1000, "FLOKI/USD": 1000, "PEPE/USD": 1000, "SHIB/USD": 1000}


def roostoo_to_perp(sym: str) -> tuple[str, int]:
    """Roostoo 符号 → (Binance 永续符号, 合约乘数)。没有 perp 的币会在下载时 404, 不在这里判。"""
    m = PERP_MULT.get(sym, 1)
    return (f"{m}" + roostoo_to_binance(sym) if m != 1 else roostoo_to_binance(sym)), m


def universe(cache_bars: pathlib.Path) -> list[str]:
    b = pd.read_parquet(cache_bars)
    return sorted(b.symbol.unique())


def default_symbols() -> list[str]:
    """宇宙符号(Roostoo 格式), 剔除 EXCLUDED。

    优先用本地 1h bars 缓存; EC2 上 parquet 不入 git → 回退到 partB/crypto.txt
    (签入的 Binance 符号清单, 与缓存的 67 个币逐一对齐), 保证 cron 不依赖大文件。
    """
    partb = pathlib.Path(__file__).resolve().parent.parent
    bars = partb / "cache" / "bars_1h_all.parquet"
    if bars.exists():
        syms = universe(bars)
    else:
        raw = (partb / "crypto.txt").read_text().split()
        syms = [binance_to_roostoo(s) for s in raw if s.endswith("USDT")]
    return [s for s in syms if s not in EXCLUDED]


def build_funding_panel(symbols_roostoo: list[str], start_ms: int | None = None) -> pd.DataFrame:
    """下载并**增量合并** funding.parquet。

    必须合并而不是整表覆盖: 传 `--symbols A B C` 补几个币时, 覆盖写会把其余 60 多个币的历史
    一次性抹掉, 而且脚本看起来是"成功"的。被刷新的 symbol 先整段删掉再写入, 所以重复跑不会
    留下旧数据。
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / "funding.parquet"
    old = pd.read_parquet(path) if path.exists() else pd.DataFrame(
        columns=["ts", "symbol", "funding_rate"])
    todo = set(symbols_roostoo)
    keep = old[~old.symbol.isin(todo)] if len(old) else old
    parts = [keep] if len(keep) else []
    for sym in symbols_roostoo:
        # 用 roostoo_to_perp 而不是 roostoo_to_binance: BONK/FLOKI/PEPE/SHIB 的合约是 1000 倍符号,
        # 直接拼 PEPEUSDT 会 404, 之前 funding.parquet 因此少了这 4 个币（61/65）。
        # 资金费率是个比率, 与合约乘数无关, 所以这里不需要再除回去。
        perp, _ = roostoo_to_perp(sym)
        df = fetch_funding(perp, start_ms, roostoo=sym)
        if len(df):
            parts.append(df)
            print(f"[ok] {sym}: {len(df)} settlements")
        else:
            print(f"[skip] {sym}: 无 perp funding")
    out = pd.concat(parts).drop_duplicates(["ts", "symbol"]).sort_values(["ts", "symbol"]).reset_index(drop=True) \
        if parts else pd.DataFrame(columns=["ts", "symbol", "funding_rate"])
    out.to_parquet(path, index=False)
    print(f"funding.parquet: {len(out)} 行, {out.symbol.nunique()} 个币")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--what", choices=["funding", "oi", "basis"], default="funding")
    ap.add_argument("--start", default="2024-10")
    ap.add_argument("--symbols", nargs="+", default=None)
    ap.add_argument("--force", action="store_true", help="basis: 忽略已存在的 parquet, 全部重下")
    a = ap.parse_args()
    syms = a.symbols or default_symbols()
    if a.what == "funding":
        start_ms = int(pd.Timestamp(a.start + "-01", tz="UTC").value // 10**6)
        build_funding_panel(syms, start_ms)
    elif a.what == "oi":
        collect_oi(syms)
    else:
        CACHE.mkdir(parents=True, exist_ok=True)
        for s in syms:
            path = CACHE / f"fut4h_{s.replace('/', '-')}.parquet"
            if path.exists() and not a.force:
                continue                                  # 断点续传: 已下过的不重来
            perp, mult = roostoo_to_perp(s)
            df = fetch_futures_klines(perp, "4h", a.start, roostoo=s, mult=mult)
            if len(df):
                df.to_parquet(path, index=False)
                print(f"[ok] {s} ({perp}, x{mult}): {len(df)} bars")
            else:
                print(f"[skip] {s} ({perp}): vision 上找不到, 多半是真没有 perp")
