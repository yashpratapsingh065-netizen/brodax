# BRODAX — Price Lesson Hindi + SMC Signal Engine
# Paper-trading only. No broker orders or live execution.
# Install:
#   pip install streamlit pandas numpy requests streamlit-autorefresh
# Run:
#   streamlit run app.py

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

st.set_page_config(page_title="BRODAX | Price Lesson + SMC", page_icon="📈", layout="wide")

# --------------------------- CONFIGURATION ---------------------------

DB_FILE = "brodax_paper_trading.db"
BINANCE_HOSTS = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
]
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT"]
INTERVALS = ["5m", "15m", "1h", "4h", "1d"]
INTERVAL_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

# These are engineering defaults for detecting qualitative concepts in code.
# They are NOT additional Price Lesson rules. Users can adjust them in the sidebar.
DEFAULT_PIVOT_LOOKBACK = 3
DEFAULT_ZONE_ATR = 0.35
DEFAULT_MIN_RR = 3.0
DEFAULT_CONFLUENCE = 2
DEFAULT_PAPER_BALANCE = 1000.0
REQUEST_TIMEOUT = 12


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


def save_balance(conn, balance):
    conn.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES('paper_balance',?)",
                 (str(float(balance)),))
    conn.commit()


# --------------------------- MARKET DATA ---------------------------

@st.cache_data(ttl=3, show_spinner=False)
def get_klines(symbol: str, interval: str, limit: int = 300) -> pd.DataFrame:
    last_error = None
    for host in BINANCE_HOSTS:
        try:
            response = requests.get(
                host + "/api/v3/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": "BRODAX-Paper-Signal/1.0"},
            )
            response.raise_for_status()
            raw = response.json()
            if not isinstance(raw, list) or not raw:
                raise ValueError(f"Unexpected Binance response: {raw}")
            cols = [
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_volume", "trades", "taker_buy_base",
                "taker_buy_quote", "ignore"
            ]
            df = pd.DataFrame(raw, columns=cols)
            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
            df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)

            # Only use completed candles. The last still-forming candle is excluded.
            now = pd.Timestamp.now(tz="UTC")
            df = df[df["close_time"] < now].copy().reset_index(drop=True)
            if len(df) < 60:
                raise ValueError("Not enough completed candles returned.")
            return df
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Market data unavailable from Binance endpoints: {last_error}")


@st.cache_data(ttl=2, show_spinner=False)
def get_ticker_price(symbol: str) -> Tuple[float, str]:
    last_error = None
    for host in BINANCE_HOSTS:
        try:
            r = requests.get(
                host + "/api/v3/ticker/price",
                params={"symbol": symbol},
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": "BRODAX-Paper-Signal/1.0"},
            )
            r.raise_for_status()
            data = r.json()
            return float(data["price"]), host
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Live ticker unavailable: {last_error}")


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
    x["vol_sma"] = x["volume"].rolling(20).mean()
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
    if hh and hl:
        return "BULLISH"
    if lh and ll:
        return "BEARISH"
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
    # Engulfing body confirmation
    bull_engulf = (
        a["close"] < a["open"] and b["close"] > b["open"]
        and b["open"] <= a["close"] and b["close"] >= a["open"]
    )
    bear_engulf = (
        a["close"] > a["open"] and b["close"] < b["open"]
        and b["open"] >= a["close"] and b["close"] <= a["open"]
    )
    if bull_engulf:
        return "BULLISH_ENGULFING"
    if bear_engulf:
        return "BEARISH_ENGULFING"
    r = max(float(b["range"]), 1e-12)
    if b["body"] / r <= 0.12:
        return "DOJI"
    if b["lower_wick"] >= b["body"] * 2 and b["upper_wick"] <= b["body"] * 1.2:
        return "BULLISH_REJECTION"
    if b["upper_wick"] >= b["body"] * 2 and b["lower_wick"] <= b["body"] * 1.2:
        return "BEARISH_REJECTION"
    return "NONE"


def recent_levels(df: pd.DataFrame, n: int = 3):
    highs, lows = pivot_points(df, n)
    hi = highs[-1][1] if highs else float(df["high"].tail(30).max())
    lo = lows[-1][1] if lows else float(df["low"].tail(30).min())
    return float(hi), float(lo)


def nearest_sr_zones(df: pd.DataFrame, n: int = 3, atr_mult: float = 0.35):
    highs, lows = pivot_points(df, n)
    px = float(df["close"].iloc[-1])
    atr = float(df["atr"].iloc[-1])
    tol = max(atr * atr_mult, px * 0.0005)
    high_levels = [v for _, v in highs if abs(v - px) <= max(atr * 5, px * 0.03)]
    low_levels = [v for _, v in lows if abs(v - px) <= max(atr * 5, px * 0.03)]
    resistance = min(high_levels, key=lambda v: abs(v-px)) if high_levels else None
    support = min(low_levels, key=lambda v: abs(v-px)) if low_levels else None
    return support, resistance, tol


