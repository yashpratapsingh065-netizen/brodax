import os
import sqlite3
from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import requests

# Indian Standard Time (IST) Offset: UTC + 5:30
IST = timezone(timedelta(hours=5, minutes=30))

st.set_page_config(
    page_title="BRODAX — 24/7 Crypto Institutional Bot",
    page_icon="⚡",
    layout="wide"
)

DB_PATH = os.getenv("BRODAX_DB_PATH", "brodax_crypto_bot.db")

CRYPTO_SYMBOLS = {
    "Bitcoin (BTC/USD)": {"coincap_id": "bitcoin", "tv": "BINANCE:BTCUSDT", "lot": 0.01, "default_p": 90000.0},
    "Ethereum (ETH/USD)": {"coincap_id": "ethereum", "tv": "BINANCE:ETHUSDT", "lot": 0.1, "default_p": 3300.0},
    "Solana (SOL/USD)": {"coincap_id": "solana", "tv": "BINANCE:SOLUSDT", "lot": 1.0, "default_p": 180.0},
    "Ripple (XRP/USD)": {"coincap_id": "ripple", "tv": "BINANCE:XRPUSDT", "lot": 10.0, "default_p": 2.5}
}

INTERVALS = {
    "5 min": {"coincap": "m5", "tv": "5", "minutes": 5},
    "15 min": {"coincap": "m15", "tv": "15", "minutes": 15},
    "1 hour": {"coincap": "h1", "tv": "60", "minutes": 60},
    "4 hours": {"coincap": "h2", "tv": "240", "minutes": 240},
    "1 day": {"coincap": "d1", "tv": "D", "minutes": 1440}
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

def get_market_data(symbol_key, interval_key, limit=100):
    info = CRYPTO_SYMBOLS[symbol_key]
    cfg = INTERVALS[interval_key]
    
    # Try fetching live asset price first
    current_price = info["default_p"]
    try:
        p_resp = requests.get(f"https://api.coincap.io/v2/assets/{info['coincap_id']}", timeout=5)
        p_data = p_resp.json()
        if "data" in p_data and "priceUsd" in p_data["data"]:
            current_price = float(p_data["data"]["priceUsd"])
    except Exception:
        pass

    # Try fetching candles from CoinCap with exchange
    url = f"https://api.coincap.io/v2/assets/{info['coincap_id']}/candles"
    params = {"exchange": "binance", "interval": cfg["coincap"]}
    
    try:
        resp = requests.get(url, params=params, timeout=6)
        data = resp.json()
        if "data" in data and isinstance(data["data"], list) and len(data["data"]) > 0:
            df = pd.DataFrame(data["data"])
            for col in ['open', 'high', 'low', 'close']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            df['datetime'] = pd.to_datetime(df['period'], unit='ms', utc=True)
            df['datetime_ist'] = df['datetime'].dt.tz_convert(IST)
            df = df.dropna(subset=['open', 'high', 'low', 'close'])
            if not df.empty:
                df = df.sort_values('datetime').set_index('datetime')
                return df[['open', 'high', 'low', 'close', 'datetime_ist']].tail(limit), "CoinCap Live Feed"
    except Exception:
        pass

    # Fallback: Create safe synthetic dataframe anchored around live price so app never crashes
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=limit, freq=cfg["coincap"].upper().replace('M', 'min').replace('H', 'h').replace('D', 'D'))
    df = pd.DataFrame(index=dates)
    df['open'] = current_price * 0.999
    df['high'] = current_price * 1.002
    df['low'] = current_price * 0.998
    df['close'] = current_price
    df['datetime_ist'] = df.index.tz_convert(IST)
    return df[['open', 'high', 'low', 'close', 'datetime_ist']], "Safe Live Price Fallback"

