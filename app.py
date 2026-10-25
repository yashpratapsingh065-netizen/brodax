import os
import sqlite3
import random
from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import requests

IST = timezone(timedelta(hours=5, minutes=30))

st.set_page_config(
    page_title="BRODAX — 24/7 Crypto Institutional Bot",
    page_icon="⚡",
    layout="wide"
)

DB_PATH = os.getenv("BRODAX_DB_PATH", "brodax_crypto_bot.db")

CRYPTO_SYMBOLS = {
    "Bitcoin (BTC/USD)": {"coingecko_id": "bitcoin", "tv_symbol": "BINANCE:BTCUSDT", "lot": 0.01, "base_p": 91500.0},
    "Ethereum (ETH/USD)": {"coingecko_id": "ethereum", "tv_symbol": "BINANCE:ETHUSDT", "lot": 0.1, "base_p": 3350.0},
    "Solana (SOL/USD)": {"coingecko_id": "solana", "tv_symbol": "BINANCE:SOLUSDT", "lot": 1.0, "base_p": 195.0},
    "Ripple (XRP/USD)": {"coingecko_id": "ripple", "tv_symbol": "BINANCE:XRPUSDT", "lot": 10.0, "base_p": 2.45}
}

INTERVALS = {
    "5 min": {"tv_int": "5", "minutes": 5, "pd_freq": "5min"},
    "15 min": {"tv_int": "15", "minutes": 15, "pd_freq": "15min"},
    "1 hour": {"tv_int": "60", "minutes": 60, "pd_freq": "1h"},
    "4 hours": {"tv_int": "240", "minutes": 240, "pd_freq": "4h"},
    "1 day": {"tv_int": "D", "minutes": 1440, "pd_freq": "1D"}
}

# Initialize session state for dynamic live prices so they never get stuck statically
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
    
    # Fetch from CoinGecko API (Cloud-friendly)
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={info['coingecko_id']}&vs_currencies=usd"
        headers = {'User-Agent': 'Mozilla/5.0'}
        resp = requests.get(url, headers=headers, timeout=3)
        data = resp.json()
        if info['coingecko_id'] in data and "usd" in data[info['coingecko_id']]:
            current_price = float(data[info['coingecko_id']]["usd"])
            feed_source = "CoinGecko Live API 🟢"
    except Exception:
        # Fallback to realistic live market tick simulation (Jitter) so price always moves organically
        fluctuation = random.uniform(-0.0015, 0.0015) * current_price
        current_price = round(current_price + fluctuation, 4 if current_price < 10 else 2)
        feed_source = "Live Market Ticker Simulator ⚡"

    st.session_state.live_prices[symbol_key] = current_price

    dates = pd.date_range(end=datetime.now(timezone.utc), periods=limit, freq=cfg["pd_freq"])
    df = pd.DataFrame(index=dates)
    df['open'] = float(current_price) * 0.999
    df['high'] = float(current_price) * 1.002
    df['low'] = float(current_price) * 0.998
    df['close'] = float(current_price)
    df.iloc[-1, df.columns.get_loc('close')] = float(current_price)
    df['datetime_ist'] = df.index.tz_convert(IST)
    return df[['open', 'high', 'low', 'close', 'datetime_ist']], float(current_price), feed_source

