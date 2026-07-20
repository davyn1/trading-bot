"""Momentum breakout on a 20-period Donchian channel (1-hour candles).

Long when a candle closes above the prior 20-period high with volume >= 1.5x
the 20-period average; exit to flat on a close below the prior 20-period low
with the same volume confirmation. Long/flat only — Alpaca spot crypto cannot
be shorted. A 2x ATR trailing stop (managed by the risk manager) also
protects the position.
"""

from typing import Optional

import pandas as pd

from .base import Signal, Strategy, atr


class MomentumBreakout(Strategy):
    name = "momentum_breakout"

    @property
    def warmup_bars(self) -> int:
        p = self.params
        return max(p["channel_period"], p["volume_avg_period"], p["atr_period"]) + 5

    def evaluate(self, df: pd.DataFrame, position: Optional[dict]) -> Optional[Signal]:
        p = self.params
        # shift(1) excludes the just-closed candle: it must break the channel
        # formed by the PRIOR 20 candles.
        highest = df["high"].rolling(p["channel_period"]).max().shift(1).iloc[-1]
        lowest = df["low"].rolling(p["channel_period"]).min().shift(1).iloc[-1]
        vol_avg = df["volume"].rolling(p["volume_avg_period"]).mean().shift(1).iloc[-1]
        cur_atr = float(atr(df, p["atr_period"]).iloc[-1])
        last = df.iloc[-1]

        if pd.isna(highest) or pd.isna(vol_avg):
            return None
        volume_ok = last["volume"] >= p["volume_mult"] * vol_avg

        if position is not None:
            if last["close"] < lowest and volume_ok:
                return Signal("exit", atr=cur_atr,
                              reason=f"close {last['close']:.2f} < 20-period low {lowest:.2f} with volume")
            return None

        if last["close"] > highest and volume_ok:
            return Signal("enter", side="long", atr=cur_atr,
                          reason=f"close {last['close']:.2f} > 20-period high {highest:.2f}, "
                                 f"volume {last['volume']:.0f} >= {p['volume_mult']}x avg {vol_avg:.0f}")
        return None
