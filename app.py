import os
import sqlite3
import time
import requests
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from datetime import datetime, timezone, timedelta
from streamlit_autorefresh import st_autorefresh


# ==========================================================
# BRODAX PRO — PRICE LESSON HINDI EDITION
# PAPER TRADING ONLY
# Original strategy logic preserved
# ==========================================================

IST = timezone(timedelta(hours=5, minutes=30))

DB_PATH = os.getenv(
    "BRODAX_DB_PATH",
    "brodax_price_lesson_paper.db"
)

INITIAL_BALANCE = 1000.0
TRADE_DURATION = 900
MAGIC_NUMBER = 999888

st.set_page_config(
    page_title="BRODAX PRO",
    page_icon="⚡",
    layout="wide"
)


# ==========================================================
# DESIGN
# ==========================================================

st.markdown("""
<style>

.stApp {
    background: #080d18;
    color: white;
}

[data-testid="stMetric"] {
    background: #141d2d;
    border: 1px solid #273750;
    padding: 16px;
    border-radius: 12px;
}

h1, h2, h3 {
    color: #e6f0ff !important;
}

.stButton button {
    border-radius: 8px;
    font-weight: bold;
}

[data-testid="stSidebar"] {
    background: #101827;
}

</style>
""", unsafe_allow_html=True)


# ==========================================================
# ASSETS
# ==========================================================

CRYPTO = {

    "Bitcoin BTC/USD": {
        "symbol": "BTCUSDT",
        "tv": "BINANCE:BTCUSDT",
        "quantity": 0.01,
        "decimals": 2
    },

    "Ethereum ETH/USD": {
        "symbol": "ETHUSDT",
        "tv": "BINANCE:ETHUSDT",
        "quantity": 0.1,
        "decimals": 2
    },

    "Solana SOL/USD": {
        "symbol": "SOLUSDT",
        "tv": "BINANCE:SOLUSDT",
        "quantity": 1.0,
        "decimals": 4
    },

    "Ripple XRP/USD": {
        "symbol": "XRPUSDT",
        "tv": "BINANCE:XRPUSDT",
        "quantity": 10.0,
        "decimals": 5
    }

}


# ==========================================================
# BINANCE CONNECTION
# ==========================================================

BINANCE_HOSTS = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision"
]


def binance_request(endpoint, params=None):

    errors = []

    for host in BINANCE_HOSTS:

        try:

            response = requests.get(
                host + endpoint,
                params=params,
                timeout=5,
                headers={
                    "User-Agent": "BRODAX-PRO/1.0"
                }
            )

            response.raise_for_status()

            data = response.json()

            if isinstance(data, dict) and "code" in data:
                raise RuntimeError(data.get("msg", "Binance API error"))

            return data, host

        except Exception as e:

            errors.append(str(e))

    raise ConnectionError(
        "All Binance endpoints failed: " + " | ".join(errors)
    )


# ==========================================================
# DATABASE
# ==========================================================

def connect_db():

    con = sqlite3.connect(
        DB_PATH,
        check_same_thread=False,
        timeout=30
    )

    con.execute("PRAGMA journal_mode=WAL")

    con.execute("""
        CREATE TABLE IF NOT EXISTS account (

            id INTEGER PRIMARY KEY,
            balance REAL NOT NULL

        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS open_positions (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,

            entry_price REAL NOT NULL,
            quantity REAL NOT NULL,

            bias TEXT NOT NULL,

            opened_at REAL NOT NULL,
            opened_at_ist TEXT NOT NULL,

            signal_candle TEXT NOT NULL,

            magic_number INTEGER NOT NULL

        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS trade_history (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,

            entry_price REAL NOT NULL,
            exit_price REAL NOT NULL,

            quantity REAL NOT NULL,

            bias TEXT NOT NULL,

            opened_at_ist TEXT NOT NULL,
            closed_at_ist TEXT NOT NULL,

            holding_seconds INTEGER,

            exit_reason TEXT NOT NULL,

            pnl REAL NOT NULL,

            magic_number INTEGER NOT NULL

        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS signal_log (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            symbol TEXT NOT NULL,
            candle_timestamp TEXT NOT NULL,

            bias TEXT NOT NULL,

            UNIQUE(symbol, candle_timestamp)

        )
    """)

    con.execute("""
        INSERT OR IGNORE INTO account
        (id, balance)
        VALUES (1, 1000.0)
    """)

    con.commit()

    return con


