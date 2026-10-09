"""Backtester: runs the live strategy code against Alpaca historical data.

Usage:  python -m bot.backtest [--months 6] [--max-position 0.5]
                               [--max-exposure 1.0] [--save-baseline]

Simulation model (chosen to mirror the live bot, not to flatter results):
  - Signals evaluate on completed candles via the SAME Strategy classes the
    bot trades with; indicators see only data up to the signal bar.
  - Orders fill at the NEXT bar's open (the bot submits market orders seconds
    after a candle closes), with 0.05% adverse slippage on EVERY fill and $0
    commission (Alpaca).
  - Stops are checked intrabar against each bar's high/low; a gap through the
    stop fills at the open (worse than the stop), like a real market order.
  - Trailing stops recalculate on each completed bar from the highest high /
    lowest low since entry, and never move against the position.
  - Position sizing: 1% of current equity to the initial stop, capped by the
    same per-position and total-exposure notional limits as the live bot.
  - Extra history is fetched BEFORE the test window so every indicator is
    fully warmed up from the first tested bar.
Per-instrument runs are independent ($100k each). The combined run shares one
$100k account across all instruments with the correlation filter active.
"""

import argparse
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

import config
from bot.portfolio import Broker
from bot.risk_manager import cap_notional, is_crypto
from bot.strategies import build_strategy
from bot.strategies.base import Signal, atr as atr_fn

log = logging.getLogger("backtest")

SLIPPAGE = 0.0005          # 0.05% adverse, applied to every fill
START_EQUITY = 100_000.0
COMMISSION = 0.0           # Alpaca is commission-free

# Equity bars per 6.5h regular session (partial first/last bars rounded up).
# Crypto history is sized from calendar hours instead, so it doesn't use this.
BARS_PER_DAY = {15: 26, 30: 13, 60: 7, 120: 4, 240: 2}
TRADING_DAYS_PER_MONTH = 21
CAL_DAYS_PER_MONTH = 30.44
BASELINE_FILE = "backtest_baseline.json"  # read by evening_report.py


@dataclass
class Position:
    side: str
    qty: float
    entry_price: float
    entry_time: pd.Timestamp
    stop_price: float
    trailing: bool
    stop_mult: float
    highest_high: float
    lowest_low: float


@dataclass
class Trade:
    symbol: str
    strategy: str
    side: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    reason: str


@dataclass
class Pending:
    action: str                      # enter | exit | reverse
    side: Optional[str] = None
    atr: float = 0.0
    reason: str = ""


def buy_fill(price: float) -> float:
    return price * (1 + SLIPPAGE)


def sell_fill(price: float) -> float:
    return price * (1 - SLIPPAGE)