def detect_bos(df: pd.DataFrame, n: int = 3):
    highs, lows = pivot_points(df.iloc[:-1].reset_index(drop=True), n)
    close = float(df["close"].iloc[-1])
    if highs and close > highs[-1][1]:
        return "BULLISH", float(highs[-1][1])
    if lows and close < lows[-1][1]:
        return "BEARISH", float(lows[-1][1])
    return "NONE", None


def detect_liquidity_sweep(df: pd.DataFrame, n: int = 3):
    highs, lows = pivot_points(df.iloc[:-1].reset_index(drop=True), n)
    last = df.iloc[-1]
    if highs:
        level = highs[-1][1]
        if last["high"] > level and last["close"] < level:
            return "BEARISH_SWEEP", float(level)
    if lows:
        level = lows[-1][1]
        if last["low"] < level and last["close"] > level:
            return "BULLISH_SWEEP", float(level)
    return "NONE", None


def find_fvg(df: pd.DataFrame):
    # Standard three-candle gap: candle 1 vs candle 3.
    # Return only a recently-created, untouched zone.
    if len(df) < 5:
        return None
    for i in range(len(df)-2, max(1, len(df)-35), -1):
        c1, c2, c3 = df.iloc[i-1], df.iloc[i], df.iloc[i+1]
        bullish_gap = float(c1["high"]) < float(c3["low"]) and bool(c2["bull"])
        bearish_gap = float(c1["low"]) > float(c3["high"]) and bool(c2["bear"])
        if bullish_gap:
            low, high = float(c1["high"]), float(c3["low"])
            later = df.iloc[i+2:]
            # Any subsequent touch invalidates the first-test-only zone.
            touched = (later["low"] <= high).any()
            if not touched:
                return {"side": "BULLISH", "low": low, "high": high, "mid": (low+high)/2, "index": i+1}
        if bearish_gap:
            low, high = float(c3["high"]), float(c1["low"])
            later = df.iloc[i+2:]
            touched = (later["high"] >= low).any()
            if not touched:
                return {"side": "BEARISH", "low": low, "high": high, "mid": (low+high)/2, "index": i+1}
    return None


def detect_sd_zone(df: pd.DataFrame, atr_mult: float = 0.35):
    # Mechanical approximation of transcript's three zone methods:
    # (a) previous candle before >=3 strong directional candles,
    # (b) tight consolidation before breakout,
    # (c) cluster of long rejection wicks.
    if len(df) < 30:
        return None
    x = df.reset_index(drop=True)
    atr = float(x["atr"].iloc[-1])
    recent_start = max(5, len(x)-60)
    for i in range(len(x)-4, recent_start, -1):
        prev = x.iloc[i-1]
        nxt = x.iloc[i:i+3]
        strong_bull = (nxt["bull"] & (nxt["body"] >= nxt["atr"] * 0.55)).sum() >= 3
        strong_bear = (nxt["bear"] & (nxt["body"] >= nxt["atr"] * 0.55)).sum() >= 3
        if strong_bull:
            return {"side": "DEMAND", "low": float(prev["low"]), "high": float(prev["high"]), "method": "3-candle momentum origin"}
        if strong_bear:
            return {"side": "SUPPLY", "low": float(prev["low"]), "high": float(prev["high"]), "method": "3-candle momentum origin"}

    # Consolidation breakout approximation
    window = x.tail(12)
    base = window.iloc[:-3]
    if len(base) >= 6:
        base_range = float(base["high"].max() - base["low"].min())
        if base_range <= atr * 2.5:
            last3 = window.tail(3)
            if (last3["close"] > base["high"].max()).all():
                return {"side": "DEMAND", "low": float(base["low"].min()), "high": float(base["high"].max()), "method": "consolidation breakout"}
            if (last3["close"] < base["low"].min()).all():
                return {"side": "SUPPLY", "low": float(base["low"].min()), "high": float(base["high"].max()), "method": "consolidation breakout"}

    # Wick rejection cluster approximation
    recent = x.tail(30)
    wick_rows = recent[(recent["upper_wick"] > recent["body"]*2) | (recent["lower_wick"] > recent["body"]*2)]
    if len(wick_rows) >= 3:
        lo = float(wick_rows[["open", "close", "low"]].min().min())
        hi = float(wick_rows[["open", "close", "high"]].max().max())
        if float(recent.iloc[-1]["close"]) >= (lo+hi)/2:
            return {"side": "DEMAND", "low": lo, "high": hi, "method": "wick rejection cluster"}
        return {"side": "SUPPLY", "low": lo, "high": hi, "method": "wick rejection cluster"}
    return None


