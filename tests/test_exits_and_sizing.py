"""Regression tests for the 2026-09 incident: a stop exit that failed once
(Alpaca "position not found" / unconfirmed fill) retried forever and aborted
every loop, so SPY's stop was never checked; and stop-based sizing put ~3.4x
equity into one SPY position because nothing capped notional.

Run from the project root:  python -m pytest tests
"""

import pytest

import config
from bot.main import Bot
from bot.portfolio import PositionNotFound
from bot.risk_manager import RiskManager, cap_notional
from bot.state import StateStore

EQUITY = 100_000.0


class FakeBroker:
    """Just enough of portfolio.Broker for Bot.check_stops/close_position."""

    def __init__(self, prices):
        self.prices = prices
        self.pending_orders = {}      # symbol -> list of open orders
        self.close_behavior = {}      # symbol -> exception to raise, or None
        self.fill_prices = {}         # symbol -> last filled order price
        self.close_calls = []

    def market_open(self):
        return True

    def latest_price(self, symbol):
        return self.prices[symbol]

    def open_orders(self, symbol=None):
        return self.pending_orders.get(symbol, [])

    def last_fill_price(self, symbol):
        return self.fill_prices.get(symbol)

    def close_position_market(self, symbol):
        self.close_calls.append(symbol)
        exc = self.close_behavior.get(symbol)
        if exc is not None:
            raise exc
        return self.prices[symbol], 1.0


def position(side="long", qty=1.0, entry=100.0, stop=95.0):
    return {"strategy": "mean_reversion", "side": side, "qty": qty,
            "entry_price": entry, "entry_time": "2026-09-01T00:00:00+00:00",
            "stop_price": stop, "trailing": False, "stop_atr_mult": 2.0,
            "highest_high": entry, "lowest_low": None, "last_atr": 1.0}


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRADES_CSV", str(tmp_path / "trades.csv"))
    monkeypatch.setattr(config, "DAILY_PNL_CSV", str(tmp_path / "daily_pnl.csv"))
    b = Bot.__new__(Bot)  # skip __init__: no Alpaca credentials in tests
    b.state = StateStore(str(tmp_path / "state.json"))
    b.risk = RiskManager(b.state)
    b.broker = FakeBroker({"TSLA": 90.0, "SPY": 700.0})
    return b


def trade_rows():
    with open(config.TRADES_CSV, encoding="utf-8") as f:
        return f.read().strip().splitlines()[1:]


# --- exits -------------------------------------------------------------------

def test_close_when_alpaca_has_no_position_drops_it_and_logs_trade(bot):
    bot.state.set_position("TSLA", position(entry=100.0))
    bot.broker.close_behavior["TSLA"] = PositionNotFound("TSLA")
    bot.broker.fill_prices["TSLA"] = 92.5

    bot.close_position("TSLA", reason="stop hit")

    assert bot.state.get_position("TSLA") is None
    rows = trade_rows()
    assert len(rows) == 1 and "TSLA" in rows[0] and "92.5" in rows[0]


def test_close_with_pending_order_does_not_resubmit(bot):
    bot.state.set_position("TSLA", position())
    bot.broker.pending_orders["TSLA"] = [object()]

    bot.close_position("TSLA")

    assert bot.broker.close_calls == []
    assert bot.state.get_position("TSLA") is not None


def test_close_timeout_keeps_position_and_does_not_raise(bot):
    bot.state.set_position("TSLA", position())
    bot.broker.close_behavior["TSLA"] = TimeoutError("not filled after 60s")

    bot.close_position("TSLA")

    assert bot.state.get_position("TSLA") is not None


def test_failed_stop_exit_does_not_block_other_stops(bot):
    # TSLA is checked first and its exit blows up; SPY's stop must still fire.
    bot.state.set_position("TSLA", position(entry=100.0, stop=95.0))
    bot.state.set_position("SPY", position(entry=772.68, stop=770.0))
    bot.broker.close_behavior["TSLA"] = RuntimeError("order rejected")

    bot.check_stops()

    assert bot.broker.close_calls == ["TSLA", "SPY"]
    assert bot.state.get_position("SPY") is None
    assert bot.state.get_position("TSLA") is not None  # retried next loop


# --- sizing --------------------------------------------------------------------

def test_per_position_cap():
    qty = cap_notional("SPY", 454.0, 772.68, EQUITY, open_exposure=0.0)
    assert qty * 772.68 == pytest.approx(0.5 * EQUITY)


def test_total_exposure_cap():
    qty = cap_notional("QQQ", 1000.0, 500.0, EQUITY, open_exposure=80_000.0)
    assert qty * 500.0 == pytest.approx(20_000.0)


def test_no_room_left_means_no_trade():
    assert cap_notional("GLD", 10.0, 300.0, EQUITY, open_exposure=100_000.0) == 0.0


def test_small_position_untouched():
    assert cap_notional("GLD", 10.0, 300.0, EQUITY, open_exposure=0.0) == 10.0


def test_incident_spy_sizing_now_capped(tmp_path):
    # The real trade: 2x ATR stop (~$2.20) on SPY at $772.68, 4x day buying
    # power. Previously sized ~454 shares (~$351k); now capped at 0.5x equity.
    risk = RiskManager(StateStore(str(tmp_path / "state.json")))
    qty = risk.position_size(symbol="SPY", side="long", equity=EQUITY,
                             buying_power=4 * EQUITY, price=772.68,
                             stop_distance=2.2, risk_pct=0.01, open_exposure=0.0)
    assert qty * 772.68 <= 0.5 * EQUITY
    assert qty > 0