class Engine:
    """Event-driven simulator over one or many instruments sharing an account."""

    def __init__(self, bars: Dict[str, pd.DataFrame], window_start: pd.Timestamp,
                 use_correlation_filter: bool = False):
        self.bars = bars
        self.window_start = window_start
        self.use_corr = use_correlation_filter
        self.strategies = {s: build_strategy(s) for s in bars}
        self.scfg = {s: config.STRATEGIES[config.INSTRUMENTS[s]["strategy"]]
                     for s in bars}
        self.cash = START_EQUITY
        self.positions: Dict[str, Position] = {}
        self.pending: Dict[str, Pending] = {}
        self.trades: List[Trade] = []
        self.curve: List[tuple] = []          # (timestamp, equity)
        self.last_close: Dict[str, float] = {}
        self.corr_blocked = 0

    # --- accounting ---------------------------------------------------------
    def equity(self) -> float:
        eq = self.cash
        for sym, p in self.positions.items():
            price = self.last_close.get(sym, p.entry_price)
            eq += p.qty * price if p.side == "long" else -p.qty * price
        return eq

    def _open(self, sym: str, side: str, fill: float, atr: float,
              when: pd.Timestamp) -> None:
        scfg = self.scfg[sym]
        dist = scfg["stop_atr_mult"] * atr
        if dist <= 0:
            return
        eq = self.equity()
        qty = (eq * scfg["risk_pct"]) / dist
        exposure = sum(p.qty * self.last_close.get(s, p.entry_price)
                       for s, p in self.positions.items())
        qty = cap_notional(sym, qty, fill, eq, exposure)
        if is_crypto(sym):
            qty = np.floor(qty * 1e6) / 1e6
        elif side == "short":
            qty = float(int(qty))
        else:
            qty = np.floor(qty * 1e3) / 1e3
        if qty <= 0:
            return
        if side == "long":
            self.cash -= qty * fill
            stop = fill - dist
        else:
            self.cash += qty * fill
            stop = fill + dist
        self.positions[sym] = Position(
            side=side, qty=qty, entry_price=fill, entry_time=when, stop_price=stop,
            trailing=scfg["trailing"], stop_mult=scfg["stop_atr_mult"],
            highest_high=fill, lowest_low=fill)

    def _close(self, sym: str, fill: float, when: pd.Timestamp, reason: str) -> None:
        p = self.positions.pop(sym)
        if p.side == "long":
            self.cash += p.qty * fill
            pnl = (fill - p.entry_price) * p.qty
        else:
            self.cash -= p.qty * fill
            pnl = (p.entry_price - fill) * p.qty
        self.trades.append(Trade(
            symbol=sym, strategy=self.strategies[sym].name, side=p.side,
            entry_time=p.entry_time, exit_time=when, entry_price=p.entry_price,
            exit_price=fill, qty=p.qty, pnl=pnl, reason=reason))

    def _corr_filter_blocks(self, sym: str, side: str) -> bool:
        if not self.use_corr:
            return False
        cf = config.CORRELATION_FILTER
        if side != "long" or sym not in cf["block_new_longs"]:
            return False
        return all(self.positions.get(s) is not None
                   and self.positions[s].side == "long"
                   for s in cf["when_all_long"])

    # --- main loop ----------------------------------------------------------
    def run(self) -> None:
        events = []   # (close_time, symbol, bar_index)
        for sym, df in self.bars.items():
            tf = self.scfg[sym]["timeframe_minutes"]
            for i, ts in enumerate(df.index):
                events.append((ts + pd.Timedelta(minutes=tf), sym, i))
        events.sort(key=lambda e: e[0])

        slice_len = {s: self.strategies[s].warmup_bars + 150 for s in self.bars}
        last_index = {s: len(df) - 1 for s, df in self.bars.items()}

        for t_close, sym, i in events:
            df = self.bars[sym]
            bar = df.iloc[i]
            strat = self.strategies[sym]

            # 1. Fill the order queued on the previous bar at this bar's open.
            pend = self.pending.pop(sym, None)
            if pend is not None:
                if pend.action in ("exit", "reverse") and sym in self.positions:
                    p = self.positions[sym]
                    fill = sell_fill(bar["open"]) if p.side == "long" \
                        else buy_fill(bar["open"])
                    self._close(sym, fill, df.index[i], pend.reason)
                if pend.action in ("enter", "reverse") and sym not in self.positions:
                    if self._corr_filter_blocks(sym, pend.side):
                        self.corr_blocked += 1
                    else:
                        fill = buy_fill(bar["open"]) if pend.side == "long" \
                            else sell_fill(bar["open"])
                        self._open(sym, pend.side, fill, pend.atr, df.index[i])

            # 2. Intrabar stop check (stop level from BEFORE this bar).
            p = self.positions.get(sym)
            if p is not None:
                if p.side == "long" and bar["low"] <= p.stop_price:
                    fill = sell_fill(min(bar["open"], p.stop_price))
                    self._close(sym, fill, df.index[i], "stop hit")
                elif p.side == "short" and bar["high"] >= p.stop_price:
                    fill = buy_fill(max(bar["open"], p.stop_price))
                    self._close(sym, fill, df.index[i], "stop hit")

            self.last_close[sym] = float(bar["close"])

            # 3. Trailing-stop update on the completed bar.
            p = self.positions.get(sym)
            lo = max(0, i + 1 - slice_len[sym])
            window = df.iloc[lo:i + 1]
            if p is not None and p.trailing:
                cur_atr = float(atr_fn(window, strat.params["atr_period"]).iloc[-1])
                dist = p.stop_mult * cur_atr
                if p.side == "long":
                    p.highest_high = max(p.highest_high, float(bar["high"]))
                    p.stop_price = max(p.stop_price, p.highest_high - dist)
                else:
                    p.lowest_low = min(p.lowest_low, float(bar["low"]))
                    p.stop_price = min(p.stop_price, p.lowest_low + dist)

            # 4. Evaluate the strategy on completed candles (inside the window,
            #    fully warmed up, and never on the final bar — no next open).
            if (t_close >= self.window_start and len(window) >= strat.warmup_bars
                    and i < last_index[sym]):
                pos_view = {"side": p.side} if (p := self.positions.get(sym)) else None
                sig: Optional[Signal] = strat.evaluate(window, pos_view)
                if sig is not None:
                    if sig.action == "exit" and sym in self.positions:
                        self.pending[sym] = Pending("exit", reason=sig.reason)
                    elif sig.action == "enter" and sym not in self.positions:
                        self.pending[sym] = Pending("enter", side=sig.side,
                                                    atr=sig.atr, reason=sig.reason)
                    elif sig.action == "reverse":
                        self.pending[sym] = Pending("reverse", side=sig.side,
                                                    atr=sig.atr, reason=sig.reason)

            if t_close >= self.window_start:
                self.curve.append((t_close, self.equity()))

        # Force-close anything still open at the last seen price so the final
        # equity and per-trade stats are fully realized.
        for sym in list(self.positions):
            price = self.last_close[sym]
            p = self.positions[sym]
            fill = sell_fill(price) if p.side == "long" else buy_fill(price)
            self._close(sym, fill, self.bars[sym].index[-1], "end of backtest")
        if self.curve:
            self.curve[-1] = (self.curve[-1][0], self.equity())