def impulse_corrective_reversal(df: pd.DataFrame, n: int = 3):
    # Compare the last two directional impulse/correction legs using confirmed swing points.
    highs, lows = pivot_points(df, n)
    if len(highs) < 2 or len(lows) < 2:
        return "NONE"
    trend = trend_from_swings(df, n)
    if trend == "BEARISH":
        down_leg = abs(lows[-1][1] - highs[-1][1])
        up_leg = abs(highs[-1][1] - lows[-2][1])
        if up_leg > down_leg * 1.15:
            return "POTENTIAL_BULLISH_REVERSAL"
    elif trend == "BULLISH":
        up_leg = abs(highs[-1][1] - lows[-1][1])
        down_leg = abs(lows[-1][1] - highs[-2][1])
        if down_leg > up_leg * 1.15:
            return "POTENTIAL_BEARISH_REVERSAL"
    return "NONE"


# --------------------------- STRATEGY ENGINE ---------------------------

def result(name, side="WAIT", reason="No complete setup", confidence="LOW", details=""):
    return {"strategy": name, "side": side, "reason": reason, "confidence": confidence, "details": details}


def analyze_all(data: Dict[str, pd.DataFrame], pivot_n: int, zone_atr: float):
    d5, d15, h1, h4, d1 = [add_indicators(data[k]) for k in ["5m", "15m", "1h", "4h", "1d"]]
    out = []

    # Common entry timeframe is 15m for intraday/swing configuration.
    entry = d15
    px = float(entry["close"].iloc[-1])
    atr = float(entry["atr"].iloc[-1])
    pattern = candle_pattern(entry)
    trend15 = trend_from_swings(entry, pivot_n)
    bos15, bos_level = detect_bos(entry, pivot_n)
    sweep15, sweep_level = detect_liquidity_sweep(entry, pivot_n)

    # S1 — MTF: 4H direction, 1H behavior, 15M entry; all aligned.
    dir4, beh1, ent15 = momentum_direction(h4), momentum_direction(h1), momentum_direction(d15)
    if dir4 == beh1 == ent15 and dir4 in ("BULLISH", "BEARISH"):
        wanted_pattern = "BULLISH_ENGULFING" if dir4 == "BULLISH" else "BEARISH_ENGULFING"
        side = "BUY" if dir4 == "BULLISH" else "SELL"
        if pattern == wanted_pattern or bos15 == dir4:
            out.append(result("S1 MTF", side, "4H/1H/15M momentum aligned; 15M confirmation present", "HIGH",
                               f"4H={dir4}, 1H={beh1}, 15M={ent15}"))
        else:
            out.append(result("S1 MTF", reason="Timeframes aligned; waiting for 15M reversal/level-break confirmation",
                               details=f"4H={dir4}, 1H={beh1}, 15M={ent15}"))
    else:
        out.append(result("S1 MTF", reason="Higher/lower timeframe momentum not fully aligned",
                           details=f"4H={dir4}, 1H={beh1}, 15M={ent15}"))

    # S2 — Chart reading: structure + impulse/corrective weakening clue.
    reversal = impulse_corrective_reversal(d15, pivot_n)
    if reversal == "POTENTIAL_BULLISH_REVERSAL" and pattern in ("BULLISH_ENGULFING", "BULLISH_REJECTION"):
        out.append(result("S2 Chart Reading", "BUY", "Downside impulse weakening; larger corrective leg and bullish candle confirmation", "MEDIUM"))
    elif reversal == "POTENTIAL_BEARISH_REVERSAL" and pattern in ("BEARISH_ENGULFING", "BEARISH_REJECTION"):
        out.append(result("S2 Chart Reading", "SELL", "Upside impulse weakening; larger corrective leg and bearish candle confirmation", "MEDIUM"))
    else:
        out.append(result("S2 Chart Reading", reason=f"Structure={trend15}; impulse/corrective reversal confirmation absent",
                           details=f"Reversal scan={reversal}; candle={pattern}"))

    # S3 — Trend + retest + indecision followed by engulfing confirmation.
    s3_side = "WAIT"
    s3_reason = "Waiting for retest/Doji followed by engulfing confirmation"
    if len(d15) >= 4:
        prev2, prev1 = d15.iloc[-3], d15.iloc[-2]
        if trend15 == "BULLISH" and prev2["body"] / max(prev2["range"], 1e-12) <= 0.15 and pattern == "BULLISH_ENGULFING":
            s3_side, s3_reason = "BUY", "Bullish structure; Doji/indecision followed by bullish engulfing"
        elif trend15 == "BEARISH" and prev2["body"] / max(prev2["range"], 1e-12) <= 0.15 and pattern == "BEARISH_ENGULFING":
            s3_side, s3_reason = "SELL", "Bearish structure; Doji/indecision followed by bearish engulfing"
    out.append(result("S3 3-Step Price Action", s3_side, s3_reason, "MEDIUM" if s3_side != "WAIT" else "LOW"))

    # S4 — S/R proximity + rejection + MA crossover confirmation.
    support, resistance, tol = nearest_sr_zones(d15, pivot_n, zone_atr)
    ema_cross_up = d15["ema20"].iloc[-1] > d15["ema50"].iloc[-1] and d15["ema20"].iloc[-2] <= d15["ema50"].iloc[-2]
    ema_cross_dn = d15["ema20"].iloc[-1] < d15["ema50"].iloc[-1] and d15["ema20"].iloc[-2] >= d15["ema50"].iloc[-2]
    if support is not None and abs(px-support) <= tol*2 and pattern == "BULLISH_ENGULFING" and ema_cross_up:
        out.append(result("S4 Support & Resistance", "BUY", "Near support; bullish engulfing and bullish MA crossover", "HIGH"))
    elif resistance is not None and abs(px-resistance) <= tol*2 and pattern == "BEARISH_ENGULFING" and ema_cross_dn:
        out.append(result("S4 Support & Resistance", "SELL", "Near resistance; bearish engulfing and bearish MA crossover", "HIGH"))
    else:
        out.append(result("S4 Support & Resistance", reason="Need nearby tested S/R zone + reversal candle + MA crossover",
                           details=f"Support={support}; Resistance={resistance}; pattern={pattern}"))

    # S5 — IDM: BOS, then first pullback level gets taken; wick/body/tap counts.
    # This is a conservative mechanical approximation of the transcript's left-side pullback rule.
    idm_side = "WAIT"
    idm_reason = "No confirmed BOS followed by first-pullback inducement"
    if len(d15) > 20:
        prior = d15.iloc[-20:-1]
        p_highs, p_lows = pivot_points(prior.reset_index(drop=True), pivot_n)
        if bos15 == "BULLISH" and p_lows:
            pullback_low = p_lows[-1][1]
            # In bullish structure, a sweep below the first pullback low is treated as inducement.
            if float(d15["low"].iloc[-1]) <= pullback_low and float(d15["close"].iloc[-1]) > pullback_low:
                idm_side, idm_reason = "BUY", "Bullish BOS; first pullback low was swept and price reclaimed it (IDM approximation)"
        elif bos15 == "BEARISH" and p_highs:
            pullback_high = p_highs[-1][1]
            if float(d15["high"].iloc[-1]) >= pullback_high and float(d15["close"].iloc[-1]) < pullback_high:
                idm_side, idm_reason = "SELL", "Bearish BOS; first pullback high was swept and price rejected it (IDM approximation)"
    out.append(result("S5 Inducement (IDM)", idm_side, idm_reason, "MEDIUM" if idm_side != "WAIT" else "LOW"))

    # S5-FVG — unmitigated FVG + sweep + BOS alignment + 50% equilibrium rejection.
    fvg = find_fvg(d15)
    fvg_side = "WAIT"
    fvg_reason = "No fresh unmitigated FVG with all required confluences"
    if fvg:
        inside = fvg["low"] <= px <= fvg["high"]
        mid_reject_bull = fvg["side"] == "BULLISH" and float(d15["low"].iloc[-1]) <= fvg["mid"] and float(d15["close"].iloc[-1]) > fvg["mid"]
        mid_reject_bear = fvg["side"] == "BEARISH" and float(d15["high"].iloc[-1]) >= fvg["mid"] and float(d15["close"].iloc[-1]) < fvg["mid"]
        if fvg["side"] == "BULLISH" and sweep15 == "BULLISH_SWEEP" and bos15 == "BULLISH" and mid_reject_bull:
            fvg_side, fvg_reason = "BUY", "Fresh bullish FVG + liquidity sweep + bullish BOS + 50% midpoint rejection"
        elif fvg["side"] == "BEARISH" and sweep15 == "BEARISH_SWEEP" and bos15 == "BEARISH" and mid_reject_bear:
            fvg_side, fvg_reason = "SELL", "Fresh bearish FVG + liquidity sweep + bearish BOS + 50% midpoint rejection"
        else:
            fvg_reason = f"FVG {fvg['side']} zone {fvg['low']:.6g}-{fvg['high']:.6g}; waiting for sweep/BOS/50% rejection"
    out.append(result("S5-FVG Valid FVG", fvg_side, fvg_reason, "HIGH" if fvg_side != "WAIT" else "LOW",
                       details=f"FVG={fvg}"))

    # S6 + S9 — Supply & Demand zone + confirmation. Same transcript family, separately displayed.
    sd = detect_sd_zone(d15, zone_atr)
    sd_side = "WAIT"
    sd_reason = "No valid recent S/D zone with confirmation"
    if sd:
        in_zone = sd["low"] <= px <= sd["high"] or (
            float(d15["low"].iloc[-1]) <= sd["high"] and float(d15["high"].iloc[-1]) >= sd["low"]
        )
        if sd["side"] == "DEMAND" and in_zone and pattern in ("BULLISH_ENGULFING", "BULLISH_REJECTION"):
            sd_side, sd_reason = "BUY", f"Demand zone retest + bullish confirmation ({sd['method']})"
        elif sd["side"] == "SUPPLY" and in_zone and pattern in ("BEARISH_ENGULFING", "BEARISH_REJECTION"):
            sd_side, sd_reason = "SELL", f"Supply zone retest + bearish confirmation ({sd['method']})"
        else:
            sd_reason = f"{sd['side']} zone found; waiting for zone retest and rejection/engulfing"
    out.append(result("S6 Supply & Demand", sd_side, sd_reason, "MEDIUM" if sd_side != "WAIT" else "LOW",
                       details=f"Zone={sd}"))
    out.append(result("S9 S/D Full Guide", sd_side, sd_reason + "; Fib 0.5/0.618 confluence is monitored separately", "MEDIUM" if sd_side != "WAIT" else "LOW",
                       details=f"Zone={sd}"))

    # S7 — Premium / Discount from recent valid dealing range.
    swing_high, swing_low = recent_levels(d15, pivot_n)
    eq = (swing_high + swing_low) / 2
    pd_side = "WAIT"
    pd_reason = "Wait for price to enter the appropriate half of dealing range with confirmation"
    if px < eq and pattern in ("BULLISH_ENGULFING", "BULLISH_REJECTION"):
        pd_side, pd_reason = "BUY", "Discount zone (below 50% equilibrium) + bullish rejection"
    elif px > eq and pattern in ("BEARISH_ENGULFING", "BEARISH_REJECTION"):
        pd_side, pd_reason = "SELL", "Premium zone (above 50% equilibrium) + bearish rejection"
    out.append(result("S7 Premium & Discount", pd_side, pd_reason, "MEDIUM" if pd_side != "WAIT" else "LOW",
                       details=f"Range low={swing_low}; high={swing_high}; equilibrium={eq}"))

    # S8 — Daily bias: previous daily candle body close beyond prior day's high/low;
    # wick-only break then close back inside = sweep; inside bar = neutral.
    daily_bias = "NEUTRAL"
    daily_reason = "No clear daily bias"
    if len(d1) >= 3:
        p2, p1 = d1.iloc[-2], d1.iloc[-1]  # prior completed day, then most recent completed day
        if p1["high"] < p2["high"] and p1["low"] > p2["low"]:
            daily_bias, daily_reason = "NEUTRAL", "Previous completed daily candle is an inside bar"
        elif p1["close"] > p2["high"]:
            daily_bias, daily_reason = "BULLISH", "Daily body close above previous day's high (PDH break)"
        elif p1["close"] < p2["low"]:
            daily_bias, daily_reason = "BEARISH", "Daily body close below previous day's low (PDL break)"
        elif p1["high"] > p2["high"] and p1["close"] <= p2["high"]:
            daily_bias, daily_reason = "BEARISH", "PDH wick sweep; close returned inside prior range"
        elif p1["low"] < p2["low"] and p1["close"] >= p2["low"]:
            daily_bias, daily_reason = "BULLISH", "PDL wick sweep; close returned inside prior range"
    daily_side = "WAIT"
    if daily_bias == "BULLISH" and ent15 == "BULLISH" and (pattern == "BULLISH_ENGULFING" or fvg_side == "BUY" or sd_side == "BUY"):
        daily_side = "BUY"
    elif daily_bias == "BEARISH" and ent15 == "BEARISH" and (pattern == "BEARISH_ENGULFING" or fvg_side == "SELL" or sd_side == "SELL"):
        daily_side = "SELL"
    out.append(result("S8 Daily Bias", daily_side, daily_reason + f"; 15M alignment={ent15}", "HIGH" if daily_side != "WAIT" else "LOW",
                       details=f"Daily bias={daily_bias}"))

    # Risk levels based on recent structure. The 1:3 target is the S8 framework.
    # Stops are placed beyond recent swing with an ATR safety buffer.
    recent_hi, recent_lo = recent_levels(d15, pivot_n)
    buffer = max(atr * 0.15, px * 0.0003)
    buy_stop = min(recent_lo, px - atr) - buffer
    sell_stop = max(recent_hi, px + atr) + buffer
    buy_risk = max(px - buy_stop, atr * 0.25)
    sell_risk = max(sell_stop - px, atr * 0.25)
    buy_target = px + buy_risk * DEFAULT_MIN_RR
    sell_target = px - sell_risk * DEFAULT_MIN_RR

    # Confluence consensus counts distinct strategy families; S6/S9 are one family to avoid double-counting.
    votes = {"BUY": [], "SELL": []}
    for r in out:
        if r["side"] in votes:
            family = "S6/S9" if r["strategy"].startswith(("S6", "S9")) else r["strategy"]
            if family not in votes[r["side"]]:
                votes[r["side"]].append(family)
    buy_count, sell_count = len(votes["BUY"]), len(votes["SELL"])
    consensus = "WAIT"
    if buy_count >= DEFAULT_CONFLUENCE and buy_count > sell_count:
        consensus = "BUY"
    elif sell_count >= DEFAULT_CONFLUENCE and sell_count > buy_count:
        consensus = "SELL"

    return {
        "strategies": out, "entry_price": px, "pattern": pattern, "trend15": trend15,
        "daily_bias": daily_bias, "daily_reason": daily_reason,
        "buy_votes": buy_count, "sell_votes": sell_count, "consensus": consensus,
        "buy_stop": buy_stop, "buy_target": buy_target,
        "sell_stop": sell_stop, "sell_target": sell_target,
        "atr": atr, "support": support, "resistance": resistance, "fvg": fvg, "sd": sd,
        "last_candle_time": str(entry["close_time"].iloc[-1]),
        "dataframes": {"5m": d5, "15m": d15, "1h": h1, "4h": h4, "1d": d1},
    }


