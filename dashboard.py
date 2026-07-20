"""Streamlit monitoring dashboard for the trading bot.

Run alongside the bot:  python -m streamlit run dashboard.py

Read-only: renders the bot's own files (state.json, trades.csv,
daily_pnl.csv, bot.log) plus live account/price/candle data from Alpaca.
The signals section computes indicators with the SAME code the bot trades
with (bot.strategies.base), so what you see is what the bot sees. It never
places orders or mutates bot state. Auto-refreshes every 30 seconds.
"""

import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

import config
from bot.portfolio import Broker
from bot.strategies import build_strategy
from bot.strategies.base import atr as atr_fn
from bot.strategies.base import ema as ema_fn
from bot.strategies.base import sma as sma_fn

st.set_page_config(page_title="Trading Bot", page_icon="📈", layout="wide")

REFRESH_SECONDS = 30
CHART_CANDLES = 120
ET = ZoneInfo("America/New_York")

# --- palette (dataviz reference palette; light/dark selected per theme) -----
INK = {
    "light": {"primary": "#0b0b0b", "secondary": "#52514e", "muted": "#898781",
              "grid": "#e1e0d9", "baseline": "#c3c2b7"},
    "dark": {"primary": "#ffffff", "secondary": "#c3c2b7", "muted": "#898781",
             "grid": "#2c2c2a", "baseline": "#383835"},
}
SERIES = {  # fixed categorical order: strategy -> (light, dark)
    "mean_reversion": ("#2a78d6", "#3987e5"),
    "momentum_breakout": ("#008300", "#008300"),
    "trend_following": ("#e87ba4", "#d55181"),
}
# Status palette (icon + label always travel together — never color alone)
STATUS = {
    "in_trade": ("#0ca30c", "● IN TRADE"),
    "signal": ("#ec835a", "● SIGNAL"),
    "forming": ("#fab219", "● FORMING"),
    "scanning": ("#898781", "● SCANNING"),
    "warmup": ("#898781", "● WARMING UP"),
    "offline": ("#898781", "● NO DATA"),
}
CANDLE_UP, CANDLE_DOWN = "#0ca30c", "#d03b3b"

STRAT_LABEL = {"mean_reversion": "Mean Reversion", "momentum_breakout": "Momentum Breakout",
               "trend_following": "Trend Following"}


def tf_label(minutes: int) -> str:
    return f"{minutes}m" if minutes < 60 else f"{minutes // 60}H"


# --- data access ------------------------------------------------------------
@st.cache_resource
def broker() -> Broker:
    return Broker()


@st.cache_data(ttl=20)
def account_snapshot():
    b = broker()
    acct = b.account()
    clk = b.clock()
    return {"equity": float(acct.equity), "buying_power": float(acct.buying_power),
            "market_open": bool(clk.is_open), "next_open": str(clk.next_open)}


@st.cache_data(ttl=20)
def live_prices(symbols: tuple):
    b = broker()
    out = {}
    for s in symbols:
        try:
            out[s] = b.latest_price(s)
        except Exception:
            out[s] = None
    return out


@st.cache_data(ttl=60)
def bars_cached(symbol: str, tf_minutes: int, n: int) -> pd.DataFrame:
    return broker().get_bars(symbol, tf_minutes, n)


