from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd


@dataclass(frozen=True)
class Target:
    symbol: str
    target_weight: float
    reason: str = ""
    ts: Optional[int] = None

    def __post_init__(self):
        if not self.symbol:
            raise ValueError("symbol cannot be Null")
        if not isinstance(self.target_weight, (int, float)):
            raise TypeError("target_weight should be a number")
        if self.reason is None:
            raise ValueError("reason cannot be None")


class StrategyABC(ABC):
    warmup_bars: int = 0

    @abstractmethod
    def on_bar(self, bars: pd.DataFrame) -> List[Target]:
        """
        笔记
        Input:
            bars: long format DataFrame, BAR_COLUMNS
                只包含 is_final=True 的 bar
                按 ts, symbol 排序
                每个 symbol 的历史长度 >= warmup_bars
                所有 bar 的 ts < 当前决策时刻（严格小于，避免未来函数）
        Output:
            List[Target]
        """
        return None

    def warmup(self, history: pd.DataFrame) -> None:
        return None