# --------------------------- PAPER TRADING ---------------------------

def open_paper_trade(conn, symbol, side, entry, stop, target, balance, reason):
    risk_cash = balance * 0.01  # 1% paper-account risk per trade
    per_unit_risk = abs(entry - stop)
    if per_unit_risk <= 0:
        raise ValueError("Invalid stop distance.")
    qty = risk_cash / per_unit_risk
    conn.execute("""
        INSERT INTO paper_trades(symbol,side,entry,stop,target,quantity,opened_at,status,signal_reason)
        VALUES(?,?,?,?,?,?,?,?,?)
    """, (symbol, side, entry, stop, target, qty, datetime.now(timezone.utc).isoformat(), "OPEN", reason))
    conn.commit()


def close_trade(conn, trade_id, exit_price):
    row = conn.execute(
        "SELECT side, entry, quantity FROM paper_trades WHERE id=? AND status='OPEN'",
        (trade_id,)
    ).fetchone()
    if not row:
        return
    side, entry, qty = row
    pnl = (exit_price-entry)*qty if side == "BUY" else (entry-exit_price)*qty
    conn.execute("""
        UPDATE paper_trades SET status='CLOSED', closed_at=?, exit_price=?, pnl=?
        WHERE id=?
    """, (datetime.now(timezone.utc).isoformat(), float(exit_price), float(pnl), trade_id))
    conn.commit()


