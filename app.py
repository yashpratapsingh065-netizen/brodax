import os
import sqlite3
import random
from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import requests

# Indian Standard Time (IST) Offset: UTC + 5:30
IST = timezone(timedelta(hours=5, minutes=30))

st.set_page_config(
    page_title="BIGMAN2 MT5-Style Pro Bot",
    page_icon="⚡",
    layout="wide"
)

DB_PATH = os.getenv("BIGMAN_DB_PATH", "bigman_mt5_pro_v3.db")

SYMBOLS = {
    "XAU/USD (Gold)": {"tv": "OANDA:XAUUSD", "td": "XAU/USD", "yf": "GC=F", "base": 4007.33, "lot": 0.01, "is_gold": True},
    "EUR/USD": {"tv": "FX:EURUSD", "td": "EUR/USD", "yf": "EURUSD=X", "base": 1.0850, "lot": 0.01, "is_gold": False},
    "GBP/USD": {"tv": "FX:GBPUSD", "td": "GBP/USD", "yf": "GBPUSD=X", "base": 1.2720, "lot": 0.01, "is_gold": False},
    "Bitcoin (BTC/USD)": {"tv": "BINANCE:BTCUSDT", "td": "BTC/USD", "yf": "BTC-USD", "base": 80090.0, "lot": 0.001, "is_gold": True}
}

INTERVALS = {
    "5 min": {"td": "5min", "yf": "5m", "tv_int": "5", "minutes": 5, "freq": "5min"},
    "15 min": {"td": "15min", "yf": "15m", "tv_int": "15", "minutes": 15, "freq": "15min"},
    "1 hour": {"td": "1h", "yf": "60m", "tv_int": "60", "minutes": 60, "freq": "1h"},
    "4 hours": {"td": "4h", "yf": "1h", "tv_int": "240", "minutes": 240, "freq": "4h"},
    "1 day": {"td": "1day", "yf": "1d", "tv_int": "D", "minutes": 1440, "freq": "1D"}
}

