"""Mean reversion on Bollinger-style z-score bands (15-minute candles).

Long when close < SMA20 - z*std, short when close > SMA20 + z*std
(z is per-instrument: 1.5 SPY, 1.8 QQQ). Exit when a candle closes at or
beyond the SMA. Entries are skipped during volatility spikes so news-driven
moves are not faded. Fixed 1x ATR stop, no trailing.
"""

import logging
from typing import Optional

import pandas as pd

from .base import Signal, Strategy, atr, sma

log = logging.getLogger(__name__)


class MeanReversion(Strategy):
    name = "mean_reversion"

    @property
    def warmup_bars(self) -> int:
        p = self.params
        need = max(
            p["sma_period"],
            p["atr_period"] + p["vol_filter_atr_avg_period"],
        )
        return need + 5

    def evaluate(self, df: pd.DataFrame, position: Optional[dict]) -> Optional[Signal]:
        p = self.params
        close = df["close"]
        mean = sma(close, p["sma_period"]).iloc[-1]
        std = close.rolling(p["sma_period"]).std().iloc[-1]
        atr_series = atr(df, p["atr_period"])
        cur_atr = float(atr_series.iloc[-1])
        last = df.iloc[-1]

        if position is not None:
            # Exit when a candle closes at or beyond the moving average.
            if position["side"] == "long" and last["close"] >= mean:
                return Signal("exit", atr=cur_atr, reason=f"close {last['close']:.2f} >= SMA {mean:.2f}")
            if position["side"] == "short" and last["close"] <= mean:
                return Signal("exit", atr=cur_atr, reason=f"close {last['close']:.2f} <= SMA {mean:.2f}")
            return None

        if std == 0 or pd.isna(std):
            return None

        upper = mean + p["entry_z"] * std
        lower = mean - p["entry_z"] * std
        if not (last["close"] < lower or last["close"] > upper):
            return None

        # Volatility spike filter — never fade a news-driven move.
        atr_avg = float(atr_series.rolling(p["vol_filter_atr_avg_period"]).mean().iloc[-1])
        candle_range = float(last["high"] - last["low"])
        if candle_range > p["vol_filter_range_mult"] * cur_atr:
            log.info("%s: entry skipped, candle range %.3f > %.1fx ATR %.3f",
                     self.symbol, candle_range, p["vol_filter_range_mult"], cur_atr)
            return None
        if cur_atr > p["vol_filter_atr_ratio"] * atr_avg:
            log.info("%s: entry skipped, ATR %.3f > %.1fx its 20-period avg %.3f",
                     self.symbol, cur_atr, p["vol_filter_atr_ratio"], atr_avg)
            return None

        if last["close"] < lower:
            return Signal("enter", side="long", atr=cur_atr,
                          reason=f"close {last['close']:.2f} < lower band {lower:.2f} (z={p['entry_z']})")
        return Signal("enter", side="short", atr=cur_atr,
                      reason=f"close {last['close']:.2f} > upper band {upper:.2f} (z={p['entry_z']})")
