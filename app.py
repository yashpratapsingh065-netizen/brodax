# BRODAX — Price Lesson Hindi + SMC Signal Engine (BTC Optimized via Twelve Data)
# Paper-trading only. No broker orders or live execution.

import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
import streamlit as st

try:
    from streamlit_autorefresh import st_autorefresh
    AUTOREFRESH_AVAILABLE = True
except Exception:
    AUTOREFRESH_AVAILABLE = False

st.set_page_config(page_title="BRODAX | BTC SMC Terminal", page_icon="⚡", layout="wide")

# --------------------------- CONFIGURATION ---------------------------

DB_FILE = "brodax_btc_paper.db"
SYMBOL_TD = "BTC/USD"
INTERVALS = {"5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h", "1d": "1day"}

DEFAULT_PIVOT_LOOKBACK = 3
DEFAULT_ZONE_ATR = 0.35
DEFAULT_MIN_RR = 3.0
DEFAULT_CONFLUENCE = 2
DEFAULT_PAPER_BALANCE = 1000.0
REQUEST_TIMEOUT = 12

# Get Twelve Data API Key from Streamlit Secrets or Environment
TWELVE_KEY = ""
try:
    if "TWELVE_DATA_API_KEY" in st.secrets:
        TWELVE_KEY = st.secrets["TWELVE_DATA_API_KEY"]
except Exception:
    pass

if not TWELVE_KEY:
    TWELVE_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()


# --------------------------- DATABASE ---------------------------

def db_connect():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            entry REAL NOT NULL,
            stop REAL NOT NULL,
            target REAL NOT NULL,
            quantity REAL NOT NULL,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            exit_price REAL,
            pnl REAL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            signal_reason TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def load_balance(conn):
    row = conn.execute("SELECT value FROM app_settings WHERE key='paper_balance'").fetchone()
    if row:
        try:
            return float(row[0])
        except Exception:
            pass
    conn.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES('paper_balance',?)",
                 (str(DEFAULT_PAPER_BALANCE),))
    conn.commit()
    return DEFAULT_PAPER_BALANCE


# --------------------------- MARKET DATA (TWELVE DATA) ---------------------------

@st.cache_data(ttl=10, show_spinner=False)
def get_twelve_klines(interval_td: str, limit: int = 150) -> pd.DataFrame:
    if not TWELVE_KEY:
        raise RuntimeError("Twelve Data API key not found! Please configure TWELVE_DATA_API_KEY in Streamlit Secrets.")
    
    url = f"https://api.twelvedata.com/time_series?symbol={SYMBOL_TD}&interval={interval_td}&outputsize={limit}&timezone=UTC&apikey={TWELVE_KEY}"
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        res = r.json()
        if "values" not in res:
            raise ValueError(f"Twelve Data Error: {res.get('message', res)}")
        
        df = pd.DataFrame(res["values"])
        df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.sort_values("datetime").reset_index(drop=True)
        df = df.rename(columns={"datetime": "close_time"})
        if len(df) < 30:
            raise ValueError("Not enough completed candles from Twelve Data.")
        return df
    except Exception as exc:
        raise RuntimeError(f"Twelve Data market fetch failed: {exc}")


@st.cache_data(ttl=5, show_spinner=False)
def get_twelve_price() -> float:
    if not TWELVE_KEY:
        raise RuntimeError("Twelve Data API key missing.")
    url = f"https://api.twelvedata.com/price?symbol={SYMBOL_TD}&apikey={TWELVE_KEY}"
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if "price" in data:
            return float(data["price"])
        raise ValueError(data.get("message", "Price not found"))
    except Exception as exc:
        raise RuntimeError(f"Live ticker unavailable: {exc}")


# --------------------------- INDICATORS / STRUCTURE ---------------------------

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x["body"] = (x["close"] - x["open"]).abs()
    x["range"] = (x["high"] - x["low"]).replace(0, np.nan)
    x["upper_wick"] = x["high"] - x[["open", "close"]].max(axis=1)
    x["lower_wick"] = x[["open", "close"]].min(axis=1) - x["low"]
    x["bull"] = x["close"] > x["open"]
    x["bear"] = x["close"] < x["open"]
    x["ema20"] = x["close"].ewm(span=20, adjust=False).mean()
    x["ema50"] = x["close"].ewm(span=50, adjust=False).mean()
    x["atr"] = (
        pd.concat([
            x["high"] - x["low"],
            (x["high"] - x["close"].shift()).abs(),
            (x["low"] - x["close"].shift()).abs()
        ], axis=1).max(axis=1).rolling(14).mean()
    )
    return x.dropna(subset=["atr"]).reset_index(drop=True)


