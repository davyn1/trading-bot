"""Persistent bot state and CSV trade/P&L logs.

state.json is rewritten atomically after every change so a crash or restart
never orphans a position without its stop level. Layout:

{
  "positions": {
    "BTC/USD": {
      "strategy": "momentum_breakout", "side": "long", "qty": 0.12,
      "entry_price": 60000.0, "entry_time": "...", "stop_price": 58000.0,
      "trailing": true, "stop_atr_mult": 2.0,
      "highest_high": 61000.0, "lowest_low": null, "last_atr": 1000.0
    }, ...
  },
  "last_candle": {"SPY": "<iso ts of last evaluated candle>", ...},
  "daily": {"date": "2026-07-19", "start_equity": 100000.0,
             "last_equity": 99500.0, "realized_pnl": -120.5, "halted": false}
}
"""

import csv
import json
import logging
import os
import tempfile
from typing import Optional

import config

log = logging.getLogger(__name__)

TRADE_FIELDS = [
    "timestamp", "instrument", "strategy", "direction",
    "entry_price", "exit_price", "pnl", "position_size",
]
DAILY_FIELDS = [
    "date", "start_equity", "end_equity", "realized_pnl", "pnl", "pnl_pct", "halted",
]


class StateStore:
    def __init__(self, path: str = config.STATE_FILE):
        self.path = path
        self.data = {"positions": {}, "last_candle": {}, "daily": {}}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            for key in self.data:
                self.data[key] = loaded.get(key, self.data[key])
            log.info("Loaded state from %s: %d position(s)", path, len(self.data["positions"]))

    def save(self) -> None:
        """Atomic write: dump to a temp file, then replace."""
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(self.path)),
                                   prefix=".state_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # --- positions ---------------------------------------------------------
    @property
    def positions(self) -> dict:
        return self.data["positions"]

    def get_position(self, symbol: str) -> Optional[dict]:
        return self.data["positions"].get(symbol)

    def set_position(self, symbol: str, record: dict) -> None:
        self.data["positions"][symbol] = record
        self.save()

    def remove_position(self, symbol: str) -> None:
        self.data["positions"].pop(symbol, None)
        self.save()

    # --- candle bookkeeping ------------------------------------------------
    def last_candle(self, symbol: str) -> Optional[str]:
        return self.data["last_candle"].get(symbol)

    def set_last_candle(self, symbol: str, ts_iso: str) -> None:
        self.data["last_candle"][symbol] = ts_iso
        self.save()

    # --- daily P&L / kill switch -------------------------------------------
    @property
    def daily(self) -> dict:
        return self.data["daily"]

    # --- CSV logs ----------------------------------------------------------
    @staticmethod
    def _append_csv(path: str, fields: list, row: dict) -> None:
        new_file = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            if new_file:
                writer.writeheader()
            writer.writerow(row)

    def log_trade(self, *, timestamp: str, instrument: str, strategy: str,
                  direction: str, entry_price: float, exit_price: float,
                  pnl: float, position_size: float) -> None:
        self._append_csv(config.TRADES_CSV, TRADE_FIELDS, {
            "timestamp": timestamp,
            "instrument": instrument,
            "strategy": strategy,
            "direction": direction,
            "entry_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4) if exit_price is not None else "",
            "pnl": round(pnl, 2) if pnl is not None else "",
            "position_size": round(position_size, 6),
        })

    def log_daily(self, *, date: str, start_equity: float, end_equity: float,
                  realized_pnl: float, halted: bool) -> None:
        pnl = end_equity - start_equity
        self._append_csv(config.DAILY_PNL_CSV, DAILY_FIELDS, {
            "date": date,
            "start_equity": round(start_equity, 2),
            "end_equity": round(end_equity, 2),
            "realized_pnl": round(realized_pnl, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(100.0 * pnl / start_equity, 3) if start_equity else 0,
            "halted": halted,
        })
