# CLAUDE.md

Context for Claude Code sessions. What the bot does and how it's structured is
in [README.md](README.md); deployment is in [DEPLOY.md](DEPLOY.md). This file
covers what those don't: local setup quirks, how work gets deployed, how we
backtest, and what the backtests have shown so far.

## Working with Davy

- Prefers going **one step / one setting at a time**, with results explained
  in plain terms before moving on. Ask before multi-step changes.
- Paper trading only. Davy doesn't want to trade crypto personally — BTC/USD is
  kept for paper testing and will be dropped later.
- Keep work on separate strategies separate: new strategy work goes on its own
  branch (e.g. `stock-strategy`), not straight onto `master`.
- Backtest results are engineering evidence, not financial advice — say so
  when presenting them.

## Local setup (Windows 11 PC)

- Python 3.12 venv at `.venv/` (`.venv\Scripts\python.exe`). Install with
  `pip install -r requirements.txt`, then **pin `pandas==2.2.3`**: Windows Smart
  App Control blocks a DLL in pandas 3.x (`DLL load failed ... Application
  Control policy`). Don't upgrade pandas locally. The server (Linux) is unaffected.
- `.env` holds the Alpaca **paper** keys (gitignored). Never print or echo it.
- A TLS-intercepting VPN is sometimes active. `config.py` injects `truststore`
  so `requests`-based code trusts the Windows cert store. Libraries with their
  own CA bundle (e.g. `curl_cffi`/`yfinance`) need a bundle built from
  `ssl.enum_certificates("ROOT"/"CA")` plus certifi — never disable verification.
- `winget` needs `--source winget` (the msstore source fails its cert check).
- Git author for this repo is `davyn <davyn@uci.edu>`; it is **not** set in
  global git config, so commit with
  `git -c user.name="davyn" -c user.email="davyn@uci.edu" commit ...`.
  GitHub credentials are stored by Git Credential Manager, so push works.

## Tests

`python -m pytest tests` — no API keys needed. Covers stop-exit failure
handling, notional caps, and regular-session candle building. Add a regression
test with every bug fix.

## Deployment (DigitalOcean droplet)

- The bot runs as systemd service `trading-bot`, user `trader`, in
  `/home/trader/trading-bot` (own `.venv`, own `.env`). The server pulls `master`.
- Davy runs server commands in the **DigitalOcean web console** (Droplet →
  Access → Launch Droplet Console). The SSH key on this PC
  (`~/.ssh/id_ed25519`, made 2026-09-24 after a Windows reset) has **not** been
  added to the server, so Claude can't SSH in.
- Deploy: `sudo -u trader git -C /home/trader/trading-bot pull`, then
  `sudo -u trader .venv/bin/python -m pytest tests -q` from that directory.
- **Status as of 2026-10-09: stopped and disabled** since 2026-09-24 while
  strategies are reworked. Re-enable only when Davy decides:
  `sudo systemctl enable --now trading-bot`, then check
  `journalctl -u trading-bot -n 40 --no-pager`.

## Backtesting method

- `python -m bot.backtest --months 6` for the standard report; ad-hoc sweeps
  are scripts that override `config` in memory (never edit `config.py` for an
  experiment).
- Split history into a **tuning period and an unseen period** (e.g. first 4 of
  6 months / last 2). Only trust settings that hold up on the unseen period.
- Always compare against **buy-and-hold** (the instruments themselves and SPY).
- Avoid hindsight in stock selection: test on a **neutral basket** chosen
  without knowing outcomes (e.g. largest US companies as of the start date),
  not just stocks already known to have soared.
- Data: Alpaca free **IEX** feed, intraday history starts **2020-07-27**. IEX is
  a small slice of total volume, so volume filters are approximate. 0.05%
  slippage per fill, $0 commission. Historical earnings dates aren't
  available, so the earnings blackout can't be backtested.

## Findings so far (backtests run 2026-09-24 → 2026-09-30)

All with current notional caps (0.5x equity per position, 1.0x total) and
regular-session candles unless noted.

- **Mean reversion (SPY, QQQ, 15m)** — loses overall (−$8.4k / 6 months).
  SPY loses on every timeframe tried (15m/30m/1h/2h); slower candles don't
  help. **Shorts lost in every test**; QQQ longs were the only profitable piece
  (+2.3% over 6 months while QQQ rose ~31%). The original prompt settings
  (1.5σ, 1x ATR stop, no caps) lost ~70% in 6 months. SPX/NDX index data gives
  near-identical signals to SPY/QQQ (and neither is tradable on Alpaca).
  Davy chose to keep it as-is for now.
- **Momentum breakout (BTC, 1h)** — loses (~24% win rate). Low priority given
  BTC will be dropped.
- **Trend following (GLD, USO, 4h)** — ~flat, but only ~5 trades in 6 months;
  needs a multi-year backtest to judge.
- **Stock momentum (6 stocks, 4h)** — best of the four. Over Oct 2020 → Sep
  2026: +79.8% on the configured 6 stocks vs ~+135% SPY buy-and-hold, but only
  **+5.7% on a neutral 24-stock basket** — the result is mostly stock
  selection, not timing. Its real strength was 2022 (−1% to −3% vs −20% to
  −56% for buy-and-hold).

## History worth knowing

- 2026-09 incident: a stop exit that failed once (Alpaca `40410000` "position
  not found") retried forever and aborted the whole loop, so SPY's stop and
  exit never fired; stop-based sizing with no notional cap had put ~3.4x equity
  into SPY. Fixed in `8a41100` (isolated exits, notional caps). TSLA was never a
  "phantom" — it's a configured stock_momentum instrument.
- Pre/post-market IEX bars used to leak into equity candles; equities are now
  regular-session only (`portfolio.session_bars`).
