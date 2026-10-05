import numpy as np, pandas as pd
from partB.data.schema import validate
from partB.data.binance_vision import resample
from partB import factors as F

def synth(n_sym=10, n=24*120):
    rng = np.random.default_rng(0); rows = []
    syms = ["BTC/USD"] + [f"C{i}/USD" for i in range(n_sym - 1)]
    for s in syms:
        c = 100 * np.exp(np.cumsum(rng.normal(0, .01, n)))
        o = np.r_[c[0], c[:-1]]
        rows.append(pd.DataFrame({"ts": np.arange(n) * 3_600_000, "symbol": s, "open": o,
            "high": np.maximum(o, c) * 1.002, "low": np.minimum(o, c) * .998, "close": c,
            "volume": 1e4, "quote_volume": rng.uniform(1e6, 5e6, n), "n_trades": 100, "is_final": True}))
    return validate(pd.concat(rows), "1h")

def test_resample():
    h = synth(); b4 = resample(h, "4h")
    x = h[h.symbol == "BTC/USD"].iloc[:4]; y = b4[b4.symbol == "BTC/USD"].iloc[0]
    assert y.open == x.open.iloc[0] and y.close == x.close.iloc[-1] and y.high == x.high.max()

def test_no_lookahead():
    """篡改未来数据, 过去的因子值不应改变。"""
    b4 = resample(synth(), "4h"); cut = b4.ts.unique()[300]
    f1 = F.compute_all(b4)
    b4m = b4.copy(); fut = b4m.ts > cut
    b4m.loc[fut, ["open","high","low","close"]] *= 3; b4m.loc[fut, "quote_volume"] *= 7
    f2 = F.compute_all(b4m)
    for k in f1:
        pd.testing.assert_frame_equal(f1[k].loc[:cut], f2[k].loc[:cut], obj=k)

def test_report():
    b4 = resample(synth(), "4h"); fs = F.compute_all(b4)
    rep = F.factor_report(fs, F.to_wide(b4), 6); corr = F.factor_corr(fs)
    print(rep.round(3)); print(F.select_low_corr(rep, corr))
