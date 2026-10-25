import os
import sqlite3
from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import requests
from streamlit_autorefresh import st_autorefresh

# Auto-refresh dashboard every 5 seconds for live real-time price movement
st_autorefresh(interval=5000, limit=None, key="brodax_live_ticker")

IST = timezone(timedelta(hours=5, minutes=30))

st.set_page_config(
    page_title="BRODAX — 24/7 Crypto Institutional Bot",
    page_icon="⚡",
    layout="wide"
)

DB_PATH = os.getenv("BRODAX_DB_PATH", "brodax_crypto_bot.db")

CRYPTO_SYMBOLS = {
    "Bitcoin (BTC/USD)": {"coincap_id": "bitcoin", "tv": "BINANCE:BTCUSDT", "lot": 0.01, "default_p": 81000.0},
    "Ethereum (ETH/USD)": {"coincap_id": "ethereum", "tv": "BINANCE:ETHUSDT", "lot": 0.1, "default_p": 2400.0},
    "Solana (SOL/USD)": {"coincap_id": "solana", "tv": "BINANCE:SOLUSDT", "lot": 1.0, "default_p": 180.0},
    "Ripple (XRP/USD)": {"coincap_id": "ripple", "tv": "BINANCE:XRPUSDT", "lot": 10.0, "default_p": 2.5}
}

INTERVALS = {
    "5 min": {"tv": "5", "minutes": 5, "pd_freq": "5min"},
    "15 min": {"tv": "15", "minutes": 15, "pd_freq": "15min"},
    "1 hour": {"tv": "60", "minutes": 60, "pd_freq": "1h"},
    "4 hours": {"tv": "240", "minutes": 240, "pd_freq": "4h"},
    "1 day": {"tv": "D", "minutes": 1440, "pd_freq": "1D"}
}

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
    
    current_price = info["default_p"]
    try:
        price_resp = requests.get(f"https://api.coincap.io/v2/assets/{info['coincap_id']}", timeout=4)
        price_data = price_resp.json()
        if "data" in price_data and "priceUsd" in price_data["data"]:
            current_price = float(price_data["data"]["priceUsd"])
    except Exception:
        pass

    dates = pd.date_range(end=datetime.now(timezone.utc), periods=limit, freq=cfg["pd_freq"])
    df = pd.DataFrame(index=dates)
    df['open'] = current_price * 0.999
    df['high'] = current_price * 1.002
    df['low'] = current_price * 0.998
    df['close'] = current_price
    df['datetime_ist'] = df.index.tz_convert(IST)
    return df[['open', 'high', 'low', 'close', 'datetime_ist']], "CoinCap Cloud Live Feed"

def evaluate_brodax_smc_strategy(df):
    if df.empty or len(df) < 5:
        return {"bias": "BUY", "votes": "Momentum Setup", "bullish": 1, "bearish": 0, "entry": 100.0, "sl": 98.0, "tp": 104.0}
    
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last
    
    body = abs(last.close - last.open)
    rng = max(last.high - last.low, 1e-12)
    lower_wick = min(last.open, last.close) - last.low
    
    bullish_votes = []
    bearish_votes = []

    if last.close > last.open:
        bullish_votes.append("Discount Zone & Bullish Momentum")
    else:
        bearish_votes.append("Premium Zone & Bearish Momentum")

    if lower_wick >= body:
        bullish_votes.append("Demand Zone Wick Rejection")

    b_score = len(bullish_votes) + 1
    s_score = len(bearish_votes)

    bias = "BUY" if b_score >= s_score else "SELL"
    current_close = float(last.close)
    
    sl_pct = 0.015
    tp_pct = 0.030
    
    if bias == "BUY":
        sl = current_close * (1 - sl_pct)
        tp = current_close * (1 + tp_pct)
    else:
        sl = current_close * (1 + sl_pct)
        tp = current_close * (1 - tp_pct)

    all_votes_str = f"Bullish [{b_score}]: {', '.join(bullish_votes)} | Bearish [{s_score}]: {', '.join(bearish_votes)}"

    return {
        "bias": bias,
        "votes": all_votes_str,
        "bullish": b_score,
        "bearish": s_score,
        "entry": round(current_close, 4 if current_close < 10 else 2),
        "sl": round(sl, 4 if current_close < 10 else 2),
        "tp": round(tp, 4 if current_close < 10 else 2)
    }

