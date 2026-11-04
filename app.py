import os
import sqlite3
from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import requests

# ==================== CONFIGURATION ====================
IST = timezone(timedelta(hours=5, minutes=30))

st.set_page_config(
    page_title="BRODAX — Synchronized Crypto Master",
    page_icon="⚡",
    layout="wide"
)

# --- PROFESSIONAL STYLING ---
st.markdown("""
    <style>
    .main { background-color: #0e1117; color: #ffffff; }
    .stMetric { background-color: #161b22; padding: 15px; border-radius: 10px; border: 1px solid #30363d; }
    .stAlert { background-color: #1f242d; border: 1px solid #30363d; color: #c9d1d9; }
    h1, h2, h3 { color: #f0f6fc !important; font-family: 'Inter', sans-serif; }
    </style>
""", unsafe_allow_html=True)

DB_PATH = os.getenv("BRODAX_DB_PATH", "brodax_crypto_synced.db")

CRYPTO_SYMBOLS = {
    "Bitcoin (BTC/USD)": {"binance_symbol": "BTCUSDT", "tv_symbol": "BINANCE:BTCUSDT", "lot": 0.01, "base_p": 91500.0},
    "Ethereum (ETH/USD)": {"binance_symbol": "ETHUSDT", "tv_symbol": "BINANCE:ETHUSDT", "lot": 0.1, "base_p": 3350.0},
    "Solana (SOL/USD)": {"binance_symbol": "SOLUSDT", "tv_symbol": "BINANCE:SOLUSDT", "lot": 1.0, "base_p": 195.0},
    "Ripple (XRP/USD)": {"binance_symbol": "XRPUSDT", "tv_symbol": "BINANCE:XRPUSDT", "lot": 10.0, "base_p": 2.45}
}

INTERVALS = {
    "15 min": {"tv_int": "15", "binance_int": "15m", "pd_freq": "15min"},
    "1 hour": {"tv_int": "60", "binance_int": "1h", "pd_freq": "1h"},
    "4 hours": {"tv_int": "240", "binance_int": "4h", "pd_freq": "4h"},
    "1 day": {"tv_int": "D", "binance_int": "1d", "pd_freq": "1D"}
}

