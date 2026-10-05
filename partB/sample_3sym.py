import pandas as pd
from pathlib import Path
from partB.data.schema import validate
from partB.data.binance_vision import resample

frames = [pd.read_parquet(p) for p in Path("bars").glob("*_1h.parquet")]
bars_1h = validate(pd.concat(frames, ignore_index=True), "1h")
print("1h:", bars_1h.shape, bars_1h.symbol.nunique())

bars_4h = resample(bars_1h, "4h")
print("4h:", bars_4h.shape, bars_4h.symbol.nunique())
for sym, g in bars_4h.groupby("symbol"):
    print(sym, len(g), g.ts.min(), g.ts.max())

bars_4h.to_parquet("bars/all_4h.parquet")
print("saved bars/all_4h.parquet")