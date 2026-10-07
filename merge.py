import os
import pandas as pd
from data.schema import validate, INTERVAL_MS


def resample_safe(df, to, src_interval="1h"):
    step = INTERVAL_MS[to]
    src_step = INTERVAL_MS[src_interval]
    g = df.assign(b=df.ts // step * step).groupby(["symbol", "b"])
    out = g.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                close=("close", "last"), volume=("volume", "sum"),
                quote_volume=("quote_volume", "sum"), n_trades=("n_trades", "sum"),
                cnt=("ts", "size")).reset_index().rename(columns={"b": "ts"})
    out = out[out.cnt == step // src_step].drop(columns="cnt")
    out["is_final"] = True
    return validate(out, to)


CACHE = r"C:\Users\weibin.WSSHAFP5BV7TX\Desktop\partB 2\cache"
BARS = r"C:\Users\weibin.WSSHAFP5BV7TX\Desktop\partB 2\bars"
os.makedirs(BARS, exist_ok=True)

bars_1h = pd.read_parquet(CACHE + r"\bars_1h_all.parquet")
bars_1h = validate(bars_1h, "1h")
print("1h:", bars_1h.shape, bars_1h.symbol.nunique())

bars_4h = resample_safe(bars_1h, "4h")
print("4h:", bars_4h.shape, bars_4h.symbol.nunique())
bars_4h.to_parquet(BARS + r"\all_4h.parquet")
print("saved", BARS + r"\all_4h.parquet")