def pivot_points(df: pd.DataFrame, n: int = 3):
    highs, lows = [], []
    if len(df) < 2 * n + 5:
        return highs, lows
    for i in range(n, len(df) - n):
        h = float(df["high"].iloc[i])
        l = float(df["low"].iloc[i])
        if h >= float(df["high"].iloc[i-n:i+n+1].max()):
            highs.append((i, h))
        if l <= float(df["low"].iloc[i-n:i+n+1].min()):
            lows.append((i, l))
    return highs, lows


def trend_from_swings(df: pd.DataFrame, n: int = 3) -> str:
    highs, lows = pivot_points(df, n)
    if len(highs) < 2 or len(lows) < 2:
        return "NEUTRAL"
    hh = highs[-1][1] > highs[-2][1]
    hl = lows[-1][1] > lows[-2][1]
    lh = highs[-1][1] < highs[-2][1]
    ll = lows[-1][1] < lows[-2][1]
    if hh and hl: return "BULLISH"
    if lh and ll: return "BEARISH"
    return "NEUTRAL"


def momentum_direction(df: pd.DataFrame) -> str:
    if len(df) < 25:
        return "NEUTRAL"
    last = df.iloc[-1]
    if last["close"] > last["ema20"] > last["ema50"] and last["ema20"] > df["ema20"].iloc[-4]:
        return "BULLISH"
    if last["close"] < last["ema20"] < last["ema50"] and last["ema20"] < df["ema20"].iloc[-4]:
        return "BEARISH"
    return "NEUTRAL"


def candle_pattern(df: pd.DataFrame) -> str:
    if len(df) < 3:
        return "NONE"
    a, b = df.iloc[-2], df.iloc[-1]
    bull_engulf = (a["close"] < a["open"] and b["close"] > b["open"] and b["open"] <= a["close"] and b["close"] >= a["open"])
    bear_engulf = (a["close"] > a["open"] and b["close"] < b["open"] and b["open"] >= a["close"] and b["close"] <= a["open"])
    if bull_engulf: return "BULLISH_ENGULFING"
    if bear_engulf: return "BEARISH_ENGULFING"
    r = max(float(b["range"]), 1e-12)
    if b["body"] / r <= 0.12: return "DOJI"
    if b["lower_wick"] >= b["body"] * 2: return "BULLISH_REJECTION"
    if b["upper_wick"] >= b["body"] * 2: return "BEARISH_REJECTION"
    return "NONE"


def recent_levels(df: pd.DataFrame, n: int = 3):
    highs, lows = pivot_points(df, n)
    hi = highs[-1][1] if highs else float(df["high"].tail(30).max())
    lo = lows[-1][1] if lows else float(df["low"].tail(30).min())
    return float(hi), float(lo)


def find_fvg(df: pd.DataFrame):
    if len(df) < 5: return None
    for i in range(len(df)-2, max(1, len(df)-20), -1):
        c1, c2, c3 = df.iloc[i-1], df.iloc[i], df.iloc[i+1]
        if float(c1["high"]) < float(c3["low"]) and bool(c2["bull"]):
            return {"side": "BULLISH", "low": float(c1["high"]), "high": float(c3["low"])}
        if float(c1["low"]) > float(c3["high"]) and bool(c2["bear"]):
            return {"side": "BEARISH", "low": float(c3["high"]), "high": float(c1["low"])}
    return None


def detect_sd_zone(df: pd.DataFrame):
    if len(df) < 20: return None
    x = df.tail(20)
    lo = float(x["low"].min())
    hi = float(x["high"].max())
    px = float(x["close"].iloc[-1])
    if px <= (lo + hi) / 2:
        return {"side": "DEMAND", "low": lo, "high": (lo+hi)/2}
    else:
        return {"side": "SUPPLY", "low": (lo+hi)/2, "high": hi}


# --------------------------- STRATEGY ENGINE ---------------------------

def result(name, side="WAIT", reason="No complete setup", confidence="LOW"):
    return {"strategy": name, "side": side, "reason": reason, "confidence": confidence}