# ==========================================================
# BALANCE
# ==========================================================

def get_balance(con):

    row = con.execute(
        "SELECT balance FROM account WHERE id=1"
    ).fetchone()

    return float(row[0])


def update_balance(con, pnl):

    con.execute(
        """
        UPDATE account
        SET balance = balance + ?
        WHERE id=1
        """,
        (float(pnl),)
    )

    con.commit()


# ==========================================================
# LIVE PRICE
# ==========================================================

@st.cache_data(ttl=1, show_spinner=False)
def get_live_price(symbol):

    data, source = binance_request(
        "/api/v3/ticker/price",
        {"symbol": symbol}
    )

    return float(data["price"]), source


# ==========================================================
# HISTORICAL CANDLES
# ==========================================================

@st.cache_data(ttl=2, show_spinner=False)
def get_market_data(symbol):

    data, source = binance_request(
        "/api/v3/klines",
        {
            "symbol": symbol,
            "interval": "15m",
            "limit": 100
        }
    )

    now_ms = int(time.time() * 1000)

    candles = []

    for row in data:

        # Ignore incomplete candle.
        if int(row[6]) > now_ms:
            continue

        candles.append({

            "datetime": pd.to_datetime(
                row[0],
                unit="ms",
                utc=True
            ),

            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4])

        })

    if not candles:

        raise ValueError(
            "No completed Binance candles received."
        )

    df = pd.DataFrame(candles)

    df.set_index("datetime", inplace=True)

    df["datetime_ist"] = (
        df.index.tz_convert(IST)
    )

    return df, source


# ==========================================================
# PRICE LESSON HINDI STRATEGY
# ORIGINAL ENTRY RULES
# ==========================================================

def evaluate_strategy(df):

    result = {

        "bias": "WAIT",

        "bullish": 0,
        "bearish": 0,

        "analysis": "Waiting for confirmation",

        "votes": "No valid signal",

        "candle_ts": None

    }

    if df is None or len(df) < 20:

        result["analysis"] = (
            "Insufficient historical candles. "
            "Minimum 20 required."
        )

        return result

    current = df.iloc[-1]
    previous = df.iloc[-2]

    candle_timestamp = str(df.index[-1])

    current_body = abs(
        current["close"] - current["open"]
    )

    previous_body = abs(
        previous["close"] - previous["open"]
    )

    # ORIGINAL IMPULSE RULE
    impulse = (
        current_body > previous_body * 1.1
    )

    bullish_candle = (
        current["close"] > current["open"]
    )

    bearish_candle = (
        current["close"] < current["open"]
    )

    # ORIGINAL BUY CONDITION
    buy_condition = (

        impulse

        and bullish_candle

        and current["close"] > previous["high"]

    )

    # ORIGINAL SELL CONDITION
    sell_condition = (

        impulse

        and bearish_candle

        and current["close"] < previous["low"]

    )

    if buy_condition:

        result["bias"] = "BUY"

        result["bullish"] = 1

        result["analysis"] = (
            "Bullish impulse confirmed. "
            "Close above previous candle high."
        )

        result["votes"] = (
            "Bullish impulse + Previous High Breakout"
        )

    elif sell_condition:

        result["bias"] = "SELL"

        result["bearish"] = 1

        result["analysis"] = (
            "Bearish impulse confirmed. "
            "Close below previous candle low."
        )

        result["votes"] = (
            "Bearish impulse + Previous Low Breakdown"
        )

    else:

        result["bias"] = "WAIT"

        result["analysis"] = (
            "Original Price Lesson confirmation "
            "conditions not satisfied."
        )

        result["votes"] = (
            f"Impulse: {impulse} | "
            f"Bullish candle: {bullish_candle} | "
            f"Bearish candle: {bearish_candle}"
        )

    result["candle_ts"] = candle_timestamp

    return result


# ==========================================================
# EXECUTE PAPER TRADE
# ==========================================================

