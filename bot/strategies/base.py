"""Shared strategy plumbing: indicator helpers, the Signal type, and the
Strategy base class.

Strategies are pure signal generators: they look at a DataFrame of COMPLETED
candles plus the current position record and return a Signal (or None).
Order execution, sizing, and stops live elsewhere.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import pandas as pd


# --- Indicator helpers ------------------------------------------------------

def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Wilder's Average True Range."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


# --- Signal -----------------------------------------------------------------

@dataclass
class Signal:
    """What a strategy wants done after seeing a completed candle.

    action: "enter"   — open a new position (side = long/short)
            "exit"    — close the current position
            "reverse" — close the current position and open the opposite side
    atr:    the current 14-period ATR on the strategy's timeframe, used by the
            risk manager for stop distance and position sizing.
    """

    action: str
    side: Optional[str] = None  # "long" | "short" (for enter/reverse)
    atr: float = 0.0
    reason: str = ""


# --- Base class -------------------------------------------------------------

class Strategy(ABC):
    name: str = "base"

    def __init__(self, symbol: str, params: dict):
        self.symbol = symbol
        self.params = params

    @property
    @abstractmethod
    def warmup_bars(self) -> int:
        """Completed candles required before any signal may be generated."""

    @abstractmethod
    def evaluate(self, df: pd.DataFrame, position: Optional[dict]) -> Optional[Signal]:
        """df: completed candles only (open/high/low/close/volume), oldest
        first. position: the bot's state record for this symbol, or None if
        flat. Called exactly once per newly completed candle."""