# --- metrics ----------------------------------------------------------------
def compute_metrics(trades: List[Trade], curve: List[tuple],
                    annualization: int) -> dict:
    eq = pd.Series({t: v for t, v in curve}).sort_index()
    daily = eq.resample("1D").last().ffill().dropna()
    rets = daily.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(annualization)) \
        if len(rets) > 1 and rets.std() > 0 else 0.0
    max_dd = float((eq / eq.cummax() - 1.0).min()) if len(eq) else 0.0
    total_ret = float(eq.iloc[-1] / START_EQUITY - 1.0) if len(eq) else 0.0

    pnls = [t.pnl for t in trades]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    gross_w, gross_l = sum(wins), abs(sum(losses))
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(pnls) if pnls else 0.0,
        "avg_win": float(np.mean(wins)) if wins else 0.0,
        "avg_loss": float(np.mean(losses)) if losses else 0.0,
        "profit_factor": gross_w / gross_l if gross_l > 0 else float("inf") if gross_w else 0.0,
        "max_dd": max_dd,
        "sharpe": sharpe,
        "total_return": total_ret,
        "equity": eq,
        "daily": daily,
    }


# --- data -------------------------------------------------------------------
def fetch_history(broker: Broker, months: int) -> Dict[str, pd.DataFrame]:
    bars = {}
    for sym in config.INSTRUMENTS:
        scfg = config.STRATEGIES[config.INSTRUMENTS[sym]["strategy"]]
        tf = scfg["timeframe_minutes"]
        strat = build_strategy(sym)
        if is_crypto(sym):
            window_bars = int((60 / tf) * 24 * CAL_DAYS_PER_MONTH * months)
        else:
            window_bars = int(BARS_PER_DAY[tf] * TRADING_DAYS_PER_MONTH * months)
        need = window_bars + strat.warmup_bars + 160
        log.info("Fetching %s: %d bars of %dm candles (window %d + warmup %d)...",
                 sym, need, tf, window_bars, strat.warmup_bars + 160)
        df = broker.get_bars(sym, tf, need)
        if df.empty:
            raise RuntimeError(f"No historical data returned for {sym}")
        log.info("  got %d bars: %s -> %s", len(df), df.index[0], df.index[-1])
        bars[sym] = df
    return bars


