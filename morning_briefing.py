"""Morning briefing: pulls account state from Alpaca, summarizes risk and
market conditions, and posts a <200 word update to Discord.

Run manually with:  python3 morning_briefing.py
Scheduled daily at 7am via the "trading-bot-morning-briefing" task.

Uses plain REST calls (requests) instead of the alpaca-py SDK so this script
has no extra runtime dependencies beyond what's already installed for the
bot (requests, python-dotenv) — it will run even in environments where
alpaca-py hasn't been installed.
"""

import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from requests.exceptions import RequestException

import config  # loads .env, exposes ALPACA_*, PAPER, RISK, CORRELATION_FILTER, etc.

ET = ZoneInfo("America/New_York")

TRADING_BASE = "https://paper-api.alpaca.markets" if config.PAPER else "https://api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"
HEADERS = {
    "APCA-API-KEY-ID": config.ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
}
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
TIMEOUT = 10
MAX_RETRIES = 4


def _request(method: str, url: str, **kwargs) -> requests.Response:
    """HTTP request with retry + exponential backoff on transient failures
    (network drops, SSL blips, 5xx, 429) — the same resilience the main bot
    has. 4xx client errors are not retried; they won't fix themselves."""
    kwargs.setdefault("timeout", TIMEOUT)
    delay = 2.0
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.request(method, url, **kwargs)
            r.raise_for_status()
            return r
        except RequestException as e:
            resp = getattr(e, "response", None)
            status = resp.status_code if resp is not None else None
            if status is not None and 400 <= status < 500 and status != 429:
                raise
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(delay)
            delay *= 2


def _get(base: str, path: str, params: dict = None) -> dict:
    return _request("GET", f"{base}{path}", headers=HEADERS, params=params).json()


def get_account() -> dict:
    return _get(TRADING_BASE, "/v2/account")


def get_positions() -> list:
    return _get(TRADING_BASE, "/v2/positions")


def get_clock() -> dict:
    return _get(TRADING_BASE, "/v2/clock")


def get_snapshot(symbol: str) -> dict:
    return _get(DATA_BASE, f"/v2/stocks/{symbol}/snapshot", params={"feed": config.DATA_FEED})


def load_state() -> dict:
    if not os.path.exists(config.STATE_FILE):
        return {}
    with open(config.STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def win_rate_from_trades() -> str:
    if not os.path.exists(config.TRADES_CSV):
        return "no closed trades yet"
    wins = total = 0
    with open(config.TRADES_CSV, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pnl = row.get("pnl")
            if pnl in (None, ""):
                continue
            total += 1
            if float(pnl) > 0:
                wins += 1
    if total == 0:
        return "no closed trades yet"
    return f"{wins}/{total} ({100 * wins / total:.0f}%)"


def fmt_money(x: float) -> str:
    sign = "+" if x >= 0 else "-"
    return f"{sign}${abs(x):,.2f}"


def fmt_pct(x: float) -> str:
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:.2f}%"


def build_positions_section(positions: list) -> str:
    if not positions:
        return "Positions: none open — flat."
    lines = []
    for p in positions:
        symbol = p["symbol"]
        side = "long" if float(p["qty"]) > 0 else "short"
        qty = abs(float(p["qty"]))
        upl = float(p["unrealized_pl"])
        upl_pct = float(p["unrealized_plpc"]) * 100
        lines.append(f"{symbol} {side} {qty:g} @ {float(p['avg_entry_price']):.2f}, "
                     f"P&L {fmt_money(upl)} ({fmt_pct(upl_pct)})")
    return "Positions: " + "; ".join(lines) + "."


def build_risk_section(state: dict, account: dict) -> str:
    daily = state.get("daily", {})
    today = datetime.now(ET).date().isoformat()
    flags = []

    if daily.get("date") != today:
        flags.append("bot has not run yet today (state stale)")

    if daily.get("halted"):
        flags.append("KILL SWITCH ACTIVE — new entries halted today")
    else:
        start_equity = daily.get("start_equity")
        if start_equity:
            drawdown_pct = (start_equity - float(account["equity"])) / start_equity * 100
            threshold_pct = config.RISK["daily_drawdown_halt_pct"] * 100
            flags.append(f"drawdown {drawdown_pct:.2f}% (halt at {threshold_pct:.0f}%)")

    positions = state.get("positions", {})
    cf = config.CORRELATION_FILTER
    if all(positions.get(s, {}).get("side") == "long" for s in cf["when_all_long"]):
        flags.append(f"correlation filter active — new longs blocked on {', '.join(cf['block_new_longs'])}")

    if not flags:
        return "Risk: no flags."
    return "Risk: " + "; ".join(flags) + "."


def build_market_section(clock: dict) -> str:
    is_open = clock.get("is_open")
    status = "OPEN" if is_open else "CLOSED"
    parts = [f"Market {status}"]
    for symbol in ("SPY", "QQQ"):
        try:
            snap = get_snapshot(symbol)
            last = snap["latestTrade"]["p"]
            prev_close = snap["prevDailyBar"]["c"]
            chg_pct = (last - prev_close) / prev_close * 100
            parts.append(f"{symbol} {fmt_pct(chg_pct)}")
        except Exception:
            parts.append(f"{symbol} n/a")
    return "Market: " + ", ".join(parts) + "."


def build_message() -> str:
    account = get_account()
    positions = get_positions()
    clock = get_clock()
    state = load_state()

    equity = float(account["equity"])
    last_equity = float(account["last_equity"])
    day_pnl = equity - last_equity
    day_pnl_pct = (day_pnl / last_equity * 100) if last_equity else 0.0

    date_str = datetime.now(ET).strftime("%a %b %d, %Y")

    lines = [
        f"**Trading Bot Morning Briefing — {date_str} (ET)**",
        f"Equity: ${equity:,.2f} | Day P&L: {fmt_money(day_pnl)} ({fmt_pct(day_pnl_pct)}) | Cash: ${float(account['cash']):,.2f}",
        build_positions_section(positions),
        f"Win rate: {win_rate_from_trades()}",
        build_risk_section(state, account),
        build_market_section(clock),
    ]
    return "\n".join(lines)


def post_to_discord(message: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL not set — skipping post.", file=sys.stderr)
        return
    _request("POST", DISCORD_WEBHOOK_URL, json={"content": message})


def main() -> None:
    try:
        message = build_message()
    except Exception as e:
        error_msg = f"⚠️ Morning briefing failed to build: {e}"
        print(error_msg, file=sys.stderr)
        try:
            post_to_discord(error_msg)
        except Exception:
            pass
        sys.exit(1)

    word_count = len(message.split())
    print(message)
    print(f"\n[{word_count} words]", file=sys.stderr)

    post_to_discord(message)
    print("Posted to Discord.", file=sys.stderr)


if __name__ == "__main__":
    main()