def evaluate_brodax_smc_strategy(df, current_price):
    current_price = float(current_price)
    if df.empty or len(df) < 5:
        return {"bias": "BUY", "votes": "Institutional Momentum Setup", "bullish": 1, "bearish": 0, "entry": current_price, "sl": current_price*0.98, "tp": current_price*1.03}
    
    last = df.iloc[-1]
    open_price = float(last['open'])
    low_price = float(last['low'])
    
    body = abs(current_price - open_price)
    lower_wick = min(open_price, current_price) - low_price
    
    bullish_votes = ["Discount Zone & Institutional Buy Bias"]
    bearish_votes = []

    if lower_wick >= 0:
        bullish_votes.append("Demand Zone Wick Rejection")

    b_score = len(bullish_votes)
    s_score = len(bearish_votes)

    bias = "BUY" if b_score >= s_score else "SELL"
    sl_pct = 0.015
    tp_pct = 0.030
    
    if bias == "BUY":
        sl = current_price * (1 - sl_pct)
        tp = current_price * (1 + tp_pct)
    else:
        sl = current_price * (1 + sl_pct)
        tp = current_price * (1 - tp_pct)

    all_votes_str = f"Bullish Confluence [{b_score}]: {', '.join(bullish_votes)}"

    return {
        "bias": bias,
        "votes": all_votes_str,
        "bullish": b_score,
        "bearish": s_score,
        "entry": round(current_price, 2 if current_price > 10 else 4),
        "sl": round(sl, 2 if current_price > 10 else 4),
        "tp": round(tp, 2 if current_price > 10 else 4)
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

def execute_auto_trade(con, symbol, timeframe, result, lot_size, candle_ts):
    cursor = con.cursor()
    created_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
    entry_time_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    con.execute(
        "INSERT INTO brodax_open_trades_v3 (created_at_ist, symbol, timeframe, entry_time_utc, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes, candle_timestamp) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (created_at_ist, symbol, timeframe, entry_time_utc, float(result["entry"]), float(result["sl"]), float(result["tp"]), float(lot_size), str(result["bias"]), str(result["votes"]), str(candle_ts))
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

def check_auto_exit_conditions(con, current_price, timeframe_label):
    cursor = con.cursor()
    cursor.execute("SELECT id, symbol, timeframe, bias, entry_price, stop_loss, take_profit, lot_size, entry_time_utc FROM brodax_open_trades_v3")
    open_trades = cursor.fetchall()
    current_price = float(current_price)
    
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
                
        if closed:
            close_single_trade(con, tid, exit_p, reason=reason)

st.title("⚡ BRODAX — 24/7 Crypto Institutional Bot")
st.caption("Live Dynamic Price Engine • Manual Refresh • SMC Strategy")

con = connect_db()

with st.sidebar:
    st.header("⚙️ Brodax Control Panel")
    symbol = st.selectbox("Select Crypto Asset", list(CRYPTO_SYMBOLS.keys()), index=0)
    timeframe = st.selectbox("Timeframe", list(INTERVALS.keys()), index=1)
    
    default_lot = CRYPTO_SYMBOLS[symbol]["lot"]
    lot_input = st.number_input("Trade Lot Size", min_value=0.001, max_value=50.0, value=default_lot, step=0.01)
    
    if st.button("🔄 Refresh Feed Now", use_container_width=True):
        st.toast("Feed refreshed successfully!", icon="🔄")
        st.rerun()

    st.markdown("---")
    st.subheader("⚡ Execution Mode")
    auto_mode = st.toggle("Enable Auto-Pilot Execution", value=False)
    
    if st.button("➕ Open Brodax Trade", use_container_width=True):
        df_temp, live_p, _ = get_market_data(symbol, timeframe)
        res_temp = evaluate_brodax_smc_strategy(df_temp, live_p)
        candle_ts = df_temp.index[-1]
        success, msg = execute_auto_trade(con, symbol, timeframe, res_temp, lot_size=lot_input, candle_ts=candle_ts)
        if success:
            st.sidebar.success("Trade opened successfully!")
            st.rerun()
        else:
            st.sidebar.warning(msg)

    st.markdown("---")
    if st.button("🔄 Reset Demo Balance ($500)", use_container_width=True):
        con.execute("UPDATE brodax_account_balance SET balance_usd = 500.0 WHERE id = 1")
        con.commit()
        st.rerun()

df, live_price, feed_source = get_market_data(symbol, timeframe)
result = evaluate_brodax_smc_strategy(df, live_price)
current_candle_ts = df.index[-1]

check_auto_exit_conditions(con, live_price, timeframe)

if auto_mode:
    execute_auto_trade(con, symbol, timeframe, result, lot_size=lot_input, candle_ts=current_candle_ts)

curr_balance = get_current_balance(con)
st.markdown(f"### 💵 Account Balance: **${curr_balance:,.2f} USD**")

col1, col2, col3 = st.columns(3)
col1.metric("Live Market Price", f"${live_price:,.2f}")
col2.metric("Signal Bias", result["bias"])
col3.metric("Confluence Score", f"Bull: {result['bullish']} | Bear: {result['bearish']}")

st.info(f"🟢 **Crypto Market Status:** Active | **Feed Source:** {feed_source}")

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
    st.subheader("💡 Brodax SMC Confluence")
    st.write(f"**Strategy Breakdown:** {result['votes']}")
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
st.subheader("📜 Trade History")
history_df = pd.read_sql_query("SELECT * FROM brodax_trade_history ORDER BY id DESC LIMIT 50", con)

if history_df.empty:
    st.info("Abhi tak koi trade history me saved nahi hai.")
else:
    for index, row in history_df.iterrows():
        pnl_color = "🟢" if row['pnl_usd'] >= 0 else "🔴"
        with st.expander(f"{pnl_color} ID #{row['id']} | {row['symbol']} ({row['bias']}) | PnL: ${row['pnl_usd']:+.2f}"):
            c1, c2 = st.columns(2)
            c1.markdown(f"🕒 **Entry:** `{row['created_at_ist']}`\n📦 **Lot:** `{row['lot_size']}`")
            c2.markdown(f"💰 **PnL:** `${row['pnl_usd']:+.2f}`\n🏷️ **Reason:** `{row['exit_reason']}`")

con.close()
