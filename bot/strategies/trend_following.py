"""Trend following on a 50/200 EMA cross (4-hour candles).

Golden cross (50 EMA crosses above 200 EMA) -> long; death cross -> exit any
long and go short. Entries happen only on the cross event itself, so a
position stopped out mid-trend stays flat until the next cross. A 3x ATR
trailing stop (managed by the risk manager) protects open positions.
"""

from typing import Optional

import pandas as pd

from .base import Signal, Strategy, atr, ema


class TrendFollowing(Strategy):
    name = "trend_following"

    @property
    def warmup_bars(self) -> int:
        # EMA needs extra history beyond its period to converge.
        return self.params["slow_ema"] * 2

    def evaluate(self, df: pd.DataFrame, position: Optional[dict]) -> Optional[Signal]:
        p = self.params
        fast = ema(df["close"], p["fast_ema"])
        slow = ema(df["close"], p["slow_ema"])
        diff_prev = float(fast.iloc[-2] - slow.iloc[-2])
        diff_now = float(fast.iloc[-1] - slow.iloc[-1])
        cur_atr = float(atr(df, p["atr_period"]).iloc[-1])

        golden = diff_prev <= 0 < diff_now
        death = diff_prev >= 0 > diff_now

        if golden:
            reason = f"50 EMA crossed above 200 EMA ({diff_prev:.3f} -> {diff_now:.3f})"
            if position is not None and position["side"] == "short":
                return Signal("reverse", side="long", atr=cur_atr, reason=reason)
            if position is None:
                return Signal("enter", side="long", atr=cur_atr, reason=reason)
        if death:
            reason = f"50 EMA crossed below 200 EMA ({diff_prev:.3f} -> {diff_now:.3f})"
            if position is not None and position["side"] == "long":
                return Signal("reverse", side="short", atr=cur_atr, reason=reason)
            if position is None:
                return Signal("enter", side="short", atr=cur_atr, reason=reason)
        return None
