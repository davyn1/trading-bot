"""Broker layer: all Alpaca API access (alpaca-py SDK).

Responsibilities: historical bars (completed candles only), latest prices,
account/positions/orders, market orders with fill confirmation, market clock,
and retry with exponential backoff on transient API errors.
"""

import logging
import math
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import (
    CryptoBarsRequest,
    CryptoLatestTradeRequest,
    StockBarsRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

import config
from bot.risk_manager import is_crypto

log = logging.getLogger(__name__)

TRANSIENT_ERRORS = (requests.exceptions.RequestException, ConnectionError, TimeoutError)
FILL_TIMEOUT_SECONDS = 60
POSITION_NOT_FOUND_CODE = 40410000

# US equity regular session, as minutes after midnight ET (09:30-16:00).
ET = ZoneInfo("America/New_York")
SESSION_OPEN_MINUTE = 9 * 60 + 30
SESSION_CLOSE_MINUTE = 16 * 60
# How long after a candle's end to accept it even if its final base bar never
# printed (thin names can have no IEX trades in a 15-minute slice).
SESSION_BAR_GRACE = pd.Timedelta(minutes=5)


class PositionNotFound(Exception):
    """Alpaca has no position for the symbol (already closed or never held)."""


def _error_code(e: APIError) -> Optional[int]:
    """Alpaca's numeric error code (e.g. 40410000), or None if the error body
    isn't the usual JSON (APIError.code raises on non-JSON bodies)."""
    try:
        return int(e.code)
    except Exception:
        return None


def trade_symbol(symbol: str) -> str:
    """Alpaca returns crypto positions without the slash (BTCUSD)."""
    return symbol.replace("/", "")


def with_retry(fn, *args, retries: int = 4, **kwargs):
    """Retry transient failures with exponential backoff. Client errors
    (4xx other than 429) are not retried — they won't fix themselves."""
    delay = 2.0
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except APIError as e:
            code = getattr(e, "status_code", None)
            if code is not None and 400 <= code < 500 and code != 429:
                raise
            if attempt == retries - 1:
                raise
            log.warning("API error (%s), retrying in %.0fs: %s", code, delay, e)
        except TRANSIENT_ERRORS as e:
            if attempt == retries - 1:
                raise
            log.warning("Connection error, retrying in %.0fs: %s", delay, e)
        time.sleep(delay)
        delay *= 2


def session_base_minutes(timeframe_minutes: int) -> int:
    """Base bar size for building equity candles: the largest size that
    divides both the timeframe and 30, so base bars tile the 09:30 open
    exactly (15 -> 15, 30 -> 30, 60 -> 30, 240 -> 30)."""
    return math.gcd(timeframe_minutes, 30)


def session_bars(df: pd.DataFrame, timeframe_minutes: int, base_minutes: int,
                 now: datetime) -> pd.DataFrame:
    """Regular-session candles from base bars (tz-aware UTC index = bar start).

    Pre/post-market bars are dropped: on the IEX feed they carry ~1% of
    regular-hours volume, and their tiny ranges distort SMA/std/ATR (a lower
    ATR means a tighter stop and a bigger position). Candles are anchored to
    the 09:30 ET open, so a 60m day is 09:30, 10:30, ... 15:30 (the last one
    30 minutes long, ending at the 16:00 close).

    A candle is returned only once complete: its end (start + timeframe,
    capped at 16:00) has passed AND its final base bar has been published —
    or GRACE has elapsed, for thin names where IEX may have no final print."""
    if df.empty:
        return df
    et = df.index.tz_convert(ET)
    minute_of_day = et.hour * 60 + et.minute
    in_session = (minute_of_day >= SESSION_OPEN_MINUTE) & (minute_of_day < SESSION_CLOSE_MINUTE)
    df, et = df[in_session], et[in_session]
    if df.empty:
        return df

    tf = pd.Timedelta(minutes=timeframe_minutes)
    base = pd.Timedelta(minutes=base_minutes)
    session_open = et.normalize() + pd.Timedelta(minutes=SESSION_OPEN_MINUTE)
    candle_start = session_open + ((et - session_open) // tf) * tf
    # Keep the source tz object: mixing tz representations across symbols
    # (e.g. with crypto bars) breaks datetime indexes built from both.
    grouped = df.groupby(candle_start.tz_convert(df.index.tz))
    out = grouped.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                      close=("close", "last"), volume=("volume", "sum"))
    last_base_start = grouped.apply(lambda g: g.index[-1])

    starts_et = out.index.tz_convert(ET)
    session_close = starts_et.normalize() + pd.Timedelta(minutes=SESSION_CLOSE_MINUTE)
    end = pd.Series((starts_et + tf).where(starts_et + tf < session_close, session_close)
                    .tz_convert(df.index.tz), index=out.index)
    now_ts = pd.Timestamp(now)
    has_final_bar = last_base_start + base >= end
    complete = (end <= now_ts) & (has_final_bar | (end + SESSION_BAR_GRACE <= now_ts))
    return out[complete.values]


def to_timeframe(minutes: int) -> TimeFrame:
    if minutes < 60:
        return TimeFrame(minutes, TimeFrameUnit.Minute)
    if minutes % 60 == 0 and minutes < 1440:
        return TimeFrame(minutes // 60, TimeFrameUnit.Hour)
    raise ValueError(f"Unsupported timeframe: {minutes} minutes")


class Broker:
    def __init__(self):
        if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
            raise RuntimeError(
                "Missing Alpaca credentials. Copy .env.example to .env and fill in "
                "ALPACA_API_KEY and ALPACA_SECRET_KEY.")
        self.trading = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY,
                                     paper=config.PAPER)
        self.stock_data = StockHistoricalDataClient(config.ALPACA_API_KEY,
                                                    config.ALPACA_SECRET_KEY)
        self.crypto_data = CryptoHistoricalDataClient(config.ALPACA_API_KEY,
                                                      config.ALPACA_SECRET_KEY)
        self._clock_cache: Tuple[Optional[object], float] = (None, 0.0)
        mode = "PAPER" if config.PAPER else "LIVE"
        log.info("Broker initialized against the %s endpoint", mode)
        if not config.PAPER:
            log.warning("LIVE TRADING ENABLED — orders will use real money")

    # --- account / clock ----------------------------------------------------
    def account(self):
        return with_retry(self.trading.get_account)

    def equity(self) -> float:
        return float(self.account().equity)

    def clock(self, max_age: float = 60.0):
        cached, ts = self._clock_cache
        if cached is not None and time.time() - ts < max_age:
            return cached
        clk = with_retry(self.trading.get_clock)
        self._clock_cache = (clk, time.time())
        return clk

    def market_open(self) -> bool:
        return bool(self.clock().is_open)

    # --- market data --------------------------------------------------------
    def get_bars(self, symbol: str, timeframe_minutes: int, bars_needed: int) -> pd.DataFrame:
        """Fetch recent bars and return only COMPLETED candles (a bar whose
        start + timeframe is still in the future is partial and dropped).
        Equities are regular-session only, anchored to the 09:30 ET open
        (see session_bars); crypto uses Alpaca's native 24/7 bars.
        Index is tz-aware UTC, oldest first."""
        now = datetime.now(timezone.utc)
        # Span estimate: crypto trades ~24/7; equities ~6.5h/day plus
        # weekends/holidays, so overshoot generously and trim with tail().
        # The flat 3-day pad covers a weekend (+ holiday) right before "now" —
        # a 1-day pad left SPY short of warmup on a Monday morning.
        factor = 1.5 if is_crypto(symbol) else 6.5
        start = now - timedelta(minutes=int(timeframe_minutes * bars_needed * factor) + 4320)

        if is_crypto(symbol):
            req = CryptoBarsRequest(symbol_or_symbols=symbol,
                                    timeframe=to_timeframe(timeframe_minutes), start=start)
            bars = with_retry(self.crypto_data.get_crypto_bars, req)
        else:
            # Equities: fetch base bars that tile the 09:30 open, then keep
            # the regular session only and aggregate up (see session_bars).
            base = session_base_minutes(timeframe_minutes)
            req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=to_timeframe(base),
                                   start=start, adjustment=Adjustment.SPLIT,
                                   feed=DataFeed(config.DATA_FEED))
            bars = with_retry(self.stock_data.get_stock_bars, req)

        df = bars.df
        if df.empty:
            return df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        df = df[["open", "high", "low", "close", "volume"]].sort_index()
        if not is_crypto(symbol):
            return session_bars(df, timeframe_minutes, base, now).tail(bars_needed + 10)
        # Keep completed candles only — never evaluate a partially formed bar.
        cutoff = now - timedelta(minutes=timeframe_minutes)
        df = df[df.index <= cutoff]
        return df.tail(bars_needed + 10)

    def latest_price(self, symbol: str) -> float:
        if is_crypto(symbol):
            req = CryptoLatestTradeRequest(symbol_or_symbols=symbol)
            trades = with_retry(self.crypto_data.get_crypto_latest_trade, req)
            return float(trades[symbol].price)
        req = StockLatestTradeRequest(symbol_or_symbols=symbol,
                                      feed=DataFeed(config.DATA_FEED))
        trades = with_retry(self.stock_data.get_stock_latest_trade, req)
        return float(trades[symbol].price)

    # --- positions / orders -------------------------------------------------
    def open_positions(self) -> dict:
        """Alpaca positions keyed by slash-less symbol (BTCUSD)."""
        return {p.symbol: p for p in with_retry(self.trading.get_all_positions)}

    def gross_exposure(self) -> float:
        """Combined absolute market value of every open position at Alpaca
        (including unmanaged ones — they use the same account)."""
        return sum(abs(float(p.market_value)) for p in self.open_positions().values())

    def has_position(self, symbol: str) -> bool:
        return trade_symbol(symbol) in self.open_positions()

    def open_orders(self, symbol: Optional[str] = None) -> list:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN,
                               symbols=[trade_symbol(symbol)] if symbol else None)
        return with_retry(self.trading.get_orders, req)

    def last_fill_price(self, symbol: str) -> Optional[float]:
        """Average fill price of the most recent filled order on symbol, or
        None. Used to record the real exit when a close confirmed late."""
        req = GetOrdersRequest(status=QueryOrderStatus.CLOSED,
                               symbols=[trade_symbol(symbol)], limit=10)
        for order in with_retry(self.trading.get_orders, req):
            if order.status == OrderStatus.FILLED and order.filled_avg_price:
                return float(order.filled_avg_price)
        return None

    def cancel_order(self, order_id) -> None:
        with_retry(self.trading.cancel_order_by_id, order_id)

    def market_order(self, symbol: str, side: str, qty: float) -> Tuple[float, float]:
        """Submit a market order and wait for the fill. Returns
        (filled_avg_price, filled_qty) — the ACTUAL fill, not the signal price.

        A client_order_id makes the submit safe against retry after a network
        error: if the first attempt actually reached Alpaca, we find the order
        by that id instead of submitting a duplicate."""
        client_id = f"bot-{uuid.uuid4().hex[:16]}"
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.GTC if is_crypto(symbol) else TimeInForce.DAY,
            client_order_id=client_id,
        )
        try:
            order = self.trading.submit_order(req)
        except TRANSIENT_ERRORS:
            log.warning("Network error during submit for %s — checking whether the "
                        "order went through (client id %s)", symbol, client_id)
            time.sleep(3)
            order = with_retry(self.trading.get_order_by_client_id, client_id)
        return self._await_fill(order)

    def close_position_market(self, symbol: str) -> Tuple[float, float]:
        """Liquidate the entire position via Alpaca's close endpoint (handles
        crypto fractional dust cleanly). Returns (fill_price, filled_qty).
        Raises PositionNotFound if Alpaca holds no position for the symbol."""
        try:
            order = with_retry(self.trading.close_position, trade_symbol(symbol))
        except APIError as e:
            if _error_code(e) == POSITION_NOT_FOUND_CODE:
                raise PositionNotFound(symbol) from e
            raise
        return self._await_fill(order)

    def _await_fill(self, order) -> Tuple[float, float]:
        deadline = time.time() + FILL_TIMEOUT_SECONDS
        while time.time() < deadline:
            order = with_retry(self.trading.get_order_by_id, order.id)
            if order.status == OrderStatus.FILLED:
                return float(order.filled_avg_price), float(order.filled_qty)
            if order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED,
                                OrderStatus.EXPIRED):
                raise RuntimeError(f"Order {order.id} for {order.symbol} ended "
                                   f"{order.status}: {order}")
            time.sleep(1)
        # Timed out — accept a partial fill if there is one, else give up.
        filled = float(order.filled_qty or 0)
        if filled > 0:
            log.warning("Order %s only partially filled (%.6f); using partial fill",
                        order.id, filled)
            return float(order.filled_avg_price), filled
        raise TimeoutError(f"Order {order.id} for {order.symbol} not filled after "
                           f"{FILL_TIMEOUT_SECONDS}s (status {order.status})")
