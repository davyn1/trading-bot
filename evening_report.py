"""Evening performance report: today's trades, P&L, best/worst trade, equity,
backtest-tracking check, and stop-loss verification — posted to Discord.

Run manually with:  python3 evening_report.py
Scheduled daily at 9pm via the "trading-bot-evening-report" task.

Same design as morning_briefing.py: plain REST calls (requests) against
Alpaca, no alpaca-py dependency required to run this script.

Backtest comparison: looks for backtest_baseline.json in the project root
(schema: {"<strategy>": {"win_rate": 0.0-1.0, "avg_trade_pnl_pct": float,
"sharpe": float}, ...}). That file doesn't exist yet — see the note this
script prints when it's missing for how to generate it. Until then this
section is omitted from the report rather than guessed at.
"""

import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

import config

ET = ZoneInfo("America/New_York")

TRADING_BASE = "https://paper-api.alpaca.markets" if config.PAPER else "https://api.alpaca.markets"
HEADERS = {
    "APCA-API-KEY-ID": config.ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
}
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
BASELINE_FILE = "backtest_baseline.json"
TIMEOUT = 10

STOP_HIT_RE = re.compile(
    r"^(?P<ts>\S+ \S+),\d+ INFO\s+bot: (?P<symbol>[\w/]+): STOP HIT at "
    r"(?P<price>[\d.]+) \(stop (?P<stop>[\d.]+), (?P<side>\w+)\)")
CLOSED_RE = re.compile(
    r"^(?P<ts>\S+ \S+),\d+ INFO\s+bot: (?P<symbol>[\w/]+): CLOSED (?P<side>\w+) "
    r"(?P<qty>[\d.]+) @ (?P<price>[\d.]+) \(fill\), P&L (?P<pnl>-?[\d.]+) — (?P<reason>.+)$")


def _get(path: str, params: dict = None) -> dict:
    r = requests.get(f"{TRADING_BASE}{path}", headers=HEADERS, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_account() -> dict:
    return _get("/v2/account")


def today_et() -> str:
    return datetime.now(ET).date().isoformat()


def parse_trade_ts_to_et_date(ts_iso: str) -> str:
    """trades.csv timestamps are UTC isoformat from datetime.now(timezone.utc)."""
    dt = datetime.fromisoformat(ts_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET).date().isoformat()


def load_today_trades() -> list:
    if not os.path.exists(config.TRADES_CSV):
        return []
    today = today_et()
    rows = []
    with open(config.TRADES_CSV, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                if parse_trade_ts_to_et_date(row["timestamp"]) == today:
                    row["pnl"] = float(row["pnl"]) if row.get("pnl") not in (None, "") else 0.0
                    rows.append(row)
            except Exception:
                continue
    return rows


def load_baseline() -> dict:
    if not os.path.exists(BASELINE_FILE):
        return {}
    try:
        with open(BASELINE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def check_stops(today: str) -> tuple:
    """Returns (stop_hit_count, list of short verdict strings) by pairing
    STOP HIT log lines with the CLOSED line that follows them in bot.log."""
    if not os.path.exists(config.LOG_FILE):
        return 0, []
    with open(config.LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    verdicts = []
    for i, line in enumerate(lines):
        m = STOP_HIT_RE.match(line)
        if not m or not m.group("ts").startswith(today):
            continue
        symbol = m.group("symbol")
        decision_price = float(m.group("price"))
        # The matching CLOSED line is emitted right after, for the same symbol.
        for follow in lines[i + 1:i + 5]:
            cm = CLOSED_RE.match(follow)
            if cm and cm.group("symbol") == symbol and "stop hit" in cm.group("reason"):
                fill_price = float(cm.group("price"))
                slip_pct = abs(fill_price - decision_price) / decision_price * 100
                verdict = "OK" if slip_pct < 0.3 else f"slipped {slip_pct:.2f}%"
                verdicts.append(f"{symbol} ({verdict})")
                break
        else:
            verdicts.append(f"{symbol} (fill unconfirmed)")
    return len(verdicts), verdicts


def fmt_money(x: float) -> str:
    sign = "+" if x >= 0 else "-"
    return f"{sign}${abs(x):,.2f}"


def fmt_pct(x: float) -> str:
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:.2f}%"


def build_message() -> str:
    account = get_account()
    equity = float(account["equity"])
    last_equity = float(account["last_equity"])
    day_pnl = equity - last_equity
    day_pnl_pct = (day_pnl / last_equity * 100) if last_equity else 0.0

    trades = load_today_trades()
    today = today_et()
    date_str = datetime.now(ET).strftime("%a %b %d, %Y")

    lines = [f"**Trading Bot Evening Report — {date_str} (ET)**"]

    if not trades:
        lines.append("Trades today: none — bot stayed flat, no entries or exits.")
    else:
        by_instrument = {}
        for t in trades:
            by_instrument[t["instrument"]] = by_instrument.get(t["instrument"], 0) + 1
        instr_str = ", ".join(f"{sym} x{n}" for sym, n in by_instrument.items())
        lines.append(f"Trades today: {len(trades)} ({instr_str})")

        best = max(trades, key=lambda t: t["pnl"])
        worst = min(trades, key=lambda t: t["pnl"])
        lines.append(f"Best: {best['instrument']} {best['direction']} "
                     f"{best['entry_price']}->{best['exit_price']}, {fmt_money(best['pnl'])}")
        if worst is not best:
            lines.append(f"Worst: {worst['instrument']} {worst['direction']} "
                         f"{worst['entry_price']}->{worst['exit_price']}, {fmt_money(worst['pnl'])}")

    lines.append(f"P&L: {fmt_money(day_pnl)} ({fmt_pct(day_pnl_pct)}) | Equity: ${equity:,.2f}")

    stop_count, stop_verdicts = check_stops(today)
    if stop_count:
        lines.append(f"Stops hit: {stop_count} — {', '.join(stop_verdicts)}")
    else:
        lines.append("Stops hit: none today.")

    baseline = load_baseline()
    if not baseline:
        lines.append("Backtest tracking: no baseline on file yet — "
                     "generate backtest_baseline.json to enable this check.")
    else:
        strategies_today = {t["strategy"] for t in trades}
        notes = []
        for strat in strategies_today:
            exp = baseline.get(strat)
            if not exp:
                continue
            strat_trades = [t for t in trades if t["strategy"] == strat]
            wins = sum(1 for t in strat_trades if t["pnl"] > 0)
            wr = wins / len(strat_trades) if strat_trades else 0
            exp_wr = exp.get("win_rate", 0)
            diverging = abs(wr - exp_wr) > 0.25
            notes.append(f"{strat} {'diverging' if diverging else 'on track'} "
                         f"({wr:.0%} vs {exp_wr:.0%} expected)")
        if notes:
            lines.append("Backtest tracking: " + "; ".join(notes) + ".")
        else:
            lines.append("Backtest tracking: no trades today to compare.")

    return "\n".join(lines)


def post_to_discord(message: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL not set — skipping post.", file=sys.stderr)
        return
    r = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=TIMEOUT)
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Discord webhook returned {r.status_code}: {r.text}")


def main() -> None:
    try:
        message = build_message()
    except Exception as e:
        error_msg = f"⚠️ Evening report failed to build: {e}"
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