def load_state() -> dict:
    if not os.path.exists(config.STATE_FILE):
        return {"positions": {}, "daily": {}, "last_candle": {}}
    with open(config.STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_csv(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


# --- per-instrument strategy snapshot ---------------------------------------
def snapshot(symbol: str, position) -> dict:
    """Compute the strategy's current view of one instrument from completed
    candles — identical math to the bot's own evaluation."""
    strat = build_strategy(symbol)
    scfg = config.STRATEGIES[config.INSTRUMENTS[symbol]["strategy"]]
    p = strat.params
    out = {"symbol": symbol, "strategy": strat.name, "tf": scfg["timeframe_minutes"],
           "status": "offline", "lines": [], "df": None}
    try:
        df = bars_cached(symbol, scfg["timeframe_minutes"], strat.warmup_bars + CHART_CANDLES)
    except Exception:
        return out
    if len(df) < strat.warmup_bars:
        out["status"] = "warmup"
        out["lines"] = [f"{len(df)}/{strat.warmup_bars} bars collected"]
        out["df"] = df
        return out
    out["df"] = df
    close = float(df["close"].iloc[-1])

    if strat.name == "mean_reversion":
        mean_s = sma_fn(df["close"], p["sma_period"])
        std_s = df["close"].rolling(p["sma_period"]).std()
        atr_s = atr_fn(df, p["atr_period"])
        z = (close - float(mean_s.iloc[-1])) / float(std_s.iloc[-1]) if std_s.iloc[-1] else 0.0
        rng = float(df["high"].iloc[-1] - df["low"].iloc[-1])
        atr_now = float(atr_s.iloc[-1])
        atr_avg = float(atr_s.rolling(p["vol_filter_atr_avg_period"]).mean().iloc[-1])
        blocked = rng > p["vol_filter_range_mult"] * atr_now or \
            atr_now > p["vol_filter_atr_ratio"] * atr_avg
        out["overlays"] = {"mean": mean_s, "upper": mean_s + p["entry_z"] * std_s,
                           "lower": mean_s - p["entry_z"] * std_s}
        out["long_mask"] = df["close"] < (mean_s - p["entry_z"] * std_s)
        out["short_mask"] = df["close"] > (mean_s + p["entry_z"] * std_s)
        out["lines"] = [f"z-score {z:+.2f} (enters at ±{p['entry_z']})",
                        f"close {close:,.2f} · SMA{p['sma_period']} {float(mean_s.iloc[-1]):,.2f}",
                        "vol filter: 🚫 blocking entries" if blocked else "vol filter: clear"]
        if abs(z) >= p["entry_z"]:
            out["status"] = "signal"
        elif abs(z) >= 0.7 * p["entry_z"]:
            out["status"] = "forming"
        else:
            out["status"] = "scanning"

    elif strat.name == "momentum_breakout":
        hh = df["high"].rolling(p["channel_period"]).max().shift(1)
        ll = df["low"].rolling(p["channel_period"]).min().shift(1)
        vavg = df["volume"].rolling(p["volume_avg_period"]).mean().shift(1)
        vol_ratio = float(df["volume"].iloc[-1] / vavg.iloc[-1]) if vavg.iloc[-1] else 0.0
        gap_pct = (float(hh.iloc[-1]) - close) / close * 100
        out["overlays"] = {"20-high": hh, "20-low": ll}
        out["long_mask"] = (df["close"] > hh) & (df["volume"] >= p["volume_mult"] * vavg)
        out["short_mask"] = pd.Series(False, index=df.index)
        out["lines"] = [f"{gap_pct:.2f}% below 20-bar high" if gap_pct > 0
                        else f"{-gap_pct:.2f}% ABOVE 20-bar high",
                        f"volume {vol_ratio:.2f}x avg (needs {p['volume_mult']}x)",
                        "long/flat only (spot crypto)"]
        if bool(out["long_mask"].iloc[-1]):
            out["status"] = "signal"
        elif gap_pct <= 1.5:
            out["status"] = "forming"
        else:
            out["status"] = "scanning"

    else:  # trend_following
        fast = ema_fn(df["close"], p["fast_ema"])
        slow = ema_fn(df["close"], p["slow_ema"])
        spread = (float(fast.iloc[-1]) - float(slow.iloc[-1])) / float(slow.iloc[-1]) * 100
        diff = fast - slow
        crossed = (diff.iloc[-2] <= 0 < diff.iloc[-1]) or (diff.iloc[-2] >= 0 > diff.iloc[-1])
        out["overlays"] = {f"EMA{p['fast_ema']}": fast, f"EMA{p['slow_ema']}": slow}
        sign = diff.gt(0)
        out["long_mask"] = sign & ~sign.shift(1, fill_value=False)     # golden crosses
        out["short_mask"] = (~sign) & sign.shift(1, fill_value=False)  # death crosses
        out["lines"] = [f"EMA spread {spread:+.2f}% "
                        f"({'bullish' if spread > 0 else 'bearish'} regime)",
                        f"close {close:,.2f}",
                        "cross fired on last candle!" if crossed else "waiting for EMA cross"]
        if crossed:
            out["status"] = "signal"
        elif abs(spread) <= 0.3:
            out["status"] = "forming"
        else:
            out["status"] = "scanning"

    if position is not None:
        out["status"] = "in_trade"
    return out


# --- live ticking clock (client-side JS: ticks every second with no server
# rerun, unlike a 1s st.fragment which would rerender the page constantly) ----
def live_clock() -> None:
    components.html(
        """
        <div id="clk" style="text-align:right;font-family:system-ui,-apple-system,
             'Segoe UI',sans-serif;font-size:14px;color:#898781;
             font-variant-numeric:tabular-nums"></div>
        <script>
          function tick() {
            const now = new Date().toLocaleTimeString('en-US',
                {timeZone: 'America/New_York', hour12: true});
            document.getElementById('clk').innerHTML =
                "<span style='color:#d03b3b'>&#9679;</span> LIVE&nbsp;&nbsp;" + now + " ET";
          }
          tick(); setInterval(tick, 1000);
        </script>
        """,
        height=26)


# --- main dashboard body -----------------------------------------------------
@st.fragment(run_every=REFRESH_SECONDS)
def render() -> None:
    try:
        mode = st.context.theme.type or "light"
    except Exception:
        mode = "light"
    ink = INK[mode]
    series_color = {name: c[0] if mode == "light" else c[1]
                    for name, c in SERIES.items()}

    def base_layout(fig: go.Figure, title: str, height: int = 330) -> go.Figure:
        fig.update_layout(
            title=dict(text=title, font=dict(color=ink["primary"], size=15)),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(family='system-ui, -apple-system, "Segoe UI", sans-serif',
                      color=ink["secondary"], size=12),
            margin=dict(l=10, r=20, t=45, b=10), height=height,
            legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0,
                        font=dict(color=ink["secondary"])),
        )
        fig.update_xaxes(gridcolor=ink["grid"], linecolor=ink["baseline"],
                         tickfont=dict(color=ink["muted"]), zeroline=False)
        fig.update_yaxes(gridcolor=ink["grid"], linecolor=ink["baseline"],
                         tickfont=dict(color=ink["muted"]), zeroline=False,
                         tickformat=",.2f", side="right")
        return fig

    def status_chip(key: str) -> str:
        color, label = STATUS[key]
        return (f"<span style='font-size:0.78rem;font-weight:600'>"
                f"<span style='color:{color}'>{label.split()[0]}</span> "
                f"{label.split(maxsplit=1)[1]}</span>")

    # --- header -------------------------------------------------------------
    state = load_state()
    daily = state.get("daily", {})
    positions = state.get("positions", {})

    api_ok, snap_acct = True, None
    try:
        snap_acct = account_snapshot()
    except Exception as e:
        api_ok = False
        st.warning(f"Alpaca API unreachable — showing file data only. ({e})")

    left, right = st.columns([3, 2])
    with left:
        st.title("Trading Bot")
    badges = ["🟠 LIVE MODE" if not config.PAPER else "🧪 Paper trading"]
    if snap_acct:
        badges.append("🟢 Market open" if snap_acct["market_open"]
                      else "⚪ Market closed (equities)")
    badges.append("🛑 KILL SWITCH ACTIVE — entries halted today" if daily.get("halted")
                  else "✅ Kill switch armed")
    if os.path.exists(config.STATE_FILE):
        age = time.time() - os.path.getmtime(config.STATE_FILE)
        if age > 180:
            badges.append(f"⚠️ state.json last written {age/60:.0f} min ago "
                          f"— is the bot running?")
    with right:
        if not os.environ.get("DASH_NO_CLOCK"):  # capture-friendly escape hatch
            live_clock()
        st.caption("  ·  ".join(badges))

    # --- stat tiles ---------------------------------------------------------
    trades = load_csv(config.TRADES_CSV)
    equity = snap_acct["equity"] if snap_acct else daily.get("last_equity")
    start_eq = daily.get("start_equity")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Account equity", f"${equity:,.2f}" if equity else "—")
    if equity and start_eq:
        day_pnl = equity - start_eq
        c2.metric("Today's P&L", f"${day_pnl:,.2f}",
                  delta=f"{100*day_pnl/start_eq:+.2f}%")
        drawdown = max(0.0, (start_eq - equity) / start_eq)
        halt_at = config.RISK["daily_drawdown_halt_pct"]
        c3.metric("Daily drawdown", f"{100*drawdown:.2f}%",
                  delta=f"halts at {100*halt_at:.1f}%", delta_color="off")
    else:
        c2.metric("Today's P&L", "—")
        c3.metric("Daily drawdown", "—")
    c4.metric("Open positions", len(positions))
    c5.metric("Realized P&L (all time)",
              f"${trades['pnl'].sum():,.2f}" if not trades.empty else "$0.00")

    st.divider()

    # --- signals section: chart + active-signals rail -----------------------
    chart_col, rail_col = st.columns([2.4, 1])
    snaps = {s: snapshot(s, positions.get(s)) for s in config.INSTRUMENTS} if api_ok \
        else {}

    with chart_col:
        symbol = st.pills("Instrument", list(config.INSTRUMENTS), default="SPY",
                          key="sym_pick") or "SPY"
        sv = snaps.get(symbol)
        if sv is None or sv["df"] is None or sv["df"].empty:
            st.caption("No candle data available for this instrument right now.")
        else:
            df = sv["df"].tail(CHART_CANDLES)
            xs = [t.tz_convert(ET).strftime("%b %d %H:%M") for t in df.index]
            fig = go.Figure(go.Candlestick(
                x=xs, open=df["open"], high=df["high"], low=df["low"],
                close=df["close"], name=symbol, showlegend=False,
                increasing_line_color=CANDLE_UP, increasing_fillcolor=CANDLE_UP,
                decreasing_line_color=CANDLE_DOWN, decreasing_fillcolor=CANDLE_DOWN,
                line=dict(width=1)))
            dash_for = {"upper": "dot", "lower": "dot", "20-high": "dot", "20-low": "dot"}
            color_cycle = [series_color[sv["strategy"]], ink["muted"], ink["muted"]]
            for i, (name, series) in enumerate(sv.get("overlays", {}).items()):
                fig.add_trace(go.Scatter(
                    x=xs, y=series.reindex(df.index), mode="lines", name=name,
                    line=dict(color=color_cycle[min(i, 2)] if name not in dash_for
                              else ink["muted"],
                              width=2, dash=dash_for.get(name, "solid"))))
            for mask, color, sym_mark, offset in (
                    (sv.get("long_mask"), CANDLE_UP, "triangle-up", 0.998),
                    (sv.get("short_mask"), CANDLE_DOWN, "triangle-down", 1.002)):
                if mask is None:
                    continue
                m = mask.reindex(df.index).fillna(False)
                if m.any():
                    pts = df[m]
                    ys = pts["low"] * offset if sym_mark == "triangle-up" \
                        else pts["high"] * offset
                    fig.add_trace(go.Scatter(
                        x=[xs[df.index.get_loc(i)] for i in pts.index], y=ys,
                        mode="markers", name="entry condition",
                        showlegend=False,
                        marker=dict(color=color, size=9, symbol=sym_mark,
                                    line=dict(width=2, color="rgba(0,0,0,0)"))))
            pos = positions.get(symbol)
            if pos:
                fig.add_hline(y=pos["entry_price"], line_dash="dash",
                              line_color=ink["muted"], line_width=1,
                              annotation_text="entry", annotation_font_color=ink["muted"])
                fig.add_hline(y=pos["stop_price"], line_color=CANDLE_DOWN, line_width=1,
                              annotation_text="stop", annotation_font_color=ink["secondary"])
            n = len(xs)
            fig.update_xaxes(type="category",
                             tickvals=[xs[i] for i in range(0, n, max(1, n // 6))])
            fig.update_layout(xaxis_rangeslider_visible=False)
            title = (f"{symbol} · {STRAT_LABEL[sv['strategy']]} · "
                     f"{tf_label(sv['tf'])} candles")
            st.plotly_chart(base_layout(fig, title, height=430), width="stretch")

    with rail_col:
        st.markdown("**Active signals**")
        if not api_ok:
            st.caption("Signals unavailable while the API is unreachable.")
        prices = live_prices(tuple(sorted(positions))) if (positions and api_ok) else {}
        for sym, sv in snaps.items():
            with st.container(border=True):
                head_l, head_r = st.columns([1, 1])
                head_l.markdown(f"**{sym}**")
                head_r.markdown(f"<div style='text-align:right'>{status_chip(sv['status'])}</div>",
                                unsafe_allow_html=True)
                st.caption(f"{STRAT_LABEL[sv['strategy']]} · {tf_label(sv['tf'])}")
                pos = positions.get(sym)
                if pos:
                    cur = prices.get(sym)
                    d = 1 if pos["side"] == "long" else -1
                    line = (f"{pos['side']} {pos['qty']:g} @ {pos['entry_price']:,.2f} · "
                            f"stop {pos['stop_price']:,.2f}")
                    if cur:
                        unreal = (cur - pos["entry_price"]) * pos["qty"] * d
                        line += f" · P&L ${unreal:+,.2f}"
                    st.caption(line)
                for ln in sv["lines"]:
                    st.caption(ln)

    st.divider()

    # --- open positions -----------------------------------------------------
    st.subheader("Open positions")
    if positions:
        prices = live_prices(tuple(sorted(positions))) if api_ok else {}
        rows = []
        for sym, p in positions.items():
            cur = prices.get(sym)
            direction = 1 if p["side"] == "long" else -1
            unreal = (cur - p["entry_price"]) * p["qty"] * direction if cur else None
            stop_dist = abs(cur - p["stop_price"]) / cur * 100 if cur else None
            rows.append({
                "Symbol": sym, "Strategy": p["strategy"], "Side": p["side"],
                "Qty": p["qty"], "Entry": p["entry_price"], "Now": cur,
                "Unrealized $": round(unreal, 2) if unreal is not None else None,
                "Stop": round(p["stop_price"], 4),
                "To stop %": round(stop_dist, 2) if stop_dist is not None else None,
                "Trailing": "yes" if p.get("trailing") else "fixed",
            })
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    else:
        st.caption("Flat — no open positions.")

    # --- performance charts -------------------------------------------------
    daily_hist = load_csv(config.DAILY_PNL_CSV)
    ch1, ch2 = st.columns(2)

    with ch1:
        pts = []
        if not daily_hist.empty:
            pts = [(str(r["date"]), float(r["end_equity"]))
                   for _, r in daily_hist.iterrows()]
        if daily.get("date") and equity:
            pts.append((daily["date"], float(equity)))
        if len(pts) >= 2:
            xs, ys = zip(*pts)
            fig = go.Figure(go.Scatter(
                x=list(xs), y=list(ys),
                mode="lines+markers" if len(pts) < 30 else "lines",
                line=dict(color=series_color["mean_reversion"], width=2),
                marker=dict(size=8), name="Equity",
                hovertemplate="%{x}<br>$%{y:,.2f}<extra></extra>"))
            fig.update_layout(showlegend=False, hovermode="x unified")
            st.plotly_chart(base_layout(fig, "Equity curve (daily close)"),
                            width="stretch")
        else:
            st.caption("Equity curve appears after the first daily rollover "
                       "(needs two days of history).")

    with ch2:
        if not trades.empty:
            fig = go.Figure()
            t = trades.copy()
            t["timestamp"] = pd.to_datetime(t["timestamp"])
            t = t.sort_values("timestamp")
            for strat in SERIES:  # fixed order, color follows the strategy
                sub = t[t["strategy"] == strat]
                if sub.empty:
                    continue
                fig.add_trace(go.Scatter(
                    x=sub["timestamp"], y=sub["pnl"].cumsum(),
                    mode="lines",
                    line=dict(color=series_color[strat], width=2, shape="hv"),
                    name=strat.replace("_", " "),
                    hovertemplate="%{x}<br>$%{y:,.2f}<extra>" + strat + "</extra>"))
            fig.update_layout(hovermode="x unified")
            st.plotly_chart(base_layout(fig, "Cumulative realized P&L by strategy"),
                            width="stretch")
        else:
            st.caption("Strategy P&L chart appears after the first closed trade.")

    # --- per-strategy stats -------------------------------------------------
    st.subheader("Strategy performance (closed trades)")
    if not trades.empty:
        stats = []
        for strat, sub in trades.groupby("strategy"):
            wins, losses = sub[sub["pnl"] > 0], sub[sub["pnl"] <= 0]
            gross_w, gross_l = wins["pnl"].sum(), abs(losses["pnl"].sum())
            stats.append({
                "Strategy": strat, "Trades": len(sub),
                "Win rate": f"{100*len(wins)/len(sub):.0f}%",
                "Total P&L": round(sub["pnl"].sum(), 2),
                "Avg win": round(wins["pnl"].mean(), 2) if len(wins) else 0.0,
                "Avg loss": round(losses["pnl"].mean(), 2) if len(losses) else 0.0,
                "Profit factor": round(gross_w / gross_l, 2) if gross_l > 0 else None,
            })
        st.dataframe(pd.DataFrame(stats), width="stretch", hide_index=True)
    else:
        st.caption("No closed trades yet.")

    # --- trade history + log ------------------------------------------------
    tab_trades, tab_log = st.tabs(["Trade history", "Bot log"])
    with tab_trades:
        if not trades.empty:
            st.dataframe(trades.sort_values("timestamp", ascending=False).head(100),
                         width="stretch", hide_index=True)
        else:
            st.caption("trades.csv is empty — every closed round trip lands here.")
    with tab_log:
        if os.path.exists(config.LOG_FILE):
            with open(config.LOG_FILE, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-100:]
            st.code("".join(reversed(lines)), language=None)
        else:
            st.caption("bot.log not found.")


render()