# --- chart ------------------------------------------------------------------
# dataviz reference palette (light mode), fixed order per instrument.
# Instruments without a fixed color draw from EXTRA_COLORS in config order.
PALETTE = {"SPY": "#2a78d6", "QQQ": "#008300", "BTC/USD": "#e87ba4",
           "GLD": "#eda100", "USO": "#1baf7a"}
EXTRA_COLORS = ["#7c5cd6", "#d6452a", "#4a4a45", "#00a3b4", "#b36b00",
                "#9c2a8a", "#5f8f00", "#c2185b"]


def color_for(sym: str) -> str:
    if sym in PALETTE:
        return PALETTE[sym]
    extras = [s for s in config.INSTRUMENTS if s not in PALETTE]
    return EXTRA_COLORS[extras.index(sym) % len(EXTRA_COLORS)]
CHART_INK = {"surface": "#fcfcfb", "primary": "#0b0b0b", "secondary": "#52514e",
             "muted": "#898781", "grid": "#e1e0d9"}


def plot_results(single: dict, combined: dict, months: int, path: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 9), dpi=150)
    fig.patch.set_facecolor(CHART_INK["surface"])

    for ax, title in ((ax1, f"Combined portfolio — correlation filter active "
                            f"(${START_EQUITY:,.0f} start)"),
                      (ax2, "Per-instrument equity (independent "
                            f"${START_EQUITY:,.0f} accounts)")):
        ax.set_facecolor(CHART_INK["surface"])
        ax.grid(color=CHART_INK["grid"], linewidth=1)
        for spine in ax.spines.values():
            spine.set_color(CHART_INK["grid"])
        ax.set_title(title, color=CHART_INK["primary"], fontsize=12, loc="left")
        ax.tick_params(colors=CHART_INK["muted"], labelsize=9)

    ax1.plot(combined["daily"].index, combined["daily"].values,
             color=CHART_INK["primary"], linewidth=2)
    ax1.axhline(START_EQUITY, color=CHART_INK["muted"], linewidth=1, linestyle=":")

    for sym, m in single.items():
        ax2.plot(m["daily"].index, m["daily"].values, color=color_for(sym),
                 linewidth=2, label=sym)
    ax2.axhline(START_EQUITY, color=CHART_INK["muted"], linewidth=1, linestyle=":")
    ax2.legend(loc="upper left", frameon=False, fontsize=9, ncol=2,
               labelcolor=CHART_INK["secondary"])

    fig.suptitle(f"Backtest — last {months} months, 0.05% slippage per fill",
                 color=CHART_INK["secondary"], fontsize=10, y=0.995)
    fig.tight_layout()
    fig.savefig(path, facecolor=fig.get_facecolor())
    plt.close(fig)


# --- report -----------------------------------------------------------------
def annualization_for(sym: str) -> int:
    return 365 if is_crypto(sym) else 252