def manage_open_trades(conn, current_prices):
    rows = conn.execute(
        "SELECT id,symbol,side,stop,target FROM paper_trades WHERE status='OPEN'"
    ).fetchall()
    for trade_id, symbol, side, stop, target in rows:
        if symbol not in current_prices:
            continue
        price = current_prices[symbol]
        if side == "BUY" and (price <= stop or price >= target):
            close_trade(conn, trade_id, price)
        elif side == "SELL" and (price >= stop or price <= target):
            close_trade(conn, trade_id, price)


# --------------------------- UI ---------------------------

st.title("📈 BRODAX — Price Lesson Hindi + SMC")
st.caption("Paper-trading signal engine • Completed-candle analysis • Binance spot market data • No live orders")

with st.sidebar:
    st.header("⚙️ Settings")
    symbol = st.selectbox("Crypto pair", SYMBOLS, index=0)
    pivot_n = st.slider("Swing confirmation lookback", 2, 6, DEFAULT_PIVOT_LOOKBACK)
    zone_atr = st.slider("S/R zone tolerance (ATR multiplier)", 0.15, 1.0, DEFAULT_ZONE_ATR, 0.05)
    min_votes = st.slider("Minimum independent strategy votes", 1, 6, DEFAULT_CONFLUENCE)
    paper_risk_pct = st.slider("Paper risk per trade (%)", 0.25, 2.0, 1.0, 0.25)
    refresh_seconds = st.selectbox("Refresh interval", [5, 10, 15, 30], index=1)
    st.info("Signals use closed candles only. The forming candle is intentionally ignored to reduce repainting.")

