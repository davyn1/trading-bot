"""Stock momentum: Donchian breakout adapted for individual equities.

Core rules are momentum_breakout's (close above the prior 20-bar high with
volume >= 1.5x its 20-bar average; long/flat; ATR trailing stop managed by
the risk manager). On top, three stock-specific guards:

  1. Trend filter — only take breakouts with price above its 50-period EMA;
     breakouts inside downtrends are disproportionately false starts.
  2. Gap cooldown — no entries for a few bars after any open that gapped
     more than gap_atr_mult x ATR from the prior close (news moves; the
     breakout level is stale information after a gap).
  3. Earnings blackout — no entries within a configured window around known
     earnings dates (per-instrument "earnings_dates" list in config; the
     biggest overnight-gap risk a stop cannot protect against).

Exits are never filtered — filters only make the strategy more selective
about getting in, never slower to get out.
"""

import logging
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from .base import Signal, Strategy, atr, ema

log = logging.getLogger(__name__)


class StockMomentum(Strategy):
    name = "stock_momentum"

    @property
    def warmup_bars(self) -> int:
        p = self.params
        return max(p["channel_period"], p["volume_avg_period"], p["atr_period"],
                   p["trend_ema"]) + 5

    def _in_earnings_blackout(self, ts: pd.Timestamp) -> bool:
        p = self.params
        d = ts.date()
        for iso in p.get("earnings_dates", []):
            e = date.fromisoformat(iso)
            if (e - timedelta(days=p["earnings_days_before"])) <= d \
                    <= (e + timedelta(days=p["earnings_days_after"])):
                return True
        return False

    def evaluate(self, df: pd.DataFrame, position: Optional[dict]) -> Optional[Signal]:
        p = self.params
        highest = df["high"].rolling(p["channel_period"]).max().shift(1).iloc[-1]
        lowest = df["low"].rolling(p["channel_period"]).min().shift(1).iloc[-1]
        vol_avg = df["volume"].rolling(p["volume_avg_period"]).mean().shift(1).iloc[-1]
        atr_series = atr(df, p["atr_period"])
        cur_atr = float(atr_series.iloc[-1])
        last = df.iloc[-1]

        if pd.isna(highest) or pd.isna(vol_avg):
            return None
        volume_ok = last["volume"] >= p["volume_mult"] * vol_avg

        if position is not None:
            if last["close"] < lowest and volume_ok:
                return Signal("exit", atr=cur_atr,
                              reason=f"close {last['close']:.2f} < {p['channel_period']}-bar "
                                     f"low {lowest:.2f} with volume")
            return None

        if not (last["close"] > highest and volume_ok):
            return None

        # --- stock-specific entry guards -----------------------------------
        if p["use_trend_filter"]:
            trend = float(ema(df["close"], p["trend_ema"]).iloc[-1])
            if last["close"] <= trend:
                log.info("%s: breakout skipped, close %.2f below EMA%d %.2f (downtrend)",
                         self.symbol, last["close"], p["trend_ema"], trend)
                return None
        if p["use_gap_filter"]:
            gaps = (df["open"] - df["close"].shift(1)).abs() > \
                p["gap_atr_mult"] * atr_series
            if bool(gaps.tail(p["gap_cooldown_bars"]).any()):
                log.info("%s: breakout skipped, %.1fx-ATR gap within last %d bars",
                         self.symbol, p["gap_atr_mult"], p["gap_cooldown_bars"])
                return None
        if self._in_earnings_blackout(df.index[-1]):
            log.info("%s: breakout skipped, inside earnings blackout window", self.symbol)
            return None

        return Signal("enter", side="long", atr=cur_atr,
                      reason=f"close {last['close']:.2f} > {p['channel_period']}-bar high "
                             f"{highest:.2f}, volume {last['volume']:.0f} >= "
                             f"{p['volume_mult']}x avg {vol_avg:.0f}")
