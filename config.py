"""Central configuration for the trading bot.

Everything tunable lives here: which instruments run which strategy,
per-strategy risk parameters, and the paper/live switch. Adding or removing
an instrument is a one-line change in INSTRUMENTS — no strategy code edits.
"""

import os

from dotenv import load_dotenv

# Trust the Windows certificate store so HTTPS works behind Davy's
# TLS-intercepting VPN (no-op off VPN). config is imported before any network
# code, so injecting here covers every request the bot makes.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

load_dotenv()

# --- Alpaca credentials (from .env, never hardcode) -------------------------
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")

# --- Endpoint selection -----------------------------------------------------
# PAPER=True routes every order to the paper trading endpoint. Only flip this
# to False deliberately, after extensive paper results — the bot will place
# real-money orders when False.
PAPER = True

# Market data feed for stocks. "iex" is free on every account; "sip" requires
# a paid data subscription.
DATA_FEED = "iex"

# --- Main loop --------------------------------------------------------------
POLL_INTERVAL_SECONDS = 30  # price/stop checks and candle polling cadence

# --- Portfolio-level risk ---------------------------------------------------
RISK = {
    # Halt all NEW entries for the rest of the ET day once the account is
    # down this fraction of start-of-day equity (realized + unrealized).
    "daily_drawdown_halt_pct": 0.03,
}

# --- Per-strategy parameters ------------------------------------------------
# risk_pct: fraction of account equity lost if the initial stop is hit.
# stop_atr_mult: initial stop distance in ATR multiples (also the trail
# distance when trailing=True).
STRATEGIES = {
    "mean_reversion": {
        "timeframe_minutes": 15,
        "risk_pct": 0.01,
        # Tuned 2026-07-19 from 6-month backtest (was 1.0): a 1x ATR stop sat
        # inside 15-minute noise (27% win rate) and forced huge notionals whose
        # slippage consumed ~2/3 of the risk budget per trade. 2x halves the
        # notional and lifted the backtested win rate to ~42%.
        "stop_atr_mult": 2.0,
        "trailing": False,  # fixed stop at entry -/+ stop_atr_mult * ATR
        "params": {
            "sma_period": 20,
            "entry_z": 1.5,          # per-instrument override below
            "atr_period": 14,
            # Volatility spike filter: skip entries when the signal candle's
            # range > 2x ATR, or ATR > 1.5x its own 20-period average.
            "vol_filter_range_mult": 2.0,
            "vol_filter_atr_ratio": 1.5,
            "vol_filter_atr_avg_period": 20,
        },
    },
    "momentum_breakout": {
        "timeframe_minutes": 60,
        "risk_pct": 0.01,
        "stop_atr_mult": 2.0,
        "trailing": True,
        "params": {
            "channel_period": 20,     # 20-period high/low breakout channel
            "volume_avg_period": 20,
            "volume_mult": 1.5,       # volume must be >= 1.5x its 20-per avg
            "atr_period": 14,
        },
    },
    "trend_following": {
        "timeframe_minutes": 240,
        "risk_pct": 0.01,
        "stop_atr_mult": 3.0,
        "trailing": True,
        "params": {
            "fast_ema": 50,
            "slow_ema": 200,
            "atr_period": 14,
        },
    },
}

# --- Instrument -> strategy mapping -----------------------------------------
# One line per instrument. "params" entries override the strategy's defaults
# for that instrument only. Crypto symbols contain "/". To drop BTC/USD later,
# delete (or comment out) its line — nothing else needs to change.
INSTRUMENTS = {
    # entry_z tuned 2026-07-19 from 6-month backtest (was 1.5/1.8): deeper
    # entries cut trade count ~60% and the slippage churn with it.
    "SPY": {"strategy": "mean_reversion", "params": {"entry_z": 2.0}},
    "QQQ": {"strategy": "mean_reversion", "params": {"entry_z": 2.3}},
    "BTC/USD": {"strategy": "momentum_breakout"},
    "GLD": {"strategy": "trend_following"},
    "USO": {"strategy": "trend_following"},
}

# --- Correlation filter -----------------------------------------------------
# When every symbol in "when_all_long" is currently held long, block NEW long
# entries on every symbol in "block_new_longs" (risk-on exposure cap).
CORRELATION_FILTER = {
    "when_all_long": ["SPY", "QQQ"],
    "block_new_longs": ["BTC/USD"],
}

# --- Files ------------------------------------------------------------------
STATE_FILE = "state.json"
TRADES_CSV = "trades.csv"
DAILY_PNL_CSV = "daily_pnl.csv"
LOG_FILE = "bot.log"
