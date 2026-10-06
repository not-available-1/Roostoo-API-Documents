from concurrent.futures import ThreadPoolExecutor
import pathlib
from partB.data.binance_vision import fetch
syms=open('crypto.txt').read().split(); out=pathlib.Path('bars');out.mkdir(exist_ok=True)
def go(s):
    if (out/f'{s}_1h.parquet').exists(): return s,'cached'
    try:
        df=fetch(s,'1h','2024-10')
        if df.empty: return s,0
        t=out/f'.{s}.tmp'; df.to_parquet(t); t.rename(out/f'{s}_1h.parquet'); return s,len(df)
    except Exception as e: return s,str(e)[:60]
with ThreadPoolExecutor(16) as ex:
    for s,n in ex.map(go,syms): print(s,n,flush=True)