def connect_db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS brodax_open_trades_sync (
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
        CREATE TABLE IF NOT EXISTS brodax_history_sync (
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
            pnl_usd REAL NOT NULL,
            strategy_votes TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS brodax_account_sync (
            id INTEGER PRIMARY KEY,
            balance_usd REAL NOT NULL
        )
    """)
    cursor = con.cursor()
    cursor.execute("SELECT balance_usd FROM brodax_account_sync WHERE id = 1")
    if not cursor.fetchone():
        con.execute("INSERT INTO brodax_account_sync (id, balance_usd) VALUES (1, 1000.0)")
    con.commit()
    return con

@st.cache_data(ttl=2)
def get_live_binance_price(binance_symbol):
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={binance_symbol}"
        resp = requests.get(url, timeout=2)
        data = resp.json()
        if "price" in data:
            return float(data["price"]), "Binance Live Ticker 🟢"
    except Exception:
        pass
    return None, None

def get_market_data(symbol_key, interval_key, limit=100):
    info = CRYPTO_SYMBOLS[symbol_key]
    cfg = INTERVALS[interval_key]
    rt_price, feed_source = get_live_binance_price(info["binance_symbol"])
    
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={info['binance_symbol']}&interval={cfg['binance_int']}&limit={limit}"
        resp = requests.get(url, timeout=4)
        data = resp.json()
        
        if isinstance(data, list) and len(data) > 0:
            candles = [{
                "datetime": pd.to_datetime(row[0], unit='ms'),
                "open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4])
            } for row in data]
            df = pd.DataFrame(candles).set_index("datetime")
            
            if rt_price is not None:
                df.iloc[-1, df.columns.get_loc("close")] = rt_price
                df.iloc[-1, df.columns.get_loc("high")] = max(df.iloc[-1]["high"], rt_price)
                df.iloc[-1, df.columns.get_loc("low")] = min(df.iloc[-1]["low"], rt_price)
                current_price = rt_price
            else:
                current_price = float(df.iloc[-1]["close"])
                feed_source = "Binance Klines Feed 🟢"

            df['datetime_ist'] = df.index.tz_localize('UTC').tz_convert(IST) if df.index.tz is None else df.index.tz_convert(IST)
            return df[['open', 'high', 'low', 'close', 'datetime_ist']], current_price, feed_source
    except Exception:
        pass

    fallback_p = rt_price if rt_price else info["base_p"]
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=limit, freq=cfg["pd_freq"])
    df = pd.DataFrame(index=dates)
    df['open'] = df['high'] = df['low'] = df['close'] = float(fallback_p)
    df['datetime_ist'] = df.index.tz_convert(IST)
    return df[['open', 'high', 'low', 'close', 'datetime_ist']], float(fallback_p), "Static Fallback 🔴"

def evaluate_daily_bias_strategy(symbol_key, current_price):
    df_daily, _, _ = get_market_data(symbol_key, "1 day", limit=10)
    if df_daily.empty or len(df_daily) < 3:
        return "BUY", "Initializing Daily Bias...", 1, 1

    prev = df_daily.iloc[-2]
    p_prev = df_daily.iloc[-3]
    pd_high = prev['high']
    pd_low = prev['low']
    
    is_inside_bar = prev['high'] <= p_prev['high'] and prev['low'] >= p_prev['low']
    bullish_votes, bearish_votes = [], []
    
    if is_inside_bar:
        if current_price > pd_high: bullish_votes.append("Inside Bar Breakout (Bullish)")
        elif current_price < pd_low: bearish_votes.append("Inside Bar Breakdown (Bearish)")
        else: bullish_votes.append("Inside Bar Consolidation (Neutral)")
    else:
        if prev['close'] > pd_high: bullish_votes.append("Daily Close Above Prev High")
        elif prev['close'] < pd_low: bearish_votes.append("Daily Close Below Prev Low")
        else:
            if prev['low'] < p_prev['low'] and prev['close'] > prev['open']: bullish_votes.append("Prev Day Low Liquidity Sweep")
            elif prev['high'] > p_prev['high'] and prev['close'] < prev['open']: bearish_votes.append("Prev Day High Liquidity Sweep")
            else:
                if current_price >= ((pd_high + pd_low) / 2): bearish_votes.append("Premium Zone Equilibrium")
                else: bullish_votes.append("Discount Zone Equilibrium")

    b_score = len(bullish_votes) if bullish_votes else 1
    s_score = len(bearish_votes) if bearish_votes else 1
    
    bias = "BUY" if b_score >= s_score else "SELL"
    votes_str = f"Bullish: {', '.join(bullish_votes) if bullish_votes else 'None'} | Bearish: {', '.join(bearish_votes) if bearish_votes else 'None'}"
    return bias, votes_str, b_score, s_score

def evaluate_brodax_strategy(df, current_price, symbol_key):
    current_price = float(current_price)
    if df.empty or len(df) < 20:
        return {
            "bias": "BUY", "votes": "Initializing...", "bullish": 1, "bearish": 0,
            "analysis": "Gathering price action...", "entry": current_price,
            "sl": current_price * 0.99, "tp": current_price * 1.02, "candle_ts": "init"
        }
    
    daily_bias, daily_votes, b_daily, s_daily = evaluate_daily_bias_strategy(symbol_key, current_price)
    
    resistance_level = float(df['high'].max())
    support_level = float(df['low'].min())
    equilibrium = (resistance_level + support_level) / 2.0
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    p_prev = df.iloc[-3]
    candle_ts = str(df.index[-1])
    
    bullish_confluences = [f"Daily Bias: {daily_bias}"] if daily_bias == "BUY" else []
    bearish_confluences = [f"Daily Bias: {daily_bias}"] if daily_bias == "SELL" else []
    
    if current_price <= equilibrium: bullish_confluences.append("Discount Zone (< 0.5 Eq)")
    else: bearish_confluences.append("Premium Zone (> 0.5 Eq)")
        
    if last.low > p_prev.high and prev.close > prev.open: bullish_confluences.append("Bullish FVG")
    elif last.high < p_prev.low and prev.close < prev.open: bearish_confluences.append("Bearish FVG")

    body = abs(last['close'] - last['open'])
    if (min(last['open'], last['close']) - last['low']) >= body * 1.0 and last['close'] > last['open']:
        bullish_confluences.append("Demand Rejection Wick")
    elif (last['high'] - max(last['open'], last['close'])) >= body * 1.0 and last['close'] < last['open']:
        bearish_confluences.append("Supply Rejection Wick")
        
    b_score = len(bullish_confluences)
    s_score = len(bearish_confluences)
    
    bias = daily_bias if b_score == s_score else ("BUY" if b_score > s_score else "SELL")
    
    decimals = 2 if current_price > 10 else 4
    if bias == "BUY":
        sl = round(support_level * 0.992, decimals)
        tp = round(current_price + (current_price - sl) * 2.0, decimals)
        analysis = f"Daily Bias Aligned (BUY) | Support SL: ${support_level:,.2f}"
    else:
        sl = round(resistance_level * 1.008, decimals)
        tp = round(current_price - (sl - current_price) * 2.0, decimals)
        analysis = f"Daily Bias Aligned (SELL) | Resistance SL: ${resistance_level:,.2f}"
        
    votes_str = f"Bullish [{b_score}]: {', '.join(bullish_confluences)} | Bearish [{s_score}]: {', '.join(bearish_confluences)}"
    
    return {
        "bias": bias, "votes": votes_str, "bullish": b_score, "bearish": s_score,
        "analysis": analysis, "entry": round(current_price, decimals), "sl": sl, "tp": tp, "candle_ts": candle_ts
    }

def get_current_balance(con):
    cursor = con.cursor()
    cursor.execute("SELECT balance_usd FROM brodax_account_sync WHERE id = 1")
    row = cursor.fetchone()
    return float(row[0]) if row else 1000.0

def update_balance(con, delta):
    current = get_current_balance(con)
    new_bal = current + float(delta)
    con.execute("UPDATE brodax_account_sync SET balance_usd = ? WHERE id = 1", (new_bal,))
    con.commit()
    return new_bal

def execute_brodax_trade(con, symbol, timeframe, result, lot_size):
    cursor = con.cursor()
    candle_ts = str(result["candle_ts"])
    bias = result["bias"]
    
    cursor.execute("SELECT id FROM brodax_open_trades_sync WHERE symbol = ? AND candle_timestamp = ?", (symbol, candle_ts))
    if cursor.fetchone():
        return False, "Duplicate trade blocked for this candle!"

    cursor.execute("SELECT id FROM brodax_open_trades_sync WHERE symbol = ? AND bias = ?", (symbol, bias))
    if cursor.fetchone():
        return False, "Active trade with same bias exists!"

    created_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
    entry_time_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    con.execute(
        "INSERT INTO brodax_open_trades_sync (created_at_ist, symbol, timeframe, entry_time_utc, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes, candle_timestamp) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (created_at_ist, symbol, timeframe, entry_time_utc, float(result["entry"]), float(result["sl"]), float(result["tp"]), float(lot_size), str(bias), str(result["votes"]), candle_ts)
    )
    con.commit()
    return True, "Trade executed successfully!"

def close_single_trade(con, trade_id, current_price, reason="Manual Close 👤"):
    cursor = con.cursor()
    cursor.execute("SELECT created_at_ist, symbol, timeframe, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes FROM brodax_open_trades_sync WHERE id = ?", (trade_id,))
    row = cursor.fetchone()
    if not row:
        return 0.0
    
    created_at_ist, symbol, timeframe, entry_price, sl, tp, lot_size, bias, strategy_votes = row
    current_price, entry_price, lot_size = float(current_price), float(entry_price), float(lot_size)
    
    diff = (current_price - entry_price) if bias == "BUY" else (entry_price - current_price)
    pnl_usd = round(diff * lot_size, 2)
    closed_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
    
    con.execute(
        "INSERT INTO brodax_history_sync (id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, exit_price, stop_loss, take_profit, lot_size, bias, exit_reason, pnl_usd, strategy_votes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, round(current_price, 4), sl, tp, lot_size, bias, reason, pnl_usd, strategy_votes)
    )
    con.execute("DELETE FROM brodax_open_trades_sync WHERE id = ?", (trade_id,))
    con.commit()
    update_balance(con, pnl_usd)
    return pnl_usd

# --- UI INTERFACE ---
st.markdown("<h1 style='text-align: center;'>⚡ BRODAX — Synchronized Crypto Master</h1>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: #8b949e;'>Daily Bias Strategy • FVG & Equilibrium Confluence • Real-Time Binance Synchronized Feed</p>", unsafe_allow_html=True)
st.divider()

con = connect_db()

with st.sidebar:
    st.header("⚙️ Control Panel")
    symbol = st.selectbox("Select Asset", list(CRYPTO_SYMBOLS.keys()), index=0)
    timeframe = st.selectbox("Timeframe", list(INTERVALS.keys()), index=1)
    
    default_lot = CRYPTO_SYMBOLS[symbol]["lot"]
    lot_input = st.number_input("Trade Lot Size", min_value=0.001, max_value=50.0, value=default_lot, step=0.001)
    
    if st.button("🔄 Refresh Data", use_container_width=True):
        st.rerun()

    st.markdown("---")
    st.subheader("⚡ Execution Mode")
    auto_mode = st.toggle("Enable Auto-Pilot Execution", value=False)
    
    if st.button("➕ Open Brodax Trade", use_container_width=True):
        df_temp, live_p, _ = get_market_data(symbol, timeframe)
        res_temp = evaluate_brodax_strategy(df_temp, live_p, symbol)
        success, msg = execute_brodax_trade(con, symbol, timeframe, res_temp, lot_size=lot_input)
        if success:
            st.sidebar.success(msg)
            st.rerun()
        else:
            st.sidebar.warning(msg)

    st.markdown("---")
    if st.button("🗑️ Clear All Open Trades", use_container_width=True):
        con.execute("DELETE FROM brodax_open_trades_sync")
        con.commit()
        st.sidebar.success("Cleared!")
        st.rerun()

    if st.button("🔄 Reset Balance ($1,000)", use_container_width=True):
        con.execute("UPDATE brodax_account_sync SET balance_usd = 1000.0 WHERE id = 1")
        con.commit()
        st.rerun()

df, live_price, feed_source = get_market_data(symbol, timeframe)
result = evaluate_brodax_strategy(df, live_price, symbol)

if auto_mode:
    execute_brodax_trade(con, symbol, timeframe, result, lot_size=lot_input)

curr_balance = get_current_balance(con)

# --- METRICS DISPLAY ---
col1, col2, col3, col4 = st.columns(4)
col1.metric("💵 Balance", f"${curr_balance:,.2f} USD")
col2.metric("📈 Live Price", f"${live_price:,.2f}" if live_price > 10 else f"${live_price:,.4f}")
col3.metric("🎯 Daily Bias", result["bias"])
col4.metric("📊 Confluence Score", f"B: {result['bullish']} | S: {result['bearish']}")

st.info(f"🟢 **System Status:** Synchronized & Active | **Feed:** {feed_source}")

st.divider()
left, right = st.columns([1.7, 1])

with left:
    st.subheader(f"📈 Live TradingView Chart ({symbol} - {timeframe})")
    tv_sym = CRYPTO_SYMBOLS[symbol]["tv_symbol"]
    tv_int = INTERVALS[timeframe]["tv_int"]
    
    chart_html = f"""
    <div style="height:500px;width:100%">
      <iframe src="https://s.tradingview.com/widgetembed/?symbol={tv_sym}&interval={tv_int}&hidesidetoolbar=0&hidetoptoolbar=0&symboledit=1&saveimage=1&toolbarbg=f1f3f6&studies=[]&theme=dark&style=1&timezone=Asia%2FKolkata&locale=en" style="width:100%;height:500px;border:none;" scrolling="no"></iframe>
    </div>
    """
    components.html(chart_html, height=515, scrolling=False)

with right:
    st.subheader("💡 Expert Price Analysis")
    st.success(f"**Structure:** {result['analysis']}")
    st.write(f"**Breakdown:** {result['votes']}")
    st.markdown(f"🛑 **Stop Loss:** `{result['sl']:,.2f}`")
    st.markdown(f"🎯 **Take Profit:** `{result['tp']:,.2f}`")
    st.markdown("---")
    if not df.empty:
        view = df.tail(3).copy()
        view.index = view.index.strftime('%d %b %H:%M')
        st.dataframe(view, use_container_width=True)

st.divider()
st.subheader("📋 Active Open Positions")
open_df = pd.read_sql_query("SELECT * FROM brodax_open_trades_sync ORDER BY id DESC", con)

if open_df.empty:
    st.info("Abhi koi open trade nahi hai.")
else:
    for index, row in open_df.iterrows():
        with st.expander(f"📌 Trade ID #{row['id']} | {row['symbol']} ({row['bias']} - {row['lot_size']} Lot)"):
            c1, c2 = st.columns(2)
            c1.markdown(f"🕒 **Time:** `{row['created_at_ist']}`\n📍 **Entry:** `${row['entry_price']:,.2f}`\n🛑 **SL:** `${row['stop_loss']:,.2f}`")
            c2.markdown(f"🎯 **TP:** `${row['take_profit']:,.2f}`\n⚡ **Status:** `ACTIVE`\n📊 **Votes:** `{row['strategy_votes']}`")
            if st.button(f"Close Trade #{row['id']}", key=f"close_{row['id']}"):
                close_single_trade(con, row['id'], live_price, reason="Manual Close 👤")
                st.rerun()

st.divider()
st.subheader("📜 Trade History")
history_df = pd.read_sql_query("SELECT * FROM brodax_history_sync ORDER BY id DESC LIMIT 50", con)

if history_df.empty:
    st.info("History khali hai.")
else:
    for index, row in history_df.iterrows():
        pnl_color = "🟢" if row['pnl_usd'] >= 0 else "🔴"
        with st.expander(f"{pnl_color} ID #{row['id']} | {row['symbol']} ({row['bias']}) | PnL: ${row['pnl_usd']:,.2f}"):
            st.markdown(f"**Realized PnL:** `${row['pnl_usd']:,.2f} USD` | **Reason:** `{row['exit_reason']}`")

con.close()
