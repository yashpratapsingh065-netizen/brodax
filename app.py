import os
import sqlite3
import random
from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import requests

# ==================== CREDENTIALS CONFIGURATION ====================
TWELVE_DATA_API_KEY = "d941e0ca121e4ebc9f9bb40b224a7970"
DERIV_TOKEN = "pat_d7c04912466579e8714e59fcbe76d1457ffc33b73a8c5b8558cec579b8cfc0d2"
DERIV_APP_ID = "1089"
# ===================================================================

# Indian Standard Time (IST) Offset: UTC + 5:30
IST = timezone(timedelta(hours=5, minutes=30))

st.set_page_config(
    page_title="BRODAX — Crypto Trading Bot (Bigman2 Expert)",
    page_icon="⚡",
    layout="wide"
)

DB_PATH = os.getenv("BRODAX_DB_PATH", "brodax_crypto_bot.db")

CRYPTO_SYMBOLS = {
    "Bitcoin (BTC/USD)": {"twelve_symbol": "BTC/USD", "tv_symbol": "BINANCE:BTCUSDT", "lot": 0.01, "base_p": 91500.0},
    "Ethereum (ETH/USD)": {"twelve_symbol": "ETH/USD", "tv_symbol": "BINANCE:ETHUSDT", "lot": 0.1, "base_p": 3350.0},
    "Solana (SOL/USD)": {"twelve_symbol": "SOL/USD", "tv_symbol": "BINANCE:SOLUSDT", "lot": 1.0, "base_p": 195.0},
    "Ripple (XRP/USD)": {"twelve_symbol": "XRP/USD", "tv_symbol": "BINANCE:XRPUSDT", "lot": 10.0, "base_p": 2.45}
}

INTERVALS = {
    "5 min": {"tv_int": "5", "twelve_int": "5min", "pd_freq": "5min"},
    "15 min": {"tv_int": "15", "twelve_int": "15min", "pd_freq": "15min"},
    "1 hour": {"tv_int": "60", "twelve_int": "1h", "pd_freq": "1h"},
    "4 hours": {"tv_int": "240", "twelve_int": "4h", "pd_freq": "4h"},
    "1 day": {"tv_int": "D", "twelve_int": "1day", "pd_freq": "1D"}
}

if 'live_prices' not in st.session_state:
    st.session_state.live_prices = {k: v["base_p"] for k, v in CRYPTO_SYMBOLS.items()}

