"""Broker layer: all Alpaca API access (alpaca-py SDK).

Responsibilities: historical bars (completed candles only), latest prices,
account/positions/orders, market orders with fill confirmation, market clock,
and retry with exponential backoff on transient API errors.
"""

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

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
        Index is tz-aware UTC, oldest first."""
        now = datetime.now(timezone.utc)
        # Span estimate: crypto trades ~24/7; equities ~6.5h/day plus
        # weekends/holidays, so overshoot generously and trim with tail().
        # The flat 3-day pad covers a weekend (+ holiday) right before "now" —
        # a 1-day pad left SPY short of warmup on a Monday morning.
        factor = 1.5 if is_crypto(symbol) else 6.5
        start = now - timedelta(minutes=int(timeframe_minutes * bars_needed * factor) + 4320)
        tf = to_timeframe(timeframe_minutes)

        if is_crypto(symbol):
            req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=start)
            bars = with_retry(self.crypto_data.get_crypto_bars, req)
        else:
            req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=start,
                                   adjustment=Adjustment.SPLIT,
                                   feed=DataFeed(config.DATA_FEED))
            bars = with_retry(self.stock_data.get_stock_bars, req)

        df = bars.df
        if df.empty:
            return df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        df = df[["open", "high", "low", "close", "volume"]].sort_index()
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

    def open_orders(self) -> list:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        return with_retry(self.trading.get_orders, req)

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
        crypto fractional dust cleanly). Returns (fill_price, filled_qty)."""
        order = with_retry(self.trading.close_position, trade_symbol(symbol))
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