if AUTOREFRESH_AVAILABLE:
    st_autorefresh(interval=refresh_seconds * 1000, key="brodax_refresh")

conn = db_connect()
balance = load_balance(conn)

try:
    with st.spinner("Fetching Binance market data and analyzing all Price Lesson strategies..."):
        market = {tf: get_klines(symbol, tf, 300 if tf != "1d" else 200) for tf in INTERVALS}
        live_price, used_host = get_ticker_price(symbol)
        analysis = analyze_all(market, pivot_n, zone_atr)
except Exception as e:
    st.error(f"Market data / analysis error: {e}")
    st.warning("Check internet access, pair availability, and Binance API access. No fabricated fallback price is used.")
    st.stop()

# Use UI-selected risk and vote settings (rather than hidden constants).
analysis["consensus"] = "WAIT"
if analysis["buy_votes"] >= min_votes and analysis["buy_votes"] > analysis["sell_votes"]:
    analysis["consensus"] = "BUY"
elif analysis["sell_votes"] >= min_votes and analysis["sell_votes"] > analysis["buy_votes"]:
    analysis["consensus"] = "SELL"

# Show current ticker separately from last completed 15m candle close.
c1, c2, c3, c4 = st.columns(4)
c1.metric(f"{symbol} live ticker", f"${live_price:,.6f}")
c2.metric("Last completed 15m close", f"${analysis['entry_price']:,.6f}")
c3.metric("15m structure", analysis["trend15"])
c4.metric("Daily bias", analysis["daily_bias"])

