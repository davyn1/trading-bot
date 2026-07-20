"""Bot orchestrator. Run from the project root with:  python -m bot.main

Loop responsibilities, every POLL_INTERVAL_SECONDS:
  1. Daily rollover + kill-switch check (realized + unrealized, vs 3% config).
  2. Stop-trigger checks on every price update for open positions.
  3. Per-symbol candle evaluation whenever a new candle has completed on that
     strategy's timeframe (completed candles only, ET-aligned boundaries).
Equity symbols are only evaluated/traded while the market is open; crypto
runs 24/7. On startup, state is reconciled against Alpaca's actual positions.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import config
from bot.portfolio import Broker, trade_symbol
from bot.risk_manager import RiskManager, is_crypto
from bot.state import StateStore
from bot.strategies import Signal, build_strategy

ET = ZoneInfo("America/New_York")
log = logging.getLogger("bot")


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (logging.StreamHandler(), logging.FileHandler(config.LOG_FILE, encoding="utf-8")):
        handler.setFormatter(fmt)
        root.addHandler(handler)


def now_et() -> datetime:
    return datetime.now(timezone.utc).astimezone(ET)


def last_boundary(ts_et: datetime, timeframe_minutes: int) -> datetime:
    """Most recent candle boundary at or before ts, anchored to ET midnight so
    equity candles align to US/Eastern sessions (15m/1h boundaries coincide
    with UTC ones; the 4h anchor is what actually needs ET alignment)."""
    midnight = ts_et.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed_min = int((ts_et - midnight).total_seconds() // 60)
    return midnight + timedelta(minutes=(elapsed_min // timeframe_minutes) * timeframe_minutes)


class Bot:
    def __init__(self):
        self.broker = Broker()
        self.state = StateStore()
        self.risk = RiskManager(self.state)
        self.strategies = {sym: build_strategy(sym) for sym in config.INSTRUMENTS}
        self.strategy_cfg = {sym: config.STRATEGIES[config.INSTRUMENTS[sym]["strategy"]]
                             for sym in config.INSTRUMENTS}
        # In-memory: which boundary we last *successfully* evaluated per symbol.
        self._evaluated_boundary = {}
        self._warmup_warned = set()

    # ------------------------------------------------------------------ setup
    def reconcile(self) -> None:
        """Match Alpaca's actual positions/orders to our state so a restart
        resumes managing everything with stops intact."""
        log.info("Reconciling state with Alpaca...")
        alpaca_pos = self.broker.open_positions()

        # Stale open orders on managed symbols: the bot only uses market
        # orders, so anything still open is a leftover — cancel it.
        managed = {trade_symbol(s) for s in config.INSTRUMENTS}
        for order in self.broker.open_orders():
            if order.symbol in managed:
                log.warning("Canceling stale open order %s (%s %s %s)",
                            order.id, order.side, order.qty, order.symbol)
                self.broker.cancel_order(order.id)

        for symbol in config.INSTRUMENTS:
            record = self.state.get_position(symbol)
            live = alpaca_pos.get(trade_symbol(symbol))

            if live is None and record is not None:
                log.warning("%s: position in state but not at Alpaca (closed while "
                            "offline?) — logging trade with last price and dropping it", symbol)
                exit_price = self.broker.latest_price(symbol)
                self._log_closed_trade(record, symbol, exit_price)
                self.state.remove_position(symbol)
                continue

            if live is not None:
                live_qty = abs(float(live.qty))
                live_side = "long" if float(live.qty) > 0 else "short"
                if record is not None and record["side"] == live_side:
                    if abs(record["qty"] - live_qty) > 1e-9:
                        log.warning("%s: qty drift (state %.6f vs Alpaca %.6f) — using Alpaca's",
                                    symbol, record["qty"], live_qty)
                        record["qty"] = live_qty
                        self.state.set_position(symbol, record)
                    log.info("%s: resumed %s %.6f @ %.4f, stop %.4f", symbol, live_side,
                             live_qty, record["entry_price"], record["stop_price"])
                else:
                    self._adopt_position(symbol, live_side, live_qty,
                                         float(live.avg_entry_price))

        unmanaged = set(alpaca_pos) - managed
        if unmanaged:
            log.warning("Unmanaged positions at Alpaca (left untouched): %s",
                        ", ".join(sorted(unmanaged)))
        log.info("Reconciliation complete: managing %d position(s)", len(self.state.positions))

    def _adopt_position(self, symbol: str, side: str, qty: float, entry: float) -> None:
        """A position exists at Alpaca with no (matching) state — rebuild its
        stop from the current ATR so it is never left unprotected."""
        cfg = self.strategy_cfg[symbol]
        strat = self.strategies[symbol]
        df = self.broker.get_bars(symbol, cfg["timeframe_minutes"], strat.warmup_bars)
        from bot.strategies.base import atr as atr_fn
        cur_atr = float(atr_fn(df, strat.params["atr_period"]).iloc[-1])
        dist = cfg["stop_atr_mult"] * cur_atr
        record = self._new_record(symbol, side, qty, entry, dist, cur_atr,
                                  entry_time=datetime.now(timezone.utc).isoformat())
        self.state.set_position(symbol, record)
        log.warning("%s: adopted unmatched Alpaca position (%s %.6f @ %.4f), "
                    "reconstructed stop at %.4f from current ATR",
                    symbol, side, qty, entry, record["stop_price"])

    def _new_record(self, symbol: str, side: str, qty: float, entry: float,
                    stop_distance: float, cur_atr: float, entry_time: str) -> dict:
        cfg = self.strategy_cfg[symbol]
        return {
            "strategy": config.INSTRUMENTS[symbol]["strategy"],
            "side": side,
            "qty": qty,
            "entry_price": entry,
            "entry_time": entry_time,
            "stop_price": self.risk.initial_stop(entry, side, stop_distance),
            "trailing": cfg["trailing"],
            "stop_atr_mult": cfg["stop_atr_mult"],
            "highest_high": entry if side == "long" else None,
            "lowest_low": entry if side == "short" else None,
            "last_atr": cur_atr,
        }

    # ------------------------------------------------------------- daily P&L
    def roll_daily(self) -> None:
        equity = self.broker.equity()
        d = self.state.daily
        today = now_et().date().isoformat()
        if d.get("date") != today:
            if d.get("date"):
                self.state.log_daily(date=d["date"], start_equity=d["start_equity"],
                                     end_equity=d.get("last_equity", equity),
                                     realized_pnl=d.get("realized_pnl", 0.0),
                                     halted=d.get("halted", False))
            self.state.data["daily"] = {"date": today, "start_equity": equity,
                                        "last_equity": equity, "realized_pnl": 0.0,
                                        "halted": False}
            log.info("New ET trading day %s, start equity %.2f", today, equity)
        d = self.state.daily
        d["last_equity"] = equity
        self.state.save()
        self.risk.check_kill_switch(equity)

    # ------------------------------------------------------------ stop checks
    def check_stops(self) -> None:
        market_open = self.broker.market_open()
        for symbol in list(self.state.positions):
            if symbol not in config.INSTRUMENTS:
                continue
            if not is_crypto(symbol) and not market_open:
                continue  # can't exit while the market is closed
            pos = self.state.get_position(symbol)
            try:
                price = self.broker.latest_price(symbol)
            except Exception as e:
                log.warning("%s: price check failed (%s) — will retry next loop", symbol, e)
                continue
            if self.risk.stop_hit(pos, price):
                log.info("%s: STOP HIT at %.4f (stop %.4f, %s)", symbol, price,
                         pos["stop_price"], pos["side"])
                self.close_position(symbol, reason="stop hit")

    # ------------------------------------------------------ candle evaluation
    def evaluate_candles(self) -> None:
        ts = now_et()
        market_open = self.broker.market_open()
        for symbol, strat in self.strategies.items():
            cfg = self.strategy_cfg[symbol]
            if not is_crypto(symbol) and not market_open:
                continue
            boundary = last_boundary(ts, cfg["timeframe_minutes"])
            if self._evaluated_boundary.get(symbol) == boundary:
                continue
            try:
                self._evaluate_symbol(symbol, strat, cfg, boundary)
            except Exception as e:
                log.error("%s: evaluation failed: %s", symbol, e, exc_info=True)

    def _evaluate_symbol(self, symbol, strat, cfg, boundary) -> None:
        df = self.broker.get_bars(symbol, cfg["timeframe_minutes"], strat.warmup_bars)
        if len(df) < strat.warmup_bars:
            if symbol not in self._warmup_warned:
                log.info("%s: %d/%d bars — indicators not warmed up, no signals yet",
                         symbol, len(df), strat.warmup_bars)
                self._warmup_warned.add(symbol)
            self._evaluated_boundary[symbol] = boundary
            return

        last_ts = df.index[-1].isoformat()
        if self.state.last_candle(symbol) == last_ts:
            return  # newest bar not published yet — retry next loop

        pos = self.state.get_position(symbol)
        candle = df.iloc[-1]

        # Trailing stop upkeep happens on every completed candle, before the
        # strategy gets a say.
        if pos is not None:
            from bot.strategies.base import atr as atr_fn
            cur_atr = float(atr_fn(df, strat.params["atr_period"]).iloc[-1])
            if self.risk.update_trailing_stop(pos, float(candle["high"]),
                                              float(candle["low"]), cur_atr):
                log.info("%s: trailing stop moved to %.4f", symbol, pos["stop_price"])
            self.state.set_position(symbol, pos)

        signal = strat.evaluate(df, pos)
        self.state.set_last_candle(symbol, last_ts)
        self._evaluated_boundary[symbol] = boundary
        if signal is None:
            return
        log.info("%s [%s] signal: %s %s — %s", symbol, strat.name, signal.action,
                 signal.side or "", signal.reason)
        self.handle_signal(symbol, signal)

    # ------------------------------------------------------- order execution
    def handle_signal(self, symbol: str, sig: Signal) -> None:
        if sig.action == "exit":
            self.close_position(symbol, reason=sig.reason)
        elif sig.action == "enter":
            self.open_position(symbol, sig)
        elif sig.action == "reverse":
            self.close_position(symbol, reason=f"reversal: {sig.reason}")
            self.open_position(symbol, sig)

    def open_position(self, symbol: str, sig: Signal) -> None:
        side = sig.side
        if self.state.get_position(symbol) is not None:
            return
        if is_crypto(symbol) and side == "short":
            log.info("%s: short signal ignored — spot crypto is long/flat only", symbol)
            return
        blocked = self.risk.entry_blocked(symbol, side)
        if blocked:
            log.info("%s: entry blocked — %s", symbol, blocked)
            return

        cfg = self.strategy_cfg[symbol]
        stop_distance = cfg["stop_atr_mult"] * sig.atr
        account = self.broker.account()
        price = self.broker.latest_price(symbol)
        qty = self.risk.position_size(
            symbol=symbol, side=side, equity=float(account.equity),
            buying_power=float(account.buying_power), price=price,
            stop_distance=stop_distance, risk_pct=cfg["risk_pct"])
        if qty <= 0:
            return

        fill_price, fill_qty = self.broker.market_order(
            symbol, "buy" if side == "long" else "sell", qty)
        record = self._new_record(symbol, side, fill_qty, fill_price, stop_distance,
                                  sig.atr, entry_time=datetime.now(timezone.utc).isoformat())
        self.state.set_position(symbol, record)
        log.info("%s: OPENED %s %.6f @ %.4f (fill), stop %.4f, risking %.2f (%.1f%%)",
                 symbol, side, fill_qty, fill_price, record["stop_price"],
                 float(account.equity) * cfg["risk_pct"], cfg["risk_pct"] * 100)

    def close_position(self, symbol: str, reason: str = "") -> None:
        pos = self.state.get_position(symbol)
        if pos is None:
            return
        fill_price, fill_qty = self.broker.close_position_market(symbol)
        pnl = self._log_closed_trade(pos, symbol, fill_price, fill_qty)
        self.state.daily["realized_pnl"] = self.state.daily.get("realized_pnl", 0.0) + pnl
        self.state.remove_position(symbol)
        log.info("%s: CLOSED %s %.6f @ %.4f (fill), P&L %.2f — %s",
                 symbol, pos["side"], fill_qty, fill_price, pnl, reason)

    def _log_closed_trade(self, pos: dict, symbol: str, exit_price: float,
                          exit_qty: Optional[float] = None) -> float:
        qty = exit_qty if exit_qty is not None else pos["qty"]
        direction = 1.0 if pos["side"] == "long" else -1.0
        pnl = (exit_price - pos["entry_price"]) * qty * direction
        self.state.log_trade(
            timestamp=datetime.now(timezone.utc).isoformat(),
            instrument=symbol, strategy=pos["strategy"], direction=pos["side"],
            entry_price=pos["entry_price"], exit_price=exit_price, pnl=pnl,
            position_size=qty)
        return pnl

    # -------------------------------------------------------------- main loop
    def run(self) -> None:
        log.info("Starting bot: %s", ", ".join(f"{s} -> {config.INSTRUMENTS[s]['strategy']}"
                                               for s in config.INSTRUMENTS))
        self.reconcile()
        while True:
            try:
                self.roll_daily()
                self.check_stops()
                self.evaluate_candles()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log.error("Loop error (backing off 60s): %s", e, exc_info=True)
                time.sleep(60)
                continue
            time.sleep(config.POLL_INTERVAL_SECONDS)


def main() -> None:
    setup_logging()
    try:
        Bot().run()
    except KeyboardInterrupt:
        log.info("Stopped by user. Open positions remain at Alpaca with their "
                 "stops persisted in %s; restart the bot to resume managing them.",
                 config.STATE_FILE)


if __name__ == "__main__":
    main()