def execute_trade(
    con,
    symbol,
    quantity,
    result,
    live_price
):

    bias = result["bias"]

    if bias not in ["BUY", "SELL"]:

        return False, "No valid Price Lesson signal."

    if live_price is None:

        return False, "Live price unavailable."

    if result["candle_ts"] is None:

        return False, "Signal candle unavailable."

    # ONE ACTIVE TRADE PER SYMBOL
    existing = con.execute(
        """
        SELECT id
        FROM open_positions
        WHERE symbol=?
        AND magic_number=?
        """,
        (symbol, MAGIC_NUMBER)
    ).fetchone()

    if existing:

        return False, "Active trade already exists."

    # PREVENT DUPLICATE SIGNAL
    try:

        con.execute(
            """
            INSERT INTO signal_log
            (symbol, candle_timestamp, bias)
            VALUES (?, ?, ?)
            """,
            (
                symbol,
                result["candle_ts"],
                bias
            )
        )

    except sqlite3.IntegrityError:

        return False, "Signal already executed for this candle."

    now = time.time()

    opened_ist = datetime.now(
        IST
    ).strftime("%d-%m-%Y %I:%M:%S %p IST")

    con.execute(
        """
        INSERT INTO open_positions (

            symbol,
            timeframe,

            entry_price,
            quantity,

            bias,

            opened_at,
            opened_at_ist,

            signal_candle,

            magic_number

        )

        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,

        (
            symbol,
            "15m",

            float(live_price),
            float(quantity),

            bias,

            now,
            opened_ist,

            result["candle_ts"],

            MAGIC_NUMBER
        )
    )

    con.commit()

    return True, f"{bias} paper trade executed successfully!"


# ==========================================================
# CLOSE TRADE
# ==========================================================

def close_trade(
    con,
    trade_id,
    exit_price,
    reason
):

    row = con.execute(
        """
        SELECT
            symbol,
            timeframe,
            entry_price,
            quantity,
            bias,
            opened_at,
            opened_at_ist,
            magic_number

        FROM open_positions

        WHERE id=?
        """,
        (trade_id,)
    ).fetchone()

    if not row:

        return None

    (
        symbol,
        timeframe,
        entry,
        quantity,
        bias,
        opened_at,
        opened_ist,
        magic
    ) = row

    entry = float(entry)
    exit_price = float(exit_price)
    quantity = float(quantity)

    # CRYPTO PAPER PNL
    if bias == "BUY":

        pnl = (
            exit_price - entry
        ) * quantity

    else:

        pnl = (
            entry - exit_price
        ) * quantity

    pnl = round(pnl, 4)

    closed_ist = datetime.now(
        IST
    ).strftime("%d-%m-%Y %I:%M:%S %p IST")

    holding_seconds = max(
        0,
        int(time.time() - opened_at)
    )

    con.execute(
        """
        INSERT INTO trade_history (

            symbol,
            timeframe,

            entry_price,
            exit_price,

            quantity,

            bias,

            opened_at_ist,
            closed_at_ist,

            holding_seconds,

            exit_reason,
            pnl,

            magic_number

        )

        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,

        (
            symbol,
            timeframe,

            entry,
            exit_price,

            quantity,

            bias,

            opened_ist,
            closed_ist,

            holding_seconds,

            reason,
            pnl,

            magic
        )
    )

    con.execute(
        "DELETE FROM open_positions WHERE id=?",
        (trade_id,)
    )

    con.commit()

    update_balance(con, pnl)

    return pnl


# ==========================================================
# AUTOMATIC 900 SECOND EXIT
# ==========================================================

def manage_positions(con):

    positions = con.execute(
        """
        SELECT id, symbol, opened_at
        FROM open_positions
        """
    ).fetchall()

    closed = []

    for trade_id, symbol, opened_at in positions:

        elapsed = time.time() - float(opened_at)

        if elapsed >= TRADE_DURATION:

            try:

                price, _ = get_live_price(symbol)

                pnl = close_trade(
                    con,
                    trade_id,
                    price,
                    "900 Second Time Exit"
                )

                if pnl is not None:

                    closed.append(
                        (symbol, pnl)
                    )

            except Exception:

                # Never close using a fake/stale price.
                continue

    return closed


# ==========================================================
# MAIN INTERFACE
# ==========================================================

st.markdown(
    """
    <h1 style='text-align:center;'>
    ⚡ BRODAX PRO
    </h1>

    <p style='text-align:center;color:#8b949e;'>
    PRICE LESSON HINDI • CRYPTO PAPER TRADING ENGINE
    </p>
    """,
    unsafe_allow_html=True
)

st.divider()

con = connect_db()


# ==========================================================
# SIDEBAR
# ==========================================================

