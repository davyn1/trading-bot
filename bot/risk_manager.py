"""Risk management: stop-based position sizing, trailing stop mechanics,
the correlation filter, and the daily-drawdown kill switch.

Sizing rule: a move from entry to the initial stop loses exactly
risk_pct (default 1%) of account equity. Stop distance = stop_atr_mult * ATR
on the strategy's own timeframe, so wider stops mean smaller positions.
"""

import logging
import math
from typing import Optional

import config

log = logging.getLogger(__name__)


def is_crypto(symbol: str) -> bool:
    return "/" in symbol


class RiskManager:
    def __init__(self, state):
        self.state = state

    # --- position sizing ----------------------------------------------------
    def position_size(self, *, symbol: str, side: str, equity: float,
                      buying_power: float, price: float, stop_distance: float,
                      risk_pct: float) -> float:
        """Quantity such that (stop_distance * qty) == risk_pct * equity,
        capped by available buying power."""
        if stop_distance <= 0 or price <= 0:
            log.warning("%s: invalid stop distance %.4f — no trade", symbol, stop_distance)
            return 0.0
        qty = (equity * risk_pct) / stop_distance

        max_notional = buying_power * 0.95  # small buffer for price drift
        if qty * price > max_notional:
            scaled = max_notional / price
            log.warning("%s: sized qty %.6f exceeds buying power, scaling to %.6f "
                        "(risk will be under %s%%)", symbol, qty, scaled, risk_pct * 100)
            qty = scaled

        if is_crypto(symbol):
            qty = math.floor(qty * 1e6) / 1e6
        elif side == "short":
            qty = float(math.floor(qty))  # shorts must be whole shares
        else:
            qty = math.floor(qty * 1e3) / 1e3  # fractional longs to 0.001
        if qty <= 0:
            log.warning("%s: computed quantity rounded to zero — no trade", symbol)
        return qty

    @staticmethod
    def initial_stop(entry_price: float, side: str, stop_distance: float) -> float:
        return entry_price - stop_distance if side == "long" else entry_price + stop_distance

    # --- trailing stops -----------------------------------------------------
    @staticmethod
    def update_trailing_stop(pos: dict, candle_high: float, candle_low: float,
                             cur_atr: float) -> bool:
        """Called once per completed candle. Trails from the highest high
        (lowest low for shorts) since entry; never moves the stop against the
        position. Returns True if the stop moved."""
        pos["last_atr"] = cur_atr
        if not pos.get("trailing"):
            return False
        dist = pos["stop_atr_mult"] * cur_atr
        moved = False
        if pos["side"] == "long":
            pos["highest_high"] = max(pos.get("highest_high") or candle_high, candle_high)
            new_stop = pos["highest_high"] - dist
            if new_stop > pos["stop_price"]:
                pos["stop_price"] = new_stop
                moved = True
        else:
            pos["lowest_low"] = min(pos.get("lowest_low") or candle_low, candle_low)
            new_stop = pos["lowest_low"] + dist
            if new_stop < pos["stop_price"]:
                pos["stop_price"] = new_stop
                moved = True
        return moved

    @staticmethod
    def stop_hit(pos: dict, price: float) -> bool:
        if pos["side"] == "long":
            return price <= pos["stop_price"]
        return price >= pos["stop_price"]

    # --- entry gating -------------------------------------------------------
    def entry_blocked(self, symbol: str, side: str) -> Optional[str]:
        """Returns a human-readable reason if a new entry must be blocked."""
        if self.state.daily.get("halted"):
            return "daily kill switch active — no new entries today"
        if self.correlation_blocked(symbol, side):
            cf = config.CORRELATION_FILTER
            return (f"correlation filter: {' and '.join(cf['when_all_long'])} are both long, "
                    f"blocking new longs on {symbol}")
        return None

    def correlation_blocked(self, symbol: str, side: str) -> bool:
        cf = config.CORRELATION_FILTER
        if side != "long" or symbol not in cf["block_new_longs"]:
            return False
        positions = self.state.positions
        return all(
            positions.get(s, {}).get("side") == "long" for s in cf["when_all_long"]
        )

    # --- kill switch --------------------------------------------------------
    def check_kill_switch(self, current_equity: float) -> bool:
        """Halts new entries for the rest of the ET day once the drawdown from
        start-of-day equity exceeds the configured threshold. Returns True the
        moment it first triggers (so the caller can log the event once)."""
        d = self.state.daily
        start = d.get("start_equity")
        if not start or d.get("halted"):
            return False
        drawdown = (start - current_equity) / start
        threshold = config.RISK["daily_drawdown_halt_pct"]
        if drawdown >= threshold:
            d["halted"] = True
            self.state.save()
            log.warning("KILL SWITCH: daily drawdown %.2f%% >= %.2f%% "
                        "(start equity %.2f, now %.2f). New entries halted for the day; "
                        "existing positions keep their stops.",
                        drawdown * 100, threshold * 100, start, current_equity)
            return True
        return False
