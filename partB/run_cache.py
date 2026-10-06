import pandas as pd
df = pd.read_parquet("E:\\000_uni\\Quant\\roostoo_hackthon\\partB\\cache\\bars_1h_all.parquet")
print(df.symbol.nunique())   # 67
print(df.shape)              # (1026146, 10)
