"""Equity candles must be regular-session only (09:30-16:00 ET), anchored to
the open, and never returned before they're complete. Pre/post-market IEX
bars (~1% of session volume) previously leaked into SMA/std/ATR."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from bot.main import last_boundary
from bot.portfolio import SESSION_OPEN_MINUTE, session_bars, session_base_minutes

ET = ZoneInfo("America/New_York")


def base_bars(day="2026-09-28", start="08:00", end="17:00", minutes=15, drop=()):
    """One day of synthetic base bars; bar i has open=i, close=i+0.5,
    high=i+1, low=i-1, volume=1. `drop` removes bars by ET start time."""
    idx = pd.date_range(f"{day} {start}", f"{day} {end}", freq=f"{minutes}min",
                        tz=ET, inclusive="left")
    idx = idx[~idx.strftime("%H:%M").isin(drop)]
    n = len(idx)
    df = pd.DataFrame({"open": range(n), "high": [i + 1 for i in range(n)],
                       "low": [i - 1 for i in range(n)],
                       "close": [i + 0.5 for i in range(n)], "volume": [1.0] * n},
                      index=idx.tz_convert("UTC"), dtype=float)
    return df


def et_times(df):
    return list(df.index.tz_convert(ET).strftime("%H:%M"))


def after_close():
    return datetime(2026, 9, 28, 18, 0, tzinfo=ET)


def test_base_sizes_tile_the_open():
    assert [session_base_minutes(tf) for tf in (15, 30, 60, 120, 240)] == [15, 30, 30, 30, 30]


def test_15m_drops_extended_hours():
    out = session_bars(base_bars(), 15, 15, after_close())
    times = et_times(out)
    assert times[0] == "09:30" and times[-1] == "15:45"
    assert len(out) == 26


def test_60m_anchored_to_open_with_short_last_candle():
    bars = base_bars(minutes=30)
    out = session_bars(bars, 60, 30, after_close())
    assert et_times(out) == ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    # 15:30 candle is a single 30m base bar (session ends 16:00).
    assert out["volume"].iloc[-1] == 1.0 and out["volume"].iloc[0] == 2.0


def test_ohlcv_aggregation():
    bars = base_bars(minutes=15)
    out = session_bars(bars, 60, 15, after_close())
    first = bars[(bars.index >= out.index[0]) & (bars.index < out.index[1])]
    row = out.iloc[0]
    assert row["open"] == first["open"].iloc[0]
    assert row["close"] == first["close"].iloc[-1]
    assert row["high"] == first["high"].max() and row["low"] == first["low"].min()
    assert row["volume"] == 4.0


def test_incomplete_candle_not_returned():
    # 10:40 ET: the 09:30 60m candle ended at 10:30; the 10:30 one is still forming.
    now = datetime(2026, 9, 28, 10, 40, tzinfo=ET)
    bars = base_bars(end="10:45", minutes=15)
    assert et_times(session_bars(bars, 60, 15, now)) == ["09:30"]


def test_candle_waits_for_final_base_bar_then_grace():
    # 10:15 base bar not published yet at 10:31 -> the 09:30 candle is withheld...
    bars = base_bars(end="10:30", minutes=15, drop=("10:15",))
    assert session_bars(bars, 60, 15, datetime(2026, 9, 28, 10, 31, tzinfo=ET)).empty
    # ...but accepted once the grace period passes (thin names may never print).
    late = session_bars(bars, 60, 15, datetime(2026, 9, 28, 10, 36, tzinfo=ET))
    assert et_times(late) == ["09:30"]


def test_equity_boundary_grid_matches_session_candles():
    ts = datetime(2026, 9, 28, 11, 10, tzinfo=ET)
    assert last_boundary(ts, 60, SESSION_OPEN_MINUTE).strftime("%H:%M") == "10:30"
    assert last_boundary(ts, 240, SESSION_OPEN_MINUTE).strftime("%H:%M") == "09:30"
    assert last_boundary(ts, 60, 0).strftime("%H:%M") == "11:00"  # crypto grid


def test_keeps_source_timezone_object():
    # Alpaca stock bars use datetime.timezone.utc while crypto bars use a
    # different UTC object; converting to "UTC" broke the combined backtest.
    from datetime import timezone
    bars = base_bars()
    bars.index = bars.index.tz_convert(timezone.utc)
    out = session_bars(bars, 60, 15, after_close())
    assert out.index.tz is timezone.utc