def analyze_btc(data: Dict[str, pd.DataFrame], pivot_n: int):
    d5, d15, h1, h4, d1 = [add_indicators(data[k]) for k in ["5m", "15m", "1h", "4h", "1d"]]
    out = []

    entry = d15
    px = float(entry["close"].iloc[-1])
    atr = float(entry["atr"].iloc[-1])
    pattern = candle_pattern(entry)
    trend15 = trend_from_swings(entry, pivot_n)

    # S1 — MTF Alignment
    dir4, beh1, ent15 = momentum_direction(h4), momentum_direction(h1), momentum_direction(d15)
    if dir4 == beh1 == ent15 and dir4 in ("BULLISH", "BEARISH"):
        out.append(result("S1 MTF", "BUY" if dir4 == "BULLISH" else "SELL", f"Multi-timeframe momentum aligned ({dir4})", "HIGH"))
    else:
        out.append(result("S1 MTF", reason=f"Timeframes mixed: 4H={dir4}, 1H={beh1}, 15M={ent15}"))

    # S4 — S/R & Rejection
    hi, lo = recent_levels(entry, pivot_n)
    if abs(px - lo) <= atr * 1.5 and pattern in ("BULLISH_ENGULFING", "BULLISH_REJECTION"):
        out.append(result("S4 S/R Rejection", "BUY", "Near support zone with bullish reversal candle", "HIGH"))
    elif abs(px - hi) <= atr * 1.5 and pattern in ("BEARISH_ENGULFING", "BEARISH_REJECTION"):
        out.append(result("S4 S/R Rejection", "SELL", "Near resistance zone with bearish reversal candle", "HIGH"))
    else:
        out.append(result("S4 S/R Rejection", reason="Waiting for exact test of support/resistance"))

    # S5-FVG — Fair Value Gap
    fvg = find_fvg(entry)
    if fvg and fvg["side"] == "BULLISH" and pattern in ("BULLISH_ENGULFING", "BULLISH_REJECTION"):
        out.append(result("S5-FVG FVG Test", "BUY", "Bullish FVG zone test with confirmation", "MEDIUM"))
    elif fvg and fvg["side"] == "BEARISH" and pattern in ("BEARISH_ENGULFING", "BEARISH_REJECTION"):
        out.append(result("S5-FVG FVG Test", "SELL", "Bearish FVG zone test with confirmation", "MEDIUM"))
    else:
        out.append(result("S5-FVG FVG Test", reason="No active FVG zone test with candle confirmation"))

    # S6 — Supply & Demand
    sd = detect_sd_zone(entry)
    if sd and sd["side"] == "DEMAND" and trend15 == "BULLISH":
        out.append(result("S6 Supply & Demand", "BUY", "Demand zone active in bullish 15M structure", "MEDIUM"))
    elif sd and sd["side"] == "SUPPLY" and trend15 == "BEARISH":
        out.append(result("S6 Supply & Demand", "SELL", "Supply zone active in bearish 15M structure", "MEDIUM"))
    else:
        out.append(result("S6 Supply & Demand", reason="Price not in optimal S/D zone"))

    # S7 — Premium & Discount Equilibrium
    swing_hi, swing_lo = recent_levels(entry, pivot_n)
    eq = (swing_hi + swing_lo) / 2
    if px < eq and trend15 == "BULLISH":
        out.append(result("S7 Premium/Discount", "BUY", "Price in Discount zone (below 50% equilibrium)", "MEDIUM"))
    elif px > eq and trend15 == "BEARISH":
        out.append(result("S7 Premium/Discount", "SELL", "Price in Premium zone (above 50% equilibrium)", "MEDIUM"))
    else:
        out.append(result("S7 Premium/Discount", reason="Waiting for discount/premium alignment"))

    # Risk Levels for BTC
    buffer = atr * 0.5
    buy_stop = lo - buffer
    sell_stop = hi + buffer
    buy_risk = px - buy_stop
    sell_risk = sell_stop - px
    
    buy_target = px + (max(buy_risk, atr) * DEFAULT_MIN_RR)
    sell_target = px - (max(sell_risk, atr) * DEFAULT_MIN_RR)

    buy_votes = sum(1 for r in out if r["side"] == "BUY")
    sell_votes = sum(1 for r in out if r["side"] == "SELL")
    
    consensus = "WAIT"
    if buy_votes >= DEFAULT_CONFLUENCE and buy_votes > sell_votes:
        consensus = "BUY"
    elif sell_votes >= DEFAULT_CONFLUENCE and sell_votes > buy_votes:
        consensus = "SELL"

    return {
        "strategies": out, "entry_price": px, "pattern": pattern, "trend15": trend15,
        "buy_votes": buy_votes, "sell_votes": sell_votes, "consensus": consensus,
        "buy_stop": buy_stop, "buy_target": buy_target,
        "sell_stop": sell_stop, "sell_target": sell_target,
        "atr": atr, "support": lo, "resistance": hi
    }


# --------------------------- UI & EXECUTION ---------------------------

st.title("⚡ BRODAX | BTC SMC Signal Engine")
st.caption("Twelve Data API Feed • Bitcoin (BTC/USD) Optimized • Paper Trading Terminal")

