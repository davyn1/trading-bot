# Trading Bot

An automated, multi-strategy trading bot for the [Alpaca](https://alpaca.markets)
API. It runs several strategies simultaneously across equities, ETFs, and
crypto, with stop-based position sizing and portfolio-level risk controls.
**Paper trading by default** — it never touches the live endpoint unless
explicitly configured to.

Built on the official [`alpaca-py`](https://github.com/alpacahq/alpaca-py) SDK.

## How it works

Each instrument is mapped to a strategy entirely in [`config.py`](config.py),
so adding or removing an instrument is a one-line change with no strategy-code
edits. All strategies evaluate signals only on **completed candles**, and no
strategy trades until its indicators are fully warmed up from historical data
fetched on startup.

| Strategy | Instruments | Timeframe | Entry / exit | Stop |
|---|---|---|---|---|
| **Mean Reversion** | SPY, QQQ | 15 min | Enter when price closes beyond ±z·σ of the 20-period SMA (z = 2.0 SPY / 2.3 QQQ); exit at the SMA. Skips entries during volatility spikes. | 2× ATR, fixed |
| **Momentum Breakout** | BTC/USD | 1 hour | Long on a close above the 20-bar high with ≥1.5× average volume; exit to flat on a confirmed breakdown. Long/flat only (spot crypto can't be shorted). | 2× ATR, trailing |
| **Trend Following** | GLD, USO | 4 hour | Long on a 50/200 EMA golden cross; reverse to short on a death cross. | 3× ATR, trailing |
| **Stock Momentum** | AMD, NVDA, GOOGL, TSLA, WDC, DOCN | 4 hour | Donchian breakout adapted for single stocks: 20-bar-high breakout with volume, plus a 50-EMA trend filter and an earnings-blackout window. Long/flat. | 3× ATR, trailing |

## Risk management

- **Stop-based sizing** — every position is sized so that a move from entry to
  the initial stop equals exactly **1% of account equity**. Wider stops mean
  proportionally smaller positions; risk per trade stays constant.
- **Trailing stops** recalculate ATR on each completed candle, trail from the
  extreme since entry, and never move against the position. Triggers are checked
  on every price update, not just on candle close.
- **Correlation filter** — if SPY and QQQ are both long, new BTC/USD longs are
  blocked to avoid doubling up on risk-on exposure.
- **Kill switch** — if the day's drawdown (realized + unrealized) exceeds 3% of
  equity, all new entries halt for the rest of the day; existing positions keep
  their stops.
- **State recovery** — on startup the bot reconciles with Alpaca (open positions
  and orders), resumes managing them, and persists all state to `state.json`
  after every change, so a crash or restart never orphans a position.

All per-strategy risk parameters and the portfolio-level thresholds are
configurable in [`config.py`](config.py).

## Project structure

```
bot/
  strategies/
    base.py               shared indicators + Strategy/Signal interfaces
    mean_reversion.py
    momentum_breakout.py
    trend_following.py
    stock_momentum.py
  risk_manager.py         sizing, trailing stops, correlation filter, kill switch
  portfolio.py            all Alpaca API access (data, orders, retries)
  state.py                JSON state persistence + trade/PNL CSV logs
  backtest.py             historical backtester with slippage modeling
  main.py                 orchestrator / main loop
config.py                 instrument→strategy mapping + all parameters
dashboard.py              Streamlit monitoring dashboard
morning_briefing.py       Discord morning briefing (scheduled)
evening_report.py         Discord evening report (scheduled)
deploy/                   systemd service files
DEPLOY.md                 24/7 cloud deployment runbook
```

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip
cp .env.example .env                            # then fill in your Alpaca keys
```

Get **paper** API keys at the [Alpaca dashboard](https://app.alpaca.markets)
(switch the account picker to Paper → API Keys). The bot defaults to the paper
endpoint (`PAPER = True` in `config.py`).

## Usage

**Backtest** a strategy against historical data (read-only — safe to run
anytime, never places orders):

```bash
python -m bot.backtest --months 6 [--slippage 0.0005]
```

Reports per-instrument and portfolio metrics (win rate, profit factor, max
drawdown, Sharpe, return), flags any strategy with a negative Sharpe, and saves
an equity-curve chart to `backtest_results.png`.

**Run the bot:**

```bash
python -m bot.main
```

**Monitoring dashboard** (live positions, signals, and P&L):

```bash
python -m streamlit run dashboard.py
```

## Deployment

For running the bot 24/7 on a cloud server (so it doesn't depend on a local
machine staying awake), see **[DEPLOY.md](DEPLOY.md)** — a step-by-step runbook
covering a small Linux VM, `systemd` services that auto-restart on crash/reboot,
and the Discord briefing schedule.

## Adding or removing an instrument

One line in `config.py`. For example, to trade NVDA with the stock-momentum
strategy:

```python
INSTRUMENTS = {
    ...
    "NVDA": {"strategy": "stock_momentum", "params": {"earnings_dates": ["2026-08-26"]}},
}
```

Remove a line to drop an instrument. Per-instrument `params` override the
strategy defaults. Restart the bot to apply.

## Disclaimer

This is a personal research and educational project for **paper trading**. It is
not investment advice, and nothing here guarantees profitability — backtested
and live results can and do differ. Review the code and understand the risks
before ever pointing it at a live account.