def main() -> None:
    global SLIPPAGE
    # Windows consoles default to cp1252, which can't print the ⚠ flag.
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--slippage", type=float, default=SLIPPAGE,
                        help="slippage per fill as a fraction (default 0.0005 = 0.05%%)")
    parser.add_argument("--max-position", type=float,
                        default=config.RISK["max_position_pct_equity"],
                        help="per-position notional cap as a multiple of equity "
                             "(default from config.RISK)")
    parser.add_argument("--max-exposure", type=float,
                        default=config.RISK["max_total_exposure_pct_equity"],
                        help="total-exposure notional cap as a multiple of equity "
                             "(default from config.RISK)")
    parser.add_argument("--save-baseline", action="store_true",
                        help=f"write per-strategy results to {BASELINE_FILE} "
                             "(read by evening_report.py)")
    args = parser.parse_args()
    SLIPPAGE = args.slippage
    # Overrides apply to this run only; config.py on disk is unchanged.
    config.RISK["max_position_pct_equity"] = args.max_position
    config.RISK["max_total_exposure_pct_equity"] = args.max_exposure

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    broker = Broker()
    window_start = pd.Timestamp(datetime.now(timezone.utc)
                                - timedelta(days=CAL_DAYS_PER_MONTH * args.months))
    log.info("Test window: %s -> now (%d months)", window_start.date(), args.months)

    bars = fetch_history(broker, args.months)

    # Per-instrument runs (independent accounts).
    single_metrics, all_single_trades = {}, {}
    for sym in bars:
        log.info("Backtesting %s (%s)...", sym, config.INSTRUMENTS[sym]["strategy"])
        eng = Engine({sym: bars[sym]}, window_start)
        eng.run()
        single_metrics[sym] = compute_metrics(eng.trades, eng.curve,
                                              annualization_for(sym))
        all_single_trades[sym] = eng.trades

    # Combined portfolio with the correlation filter.
    log.info("Backtesting combined portfolio (correlation filter active)...")
    combo = Engine(bars, window_start, use_correlation_filter=True)
    combo.run()
    combo_metrics = compute_metrics(combo.trades, combo.curve, 252)

    plot_results(single_metrics, combo_metrics, args.months, "backtest_results.png")

    # --- summary tables -----------------------------------------------------
    rows = []
    for sym, m in single_metrics.items():
        strat = config.INSTRUMENTS[sym]["strategy"]
        rows.append({
            "Instrument": sym, "Strategy": strat, "Trades": m["trades"],
            "Win rate": f"{m['win_rate']:.0%}",
            "Avg win": f"{m['avg_win']:,.2f}", "Avg loss": f"{m['avg_loss']:,.2f}",
            "PF": f"{m['profit_factor']:.2f}" if np.isfinite(m["profit_factor"]) else "inf",
            "Max DD": f"{m['max_dd']:.2%}", "Sharpe": f"{m['sharpe']:.2f}",
            "Return": f"{m['total_return']:+.2%}",
            "Flag": "⚠ NEGATIVE SHARPE — tune params" if m["sharpe"] < 0 and m["trades"] > 0
                    else ("no trades" if m["trades"] == 0 else ""),
        })
    table = pd.DataFrame(rows)

    # Strategy-level aggregate: sum the daily equity curves of the instruments
    # sharing a strategy, then measure Sharpe on the summed curve.
    strat_rows = []
    for strat in config.STRATEGIES:
        syms = [s for s in single_metrics
                if config.INSTRUMENTS[s]["strategy"] == strat]
        if not syms:
            continue
        summed = None
        trades_n, pnls = 0, []
        for s in syms:
            d = single_metrics[s]["daily"]
            summed = d if summed is None else summed.add(d, fill_value=None).ffill()
            trades_n += single_metrics[s]["trades"]
            pnls += [t.pnl for t in all_single_trades[s]]
        rets = summed.pct_change().dropna()
        sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) \
            if len(rets) > 1 and rets.std() > 0 else 0.0
        wins = [x for x in pnls if x > 0]
        strat_rows.append({
            "Strategy": strat, "Instruments": ", ".join(syms), "Trades": trades_n,
            "Win rate": f"{len(wins)/len(pnls):.0%}" if pnls else "—",
            "Total P&L": f"{sum(pnls):,.2f}", "Sharpe": f"{sharpe:.2f}",
            "Flag": "⚠ NEGATIVE SHARPE — tune params" if sharpe < 0 and trades_n > 0
                    else ("no trades" if trades_n == 0 else ""),
        })
    strat_table = pd.DataFrame(strat_rows)

    print("\n" + "=" * 100)
    print(f"BACKTEST RESULTS — last {args.months} months, fills at next bar open, "
          f"{SLIPPAGE:.2%} slippage per fill, $0 commission")
    print(f"Notional caps: {args.max_position:g}x equity per position, "
          f"{args.max_exposure:g}x total exposure")
    print("=" * 100)
    print("\nPer instrument (independent $100k accounts):\n")
    print(table.to_string(index=False))
    print("\nPer strategy (instrument curves summed):\n")
    print(strat_table.to_string(index=False))
    m = combo_metrics
    print("\nCombined portfolio (single $100k account, correlation filter active):")
    print(f"  Trades {m['trades']} · Win rate {m['win_rate']:.0%} · "
          f"PF {m['profit_factor']:.2f} · Max DD {m['max_dd']:.2%} · "
          f"Sharpe {m['sharpe']:.2f} · Return {m['total_return']:+.2%}")
    print(f"  Entries blocked by correlation filter: {combo.corr_blocked}")
    if m["sharpe"] < 0:
        print("  ⚠ Combined portfolio Sharpe is NEGATIVE over this window.")
    print("\nEquity curve chart saved to backtest_results.png")

    if args.save_baseline:
        save_baseline(single_metrics, all_single_trades, args, window_start)
        print(f"Baseline saved to {BASELINE_FILE}")


