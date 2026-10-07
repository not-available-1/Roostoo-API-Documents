import pandas as pd
import numpy as np
from c_contracts import StrategyABC, Target
from weights import allocate_weights


class CrossSectionalMomentum(StrategyABC):
    def __init__(self, lookback: int = 24, top_k: int = 5, bottom_k: int = 5):
        self.lookback = lookback
        self.top_k = top_k
        self.bottom_k = bottom_k
        self.warmup_bars = lookback + 1

    def on_bar(self, bars: pd.DataFrame) -> list[Target]:
        latest_ts = int(bars.ts.max())
        rows = []
        for sym, g in bars.groupby("symbol"):
            g = g.sort_values("ts").tail(self.lookback + 1)
            if len(g) < self.lookback + 1:
                continue
            ret = g.close.iloc[-1] / g.close.iloc[0] - 1
            vol = np.log(g.close).diff().std()
            if vol == 0 or np.isnan(vol):
                continue
            rows.append({"symbol": sym, "score": ret / vol})

        if not rows:
            return []

        df = pd.DataFrame(rows).sort_values("score", ascending=False)
        if len(df) < self.top_k + self.bottom_k:
            return []

        longs = df.head(self.top_k)
        shorts = df.tail(self.bottom_k)
        shorts = shorts[~shorts.symbol.isin(longs.symbol)]
        if longs.empty or shorts.empty:
            return []

        targets = []
        for _, r in longs.iterrows():
            targets.append(Target(r.symbol, 1.0 / len(longs),
                                  f"mom_top {r.score:.4f}", latest_ts))
        for _, r in shorts.iterrows():
            targets.append(Target(r.symbol, -1.0 / len(shorts),
                                  f"mom_bottom {r.score:.4f}", latest_ts))
        return targets



class MultiFactorStrategy(StrategyABC):
    def __init__(self, factors: dict, selected: list, top_k: int = 5,
                 bottom_k: int = 5, net: str = "50-50",
                 warmup_bars: int = 200,
                 min_hold_bars: int = 0,
                 weight_threshold: float = 0.0,
                 weight_method: str = "equal",
                 cov_window: int = 360,
                 close_wide: pd.DataFrame = None):
        self.selected = selected
        self.factors = {k: factors[k] for k in selected}
        self.top_k = top_k
        self.bottom_k = bottom_k
        self.net = net
        self.warmup_bars = warmup_bars
        self.min_hold_bars = min_hold_bars
        self.weight_threshold = weight_threshold
        self.weight_method = weight_method
        self.cov_window = cov_window
        self.close_wide = close_wide

        self.last_rebalance_ts = 0
        self.last_targets = []

    def _allocate(self, syms):
        """给定资产列表，返回权重 dict。"""
        syms = list(syms)
        if len(syms) == 0:
            return {}
        if len(syms) == 1 or self.weight_method == "equal":
            return {s: 1.0 / len(syms) for s in syms}
        if self.close_wide is None:
            return {s: 1.0 / len(syms) for s in syms}
        rets = self.close_wide[syms].pct_change().iloc[-self.cov_window:]
        rets = rets.dropna(how="all")
        if len(rets) < 30:
            return {s: 1.0 / len(syms) for s in syms}
        cov = rets.cov()
        return allocate_weights(cov, syms, method=self.weight_method)

    def on_bar(self, bars: pd.DataFrame) -> list:
        ts = int(bars.ts.max())

        # hold 期
        if self.min_hold_bars > 0 and self.last_targets:
            interval_ms = 4 * 3600 * 1000
            if ts - self.last_rebalance_ts < self.min_hold_bars * interval_ms:
                return self.last_targets

        # 合成因子
        combined = None
        for k in self.selected:
            f = self.factors[k]
            f = f[f.index <= ts]
            if f.empty:
                continue
            v = f.iloc[-1]
            combined = v if combined is None else combined.add(v, fill_value=0)

        if combined is None:
            return self.last_targets

        combined = combined.dropna()
        if len(combined) < self.top_k + self.bottom_k:
            return self.last_targets

        longs = combined.nlargest(self.top_k).index
        shorts = combined.nsmallest(self.bottom_k).index
        shorts = [s for s in shorts if s not in set(longs)]
        if len(longs) == 0 or len(shorts) == 0:
            return self.last_targets

        if self.net == "100-0":
            total_long, total_short = 1.0, 0.0
        elif self.net == "70-30":
            total_long, total_short = 0.7, 0.3
        elif self.net == "60-40":
            total_long, total_short = 0.6, 0.4
        elif self.net == "50-50":
            total_long, total_short = 0.5, 0.5
        else:
            raise ValueError(self.net)

        lw = self._allocate(list(longs))
        sw = self._allocate(list(shorts)) if total_short > 0 else {}

        new_targets = []
        for s, w in lw.items():
            new_targets.append(Target(s, total_long * w, f"long {self.weight_method}", ts))
        for s, w in sw.items():
            new_targets.append(Target(s, -total_short * w, f"short {self.weight_method}", ts))

        # 权重阈值
        if self.weight_threshold > 0 and self.last_targets:
            old_w = {t.symbol: t.target_weight for t in self.last_targets}
            new_w = {t.symbol: t.target_weight for t in new_targets}
            all_syms = set(old_w) | set(new_w)
            max_change = max(abs(new_w.get(s, 0) - old_w.get(s, 0)) for s in all_syms)
            if max_change < self.weight_threshold:
                return self.last_targets

        self.last_rebalance_ts = ts
        self.last_targets = new_targets
        return new_targets