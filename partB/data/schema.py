"""B0 Bar schema — B→C 交接契约。变更需走 PR + 四人 review。

约定:
- ts = bar 开盘时间, UTC 毫秒 int64; bar 覆盖 [ts, ts+interval)
- 一根 bar 只有在 ts+interval 之后才可被策略使用(收盘才算完成)
- symbol 用 Roostoo 格式 "BTC/USD"(Binance BTCUSDT 映射而来)
- 长表(long format), 主键 (ts, symbol), 按 ts, symbol 排序
"""
from dataclasses import dataclass
import pandas as pd

BAR_COLUMNS = {
    "ts": "int64",            # 开盘时间 UTC ms
    "symbol": "string",       # "BTC/USD"
    "open": "float64",
    "high": "float64",
    "low": "float64",
    "close": "float64",
    "volume": "float64",      # 基础币成交量
    "quote_volume": "float64",# 计价币(USD)成交额
    "n_trades": "int64",
    "is_final": "bool",       # 实盘未收盘 bar = False; 历史数据恒 True
}
INTERVAL_MS = {"1m": 60_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


@dataclass(frozen=True)
class Bar:
    ts: int
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    n_trades: int
    is_final: bool = True


def binance_to_roostoo(sym: str) -> str:
    for q in ("USDT", "USDC", "FDUSD"):
        if sym.endswith(q):
            return f"{sym[:-len(q)]}/USD"
    raise ValueError(sym)


def roostoo_to_binance(sym: str) -> str:
    return sym.split("/")[0] + "USDT"


def validate(df: pd.DataFrame, interval: str | None = None) -> pd.DataFrame:
    missing = set(BAR_COLUMNS) - set(df.columns)
    assert not missing, f"missing columns {missing}"
    df = df[list(BAR_COLUMNS)].astype(BAR_COLUMNS)
    assert not df.duplicated(["ts", "symbol"]).any(), "duplicate (ts,symbol)"
    px = df[["open", "high", "low", "close"]]
    assert px.notna().all().all(), "价格含 NaN"
    assert (px > 0).all().all(), "价格 <= 0"
    assert (df.high >= df.low).all(), "high < low"
    assert (df.high >= df[["open", "close"]].max(axis=1)).all(), "high < max(o,c)"
    assert (df.low <= df[["open", "close"]].min(axis=1)).all(), "low > min(o,c)"
    assert (df[["volume", "quote_volume"]] >= 0).all().all()
    assert df.volume.notna().all() and df.quote_volume.notna().all(), "量含 NaN"
    if interval:
        assert (df.ts % INTERVAL_MS[interval] == 0).all(), "ts 未对齐 interval"
    return df.sort_values(["ts", "symbol"]).reset_index(drop=True)


def find_gaps(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """每币的 ts 断点: 返回 [symbol, prev_ts, next_ts, missing_bars]。空表 = 无缺口。"""
    step = INTERVAL_MS[interval]
    d = df.sort_values(["symbol", "ts"])
    dif = d.groupby("symbol").ts.diff()
    bad = d[dif > step].assign(prev=lambda x: x.ts - dif[dif > step])
    if bad.empty:
        return pd.DataFrame(columns=["symbol", "prev_ts", "next_ts", "missing_bars"])
    return pd.DataFrame({"symbol": bad.symbol.values, "prev_ts": bad.prev.values.astype("int64"),
                         "next_ts": bad.ts.values.astype("int64"),
                         "missing_bars": (dif[dif > step].values // step - 1).astype(int)})


def quality_report(df: pd.DataFrame, interval: str) -> dict:
    """软诊断(不抛异常): 给实盘 BarFeed 每轮 poll 后用, 出问题只报警不崩。

    与 validate() 的分工: validate 是入库硬门禁(脏数据直接拒绝);
    quality_report 是运行期体检(脏数据要能继续跑, 但必须喊出来)。
    """
    px = df[["open", "high", "low", "close"]]
    gaps = find_gaps(df, interval)
    step = INTERVAL_MS[interval]
    per_sym = df.groupby("symbol").size()
    return {
        "rows": int(len(df)),
        "symbols": int(df.symbol.nunique()),
        "nan_price": int(px.isna().sum().sum()),
        "nonpositive_price": int((px <= 0).sum().sum()),
        "high_lt_low": int((df.high < df.low).sum()),
        "duplicate_keys": int(df.duplicated(["ts", "symbol"]).sum()),
        "misaligned_ts": int((df.ts % step != 0).sum()),
        "gaps": int(gaps.missing_bars.sum()) if len(gaps) else 0,
        "gap_detail": gaps.head(10).to_dict("records"),
        "symbols_below_warmup": int((per_sym < 40 * 86_400_000 // step).sum()),
    }