with st.sidebar:
    st.header("⚙️ BRODAX Settings")
    st.write("Target Asset: **BTC/USD**")
    pivot_n = st.slider("Swing Lookback", 2, 6, DEFAULT_PIVOT_LOOKBACK)
    min_votes = st.slider("Min Strategy Confluence", 1, 5, DEFAULT_CONFLUENCE)
    paper_risk_pct = st.slider("Paper Risk per Trade (%)", 0.25, 2.0, 1.0, 0.25)
    refresh_seconds = st.selectbox("Refresh Interval (sec)", [10, 15, 30, 60], index=1)

if AUTOREFRESH_AVAILABLE:
    st_autorefresh(interval=refresh_seconds * 1000, key="brodax_btc_refresh")

conn = db_connect()
balance = load_balance(conn)

try:
    with st.spinner("Fetching live BTC data from Twelve Data & running SMC analysis..."):
        market = {tf: get_twelve_klines(iv, 100) for tf, iv in INTERVALS.items()}
        live_btc_price = get_twelve_price()
        analysis = analyze_btc(market, pivot_n)
except Exception as e:
    st.error(f"Data Fetch Error: {e}")
    st.warning("Please verify your `TWELVE_DATA_API_KEY` in Streamlit Cloud Secrets (`.streamlit/secrets.toml`).")
    st.stop()

# Metrics Display
m1, m2, m3, m4 = st.columns(4)
m1.metric("BTC/USD Live Ticker", f"${live_btc_price:,.2f}")
m2.metric("15M Candle Close", f"${analysis['entry_price']:,.2f}")
m3.metric("15M Structure", analysis["trend15"])
m4.metric("Consensus Bias", analysis["consensus"])

st.divider()
st.subheader("🎯 BRODAX Signal & Execution Plan")
col_sig1, col_sig2 = st.columns([1, 2])

with col_sig1:
    if analysis["consensus"] == "BUY":
        st.success("🟢 BUY SIGNAL CONFIRMED")
    elif analysis["consensus"] == "SELL":
        st.error("🔴 SELL SIGNAL CONFIRMED")
    else:
        st.warning("⏳ WAITING FOR CONFLUENCE")
    st.write(f"BUY Strategy Votes: **{analysis['buy_votes']}**")
    st.write(f"SELL Strategy Votes: **{analysis['sell_votes']}**")

with col_sig2:
    if analysis["consensus"] == "BUY":
        st.write(f"**Entry Price:** ${analysis['entry_price']:,.2f}")
        st.write(f"**Stop Loss:** ${analysis['buy_stop']:,.2f}")
        st.write(f"**Target (1:3):** ${analysis['buy_target']:,.2f}")
    elif analysis["consensus"] == "SELL":
        st.write(f"**Entry Price:** ${analysis['entry_price']:,.2f}")
        st.write(f"**Stop Loss:** ${analysis['sell_stop']:,.2f}")
        st.write(f"**Target (1:3):** ${analysis['sell_target']:,.2f}")
    else:
        st.info("Market structure is consolidating. Standing by for valid SMC setup.")

st.divider()
st.subheader("📚 SMC Strategy Breakdown")
st.dataframe(pd.DataFrame(analysis["strategies"])[["strategy", "side", "confidence", "reason"]], use_container_width=True, hide_index=True)

st.divider()
st.subheader("🧪 BTC Paper Trading Terminal")
st.write(f"**Account Balance:** ${balance:,.2f}")

if analysis["consensus"] in ("BUY", "SELL") and st.button("Execute Paper Trade"):
    side = analysis["consensus"]
    entry = analysis["entry_price"]
    stop = analysis["buy_stop"] if side == "BUY" else analysis["sell_stop"]
    target = analysis["buy_target"] if side == "BUY" else analysis["sell_target"]
    
    risk_cash = balance * (paper_risk_pct / 100.0)
    qty = risk_cash / abs(entry - stop) if abs(entry - stop) > 0 else 0.01
    
    conn.execute("""
        INSERT INTO paper_trades(symbol, side, entry, stop, target, quantity, opened_at, status, signal_reason)
        VALUES(?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
    """, ("BTC/USD", side, entry, stop, target, qty, datetime.now(timezone.utc).isoformat(), f"{side} Confluence Setup"))
    conn.commit()
    st.success("Paper trade executed successfully!")
    st.rerun()

open_trades = conn.execute("SELECT id, side, entry, stop, target, quantity, opened_at FROM paper_trades WHERE status='OPEN'").fetchall()
if open_trades:
    st.dataframe(pd.DataFrame(open_trades, columns=["ID", "Side", "Entry", "Stop", "Target", "Qty", "Opened"]), use_container_width=True, hide_index=True)
else:
    st.info("No open paper trades.")

conn.close()
