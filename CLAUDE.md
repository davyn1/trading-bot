# CLAUDE.md

Context for Claude Code sessions. What the bot does and how it's structured is
in [README.md](README.md); deployment is in [DEPLOY.md](DEPLOY.md). This file
covers what those don't: local setup quirks, how work gets deployed, how we
backtest, and what the backtests have shown so far.

## Working conventions

- **This repo is public** (github.com/davyn1/trading-bot). Never commit
  secrets, API keys, `.env`, account data (balances, positions, account IDs,
  real trade logs, `state.json`), server addresses, or personal info (names,
  emails, home paths, network details). Before every commit, check the staged
  diff for these; keep real data files in gitignored or scratch locations.
  Pushing changes public content, so ask before pushing.
- Go **one step / one setting at a time**, explaining results in plain terms
  before moving on. Ask before multi-step changes.
- **Paper trading only.** BTC/USD is kept for paper testing and is planned for
  removal; it's not a priority for tuning.
- New strategy work goes on its own branch (e.g. `stock-strategy`), not
  straight onto `master` — `master` is what the server runs.
- Backtest results are engineering evidence, not financial advice — say so
  when presenting them.

## Local setup (Windows)

- Python 3.12 venv at `.venv/` (`.venv\Scripts\python.exe`). Install with
  `pip install -r requirements.txt`, then **pin `pandas==2.2.3`**: Windows Smart
  App Control blocks a DLL in pandas 3.x (`DLL load failed ... Application
  Control policy`). Don't upgrade pandas locally. Linux is unaffected.
- `.env` holds the Alpaca **paper** keys (gitignored). Never print or echo it.
- HTTPS may be intercepted by a proxy/VPN. `config.py` injects `truststore` so
  `requests`-based code trusts the OS cert store. Libraries with their own CA
  bundle (e.g. `curl_cffi`/`yfinance`) need a bundle built from
  `ssl.enum_certificates("ROOT"/"CA")` plus certifi — never disable verification.
- `winget` needs `--source winget` (the msstore source can fail its cert check).
- The commit author isn't set in global git config; commit as the repo owner
  using the author of the latest commit:
  `git -c user.name="$(git log -1 --format=%an)" -c user.email="$(git log -1 --format=%ae)" commit ...`.

## Tests

`python -m pytest tests` — no API keys needed. Covers stop-exit failure
handling, notional caps, and regular-session candle building. Add a regression
test with every bug fix.

## Deployment (DigitalOcean droplet)

- The bot runs as systemd service `trading-bot`, user `trader`, in
  `/home/trader/trading-bot` (own `.venv`, own `.env`). The server pulls `master`.
- Server commands are run by the owner in the **DigitalOcean web console**
  (Droplet → Access → Launch Droplet Console); Claude has no SSH access.
- Deploy: `sudo -u trader git -C /home/trader/trading-bot pull`, then
  `sudo -u trader .venv/bin/python -m pytest tests -q` from that directory.
- The service has been **stopped and disabled since 2026-09-24** while the
  strategies are reworked. Only the owner decides when to re-enable it:
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
  Kept as-is for now.
- **Momentum breakout (BTC, 1h)** — loses (~24% win rate). Low priority given
  BTC will be dropped.
- **Trend following (GLD, USO, 4h)** — ~flat, but only ~5 trades in 6 months;
  needs a multi-year backtest to judge.
- **Stock momentum (6 stocks, 4h)** — best of the four. Over Oct 2020 → Sep
  2026: +79.8% on the configured 6 stocks vs ~+135% SPY buy-and-hold, but only
  **+5.7% on a neutral 24-stock basket** — the result is mostly stock
  selection, not timing. Its real strength was 2022 (−1% to −3% vs −20% to
  −56% for buy-and-hold).

## Known open issues (fix before the bot runs again)

1. **Late entry fills are lost.** `Broker._await_fill` gives up after
   `FILL_TIMEOUT_SECONDS` (60s) and raises; `Bot.open_position` then never
   records the position, so it's held at Alpaca with no stop and no exit.
   Paper market orders at the 09:30 open took 3–5 minutes to fill. Startup
   reconciliation adopts such positions, but only on restart.
2. **No HTTP timeouts.** alpaca-py (`alpaca/common/rest.py`, v0.44) calls
   `session.request` without a timeout, so a stalled request can hang the loop
   forever (this happened 2026-09-22: state.json stopped updating, no further
   log lines for two days).

## History worth knowing

- 2026-09 incident, confirmed from Alpaca order history plus the server's
  state.json/bot.log: the bot bought 446 SPY at the 2026-09-04 open; the fill
  took 3.5 minutes, the 60s wait timed out, and SPY was **never recorded**, so
  its stop and exit were never checked (open issue #1). Exits on TSLA (09-04)
  and USO (09-21) also filled after the timeout, so both stayed in state; each
  later "stop hit" retried the close, got `40410000` "position not found", and
  aborted the whole loop. Stop-based sizing with no notional cap had made SPY
  ~3.4x equity. Fixed in `8a41100` (isolated exits, position-not-found
  handling, notional caps); open issues #1 and #2 remain.
- Pre/post-market IEX bars used to leak into equity candles; equities are now
  regular-session only (`portfolio.session_bars`).
- README screenshots (`docs/screenshots/`) were rendered from the real
  dashboard fed by a 6-month backtest (via a scratch wrapper, not in the repo),
  in light and dark, with GitHub border colors (`#d0d7de` / `#30363d`).
