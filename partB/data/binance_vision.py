"""Binance Vision 下载 + 解析 + 聚合 + 校验。免费、无 key。
用法: python -m data.binance_vision --symbols BTCUSDT ETHUSDT --start 2024-10 --interval 1h --out bars/
"""
import argparse, io, zipfile, datetime as dt, pathlib, time, urllib.request
import pandas as pd
from .schema import binance_to_roostoo, validate, INTERVAL_MS

BASE = "https://data.binance.vision/data/spot"
RAW_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
            "quote_volume", "n_trades", "tb_base", "tb_quote", "ignore"]


def _get(url: str, retries: int = 4) -> bytes | None:
    """404 = 这个月的文件不存在（正常, 还没上市/已下架）→ 返回 None。
    其余异常（10054 连接被重置、DNS 抖动、超时）都是**瞬时的**, 必须重试:
    实测并发 8 线程下 21 个标的会随机挂掉 7 个, 而不重试的表现是"这个币没数据",
    很容易误判成 Binance 没上市它。退避时间递增, 让被限流的一侧缓过来。"""
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
        except Exception as e:                                   # noqa: BLE001
            last = e
        time.sleep(1.5 * (i + 1))
    raise last


def _parse(blob: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        raw = z.read(z.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw), header=None)
    if not str(df.iloc[0, 0]).isdigit():  # 部分文件带表头
        df = df.iloc[1:]
    df.columns = RAW_COLS
    ts = df.open_time.astype("int64")
    ts = ts.where(ts < 10**14, ts // 1000)  # 2025 起 spot 为微秒 → 统一毫秒
    df["ts"] = ts
    return df


def fetch(symbol: str, interval: str, start: str, end: str | None = None) -> pd.DataFrame:
    """start/end: 'YYYY-MM'. 已结束月份走 monthly, 当月走 daily。"""
    today = dt.date.today()
    cur = dt.date.fromisoformat(start + "-01")
    stop = dt.date.fromisoformat((end or today.strftime("%Y-%m")) + "-01")
    parts = []
    while cur <= stop:
        ym = cur.strftime("%Y-%m")
        b = None
        if (cur.year, cur.month) < (today.year, today.month):
            b = _get(f"{BASE}/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{ym}.zip")
            if b: parts.append(_parse(b))
        if b is None:  # 当月, 或月度包尚未发布(月初几天) → 逐日补
            d = cur
            nxt = (cur.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
            while d < min(today, nxt):
                b = _get(f"{BASE}/daily/klines/{symbol}/{interval}/{symbol}-{interval}-{d}.zip")
                if b: parts.append(_parse(b))
                d += dt.timedelta(days=1)
        cur = (cur.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts)
    out = pd.DataFrame({
        "ts": df.ts, "symbol": binance_to_roostoo(symbol),
        **{c: df[c].astype(float) for c in ["open", "high", "low", "close", "volume", "quote_volume"]},
        "n_trades": df.n_trades.astype("int64"), "is_final": True,
    }).drop_duplicates(["ts", "symbol"])
    return validate(out, interval)


def resample(df: pd.DataFrame, to: str) -> pd.DataFrame:
    """1h → 4h/1d。UTC 对齐(与 Binance 官方一致)。不完整的 bar 丢弃。"""
    step = INTERVAL_MS[to]
    g = df.assign(b=df.ts // step * step).groupby(["symbol", "b"])
    out = g.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                close=("close", "last"), volume=("volume", "sum"),
                quote_volume=("quote_volume", "sum"), n_trades=("n_trades", "sum"),
                cnt=("ts", "size")).reset_index().rename(columns={"b": "ts"})
    src = int(df.ts.diff().where(lambda s: s > 0).min()) if len(df) > 1 else step
    out = out[out.cnt == step // src].drop(columns="cnt")
    out["is_final"] = True
    return validate(out, to)


def check_against_official(mine: pd.DataFrame, official: pd.DataFrame, tol=1e-9) -> pd.DataFrame:
    """验收: 自建 4h 与官方 4h 逐根对比, 返回不一致的行(空 = 通过)。"""
    m = mine.merge(official, on=["ts", "symbol"], suffixes=("", "_o"))
    bad = pd.Series(False, index=m.index)
    for c in ["open", "high", "low", "close", "volume"]:
        bad |= (m[c] - m[c + "_o"]).abs() > tol * m[c + "_o"].abs().clip(lower=1)
    return m[bad]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", required=True)
    ap.add_argument("--start", default=(dt.date.today() - dt.timedelta(days=730)).strftime("%Y-%m"))
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--out", default="bars")
    a = ap.parse_args()
    p = pathlib.Path(a.out); p.mkdir(parents=True, exist_ok=True)
    for s in a.symbols:
        df = fetch(s, a.interval, a.start)
        if df.empty:
            print(f"[skip] {s} 无数据"); continue
        df.to_parquet(p / f"{s}_{a.interval}.parquet")
        print(f"[ok] {s}: {len(df)} bars")