with st.sidebar:

    st.header("⚙️ Control Panel")

    selected_asset = st.selectbox(
        "Select Crypto",
        list(CRYPTO.keys())
    )

    asset = CRYPTO[selected_asset]

    symbol = asset["symbol"]

    quantity = st.number_input(
        "Trade Quantity",
        min_value=0.001,
        max_value=100000.0,
        value=float(asset["quantity"]),
        step=0.001
    )

    st.markdown("---")

    auto_mode = st.toggle(
        "Enable Auto-Pilot",
        value=False
    )

    st.caption(
        "Auto-Pilot executes only confirmed "
        "Price Lesson signals."
    )

    st.markdown("---")

    st.info(
        """
        STRATEGY SETTINGS

        Timeframe: M15

        Impulse: 1.1x

        Exit: 900 seconds

        SL: Disabled

        TP: Disabled

        Mode: Paper Trading
        """
    )

    if st.button(
        "🔄 Reset Balance ($1,000)",
        use_container_width=True
    ):

        con.execute(
            "UPDATE account SET balance=1000 WHERE id=1"
        )

        con.commit()

        st.rerun()

    if st.button(
        "🗑️ Clear Open Positions",
        use_container_width=True
    ):

        con.execute(
            "DELETE FROM open_positions"
        )

        con.commit()

        st.warning(
            "Open positions cleared. "
            "No PnL was realized."
        )

        st.rerun()


# ==========================================================
# AUTOMATIC PAGE REFRESH
# ==========================================================

st_autorefresh(
    interval=2000,
    key="brodax_refresh"
)


# ==========================================================
# MARKET DATA
# ==========================================================

try:

    df, kline_source = get_market_data(symbol)

    live_price, ticker_source = get_live_price(symbol)

    feed_ok = True

    feed_message = (
        "BINANCE LIVE CONNECTED 🟢"
    )

except Exception as e:

    df = pd.DataFrame()

    live_price = None

    feed_ok = False

    feed_message = str(e)

    kline_source = None


# ==========================================================
# STRATEGY
# ==========================================================

result = evaluate_strategy(df)


# ==========================================================
# AUTO TRADE MANAGEMENT
# ==========================================================

closed_trades = manage_positions(con)

for closed_symbol, pnl in closed_trades:

    st.toast(
        f"{closed_symbol} closed | PnL: ${pnl:.4f}"
    )


# ==========================================================
# AUTO EXECUTION
# ==========================================================

if auto_mode and feed_ok:

    success, message = execute_trade(
        con,
        symbol,
        quantity,
        result,
        live_price
    )

    if success:

        st.toast(message)


# ==========================================================
# ACCOUNT METRICS
# ==========================================================

balance = get_balance(con)

open_count = con.execute(
    "SELECT COUNT(*) FROM open_positions"
).fetchone()[0]

history_stats = con.execute(
    """
    SELECT
        COUNT(*),
        COALESCE(SUM(pnl), 0)
    FROM trade_history
    """
).fetchone()

total_trades = history_stats[0]

total_pnl = float(history_stats[1])


c1, c2, c3, c4 = st.columns(4)

c1.metric(
    "💵 Balance",
    f"${balance:,.2f}"
)

c2.metric(
    "📈 Live Price",
    (
        f"${live_price:,.4f}"
        if live_price is not None
        else "FEED ERROR"
    )
)

c3.metric(
    "📌 Open Positions",
    str(open_count)
)

c4.metric(
    "📊 Total Realized PnL",
    f"${total_pnl:,.2f}"
)


# ==========================================================
# FEED STATUS
# ==========================================================

if feed_ok:

    st.success(
        f"{feed_message} | "
        f"Ticker: {ticker_source} | "
        f"Candles: {kline_source}"
    )

else:

    st.error(
        f"BINANCE FEED ERROR: {feed_message}"
    )


# ==========================================================
# SIGNAL DISPLAY
# ==========================================================

st.divider()

st.subheader("🎯 Price Lesson Hindi Signal Engine")

s1, s2, s3 = st.columns(3)

s1.metric(
    "Current Signal",
    result["bias"]
)

s2.metric(
    "Bullish Confirmation",
    result["bullish"]
)

s3.metric(
    "Bearish Confirmation",
    result["bearish"]
)

if result["bias"] == "BUY":

    st.success("🟢 BUY SIGNAL CONFIRMED")

elif result["bias"] == "SELL":

    st.error("🔴 SELL SIGNAL CONFIRMED")