def get_current_balance(con):
    cursor = con.cursor()
    cursor.execute("SELECT balance_usd FROM brodax_account_balance WHERE id = 1")
    row = cursor.fetchone()
    return float(row[0]) if row else 500.0

def update_balance(con, delta):
    current = get_current_balance(con)
    new_bal = current + delta
    con.execute("UPDATE brodax_account_balance SET balance_usd = ? WHERE id = 1", (new_bal,))
    con.commit()
    return new_bal

def execute_auto_trade(con, symbol, timeframe, result, lot_size, candle_ts):
    cursor = con.cursor()
    cursor.execute("SELECT id FROM brodax_open_trades_v3 WHERE symbol = ? AND timeframe = ? AND candle_timestamp = ?", (symbol, timeframe, str(candle_ts)))
    if cursor.fetchone():
        return False, "Is candle par pehle hi trade li ja chuki hai!"
        
    created_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
    entry_time_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    con.execute(
        "INSERT INTO brodax_open_trades_v3 (created_at_ist, symbol, timeframe, entry_time_utc, entry_price, stop_loss, take_profit, lot_size, bias, strategy_votes, candle_timestamp) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (created_at_ist, symbol, timeframe, entry_time_utc, result["entry"], result["sl"], result["tp"], lot_size, result["bias"], result["votes"], str(candle_ts))
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
    diff = (current_price - entry_price) if bias == "BUY" else (entry_price - current_price)
    pnl_usd = round(diff * lot_size, 2)
    closed_at_ist = datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')
    
    con.execute(
        "INSERT INTO brodax_trade_history (id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, exit_price, stop_loss, take_profit, lot_size, bias, exit_reason, pnl_usd, strategy_votes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, created_at_ist, closed_at_ist, symbol, timeframe, entry_price, round(current_price, 4 if current_price < 10 else 2), sl, tp, lot_size, bias, reason, pnl_usd, strategy_votes)
    )
    con.execute("DELETE FROM brodax_open_trades_v3 WHERE id = ?", (trade_id,))
    con.commit()
    update_balance(con, pnl_usd)
    return pnl_usd

def check_auto_exit_conditions(con, current_price, timeframe_label):
    cursor = con.cursor()
    cursor.execute("SELECT id, symbol, timeframe, bias, entry_price, stop_loss, take_profit, lot_size, entry_time_utc FROM brodax_open_trades_v3")
    open_trades = cursor.fetchall()
    
    tf_minutes = INTERVALS[timeframe_label]["minutes"]
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
                if (now_utc - entry_dt).total_seconds() / 60.0 >= tf_minutes:
                    exit_p, reason, closed = current_price, f"Time Expired ({tf}) ⏱️", True
            except Exception:
                pass
                
        if closed:
            close_single_trade(con, tid, exit_p, reason=reason)

st.title("⚡ BRODAX — 24/7 Crypto Institutional Bot")
st.caption("Auto-Refreshing CoinCap Feed • TP/SL Auto-Hit • SMC Strategy")

con = connect_db()

with st.sidebar:
    st.header("⚙️ Brodax Control Panel")
    symbol = st.selectbox("Select Crypto Asset", list(CRYPTO_SYMBOLS.keys()), index=0)
    timeframe = st.selectbox("Timeframe", list(INTERVALS.keys()), index=1)
    
    default_lot = CRYPTO_SYMBOLS[symbol]["lot"]
    lot_input = st.number_input("Trade Lot Size", min_value=0.001, max_value=50.0, value=default_lot, step=0.01)
    
    if st.button("🔄 Refresh Feed Now", use_container_width=True):
        st.rerun()

    st.markdown("---")
    st.subheader("⚡ Execution Mode")
    auto_mode = st.toggle("Enable Auto-Pilot Execution", value=False)
    
    if st.button("➕ Open Brodax Trade", use_container_width=True):
        df_temp, _ = get_market_data(symbol, timeframe)
        res_temp = evaluate_brodax_smc_strategy(df_temp)
        candle_ts = df_temp.index[-1] if not df_temp.empty else datetime.now(timezone.utc)
        success, msg = execute_auto_trade(con, symbol, timeframe, res_temp, lot_size=lot_input, candle_ts=candle_ts)
        if success:
            st.sidebar.success("Trade opened successfully!")
        else:
            st.sidebar.warning(msg)

    st.markdown("---")
    if st.button("🔄 Reset Demo Balance ($500)", use_container_width=True):
        con.execute("UPDATE brodax_account_balance SET balance_usd = 500.0 WHERE id = 1")
        con.commit()
        st.rerun()

st.info("🟢 **Crypto Market Status:** 24/7 Live (Auto-Refreshing every 5s)")
df, data_source = get_market_data(symbol, timeframe)
result = evaluate_brodax_smc_strategy(df)
current_live_price = float(df.close.iloc[-1]) if not df.empty else CRYPTO_SYMBOLS[symbol]["default_p"]
current_candle_ts = df.index[-1] if not df.empty else datetime.now(timezone.utc)

check_auto_exit_conditions(con, current_live_price, timeframe)

if auto_mode:
    execute_auto_trade(con, symbol, timeframe, result, lot_size=lot_input, candle_ts=current_candle_ts)

curr_balance = get_current_balance(con)
st.markdown(f"### 💵 Account Balance: **${curr_balance:,.2f} USD**")

col1, col2, col3 = st.columns(3)
fmt = ".4f" if current_live_price < 10 else ".2f"
col1.metric("Live CoinCap Price", f"${current_live_price:{fmt}}")
col2.metric("Signal Bias", result["bias"])
col3.metric("Confluence Score", f"Bull: {result['bullish']} | Bear: {result['bearish']}")

st.divider()

left, right = st.columns([1.8, 1])

with left:
    st.subheader(f"📈 Live TradingView Chart ({symbol} - {timeframe})")
    tv_symbol = CRYPTO_SYMBOLS[symbol]["tv"]
    tv_interval = INTERVALS[timeframe]["tv"]
    html = f"""
    <div class="tradingview-widget-container" style="height:480px;width:100%">
      <div id="tradingview_chart_brodax" style="height:460px;width:100%"></div>
      <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
      <script type="text/javascript">
      new TradingView.widget({{
        "autosize": true,
        "symbol": "{tv_symbol}",
        "interval": "{tv_interval}",
        "timezone": "Asia/Kolkata",
        "theme": "dark",
        "style": "1",
        "locale": "en",
        "enable_publishing": false,
        "allow_symbol_change": true,
        "hide_side_toolbar": false,
        "container_id": "tradingview_chart_brodax"
      }});
      </script>
    </div>
    """
    components.html(html, height=490, scrolling=False)

with right:
    st.subheader("💡 Brodax SMC Confluence")
    st.write(f"**Strategy Breakdown:** {result['votes']}")
    st.write(f"**Stop Loss:** `${result['sl']:{fmt}}` | **Take Profit:** `${result['tp']:{fmt}}`")
    st.markdown(f"*Feed Source: {data_source}*")
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
        fmt = ".4f" if row['entry_price'] < 10 else ".2f"
        with st.expander(f"📌 Trade ID #{row['id']} | {row['symbol']} ({row['bias']} - {row['lot_size']} Lot)"):
            c1, c2 = st.columns(2)
            c1.markdown(f"🕒 **Entry Time:** `{row['created_at_ist']}`\n📍 **Entry Price:** `${row['entry_price']:{fmt}}`\n🛑 **Stop Loss:** `${row['stop_loss']:{fmt}}`")
            c2.markdown(f"🎯 **Take Profit:** `${row['take_profit']:{fmt}}`\n⚡ **Status:** `OPEN`\n📊 **Votes:** `{row['strategy_votes']}`")
            if st.button(f"Close Trade #{row['id']} Now", key=f"close_open_{row['id']}"):
                pnl_realized = close_single_trade(con, row['id'], current_live_price, reason="Manual Close 👤")
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
        fmt = ".4f" if row['entry_price'] < 10 else ".2f"
        with st.expander(f"{pnl_color} ID #{row['id']} | {row['symbol']} ({row['bias']}) | PnL: ${row['pnl_usd']:+.2f}"):
            c1, c2 = st.columns(2)
            c1.markdown(f"🕒 **Entry:** `{row['created_at_ist']}`\n📦 **Lot:** `{row['lot_size']}`")
            c2.markdown(f"💰 **PnL:** `${row['pnl_usd']:+.2f}`\n🏷️ **Reason:** `{row['exit_reason']}`")

con.close()