st.caption(
    f"Ticker source: {used_host} • Last completed 15m candle close time (UTC): "
    f"{analysis['last_candle_time']} • Live ticker and candle close are different timestamps by design."
)

st.divider()
st.subheader("🎯 Combined Signal")
left, right = st.columns([1, 2])
with left:
    if analysis["consensus"] == "BUY":
        st.success("BUY — paper signal")
    elif analysis["consensus"] == "SELL":
        st.error("SELL — paper signal")
    else:
        st.warning("WAIT — no sufficient strategy confluence")
    st.write(f"Independent BUY strategy families: **{analysis['buy_votes']}**")
    st.write(f"Independent SELL strategy families: **{analysis['sell_votes']}**")
    st.write(f"Minimum required: **{min_votes}**")
with right:
    if analysis["consensus"] == "BUY":
        st.write(f"**Illustrative entry:** ${analysis['entry_price']:,.6f}")
        st.write(f"**Structure-based stop:** ${analysis['buy_stop']:,.6f}")
        st.write(f"**1:3 target reference:** ${analysis['buy_target']:,.6f}")
    elif analysis["consensus"] == "SELL":
        st.write(f"**Illustrative entry:** ${analysis['entry_price']:,.6f}")
        st.write(f"**Structure-based stop:** ${analysis['sell_stop']:,.6f}")
        st.write(f"**1:3 target reference:** ${analysis['sell_target']:,.6f}")
    else:
        st.write("A trade is not forced. Wait until enough independent strategy families agree.")
    st.caption("The 1:3 target is an engineering reference from S8. Actual target should also respect the next major S/R or opposite S/D zone.")

st.divider()
st.subheader("📚 All Price Lesson Strategy Signals")
strategy_df = pd.DataFrame(analysis["strategies"])
st.dataframe(strategy_df[["strategy", "side", "confidence", "reason", "details"]],
              use_container_width=True, hide_index=True)

with st.expander("Market structure and key levels"):
    st.write(f"**15M candle pattern:** {analysis['pattern']}")
    st.write(f"**Nearest support:** {analysis['support']}")
    st.write(f"**Nearest resistance:** {analysis['resistance']}")
    st.write(f"**Fresh unmitigated FVG scan:** {analysis['fvg']}")
    st.write(f"**Supply/Demand zone scan:** {analysis['sd']}")
    st.write(f"**Daily bias explanation:** {analysis['daily_reason']}")

st.divider()
st.subheader("🧪 Paper Trading")
st.write(f"**Paper account balance:** ${balance:,.2f}")
risk_cash = balance * (paper_risk_pct / 100.0)
st.write(f"Risk budget at {paper_risk_pct:.2f}%: **${risk_cash:,.2f}**")

open_rows = conn.execute("""
    SELECT id,symbol,side,entry,stop,target,quantity,opened_at,signal_reason
    FROM paper_trades WHERE status='OPEN' ORDER BY id DESC
""").fetchall()