else:

    st.warning("🟡 WAIT — NO VALID CONFIRMATION")


st.info(result["analysis"])

st.write(
    "**Strategy Breakdown:**",
    result["votes"]
)


# ==========================================================
# TRADINGVIEW CHART
# ==========================================================

st.divider()

st.subheader(
    f"📈 Binance TradingView Chart — {symbol}"
)

chart_html = f"""
<div style="height:500px;width:100%;">

<iframe

src="https://s.tradingview.com/widgetembed/?
symbol={asset['tv']}&
interval=15&
theme=dark&
style=1&
timezone=Asia%2FKolkata&
locale=en&
hidesidetoolbar=0&
hidetoptoolbar=0"

style="width:100%;height:500px;border:none;">

</iframe>

</div>
"""

components.html(
    chart_html,
    height=510,
    scrolling=False
)


# ==========================================================
# RECENT CANDLES
# ==========================================================

st.divider()

st.subheader("🕯️ Recent M15 Candles")

if not df.empty:

    candle_view = df.tail(10).copy()

    candle_view.index = (
        candle_view["datetime_ist"]
        .dt.strftime("%d %b %H:%M")
    )

    candle_view = candle_view[
        ["open", "high", "low", "close"]
    ]

    st.dataframe(
        candle_view,
        use_container_width=True
    )


# ==========================================================
# OPEN POSITIONS
# ==========================================================

st.divider()

st.subheader("📋 Active Paper Positions")

positions = pd.read_sql_query(
    """
    SELECT *
    FROM open_positions
    ORDER BY id DESC
    """,
    con
)

if positions.empty:

    st.info("No active paper trades.")

else:

    for _, row in positions.iterrows():

        with st.expander(
            f"#{row['id']} | "
            f"{row['symbol']} | "
            f"{row['bias']}"
        ):

            try:

                current_price, _ = get_live_price(
                    row["symbol"]
                )

            except Exception:

                current_price = None

            if current_price is not None:

                if row["bias"] == "BUY":

                    unrealized = (
                        current_price - row["entry_price"]
                    ) * row["quantity"]

                else:

                    unrealized = (
                        row["entry_price"] - current_price
                    ) * row["quantity"]

                st.metric(
                    "Live Unrealized PnL",
                    f"${unrealized:.4f}"
                )

            else:

                st.warning(
                    "Live price unavailable. "
                    "Cannot calculate current PnL."
                )

            st.write(
                f"Entry: ${row['entry_price']:,.4f}"
            )

            st.write(
                f"Quantity: {row['quantity']}"
            )

            elapsed = int(
                time.time() - row["opened_at"]
            )

            remaining = max(
                0,
                TRADE_DURATION - elapsed
            )

            st.write(
                f"⏱️ Remaining: {remaining} seconds"
            )

            if st.button(
                f"Close Position #{row['id']}",
                key=f"close_{row['id']}"
            ):

                if current_price is None:

                    st.error(
                        "Cannot close without a live price."
                    )

                else:

                    pnl = close_trade(
                        con,
                        row["id"],
                        current_price,
                        "Manual Close"
                    )

                    st.success(
                        f"Trade closed. PnL: ${pnl:.4f}"
                    )

                    st.rerun()


# ==========================================================
# TRADE HISTORY
# ==========================================================

st.divider()

st.subheader("📜 Trade History")

history = pd.read_sql_query(
    """
    SELECT *
    FROM trade_history
    ORDER BY id DESC
    LIMIT 100
    """,
    con
)

if history.empty:

    st.info("No completed trades yet.")

else:

    st.dataframe(
        history,
        use_container_width=True,
        hide_index=True
    )


# ==========================================================
# PERFORMANCE
# ==========================================================

st.divider()

st.subheader("📊 Performance Summary")

if not history.empty:

    wins = int(
        (history["pnl"] > 0).sum()
    )

    losses = int(
        (history["pnl"] < 0).sum()
    )

    win_rate = (
        wins / len(history) * 100
    )

    p1, p2, p3 = st.columns(3)

    p1.metric("Winning Trades", wins)

    p2.metric("Losing Trades", losses)

    p3.metric(
        "Win Rate",
        f"{win_rate:.2f}%"
    )

else:

    st.info("Performance data will appear after completed trades.")


st.caption(
    "BRODAX PRO | Price Lesson Hindi Strategy | "
    "Paper Trading Only | No Real Orders"
)

con.close()