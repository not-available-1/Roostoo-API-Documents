import time, pandas as pd
from partB.live.barfeed import BarFeed, TickerBarBuilder, fetch_klines
from partB.factors import compute_all

def test_ticker_builder():
    b = TickerBarBuilder("1h"); H = 3_600_000
    b.on_snapshot(0, {"BTC/USD": {"LastPrice": 100, "UnitTradeValue": 10}})
    b.on_snapshot(H//2, {"BTC/USD": {"LastPrice": 110, "UnitTradeValue": 15}})
    b.on_snapshot(H//2+1, {"BTC/USD": {"MaxBid": 1}})          # LastPrice 被省略
    b.on_snapshot(H+5, {"BTC/USD": {"LastPrice": 105, "UnitTradeValue": 20}})
    r = b.pop_final().iloc[0]
    assert (r.open, r.high, r.low, r.close, r.quote_volume) == (100, 110, 100, 110, 5)

def test_live_warmup(tmp_path):
    f = BarFeed(["BTC/USD", "ETH/USD", "SOL/USD"], "4h", tmp_path)
    assert f.warmup()
    h = f.history(); now = time.time() * 1000
    assert h.ts.max() + 4 * 3_600_000 <= now                       # 最后一根已收盘
    assert h.groupby("symbol").size().min() >= 6 * 35              # >=35 天
    fs = compute_all(h, "4h", min_usd=0)
    assert fs["mom_30d_skip1d"].iloc[-1].notna().all()             # 上线即有信号
    # 重启: 第二次 warmup 走缓存
    f2 = BarFeed(["BTC/USD", "ETH/USD", "SOL/USD"], "4h", tmp_path); f2.warmup()
    pd.testing.assert_frame_equal(f2.history(), h)

def test_live_matches_vision():
    """实盘源与回测源同一根 bar 逐字段一致。"""
    from partB.data.binance_vision import fetch
    v = fetch("BTCUSDT", "1h", "2026-09", "2026-09")
    s = int(pd.Timestamp("2026-09-10").value // 10**6)
    l = fetch_klines("BTCUSDT", "1h", s, s + 24 * 3_600_000)
    m = v.merge(l, on=["ts", "symbol"], suffixes=("", "_l"))
    assert len(m) == 24 and (m.close == m.close_l).all() and (m.volume - m.volume_l).abs().max() < 1e-6