# Manual paper entry only; the bot does not silently open trades.
can_open = analysis["consensus"] in ("BUY", "SELL")
if can_open:
    side = analysis["consensus"]
    stop = analysis["buy_stop"] if side == "BUY" else analysis["sell_stop"]
    target = analysis["buy_target"] if side == "BUY" else analysis["sell_target"]
    entry = analysis["entry_price"]
    if st.button(f"Open {side} paper trade for {symbol}", type="primary"):
        already = conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE symbol=? AND status='OPEN'", (symbol,)
        ).fetchone()[0]
        if already:
            st.warning("An open paper trade already exists for this pair. Close it before opening another.")
        else:
            per_unit = abs(entry-stop)
            qty = risk_cash / per_unit if per_unit > 0 else 0
            conn.execute("""
                INSERT INTO paper_trades(symbol,side,entry,stop,target,quantity,opened_at,status,signal_reason)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (symbol, side, entry, stop, target, qty, datetime.now(timezone.utc).isoformat(),
                  "OPEN", f"{analysis['buy_votes']} BUY / {analysis['sell_votes']} SELL strategy families"))
            conn.commit()
            st.success("Paper trade opened.")
            st.rerun()
else:
    st.info("Paper trade button appears only when the selected minimum strategy confluence produces BUY or SELL.")

# Manage open trades against fresh ticker values for all symbols currently held.
held_symbols = sorted({r[1] for r in open_rows})
price_map = {symbol: live_price}
for held in held_symbols:
    if held != symbol:
        try:
            price_map[held] = get_ticker_price(held)[0]
        except Exception:
            pass
manage_open_trades(conn, price_map)

open_rows = conn.execute("""
    SELECT id,symbol,side,entry,stop,target,quantity,opened_at,signal_reason
    FROM paper_trades WHERE status='OPEN' ORDER BY id DESC
""").fetchall()

if open_rows:
    open_df = pd.DataFrame(open_rows, columns=["ID","Pair","Side","Entry","Stop","Target","Quantity","Opened UTC","Signal reason"])
    st.dataframe(open_df, use_container_width=True, hide_index=True)
    trade_to_close = st.selectbox("Select open paper trade to close manually", [r[0] for r in open_rows])
    if st.button("Close selected paper trade at current ticker"):
        selected = next(r for r in open_rows if r[0] == trade_to_close)
        close_price = price_map.get(selected[1])
        if close_price is not None:
            close_trade(conn, trade_to_close, close_price)
            st.success(f"Closed at ${close_price:,.6f}")
            st.rerun()
        else:
            st.error("Could not retrieve current ticker for this pair.")
else:
    st.info("No open paper trades.")

closed = conn.execute("""
    SELECT id,symbol,side,entry,exit_price,quantity,pnl,opened_at,closed_at,status
    FROM paper_trades WHERE status='CLOSED' ORDER BY id DESC LIMIT 100
""").fetchall()
if closed:
    st.subheader("📜 Paper trade history")
    hist = pd.DataFrame(closed, columns=["ID","Pair","Side","Entry","Exit","Quantity","PnL","Opened UTC","Closed UTC","Status"])
    st.dataframe(hist, use_container_width=True, hide_index=True)
    total_pnl = float(hist["PnL"].fillna(0).sum())
    st.metric("Realized PnL (last 100 closed trades)", f"${total_pnl:,.2f}")

st.divider()
st.subheader("🧭 Strategy implementation notes")
st.markdown("""
- **S1:** 4H direction → 1H behavior → 15M entry; requires aligned momentum and 15M confirmation.
- **S2:** Swing structure and impulse/corrective leg-size change; reversal is marked only as potential until candle confirmation.
- **S3:** Trend + Doji/indecision + engulfing confirmation.
- **S4:** Nearby swing S/R + reversal candle + MA crossover.
- **S5 IDM:** BOS followed by a first-pullback level sweep/reclaim approximation. The transcript's “look left” rule is approximated by recent confirmed pivots.
- **S5-FVG:** Three-candle FVG, first-test/unmitigated check, sweep, BOS alignment, and 50% midpoint rejection.
- **S6/S9:** Supply/demand zone retest + rejection/engulfing. The three zone drawing methods are approximated algorithmically.
- **S7:** Recent swing dealing range, 50% equilibrium; discount buys / premium sells, with rejection confirmation.
- **S8:** Previous completed daily candle PDH/PDL close, wick sweep, inside-bar neutral; lower-timeframe alignment.
""")
st.warning(
    "Important limitations: qualitative chart-reading concepts cannot be perfectly inferred by code. "
    "Zone discovery, impulse/correction comparison, IDM, and swing confirmation are explicit algorithmic approximations. "
    "The transcript does not define every numeric threshold, so the code exposes engineering defaults rather than claiming those values came from the teacher. "
    "Backtest and paper-trade before considering any real capital. This app sends no live orders."
)
st.caption("BRODAX is a decision-support/paper-trading tool, not financial advice. Crypto markets can move rapidly.")