def save_baseline(single_metrics: dict, single_trades: dict, args,
                  window_start: pd.Timestamp) -> None:
    """Write per-strategy stats in the shape evening_report.py expects."""
    import json

    per_instrument = {
        sym: {"trades": m["trades"], "win_rate": round(m["win_rate"], 4),
              "sharpe": round(m["sharpe"], 3),
              "total_return": round(m["total_return"], 4),
              "max_dd": round(m["max_dd"], 4),
              "strategy": config.INSTRUMENTS[sym]["strategy"]}
        for sym, m in single_metrics.items()
    }
    baseline = {}
    for strat in config.STRATEGIES:
        syms = [s for s in single_metrics if config.INSTRUMENTS[s]["strategy"] == strat]
        pnls = [t.pnl for s in syms for t in single_trades[s]]
        if not pnls:
            continue
        # Trade-count-weighted average of each instrument's Sharpe.
        sharpe = sum(single_metrics[s]["sharpe"] * single_metrics[s]["trades"]
                     for s in syms) / len(pnls)
        baseline[strat] = {
            "win_rate": round(sum(1 for x in pnls if x > 0) / len(pnls), 4),
            "avg_trade_pnl_pct": round(100 * float(np.mean(pnls)) / START_EQUITY, 4),
            "sharpe": round(sharpe, 3),
            "trades": len(pnls),
            "instruments": syms,
        }
    baseline["_meta"] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "months": args.months,
        "window_start": window_start.isoformat(),
        "slippage": SLIPPAGE,
        "max_position_pct_equity": args.max_position,
        "max_total_exposure_pct_equity": args.max_exposure,
        "note": "Per-strategy stats pooled from independent single-instrument "
                "backtests (python -m bot.backtest --save-baseline). "
                "avg_trade_pnl_pct = mean trade P&L as % of a $100k starting "
                "account. sharpe is trade-count-weighted average of each "
                "instrument's daily-curve Sharpe.",
        "per_instrument": per_instrument,
    }
    with open(BASELINE_FILE, "w", encoding="utf-8") as f:
        json.dump(baseline, f, indent=2)


if __name__ == "__main__":
    main()