def connect_db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS mt5_open_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at_ist TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            entry_time_utc TIMESTAMP NOT NULL,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            lot_size REAL DEFAULT 0.01,
            bias TEXT NOT NULL,
            strategy_votes TEXT,
            candle_timestamp TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS mt5_trade_history (
            id INTEGER PRIMARY KEY,
            created_at_ist TEXT NOT NULL,
            closed_at_ist TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            entry_price REAL,
            exit_price REAL,
            stop_loss REAL,
            take_profit REAL,
            lot_size REAL,
            bias TEXT NOT NULL,
            exit_reason TEXT NOT NULL,
            pnl REAL NOT NULL,
            strategy_votes TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS mt5_account_balance (
            id INTEGER PRIMARY KEY,
            balance REAL NOT NULL
        )
    """)
    cursor = con.cursor()
    cursor.execute("SELECT balance FROM mt5_account_balance WHERE id = 1")
    row = cursor.fetchone()
    if not row:
        con.execute("INSERT INTO mt5_account_balance (id, balance) VALUES (1, 100000.0)")
    con.commit()
    return con

def get_market_data(symbol_key, interval_key, rows=200):
    info = SYMBOLS[symbol_key]
    cfg = INTERVALS[interval_key]
    
    # Try fetching real market data via yfinance
    try:
        import yfinance as yf
        ticker = yf.Ticker(info["yf"])
        df = ticker.history(period="5d", interval=cfg["yf"])
        if not df.empty and len(df) > 10:
            df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close"})
            df["datetime_ist"] = df.index.tz_convert(IST)
            return df[["open", "high", "low", "close", "datetime_ist"]].tail(rows), "Yahoo Finance Live Feed 🟢"
    except Exception:
        pass

    # Fallback to simulated live candles maintaining continuity with MT5 feel
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=rows, freq=cfg["freq"])
    base_p = info["base"]
    df = pd.DataFrame(index=dates)
    
    prices = [base_p]
    for _ in range(rows - 1):
        change = random.uniform(-0.002, 0.002) * prices[-1]
        prices.append(prices[-1] + change)
        
    df['close'] = prices
    df['open'] = df['close'].shift(1).fillna(base_p)
    df['high'] = df[['open', 'close']].max(axis=1) * 1.001
    df['low'] = df[['open', 'close']].min(axis=1) * 0.999
    df["datetime_ist"] = df.index.tz_convert(IST)
    return df[["open", "high", "low", "close", "datetime_ist"]], "MT5 Live Simulation Ticker ⚡"

def evaluate_price_lesson_strategies(df, current_price):
    if len(df) < 15:
        return {
            "bias": "BUY",
            "votes": "Initializing market structure...",
            "bullish": 1,
            "bearish": 0,
            "entry": current_price,
            "sl": current_price * 0.99,
            "tp": current_price * 1.02
        }
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    p_prev = df.iloc[-3]
    
    body = abs(last.close - last.open)
    rng = max(last.high - last.low, 1e-12)
    upper_wick = last.high - max(last.open, last.close)
    lower_wick = min(last.open, last.close) - last.low
    
    swing_high = float(df.high.tail(30).max())
    swing_low = float(df.low.tail(30).min())
    eq_50 = (swing_high + swing_low) / 2.0
    
    bullish_votes = []
    bearish_votes = []

    # 1. Premium & Discount Zone (50% Equilibrium)
    if current_price < eq_50:
        bullish_votes.append("Discount Zone (< 50% Eq - Buy Bias)")
    else:
        bearish_votes.append("Premium Zone (> 50% Eq - Sell Bias)")

    # 2. Fair Value Gap (FVG) Confluence
    if last.low > p_prev.high and prev.close > prev.open:
        bullish_votes.append("Bullish FVG Imbalance")
    elif last.high < p_prev.low and prev.close < prev.open:
        bearish_votes.append("Bearish FVG Imbalance")

    # 3. Supply & Demand Wick Rejections
    if lower_wick >= body * 1.5 and last.close > last.open:
        bullish_votes.append("Demand Zone Wick Rejection")
    elif upper_wick >= body * 1.5 and last.close < last.open:
        bearish_votes.append("Supply Zone Wick Rejection")

    # 4. Engulfing Patterns
    if last.close > last.open and prev.close < prev.open and last.open <= prev.close and last.close >= prev.open:
        bullish_votes.append("Bullish Engulfing Pattern")
    elif last.close < last.open and prev.close > prev.open and last.open >= prev.close and last.open <= prev.open:
        bearish_votes.append("Bearish Engulfing Pattern")

    # 5. Momentum Check
    if current_price >= last.open:
        bullish_votes.append("Bullish Momentum")
    else:
        bearish_votes.append("Bearish Momentum")

    b_score = len(bullish_votes)
    s_score = len(bearish_votes)

    bias = "BUY" if b_score >= s_score else "SELL"

    # Dynamic SL & TP buffers based on asset price scale
    is_large = current_price > 500
    sl_buf = rng * 1.5 if not is_large else current_price * 0.002
    tp_buf = rng * 3.0 if not is_large else current_price * 0.004

    if bias == "BUY":
        sl = current_price - sl_buf
        tp = current_price + tp_buf
    else:
        sl = current_price + sl_buf
        tp = current_price - tp_buf

    votes_str = f"Bullish [{b_score}]: {', '.join(bullish_votes)} | Bearish [{s_score}]: {', '.join(bearish_votes)}"

    decimals = 2 if current_price > 10 else 5
    return {
        "bias": bias,
        "votes": votes_str,
        "bullish": b_score,
        "bearish": s_score,
        "entry": round(current_price, decimals),
        "sl": round(sl, decimals),
        "tp": round(tp, decimals)
    }

def get_current_balance(con):
    cursor = con.cursor()
    cursor.execute("SELECT balance FROM mt5_account_balance WHERE id = 1")
    row = cursor.fetchone()
    return float(row[0]) if row else 100000.0

def update_balance(con, delta):
    current = get_current_balance(con)
    new_bal = current + delta
    con.execute("UPDATE mt5_account_balance SET balance = ? WHERE id = 1", (new_bal,))
    con.commit()
    return new_bal

def execute_mt5_trade(con, symbol, timeframe, result, lot_size, candle_ts):
    cursor = con.cursor()
    cursor.execute("SELECT id FROM mt5_open_trades WHERE symbol = ?", (symbol,))
    if cursor.fetchone():
        return False, "Active trade already exists for this symbol!"

    created_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
    entry_time_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    con.execute(
        "INSERT INTO mt5_open_trades (created_at_ist, symbol, timeframe, entry_time_utc, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes, candle_timestamp) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (created_at_ist, symbol, timeframe, entry_time_utc, result["entry"], result["sl"], result["tp"], lot_size, result["bias"], result["votes"], str(candle_ts))
    )
    con.commit()
    return True, "Trade executed successfully!"

def close_trade(con, trade_id, current_price, is_gold=True, reason="Manual Close 👤"):
    cursor = con.cursor()
    cursor.execute("SELECT created_at_ist, symbol, timeframe, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes FROM mt5_open_trades WHERE id = ?", (trade_id,))
    row = cursor.fetchone()
    if not row:
        return 0.0
    
    created_at_ist, symbol, timeframe, entry_price, sl, tp, lot_size, bias, strategy_votes = row
    
    diff = (current_price - entry_price) if bias == "BUY" else (entry_price - current_price)
    multiplier = 100 if is_gold else 10000
    pnl = round(diff * lot_size * multiplier, 2)
    closed_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
    
    con.execute(
        "INSERT INTO mt5_trade_history (id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, exit_price, stop_loss, take_profit, lot_size, bias, exit_reason, pnl, strategy_votes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, round(current_price, 2 if is_gold else 5), sl, tp, lot_size, bias, reason, pnl, strategy_votes)
    )
    con.execute("DELETE FROM mt5_open_trades WHERE id = ?", (trade_id,))
    con.commit()
    update_balance(con, pnl)
    return pnl

def check_auto_exits(con, current_price, timeframe_label, is_gold=True):
    cursor = con.cursor()
    cursor.execute("SELECT id, symbol, timeframe, bias, entry_price, stop_loss, take_profit, lot_size, entry_time_utc FROM mt5_open_trades")
    open_trades = cursor.fetchall()
    
    tf_mins = INTERVALS[timeframe_label]["minutes"]
    now_utc = datetime.now(timezone.utc)
    
    for tid, symbol, tf, bias, entry, sl, tp, lot, entry_time_str in open_trades:
        closed = False
        exit_p = current_price
        reason = "Manual Close 👤"
        
        if bias == "BUY":
            if current_price >= tp:
                exit_p, reason, closed = tp, "TP Hitter 🎯", True
            elif current_price <= sl:
                exit_p, reason, closed = sl, "SL Hitter 🛑", True
        elif bias == "SELL":
            if current_price <= tp:
                exit_p, reason, closed = tp, "TP Hitter 🎯", True
            elif current_price >= sl:
                exit_p, reason, closed = sl, "SL Hitter 🛑", True
                
        if not closed and tf == timeframe_label:
            try:
                entry_dt = datetime.strptime(entry_time_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                if (now_utc - entry_dt).total_seconds() / 60.0 >= tf_mins:
                    exit_p, reason, closed = current_price, f"15-Min Timer Exit ⏱️", True
            except Exception:
                pass
                
        if closed:
            close_trade(con, tid, exit_p, is_gold=is_gold, reason=reason)

# --- UI INTERFACE ---
st.title("⚡ BIGMAN2 — MT5 Pro Synchronized Bot")
st.caption("Price Lesson Strategies • S&D / FVG / Equilibrium • Live TradingView Sync")

con = connect_db()

with st.sidebar:
    st.header("⚙️ MT5 Control Panel")
    symbol = st.selectbox("Select Asset", list(SYMBOLS.keys()), index=0)
    timeframe = st.selectbox("Timeframe", list(INTERVALS.keys()), index=1)
    
    default_lot = SYMBOLS[symbol]["lot"]
    lot_input = st.number_input("Lot Size", min_value=0.001, max_value=10.0, value=default_lot, step=0.01)
    
    if st.button("🔄 Refresh Live Feed", use_container_width=True):
        st.toast("Feed refreshed successfully!", icon="🔄")
        st.rerun()

    st.markdown("---")
    st.subheader("⚡ Execution Mode")
    auto_mode = st.toggle("Enable Auto-Pilot Bot", value=False)
    
    if st.button("➕ Execute Trade Now", use_container_width=True):
        df_temp, _ = get_market_data(symbol, timeframe)
        if not df_temp.empty:
            live_p = float(df_temp.close.iloc[-1])
            res_temp = evaluate_price_lesson_strategies(df_temp, live_p)
            candle_ts = df_temp.index[-1]
            success, msg = execute_mt5_trade(con, symbol, timeframe, res_temp, lot_size=lot_input, candle_ts=candle_ts)
            if success:
                st.sidebar.success("Trade executed!")
                st.rerun()
            else:
                st.sidebar.warning(msg)

    st.markdown("---")
    if st.button("🗑️ Clear All Open Trades & History", use_container_width=True):
        con.execute("DELETE FROM mt5_open_trades")
        con.execute("DELETE FROM mt5_trade_history")
        con.commit()
        st.sidebar.success("Database cleared!")
        st.rerun()

    if st.button("🔄 Reset Balance ($100,000)", use_container_width=True):
        con.execute("UPDATE mt5_account_balance SET balance = 100000.0 WHERE id = 1")
        con.commit()
        st.rerun()

df, data_source = get_market_data(symbol, timeframe)
if not df.empty:
    current_price = float(df.close.iloc[-1])
    result = evaluate_price_lesson_strategies(df, current_price)
    is_gold = SYMBOLS[symbol]["is_gold"]
    candle_ts = df.index[-1]
    
    check_auto_exits(con, current_price, timeframe, is_gold=is_gold)
    
    if auto_mode:
        execute_mt5_trade(con, symbol, timeframe, result, lot_size=lot_input, candle_ts=candle_ts)

    balance = get_current_balance(con)
    st.markdown(f"### 💵 MT5 Balance: **${balance:,.2f} USD**")
    
    c1, c2, c3 = st.columns(3)
    c1.metric("Live Price", f"{current_price:,.2f}" if current_price > 10 else f"{current_price:,.5f}")
    c2.metric("Bot Signal Bias", result["bias"])
    c3.metric("Confluence Score", f"Bull: {result['bullish']} | Bear: {result['bearish']}")

st.info(f"🟢 **Strategy Status:** Active (S&D, FVG, 50% Eq) | **Feed Source:** {data_source}")

st.divider()

left, right = st.columns([1.8, 1])

with left:
    st.subheader(f"📈 Live TradingView Chart ({symbol} - {timeframe})")
    tv_sym = SYMBOLS[symbol]["tv"]
    tv_int = INTERVALS[timeframe]["tv_int"]
    
    chart_html = f"""
    <div style="height:480px;width:100%">
      <iframe src="https://s.tradingview.com/widgetembed/?symbol={tv_sym}&interval={tv_int}&hidesidetoolbar=0&hidetoptoolbar=0&symboledit=1&saveimage=1&toolbarbg=f1f3f6&studies=[]&theme=dark&style=1&timezone=Asia%2FKolkata&locale=en" style="width:100%;height:480px;border:none;" scrolling="no"></iframe>
    </div>
    """
    components.html(chart_html, height=495, scrolling=False)

with right:
    st.subheader("💡 Price Lesson Video Confluence")
    if not df.empty:
        st.write(f"**Market Analysis:** {result['votes']}")
        fmt = ".2f" if current_price > 10 else ".5f"
        st.write(f"**Recommended SL:** `{result['sl']:{fmt}}` | **TP:** `{result['tp']:{fmt}}`")
        st.markdown("---")
        view = df.tail(4).copy()
        view.index = view.index.strftime('%d %b %I:%M %p')
        st.dataframe(view, use_container_width=True)

st.divider()
st.subheader("📋 MT5 Active Open Positions")
open_df = pd.read_sql_query("SELECT * FROM mt5_open_trades ORDER BY id DESC", con)

if open_df.empty:
    st.info("Abhi MT5 par koi open position nahi hai.")
else:
    for _, row in open_df.iterrows():
        fmt = ".2f" if row['entry_price'] > 10 else ".5f"
        with st.expander(f"📌 Position #{row['id']} | {row['symbol']} ({row['bias']} - {row['lot_size']} Lot)"):
            c1, c2 = st.columns(2)
            c1.markdown(f"🕒 **Time:** `{row['created_at_ist']}`\n📍 **Entry:** `${row['entry_price']:{fmt}}`\n🛑 **SL:** `${row['stop_loss']:{fmt}}`")
            c2.markdown(f"🎯 **TP:** `${row['take_profit']:{fmt}}`\n⚡ **Status:** `OPEN`\n📊 **Votes:** `{row['strategy_votes']}`")
            if st.button(f"Close Position #{row['id']}", key=f"close_{row['id']}"):
                pnl = close_trade(con, row['id'], current_price, is_gold=SYMBOLS[symbol]["is_gold"], reason="Manual Close 👤")
                st.toast(f"Position closed! PnL: ${pnl:+.2f}")
                st.rerun()

st.divider()
st.subheader("📜 MT5 Permanent Trade History")
history_df = pd.read_sql_query("SELECT * FROM mt5_trade_history ORDER BY id DESC LIMIT 50", con)

if history_df.empty:
    st.info("Abhi tak trade history mein koi record nahi hai.")
else:
    for _, row in history_df.iterrows():
        color = "🟢" if row['pnl'] >= 0 else "🔴"
        fmt = ".2f" if row['entry_price'] > 10 else ".5f"
        with st.expander(f"{color} ID #{row['id']} | {row['symbol']} ({row['bias']}) | PnL: ${row['pnl']:+.2f}"):
            c1, c2 = st.columns(2)
            c1.markdown(f"🕒 **Entry:** `{row['created_at_ist']}`\n🏁 **Exit:** `{row['closed_at_ist']}`\n📍 **Entry P:** `${row['entry_price']:{fmt}}`\n🏁 **Exit P:** `${row['exit_price']:{fmt}}`")
            c2.markdown(f"🛑 **SL:** `${row['stop_loss']:{fmt}}`\n🎯 **TP:** `${row['take_profit']:{fmt}}`\n🏷️ **Reason:** `{row['exit_reason']}`\n💰 **PnL:** `${row['pnl']:+.2f} USD`")
            st.markdown(f"📊 **Votes:** `{row['strategy_votes']}`")

con.close()