def connect_db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS brodax_open_trades_v3 (
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
        CREATE TABLE IF NOT EXISTS brodax_trade_history (
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
        CREATE TABLE IF NOT EXISTS brodax_account_balance (
            id INTEGER PRIMARY KEY,
            balance_usd REAL NOT NULL
        )
    """)
    cursor = con.cursor()
    cursor.execute("SELECT balance_usd FROM brodax_account_balance WHERE id = 1")
    row = cursor.fetchone()
    if not row:
        con.execute("INSERT INTO brodax_account_balance (id, balance_usd) VALUES (1, 500.0)")
    con.commit()
    return con

def get_market_data(symbol_key, interval_key, limit=50):
    info = CRYPTO_SYMBOLS[symbol_key]
    cfg = INTERVALS[interval_key]
    
    current_price = st.session_state.live_prices[symbol_key]
    feed_source = "Session Market Feed"
    
    try:
        url = f"https://api.twelvedata.com/time_series?symbol={info['twelve_symbol']}&interval={cfg['twelve_int']}&outputsize={limit}&apikey={TWELVE_DATA_API_KEY}"
        resp = requests.get(url, timeout=5)
        data = resp.json()
        
        if "values" in data:
            raw_values = data["values"][::-1]
            candles = []
            for v in raw_values:
                candles.append({
                    "datetime": pd.to_datetime(v["datetime"]),
                    "open": float(v["open"]),
                    "high": float(v["high"]),
                    "low": float(v["low"]),
                    "close": float(v["close"])
                })
            df = pd.DataFrame(candles)
            df.set_index("datetime", inplace=True)
            current_price = float(df.iloc[-1]["close"])
            df['datetime_ist'] = df.index.tz_localize('UTC').tz_convert(IST) if df.index.tz is None else df.index.tz_convert(IST)
            feed_source = "Twelve Data API 🟢 (Live Feed)"
            st.session_state.live_prices[symbol_key] = current_price
            return df[['open', 'high', 'low', 'close', 'datetime_ist']], current_price, feed_source
        else:
            raise Exception("Twelve Data limit or error")
    except Exception:
        current_price = st.session_state.live_prices[symbol_key]
        dates = pd.date_range(end=datetime.now(timezone.utc), periods=limit, freq=cfg["pd_freq"])
        df = pd.DataFrame(index=dates)
        df['open'] = float(current_price) * 0.999
        df['high'] = float(current_price) * 1.002
        df['low'] = float(current_price) * 0.998
        df['close'] = float(current_price)
        df['datetime_ist'] = df.index.tz_convert(IST)
        feed_source = "Twelve Data Fallback Simulator ⚡"
        return df[['open', 'high', 'low', 'close', 'datetime_ist']], float(current_price), feed_source

def evaluate_brodax_strategy(df, current_price):
    current_price = float(current_price)
    if df.empty or len(df) < 10:
        return {
            "bias": "BUY",
            "votes": "Default Confluence",
            "bullish": 2,
            "bearish": 1,
            "analysis": "Market initializing...",
            "entry": current_price,
            "sl": current_price * 0.99,
            "tp": current_price * 1.025,
            "candle_ts": "init"
        }
    
    resistance_level = float(df['high'].max())
    support_level = float(df['low'].min())
    equilibrium = (resistance_level + support_level) / 2.0
    
    ma_10 = df['close'].rolling(window=10).mean().iloc[-1]
    is_uptrend = current_price >= ma_10
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    candle_ts = str(df.index[-1])
    
    bullish_votes = []
    bearish_votes = []
    
    if is_uptrend:
        bullish_votes.append("Trend Alignment (Bullish)")
    else:
        bearish_votes.append("Trend Alignment (Bearish)")

    if current_price <= equilibrium:
        bullish_votes.append("Discount Zone (< 0.5 Eq)")
    else:
        bearish_votes.append("Premium Zone (> 0.5 Eq)")
        
    body = abs(last['close'] - last['open'])
    lower_wick = min(last['open'], last['close']) - last['low']
    upper_wick = last['high'] - max(last['open'], last['close'])
    
    if lower_wick >= body * 1.0 and last['close'] > last['open']:
        bullish_votes.append("Demand Rejection Wick")
    elif upper_wick >= body * 1.0 and last['close'] < last['open']:
        bearish_votes.append("Supply Rejection Wick")

    if last['close'] > prev['close']:
        bullish_votes.append("Bullish Candle Continuation")
    else:
        bearish_votes.append("Bearish Candle Continuation")
        
    b_score = len(bullish_votes)
    s_score = len(bearish_votes)
    
    # Strict score decision
    if b_score > s_score:
        bias = "BUY"
    elif s_score > b_score:
        bias = "SELL"
    else:
        bias = "BUY" if last['close'] >= last['open'] else "SELL"
    
    decimals = 2 if current_price > 10 else 4
    if bias == "BUY":
        sl = round(support_level * 0.992, decimals)
        tp = round(current_price + (current_price - sl) * 1.5, decimals)
        analysis = f"Uptrend / Discount Zone | Support at ${support_level:,.2f} (SL)"
    else:
        sl = round(resistance_level * 1.008, decimals)
        tp = round(current_price - (sl - current_price) * 1.5, decimals)
        analysis = f"Downtrend / Premium Zone | Resistance at ${resistance_level:,.2f} (SL)"
        
    votes_str = f"Bullish [{b_score}]: {', '.join(bullish_votes)} | Bearish [{s_score}]: {', '.join(bearish_votes)}"
    
    return {
        "bias": bias,
        "votes": votes_str,
        "bullish": b_score,
        "bearish": s_score,
        "analysis": analysis,
        "entry": round(current_price, decimals),
        "sl": sl,
        "tp": tp,
        "candle_ts": candle_ts
    }

def get_current_balance(con):
    cursor = con.cursor()
    cursor.execute("SELECT balance_usd FROM brodax_account_balance WHERE id = 1")
    row = cursor.fetchone()
    return float(row[0]) if row else 500.0

def update_balance(con, delta):
    current = get_current_balance(con)
    new_bal = current + float(delta)
    con.execute("UPDATE brodax_account_balance SET balance_usd = ? WHERE id = 1", (new_bal,))
    con.commit()
    return new_bal

def execute_brodax_trade(con, symbol, timeframe, result, lot_size):
    cursor = con.cursor()
    candle_ts = result["candle_ts"]
    
    cursor.execute("SELECT id FROM brodax_open_trades_v3 WHERE symbol = ? AND candle_timestamp = ?", (symbol, candle_ts))
    if cursor.fetchone():
        return False, "Trade already exists for this specific candle timestamp!"

    created_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
    entry_time_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    con.execute(
        "INSERT INTO brodax_open_trades_v3 (created_at_ist, symbol, timeframe, entry_time_utc, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes, candle_timestamp) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (created_at_ist, symbol, timeframe, entry_time_utc, float(result["entry"]), float(result["sl"]), float(result["tp"]), float(lot_size), str(result["bias"]), str(result["votes"]), candle_ts)
    )
    con.commit()
    return True, "Trade opened successfully!"

def close_single_trade(con, trade_id, current_price, reason="Manual Close 👤"):
    cursor = con.cursor()
    cursor.execute("SELECT created_at_ist, symbol, timeframe, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes FROM brodax_open_trades_v3 WHERE id = ?", (trade_id,))
    row = cursor.fetchone()
    if not row:
        return 0.0
    
    created_at_ist, symbol, timeframe, entry_price, sl, tp, lot_size, bias, strategy_votes = row
    current_price = float(current_price)
    entry_price = float(entry_price)
    lot_size = float(lot_size)
    
    diff = (current_price - entry_price) if bias == "BUY" else (entry_price - current_price)
    pnl_usd = round(diff * lot_size, 2)
    closed_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
    
    con.execute(
        "INSERT INTO brodax_trade_history (id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, exit_price, stop_loss, take_profit, lot_size, bias, exit_reason, pnl_usd, strategy_votes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, round(current_price, 2 if current_price > 10 else 4), sl, tp, lot_size, bias, reason, pnl_usd, strategy_votes)
    )
    con.execute("DELETE FROM brodax_open_trades_v3 WHERE id = ?", (trade_id,))
    con.commit()
    update_balance(con, pnl_usd)
    return pnl_usd

def check_auto_exits(con, current_price, current_bias):
    cursor = con.cursor()
    cursor.execute("SELECT id, bias, entry_price, stop_loss, take_profit, lot_size, entry_time_utc FROM brodax_open_trades_v3")
    open_trades = cursor.fetchall()
    current_price = float(current_price)
    now_utc = datetime.now(timezone.utc)
    
    for tid, bias, entry, sl, tp, lot, entry_time_str in open_trades:
        closed = False
        exit_p = current_price
        reason = "Manual Close 👤"
        
        # 1. SIGNAL FLIP REVERSAL CHECK (Turant exit agar signal opposite ho jaye)
        if bias != current_bias:
            exit_p = current_price
            reason = f"Signal Flip Reversal (Changed to {current_bias}) 🔄"
            closed = True
        
        # 2. Standard SL / TP checks
        elif bias == "BUY":
            if current_price >= tp:
                exit_p, reason, closed = tp, "TP Hitter 🎯", True
            elif current_price <= sl:
                exit_p, reason, closed = sl, "SL Hitter 🛑", True
        elif bias == "SELL":
            if current_price <= tp:
                exit_p, reason, closed = tp, "TP Hitter 🎯", True
            elif current_price >= sl:
                exit_p, reason, closed = sl, "SL Hitter 🛑", True
                
        # 3. 15-min timer check
        if not closed:
            try:
                entry_dt = datetime.strptime(entry_time_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            except Exception:
                entry_dt = now_utc

            if now_utc - entry_dt >= timedelta(minutes=15):
                exit_p, reason, closed = current_price, "15-Min Timer Exit ⏱️", True
                
        if closed:
            close_single_trade(con, tid, exit_p, reason=reason)

# --- STREAMLIT UI ---
st.title("⚡ BRODAX — Crypto Trading Bot")
st.caption("Signal Flip Reversal Exit • Multiple Positions per Candle • Twelve Data Live Feed")

con = connect_db()

with st.sidebar:
    st.header("⚙️ Brodax Control Panel")
    symbol = st.selectbox("Select Crypto Asset", list(CRYPTO_SYMBOLS.keys()), index=0)
    timeframe = st.selectbox("Timeframe", list(INTERVALS.keys()), index=1)
    
    default_lot = CRYPTO_SYMBOLS[symbol]["lot"]
    lot_input = st.number_input("Trade Lot Size", min_value=0.001, max_value=50.0, value=default_lot, step=0.001)
    
    if st.button("🔄 Refresh Feed Now", use_container_width=True):
        st.toast("Feed refreshed successfully!", icon="🔄")
        st.rerun()

    st.markdown("---")
    st.subheader("⚡ Execution Mode")
    auto_mode = st.toggle("Enable Auto-Pilot Execution", value=False)
    
    if st.button("➕ Open Brodax Trade", use_container_width=True):
        df_temp, live_p, _ = get_market_data(symbol, timeframe)
        res_temp = evaluate_brodax_strategy(df_temp, live_p)
        success, msg = execute_brodax_trade(con, symbol, timeframe, res_temp, lot_size=lot_input)
        if success:
            st.sidebar.success("Trade opened successfully!")
            st.rerun()
        else:
            st.sidebar.warning(msg)

    st.markdown("---")
    if st.button("🗑️ Clear All Open Trades & History", use_container_width=True):
        con.execute("DELETE FROM brodax_open_trades_v3")
        con.execute("DELETE FROM brodax_trade_history")
        con.commit()
        st.sidebar.success("All trades cleared successfully!")
        st.rerun()

    if st.button("🔄 Reset Demo Balance ($500)", use_container_width=True):
        con.execute("UPDATE brodax_account_balance SET balance_usd = 500.0 WHERE id = 1")
        con.commit()
        st.rerun()

df, live_price, feed_source = get_market_data(symbol, timeframe)
result = evaluate_brodax_strategy(df, live_price)

# Check auto exits including Signal Flip Reversal
check_auto_exits(con, live_price, result["bias"])

if auto_mode:
    execute_brodax_trade(con, symbol, timeframe, result, lot_size=lot_input)

curr_balance = get_current_balance(con)
st.markdown(f"### 💵 Account Balance: **${curr_balance:,.2f} USD**")

col1, col2, col3 = st.columns(3)
col1.metric("Live Market Price", f"${live_price:,.2f}" if live_price > 10 else f"${live_price:,.4f}")
col2.metric("Brodax Bias", result["bias"])
col3.metric("Bull / Bear Score", f"Bull: {result['bullish']} | Bear: {result['bearish']}")

st.info(f"🟢 **Expert Strategy Status:** Active (Signal Flip Reversal & Multi-Trade) | **Feed:** {feed_source}")

st.divider()

left, right = st.columns([1.8, 1])

with left:
    st.subheader(f"📈 Live TradingView Chart ({symbol} - {timeframe})")
    tv_sym = CRYPTO_SYMBOLS[symbol]["tv_symbol"]
    tv_int = INTERVALS[timeframe]["tv_int"]
    
    chart_html = f"""
    <div style="height:480px;width:100%">
      <iframe src="https://s.tradingview.com/widgetembed/?symbol={tv_sym}&interval={tv_int}&hidesidetoolbar=0&hidetoptoolbar=0&symboledit=1&saveimage=1&toolbarbg=f1f3f6&studies=[]&theme=dark&style=1&timezone=Asia%2FKolkata&locale=en" style="width:100%;height:480px;border:none;" scrolling="no"></iframe>
    </div>
    """
    components.html(chart_html, height=495, scrolling=False)

with right:
    st.subheader("💡 Expert Price Analysis (S/R Based)")
    st.write(f"**Market Structure:** {result['analysis']}")
    st.write(f"**Confluence Breakdown:** {result['votes']}")
    st.write(f"**Stop Loss:** `${result['sl']:,.2f}` | **Take Profit:** `${result['tp']:,.2f}`")
    st.markdown("---")
    if not df.empty:
        view = df.tail(4).copy()
        view.index = view.index.strftime('%d %b %I:%M %p')
        st.dataframe(view, use_container_width=True)

st.divider()
st.subheader("📋 Active Open Positions")
open_df = pd.read_sql_query("SELECT * FROM brodax_open_trades_v3 ORDER BY id DESC", con)

if open_df.empty:
    st.info("Abhi koi open trade nahi hai.")
else:
    for index, row in open_df.iterrows():
        with st.expander(f"📌 Trade ID #{row['id']} | {row['symbol']} ({row['bias']} - {row['lot_size']} Lot)"):
            c1, c2 = st.columns(2)
            c1.markdown(f"🕒 **Entry Time:** `{row['created_at_ist']}`\n📍 **Entry Price:** `${row['entry_price']:,.2f}`\n🛑 **Stop Loss:** `${row['stop_loss']:,.2f}`")
            c2.markdown(f"🎯 **Take Profit:** `${row['take_profit']:,.2f}`\n⚡ **Status:** `OPEN`\n📊 **Votes:** `{row['strategy_votes']}`")
            if st.button(f"Close Trade #{row['id']} Now", key=f"close_open_{row['id']}"):
                pnl_realized = close_single_trade(con, row['id'], live_price, reason="Manual Close 👤")
                st.toast(f"Trade closed! PnL: ${pnl_realized:+.2f}")
                st.rerun()

st.divider()
st.subheader("📜 Trade History (Detailed)")
history_df = pd.read_sql_query("SELECT * FROM brodax_trade_history ORDER BY id DESC LIMIT 50", con)

if history_df.empty:
    st.info("Abhi tak koi trade history me saved nahi hai.")
else:
    for index, row in history_df.iterrows():
        pnl_color = "🟢" if row['pnl_usd'] >= 0 else "🔴"
        with st.expander(f"{pnl_color} ID #{row['id']} | {row['symbol']} ({row['bias']}) | PnL: ${row['pnl_usd']:+.2f}"):
            c1, c2 = st.columns(2)
            c1.markdown(f"🕒 **Entry Time:** `{row['created_at_ist']}`\n🕒 **Closed Time:** `{row['closed_at_ist']}`\n📍 **Entry Price:** `${row['entry_price']:,.2f}`\n🏁 **Exit Price:** `${row['exit_price']:,.2f}`")
            c2.markdown(f"🛑 **Stop Loss:** `${row['stop_loss']:,.2f}`\n🎯 **Take Profit:** `${row['take_profit']:,.2f}`\n📦 **Lot Size:** `{row['lot_size']}`\n🏷️ **Exit Reason:** `{row['exit_reason']}`")
            st.markdown(f"💰 **Realized PnL:** `${row['pnl_usd']:+.2f} USD`\n📊 **Strategy Votes:** `{row['strategy_votes']}`")

con.close()