def evaluate_brodax_smc_strategy(df):
    if df.empty or len(df) < 10:
        return {"bias": "BUY", "votes": "Default Momentum Setup", "bullish": 1, "bearish": 0, "entry": 100.0, "sl": 98.0, "tp": 104.0}
    
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last
    p_prev = df.iloc[-3] if len(df) > 2 else prev
    
    body = abs(last.close - last.open)
    rng = max(last.high - last.low, 1e-12)
    upper_wick = last.high - max(last.open, last.close)
    lower_wick = min(last.open, last.close) - last.low
    
    swing_high = float(df.high.tail(30).max())
    swing_low = float(df.low.tail(30).min())
    eq_50 = (swing_high + swing_low) / 2.0
    
    bullish_votes = []
    bearish_votes = []

    if last.close < eq_50:
        bullish_votes.append("Discount Zone (Cheap Area - Buy Bias)")
    else:
        bearish_votes.append("Premium Zone (Expensive Area - Sell Bias)")

    if last.low > p_prev.high and prev.close > prev.open:
        bullish_votes.append("Unmitigated Bullish FVG Imbalance")
    elif last.high < p_prev.low and prev.close < prev.open:
        bearish_votes.append("Unmitigated Bearish FVG Imbalance")

    if lower_wick >= body * 1.5 and last.close > last.open:
        bullish_votes.append("Demand Zone Strong Wick Rejection")
    elif upper_wick >= body * 1.5 and last.close < last.open:
        bearish_votes.append("Supply Zone Strong Wick Rejection")

    if last.close > last.open:
        bullish_votes.append("Bullish Momentum")
    else:
        bearish_votes.append("Bearish Momentum")

    b_score = len(bullish_votes)
    s_score = len(bearish_votes)

    if b_score >= s_score:
        bias = "BUY"
    else:
        bias = "SELL"

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
    
    if bias == "BUY":
        diff = current_price - entry_price
    else:
        diff = entry_price - current_price
        
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
                exit_p = tp
                reason = "TP Hitter 🎯"
                closed = True
            elif current_price <= sl:
                exit_p = sl
                reason = "SL Hitter 🛑"
                closed = True
        elif bias == "SELL":
            if current_price <= tp:
                exit_p = tp
                reason = "TP Hitter 🎯"
                closed = True
            elif current_price >= sl:
                exit_p = sl
                reason = "SL Hitter 🛑"
                closed = True
                
        if not closed and tf == timeframe_label:
            try:
                entry_dt = datetime.strptime(entry_time_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                elapsed_mins = (now_utc - entry_dt).total_seconds() / 60.0
                if elapsed_mins >= tf_minutes:
                    exit_p = current_price
                    reason = f"Time Expired ({tf}) ⏱️"
                    closed = True
            except Exception:
                pass
                
        if closed:
            close_single_trade(con, tid, exit_p, reason=reason)

# --- STREAMLIT UI ---
st.title("⚡ BRODAX — 24/7 Crypto Institutional Bot")
st.caption("Bulletproof Feed • TP/SL Auto-Hit • SMC Strategy Confluence")

con = connect_db()

with st.sidebar:
    st.header("⚙️ Brodax Control Panel")
    symbol = st.selectbox("Select Crypto Asset", list(CRYPTO_SYMBOLS.keys()), index=0)
    timeframe = st.selectbox("Timeframe", list(INTERVALS.keys()), index=1)
    
    default_lot = CRYPTO_SYMBOLS[symbol]["lot"]
    lot_input = st.number_input("Trade Lot Size", min_value=0.001, max_value=50.0, value=default_lot, step=0.01)
    
    if st.button("🔄 Refresh Feed", use_container_width=True):
        st.toast("Feed refreshed successfully!", icon="🔄")
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
            st.sidebar.success(f"Trade opened on {timeframe}!")
        else:
            st.sidebar.warning(msg)

    st.markdown("---")
    if st.button("🔄 Reset Demo Balance ($500)", use_container_width=True):
        con.execute("UPDATE brodax_account_balance SET balance_usd = 500.0 WHERE id = 1")
        con.commit()
        st.sidebar.success("Balance reset to $500.00!")
        st.rerun()

st.info("🟢 **Crypto Market Status:** 24/7 Live & Active")
df, data_source = get_market_data(symbol, timeframe)
result = evaluate_brodax_smc_strategy(df)
current_live_price = float(df.close.iloc[-1]) if not df.empty else CRYPTO_SYMBOLS[symbol]["default_p"]
current_candle_ts = df.index[-1] if not df.empty else datetime.now(timezone.utc)

check_auto_exit_conditions(con, current_live_price, timeframe)

if auto_mode:
    execute_auto_trade(con, symbol, timeframe, result, lot_size=lot_input, candle_ts=current_candle_ts)

curr_balance = get_current_balance(con)
bal_color = "🟢" if curr_balance >= 500 else "🔴"
st.markdown(f"### 💵 Account Balance: **${curr_balance:,.2f} USD** {bal_color}")

col1, col2, col3 = st.columns(3)
fmt = ".4f" if current_live_price < 10 else ".2f"
col1.metric("Live Crypto Price", f"${current_live_price:{fmt}}")
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
    fmt = ".4f" if current_live_price < 10 else ".2f"
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
        with st.expander(f"📌 Trade ID #{row['id']} | {row['symbol']} ({row['bias']} - {row['lot_size']} Lot) - TF: {row['timeframe']}"):
            c1, c2 = st.columns(2)
            c1.markdown(f"🕒 **Entry Time:** `{row['created_at_ist']}`\n📍 **Entry Price:** `${row['entry_price']:{fmt}}`\n🛑 **Stop Loss:** `${row['stop_loss']:{fmt}}`")
            c2.markdown(f"🎯 **Take Profit:** `${row['take_profit']:{fmt}}`\n⚡ **Status:** `OPEN`\n📊 **Votes:** `{row['strategy_votes']}`")
            
            if st.button(f"Close Trade #{row['id']} Now", key=f"close_open_{row['id']}"):
                pnl_realized = close_single_trade(con, row['id'], current_live_price, reason="Manual Close 👤")
                st.toast(f"Trade #{row['id']} closed! PnL: ${pnl_realized:+.2f}")
                st.rerun()

st.divider()

st.subheader("📜 Trade History (Permanent Storage)")
history_df = pd.read_sql_query("SELECT * FROM brodax_trade_history ORDER BY id DESC LIMIT 50", con)

if history_df.empty:
    st.info("Abhi tak koi trade history me saved nahi hai.")
else:
    for index, row in history_df.iterrows():
        pnl_color = "🟢" if row['pnl_usd'] >= 0 else "🔴"
        fmt = ".4f" if row['entry_price'] < 10 else ".2f"
        with st.expander(f"{pnl_color} ID #{row['id']} | {row['symbol']} ({row['bias']}) | PnL: ${row['pnl_usd']:+.2f} | Reason: {row['exit_reason']}"):
            c1, c2 = st.columns(2)
            c1.markdown(f"🕒 **Entry Time:** `{row['created_at_ist']}`\n🕒 **Exit Time:** `{row['closed_at_ist']}`\n⏱️ **Timeframe:** `{row['timeframe']}`\n📦 **Lot Size:** `{row['lot_size']}`")
            c2.markdown(f"📍 **Entry Price:** `${row['entry_price']:{fmt}}`\n🏁 **Exit Price:** `${row['exit_price']:{fmt}}`\n🏷️ **Exit Reason:** `{row['exit_reason']}`\n💰 **Net PnL:** `${row['pnl_usd']:+.2f}`")
            st.markdown(f"📊 **Strategy Confluence at Entry:** `{row['strategy_votes']}`")

con.close()
