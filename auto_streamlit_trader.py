# auto_intraday_streamlit_final_full.py
"""
Full Streamlit Intraday Trader (Zerodha Kite)
- Manual request_token once/day => saves access token, alerts on expiry
- Manual stock select
- MIS intraday w/ 5x leverage auto-qty
- Paper/Live toggle
- Manual Start / Manual Stop / Emergency STOP
- Cancel pending + exit triggered on SL / stops
- Instant SL, initial SL, trailing SL
- 15:15 Square-off, 15:20 Safety Flatten
- Live P&L color coded (green/red/blue)
- Live 5-min chart for last 30 minutes (6 candles) with VWAP + markers
- Digital clock (IST)
- Dark background (except entry fields)
- Fast2SMS alerts for every action
- IST timezone everywhere
"""

import os
import time
import json
import threading
import traceback
from datetime import datetime, timedelta, time as dtime

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import requests

# Kite optional import
try:
    from kiteconnect import KiteConnect
    KITE_AVAILABLE = True
except Exception:
    KiteConnect = None
    KITE_AVAILABLE = False

# ---------------------------- TIMEZONE (IST) ----------------------------
try:
    # Python 3.9+
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

def local_now():
    return datetime.now(IST)

def local_time():
    return local_now().time()

def now_str():
    return local_now().strftime("%Y-%m-%d %H:%M:%S")

# ---------------------------- CONFIG / EDIT ----------------------------
ACCESS_TOKEN_FILE = os.getenv("ACCESS_TOKEN_FILE", "access_token.json")
# You may optionally set API_KEY & API_SECRET via env or paste into the sidebar
API_KEY_ENV = os.getenv("ZK_API_KEY", "")
API_SECRET_ENV = os.getenv("ZK_API_SECRET", "")

# Fast2SMS: set key and default number in sidebar (or env)
FAST2SMS_KEY_ENV = os.getenv("FAST2SMS_KEY", "")
FAST2SMS_DEFAULT_NUMBER = os.getenv("FAST2SMS_NUMBER", "")  # e.g. 9198...

# Strategy defaults
DEFAULT_EXPOSURE = 50000.0
DEFAULT_LEVERAGE = 5  # user requested 5x
DEFAULT_SL_PCT = 3.0
DEFAULT_INSTANT_SL_PCT = 1.5
DEFAULT_TRAIL_PCT = 3.0
AUTO_START_DEFAULT = False

# Chart candles: last 30 minutes -> 6 x 5-min candles
CANDLES_COUNT = 6

# Polling intervals
ENGINE_SLEEP = 3
CHART_REFRESH_SEC = 6
CLOCK_REFRESH_SEC = 1

# ---------------------------- UTILITIES ----------------------------
def safe_load_json(path):
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, "r") as f:
                return json.load(f)
    except Exception:
        return None
    return None

def safe_save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, default=str, indent=2)

# Simple threaded logger stored in session_state
if "logger" not in st.session_state:
    st.session_state.logger = []

def log(msg):
    ts = now_str()
    st.session_state.logger.append(f"[{ts}] {msg}")
    # keep last 200 lines
    if len(st.session_state.logger) > 200:
        st.session_state.logger = st.session_state.logger[-200:]

def send_fast2sms(message, api_key, number):
    """Send SMS via Fast2SMS (returns True on 200)."""
    if not api_key or not number:
        log("Fast2SMS key/number missing; SMS not sent.")
        return False
    try:
        url = "https://www.fast2sms.com/dev/bulkV2"
        payload = {
            "sender_id": "FSTSMS",
            "message": message,
            "language": "english",
            "route": "v3",
            "numbers": number
        }
        headers = {'authorization': api_key}
        r = requests.post(url, data=payload, headers=headers, timeout=10)
        log(f"Fast2SMS status={r.status_code}")
        return r.status_code == 200
    except Exception as e:
        log(f"Fast2SMS error: {e}")
        return False

def send_sms(message):
    """Unified SMS sender used across app."""
    key = st.session_state.get("fast2sms_key") or FAST2SMS_KEY_ENV
    num = st.session_state.get("sms_number") or FAST2SMS_DEFAULT_NUMBER
    ok = send_fast2sms(message, key, num)
    if not ok:
        log("SMS failed (check Fast2SMS key & number).")
    return ok

# ---------------------------- PAPER BROKER ----------------------------
class PaperBroker:
    def __init__(self):
        self.positions = {}  # symbol -> {'qty':int, 'avg':float}
        self.orders = {}     # order_id -> order dict
        self._next = 1
    def _oid(self):
        oid = f"P{self._next}"
        self._next += 1
        return oid
    def place_market_buy(self, symbol, qty, price):
        oid = self._oid()
        pos = self.positions.get(symbol, {'qty':0,'avg':0.0})
        total_qty = pos['qty'] + qty
        avg = price if pos['qty']==0 else (pos['avg']*pos['qty'] + price*qty)/total_qty
        self.positions[symbol] = {'qty': total_qty, 'avg': avg}
        self.orders[oid] = {'id':oid, 'type':'BUY', 'symbol':symbol, 'qty':qty, 'price':price, 'status':'FILLED'}
        log(f"[Paper] BUY {symbol} qty={qty} @ {price} (id={oid})")
        return oid
    def place_market_sell(self, symbol, qty, price):
        oid = self._oid()
        pos = self.positions.get(symbol, {'qty':0,'avg':0.0})
        sell_qty = min(qty, pos['qty'])
        pos['qty'] = pos['qty'] - sell_qty
        if pos['qty']==0:
            pos['avg'] = 0.0
        self.positions[symbol] = pos
        self.orders[oid] = {'id':oid, 'type':'SELL', 'symbol':symbol, 'qty':sell_qty, 'price':price, 'status':'FILLED'}
        log(f"[Paper] SELL {symbol} qty={sell_qty} @ {price} (id={oid})")
        return oid
    def get_positions(self):
        rows = []
        for s,p in self.positions.items():
            if p['qty']!=0:
                rows.append({'tradingsymbol':s, 'quantity':p['qty'], 'avg_price':p['avg'], 'pnl':0.0})
        return rows
    def cancel_all(self):
        # nothing to cancel in simulation, but keep behaviour
        cancelled = list(self.orders.keys())
        self.orders.clear()
        log(f"[Paper] Cancelled orders: {cancelled}")
        return cancelled
    def close_all(self):
        closed = []
        for s,p in list(self.positions.items()):
            if p['qty']!=0:
                closed.append({'symbol':s,'qty':p['qty']})
                log(f"[Paper] Closed position {s} qty={p['qty']}")
                self.positions[s] = {'qty':0,'avg':0.0}
        return closed

# ---------------------------- TRADING ENGINE ----------------------------
class TradingEngine(threading.Thread):
    def __init__(self, kite=None, broker=None, cfg=None):
        super().__init__(daemon=True)
        self.kite = kite
        self.broker = broker or PaperBroker()
        self.cfg = cfg or {}
        self.running = False
        self._stop = threading.Event()
        # runtime state
        self.entry_price = None
        self.qty = 0
        self.entry_time = None
        self.sl_trigger = None
        self.instant_sl = None
        self.peak = None

    def stop(self):
        self._stop.set()

    def stopped(self):
        return self._stop.is_set()

    def compute_qty(self, ltp):
        exposure = float(self.cfg.get('exposure', DEFAULT_EXPOSURE))
        leverage = float(self.cfg.get('leverage', DEFAULT_LEVERAGE))
        if ltp <= 0:
            return 0
        qty = int((exposure * leverage) // ltp)
        return max(1, qty)

    def get_ltp(self, ref):
        if self.kite:
            try:
                d = self.kite.ltp(ref)
                return list(d.values())[0]['last_price']
            except Exception as e:
                log(f"LTP fetch error: {e}")
                return None
        else:
            return float(self.cfg.get('sim_ltp', 100.0))*(1+np.random.normal(0,0.001))

    def place_buy(self, exchange, tradingsymbol, qty, ltp):
        # respect global emergency stop
        if not st.session_state.trading_active:
            log("Order blocked: trading halted (emergency/manual stop).")
            return None
        if st.session_state.mode == "Paper":
            return self.broker.place_market_buy(tradingsymbol, qty, ltp)
        else:
            try:
                oid = self.kite.place_order(
                    tradingsymbol=tradingsymbol,
                    exchange=exchange,
                    transaction_type=self.kite.TRANSACTION_TYPE_BUY,
                    quantity=qty,
                    order_type=self.kite.ORDER_TYPE_MARKET,
                    product=self.kite.PRODUCT_MIS,
                    variety=self.kite.VARIETY_REGULAR
                )
                log(f"[Live] BUY placed id={oid} qty={qty}")
                return oid
            except Exception as e:
                log(f"[Live] BUY failed: {e}")
                send_sms(f"BUY failed: {e}")
                return None

    def place_sell(self, exchange, tradingsymbol, qty, ltp):
        if st.session_state.mode == "Paper":
            return self.broker.place_market_sell(tradingsymbol, qty, ltp)
        else:
            try:
                oid = self.kite.place_order(
                    tradingsymbol=tradingsymbol,
                    exchange=exchange,
                    transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                    quantity=qty,
                    order_type=self.kite.ORDER_TYPE_MARKET,
                    product=self.kite.PRODUCT_MIS,
                    variety=self.kite.VARIETY_REGULAR
                )
                log(f"[Live] SELL placed id={oid} qty={qty}")
                return oid
            except Exception as e:
                log(f"[Live] SELL failed: {e}")
                send_sms(f"SELL failed: {e}")
                return None

    def cancel_all_orders(self):
        if st.session_state.mode == "Paper":
            return self.broker.cancel_all()
        else:
            try:
                orders = self.kite.orders()
                cancelled = []
                for o in orders:
                    status = (o.get('status') or "").upper()
                    if status in ('OPEN', 'TRIGGER PENDING', 'PENDING'):
                        try:
                            self.kite.cancel_order(order_id=o['order_id'], variety=o.get('variety', self.kite.VARIETY_REGULAR))
                            cancelled.append(o['order_id'])
                        except Exception as e:
                            log(f"Cancel failed {o.get('order_id')}: {e}")
                log(f"Cancelled pending orders: {cancelled}")
                return cancelled
            except Exception as e:
                log(f"Cancel all orders error: {e}")
                return []

    def exit_all_positions(self):
        if st.session_state.mode == "Paper":
            return self.broker.close_all()
        else:
            try:
                pos = self.kite.positions()
                net = pos.get('net', []) if isinstance(pos, dict) else []
                closed = []
                for p in net:
                    qty = int(p.get('quantity', 0) or 0)
                    if qty == 0:
                        continue
                    tx = self.kite.TRANSACTION_TYPE_SELL if qty > 0 else self.kite.TRANSACTION_TYPE_BUY
                    q = abs(qty)
                    try:
                        order = self.kite.place_order(
                            tradingsymbol=p.get('tradingsymbol'),
                            exchange=p.get('exchange') or 'NSE',
                            transaction_type=tx,
                            quantity=q,
                            order_type=self.kite.ORDER_TYPE_MARKET,
                            product=self.kite.PRODUCT_MIS,
                            variety=self.kite.VARIETY_REGULAR
                        )
                        closed.append({'symbol':p.get('tradingsymbol'), 'qty':q, 'order':order})
                    except Exception as e:
                        log(f"Failed to close {p.get('tradingsymbol')}: {e}")
                log(f"Close orders placed: {closed}")
                return closed
            except Exception as e:
                log(f"Exit all positions error: {e}")
                return []

    def run(self):
        log("Engine thread started")
        self.running = True
        cfg = self.cfg
        exchange = cfg.get('exchange', 'NSE')
        tradingsymbol = cfg.get('tradingsymbol')
        symbol_ref = f"{exchange}:{tradingsymbol}"
        sim_ltp = float(cfg.get('sim_ltp', 100.0))
        sl_pct = float(cfg.get('sl_pct', DEFAULT_SL_PCT))
        instant_pct = float(cfg.get('instant_sl_pct', DEFAULT_INSTANT_SL_PCT))
        trail_pct = float(cfg.get('trail_pct', DEFAULT_TRAIL_PCT))
        start_time = cfg.get('start_time', dtime(9,15))
        squareoff = cfg.get('squareoff_time', dtime(15,15))

        first_candle_used = False

        while not self.stopped():
            try:
                now = local_now()
                if now.weekday() >= 5:
                    time.sleep(5)
                    continue

                # get last 6 5-minute candles (last 30 minutes)
                df = pd.DataFrame()
                if self.kite and cfg.get('instrument_token') and st.session_state.mode == "Live":
                    try:
                        # historical_data uses naive datetimes; pass aware converted to naive in UTC? Kite expects naive local datetimes -> use dates/times directly
                        end = now
                        start = end - timedelta(minutes=5*CANDLES_COUNT)
                        data = self.kite.historical_data(int(cfg.get('instrument_token')), start, end, '5minute')
                        df = pd.DataFrame(data)
                    except Exception as e:
                        log(f"OHLC fetch error: {e}")
                        df = pd.DataFrame()
                else:
                    # simulate candles around sim_ltp
                    base = sim_ltp
                    times = [now - timedelta(minutes=5*(CANDLES_COUNT - i)) for i in range(CANDLES_COUNT)]
                    prices = base * (1 + np.random.normal(0, 0.001, CANDLES_COUNT))
                    df = pd.DataFrame({'date': times, 'open': prices, 'high': prices*(1+0.001), 'low': prices*(1-0.001), 'close': prices, 'volume': np.random.randint(100,1000,CANDLES_COUNT)})

                if not df.empty and 'volume' in df.columns:
                    typical = (df['high'] + df['low'] + df['close'])/3.0
                    df['cum_vol'] = df['volume'].cumsum()
                    df['cum_vp'] = (typical * df['volume']).cumsum()
                    df['vwap'] = df['cum_vp'] / df['cum_vol']

                # determine ltp
                ltp = self.get_ltp(symbol_ref) or (df['close'].iloc[-1] if not df.empty else sim_ltp)
                qty = self.compute_qty(ltp)

                # Entry logic: 9:15-9:30 first candle bullish & rising -> buy
                if len(df) >= 2:
                    first_candle = df.iloc[0]
                    latest = df.iloc[-1]
                    prev = df.iloc[-2]
                    if (not first_candle_used) and (start_time <= now.time() <= (datetime.combine(now.date(), start_time) + timedelta(minutes=15)).time()):
                        if float(first_candle['close']) > float(first_candle['open']):
                            if ltp > float(first_candle['close']):
                                if self.entry_price is None:
                                    oid = self.place_buy(exchange, tradingsymbol, qty, ltp)
                                    if oid is not None:
                                        self.entry_price = ltp
                                        self.qty = qty
                                        self.entry_time = now
                                        self.peak = ltp
                                        self.instant_sl = round(self.entry_price * (1 - instant_pct/100),2)
                                        self.sl_trigger = round(self.entry_price * (1 - sl_pct/100),2)
                                        send_sms(f"BUY executed {tradingsymbol} @{self.entry_price} qty={self.qty} SL={self.sl_trigger} InstantSL={self.instant_sl}")
                                        first_candle_used = True

                    # after 9:30: uptrend AND above VWAP
                    if self.entry_price is None and now.time() > dtime(9,30):
                        try:
                            if float(latest['close']) > float(prev['close']) and float(latest['close']) > float(latest.get('vwap', -1)):
                                oid = self.place_buy(exchange, tradingsymbol, qty, ltp)
                                if oid is not None:
                                    self.entry_price = ltp
                                    self.qty = qty
                                    self.entry_time = now
                                    self.peak = ltp
                                    self.instant_sl = round(self.entry_price * (1 - instant_pct/100),2)
                                    self.sl_trigger = round(self.entry_price * (1 - sl_pct/100),2)
                                    send_sms(f"BUY executed {tradingsymbol} @{self.entry_price} qty={self.qty} SL={self.sl_trigger} InstantSL={self.instant_sl}")
                        except Exception as e:
                            log(f"After-9:30 logic error: {e}")

                # Manage open position
                if self.entry_price is not None:
                    if ltp > self.peak:
                        self.peak = ltp
                    # Instant SL
                    if ltp <= self.instant_sl:
                        log(f"Instant SL hit @{ltp}")
                        send_sms(f"Instant SL hit @{ltp} for {tradingsymbol}")
                        self.place_sell(exchange, tradingsymbol, self.qty, ltp)
                        self.cancel_all_orders()
                        self.exit_all_positions()
                        # stop engine loop after flatten
                        st.session_state.trading_active = False
                        break
                    # initial SL
                    if ltp <= self.sl_trigger:
                        log(f"Initial SL hit @{ltp}")
                        send_sms(f"Initial SL hit @{ltp} for {tradingsymbol}")
                        self.place_sell(exchange, tradingsymbol, self.qty, ltp)
                        self.cancel_all_orders()
                        self.exit_all_positions()
                        st.session_state.trading_active = False
                        break
                    # breakeven and trailing
                    profit_pct = (ltp - self.entry_price)/self.entry_price*100
                    if profit_pct >= (sl_pct * 3.0):
                        # move to breakeven
                        try:
                            # Try modify SL if live and sl order exists - best-effort (not tracked here)
                            self.sl_trigger = round(self.entry_price,2)
                            send_sms(f"Moved SL to breakeven for {tradingsymbol}")
                            log("Moved SL to breakeven")
                        except Exception as e:
                            log(f"Breakeven error: {e}")
                    # trailing
                    trailing_trigger = round(self.peak * (1 - trail_pct/100),2)
                    if trailing_trigger > self.sl_trigger:
                        self.sl_trigger = trailing_trigger
                        log(f"Trailing SL updated -> {trailing_trigger} (peak {self.peak})")
                        send_sms(f"Trailing SL updated -> {trailing_trigger} (peak {self.peak})")

                # Auto square-off at 15:15
                if now.time() >= dtime(15,15):
                    if self.entry_price is not None or st.session_state.trading_active:
                        log("Square-off time reached -> flattening")
                        send_sms("Square-off time reached - flattening positions")
                        self.cancel_all_orders()
                        self.exit_all_positions()
                        st.session_state.trading_active = False
                        break

                # Safety flatten at 15:20 is handled by the app-level watchdog (separate)
                # Loop sleep
                for _ in range(int(ENGINE_SLEEP/1)):
                    if self.stopped():
                        break
                    time.sleep(1)
            except Exception as e:
                log(f"Engine loop error: {e}")
                traceback.print_exc()
                send_sms(f"Engine error: {e}")
                # attempt to flatten on fatal error
                try:
                    self.cancel_all_orders()
                    self.exit_all_positions()
                except:
                    pass
                st.session_state.trading_active = False
                break

        self.running = False
        log("Engine thread ended")

# ---------------------------- STREAMLIT UI ----------------------------
st.set_page_config(page_title="Auto Intraday Trader (final)", layout="wide", initial_sidebar_state="expanded")
# minimal dark theme via CSS (entry fields keep default)
st.markdown(
    """
    <style>
    /* Dark background */
    .stApp {
        background: #0e1117;
        color: #d3d7de;
    }
    /* Make text areas and inputs still visible */
    .stTextInput, .stNumberInput, .stSelectbox, .stButton {
        background-color: #0e1117;
        color: #d3d7de;
    }
    .stSidebar .stTextInput, .stSidebar .stNumberInput { color: black; }
    /* Logger text area darker */
    .logger { background: #090b0f; color: #c7ccd3; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🔁 Auto Intraday MIS Trader — Final (IST, Fast2SMS)")

# Sidebar: connection & SMS
with st.sidebar:
    st.header("Connection & SMS")
    api_key = st.text_input("Kite API Key (env or paste)", value=API_KEY_ENV, type="password")
    api_secret = st.text_input("Kite API Secret", value=API_SECRET_ENV, type="password")
    st.markdown("**Kite login (once/day)** — click URL -> login -> copy request_token from redirect -> paste below")
    if KITE_AVAILABLE:
        temp_client = KiteConnect(api_key=api_key or API_KEY_ENV) if api_key or API_KEY_ENV else None
        if temp_client:
            login_url = temp_client.login_url()
            st.write(login_url)
    else:
        st.warning("kiteconnect not installed — Live mode disabled.")
    request_token = st.text_input("Paste request_token here (from Kite redirect)")
    if st.button("Generate & Save Access Token"):
        if not (api_key and api_secret and request_token):
            st.error("Provide API Key, API Secret and request_token")
        else:
            try:
                tmp = KiteConnect(api_key=api_key)
                data = tmp.generate_session(request_token.strip(), api_secret=api_secret.strip())
                safe_save_json(ACCESS_TOKEN_FILE, data)
                st.success("Saved access token")
                log("Access token saved")
            except Exception as e:
                st.error(f"Token generation failed: {e}")
                log(f"Token generation failed: {e}")

    st.markdown("---")
    st.subheader("Fast2SMS")
    fast2key = st.text_input("Fast2SMS API Key", value=FAST2SMS_KEY_ENV, type="password")
    sms_number = st.text_input("SMS Number (with country code, e.g., 9198...)", value=FAST2SMS_DEFAULT_NUMBER)
    st.caption("SMS alerts will be sent for every action (entry/exit/SL/flatten/expiry).")

# Build kite if connected
saved_access = safe_load_json(ACCESS_TOKEN_FILE)
kite = None
kite_connected = False
access_available = bool(saved_access and saved_access.get('access_token'))
if KITE_AVAILABLE and api_key:
    try:
        kite = KiteConnect(api_key=api_key)
        if access_available:
            try:
                kite.set_access_token(saved_access.get('access_token'))
                kite.profile()  # verify
                kite_connected = True
                log("Kite connected from saved token")
            except Exception as e:
                log(f"Kite token invalid/expired: {e}")
                kite_connected = False
                # alert user via SMS (if key present)
                if fast2key:
                    send_fast2sms("Access token expired or invalid for your Kite app. Please refresh.", fast2key, sms_number)
    except Exception as e:
        log(f"KiteConnect init error: {e}")
else:
    if not KITE_AVAILABLE:
        log("kiteconnect not available in environment.")
    else:
        log("Kite API key not provided yet.")

# expose runtime flags in session_state
st.session_state.mode = st.session_state.get('mode', 'Paper')
st.session_state.trading_active = st.session_state.get('trading_active', True)
st.session_state.fast2sms_key = fast2key
st.session_state.sms_number = sms_number

# Top bar: connection status and digital clock
colA, colB, colC = st.columns([2,2,6])
with colA:
    st.markdown("**Kite status**")
    if kite_connected:
        st.markdown("<span style='color: #00FF00; font-weight:bold'>● Connected</span>", unsafe_allow_html=True)
    else:
        st.markdown("<span style='color: #FF0000; font-weight:bold'>● Not connected</span>", unsafe_allow_html=True)
with colB:
    st.markdown("**Access token**")
    if access_available:
        st.markdown("<span style='color: #00FF00; font-weight:bold'>● Saved</span>", unsafe_allow_html=True)
    else:
        st.markdown("<span style='color: #FF0000; font-weight:bold'>● Not saved</span>", unsafe_allow_html=True)
with colC:
    # Digital clock (IST)
    clock_placeholder = st.empty()
    def update_clock():
        while True:
            try:
                clock_placeholder.markdown(f"<h3 style='text-align:right'>{local_now().strftime('%Y-%m-%d %H:%M:%S IST')}</h3>", unsafe_allow_html=True)
            except:
                pass
            time.sleep(CLOCK_REFRESH_SEC)
    if 'clock_thread' not in st.session_state:
        st.session_state['clock_thread'] = threading.Thread(target=update_clock, daemon=True)
        st.session_state['clock_thread'].start()

st.markdown("---")

# Main config: manual stock select and strategy params
left, right = st.columns([3,1])
with left:
    st.header("Strategy & Trading Controls")
    mode = st.selectbox("Mode (Paper / Live)", ["Paper", "Live"], index=0)
    st.session_state.mode = mode
    exchange = st.selectbox("Exchange", ["NSE", "BSE"], index=0)
    tradingsymbol = st.text_input("Trading symbol (exact, e.g., RELIANCE)", value="RELIANCE").strip().upper()
    instrument_token = st.text_input("Instrument token (numeric) - optional", value="")
    exposure = st.number_input("Exposure (₹)", value=DEFAULT_EXPOSURE)
    leverage = st.number_input("Leverage (auto 5x for MIS)", value=DEFAULT_LEVERAGE, step=1)
    sl_pct = st.number_input("Initial SL %", value=DEFAULT_SL_PCT, step=0.1)
    instant_sl_pct = st.number_input("Instant SL %", value=DEFAULT_INSTANT_SL_PCT, step=0.1)
    trail_pct = st.number_input("Trailing % after breakeven", value=DEFAULT_TRAIL_PCT, step=0.1)
    start_time = st.time_input("Auto-start time (IST) (if auto-start enabled)", value=dtime(9,15))
    squareoff_time = st.time_input("Square-off time (IST)", value=dtime(15,15))
    sim_ltp = st.number_input("Paper sim LTP", value=100.0)
    auto_start = st.checkbox("Auto-start when token present (and within trading window)", value=AUTO_START_DEFAULT)

with right:
    st.header("Quick Controls")
    if st.button("Start Engine (manual)"):
        # instantiate and run engine thread
        cfg = {
            'exchange': exchange,
            'tradingsymbol': tradingsymbol,
            'instrument_token': instrument_token,
            'exposure': exposure,
            'leverage': leverage,
            'sl_pct': sl_pct,
            'instant_sl_pct': instant_sl_pct,
            'trail_pct': trail_pct,
            'start_time': start_time,
            'squareoff_time': squareoff_time,
            'sim_ltp': sim_ltp
        }
        eng = TradingEngine(kite if (mode=="Live" and kite_connected) else None, broker=PaperBroker() if mode=="Paper" else None, cfg=cfg)
        st.session_state.engine = eng
        eng.start()
        st.success("Engine started (background)")
        send_sms(f"Engine started for {tradingsymbol} mode={mode}")

    if st.button("Stop Engine (manual)"):
        eng = st.session_state.get('engine')
        if eng:
            eng.stop()
            st.session_state.trading_active = False
            st.success("Engine stop requested")
            send_sms("Engine stop requested (manual) — flattening positions")
            # flatten immediately
            try:
                eng.cancel_all_orders()
                eng.exit_all_positions()
            except Exception as e:
                log(f"Stop flatten error: {e}")

    if st.button("Emergency STOP (flatten now)"):
        st.session_state.trading_active = False
        eng = st.session_state.get('engine')
        if eng:
            try:
                eng.cancel_all_orders()
                eng.exit_all_positions()
            except Exception as e:
                log(f"Emergency flatten error: {e}")
        send_sms("Emergency STOP pressed — all orders cancelled and positions exited.")
        st.error("Emergency STOP executed")

    st.markdown("---")
    st.markdown("**Fast2SMS & logs**")
    st.text_input("Fast2SMS API Key (sidebar also)", value=fast2key, key="fast2sms_key_input", type="password")
    st.text_input("Alert mobile number (with country code)", value=sms_number, key="sms_number_input")
    if st.button("Save Fast2SMS to session"):
        st.session_state.fast2sms_key = st.session_state.get("fast2sms_key_input") or FAST2SMS_KEY_ENV
        st.session_state.sms_number = st.session_state.get("sms_number_input") or FAST2SMS_DEFAULT_NUMBER
        st.success("Fast2SMS saved in session")

st.markdown("---")

# Chart & P&L layout
col_chart, col_info = st.columns([3,1])
with col_chart:
    st.subheader("Live 5-min chart — last 30 minutes (6 candles)")
    chart_placeholder = st.empty()
with col_info:
    st.subheader("Live P&L & Status")
    pnl_placeholder = st.empty()
    status_placeholder = st.empty()
    logs_placeholder = st.empty()

# Chart updater thread
def chart_updater():
    while True:
        try:
            eng = st.session_state.get('engine')
            cfg = {
                'exchange': exchange,
                'tradingsymbol': tradingsymbol,
                'instrument_token': instrument_token,
                'sim_ltp': sim_ltp
            }
            df = pd.DataFrame()
            if eng and eng.kite and cfg.get('instrument_token') and st.session_state.mode == "Live":
                try:
                    end = local_now()
                    start = end - timedelta(minutes=5*CANDLES_COUNT)
                    data = eng.kite.historical_data(int(cfg.get('instrument_token')), start, end, '5minute')
                    df = pd.DataFrame(data)
                except Exception as e:
                    log(f"Chart OHLC fetch error: {e}")
                    df = pd.DataFrame()
            else:
                # try one-shot kite historical if possible
                if KITE_AVAILABLE and kite and cfg.get('instrument_token') and st.session_state.mode == "Live":
                    try:
                        end = local_now()
                        start = end - timedelta(minutes=5*CANDLES_COUNT)
                        data = kite.historical_data(int(cfg.get('instrument_token')), start, end, '5minute')
                        df = pd.DataFrame(data)
                    except Exception as e:
                        df = pd.DataFrame()
                # fallback to simulated data
            if df.empty:
                base = float(sim_ltp)
                times = [local_now() - timedelta(minutes=5*(CANDLES_COUNT - i)) for i in range(CANDLES_COUNT)]
                prices = base * (1 + np.random.normal(0,0.0015,CANDLES_COUNT))
                df = pd.DataFrame({'date':times,'open':prices,'high':prices*(1+0.001),'low':prices*(1-0.001),'close':prices,'volume':np.random.randint(100,1000,CANDLES_COUNT)})
            if 'date' in df.columns:
                df = df.sort_values('date').tail(CANDLES_COUNT)
            else:
                df = df.tail(CANDLES_COUNT)
            typical = (df['high'] + df['low'] + df['close'])/3.0
            df['cum_vol'] = df['volume'].cumsum()
            df['cum_vp'] = (typical * df['volume']).cumsum()
            df['vwap'] = df['cum_vp'] / df['cum_vol']

            fig = go.Figure(data=[go.Candlestick(x=df['date'], open=df['open'], high=df['high'], low=df['low'], close=df['close'], name='candles')])
            fig.add_trace(go.Scatter(x=df['date'], y=df['vwap'], name='VWAP', mode='lines'))
            # Add entry / SL markers if engine has them
            eng_local = st.session_state.get('engine')
            if eng_local:
                ep = getattr(eng_local, 'entry_price', None)
                et = getattr(eng_local, 'entry_time', None)
                sl = getattr(eng_local, 'sl_trigger', None)
                inst = getattr(eng_local, 'instant_sl', None)
                if ep and et:
                    fig.add_trace(go.Scatter(x=[et], y=[ep], mode='markers', marker_symbol='triangle-up', marker_color='green', marker_size=12, name='BUY'))
                if sl:
                    fig.add_hline(y=sl, line_dash="dash", line_color="red", annotation_text="SL", annotation_position="top left")
                if inst:
                    fig.add_hline(y=inst, line_dash="dot", line_color="orange", annotation_text="Instant SL", annotation_position="top left")

            fig.update_layout(xaxis_rangeslider_visible=False, margin=dict(l=10,r=10,t=30,b=10), height=520, template='plotly_dark')
            chart_placeholder.plotly_chart(fig, use_container_width=True)

            # P&L and status block
            if eng_local and getattr(eng_local,'entry_price',None):
                ltp = eng_local.get_ltp(f"{eng_local.cfg.get('exchange', 'NSE')}:{eng_local.cfg.get('tradingsymbol')}")
                if ltp is None:
                    ltp = eng_local.entry_price
                unreal = (ltp - eng_local.entry_price) * eng_local.qty
                color = "green" if unreal > 0 else ("red" if unreal < 0 else "blue")
                pnl_placeholder.markdown(f"<h3 style='color:{color};'>Unreal P&L: ₹{unreal:.2f}</h3>", unsafe_allow_html=True)
                status_placeholder.markdown(f"Mode: **{st.session_state.mode}**  \nSymbol: **{eng_local.cfg.get('tradingsymbol')}**  \nEntry: ₹{eng_local.entry_price}  \nQty: {eng_local.qty}")
            else:
                # show account positions if live connected
                if KITE_AVAILABLE and kite and st.session_state.mode == "Live":
                    try:
                        pos = kite.positions()
                        net = pos.get('net', []) if isinstance(pos, dict) else []
                        if net:
                            total = 0.0
                            rows = []
                            for p in net:
                                q = int(p.get('quantity',0) or 0)
                                pnl = float(p.get('pnl',0) or 0)
                                total += pnl
                                rows.append({'Symbol':p.get('tradingsymbol'), 'Qty':q, 'PnL':pnl})
                            color = "green" if total>0 else ("red" if total<0 else "blue")
                            pnl_placeholder.markdown(f"<h3 style='color:{color};'>Total P&L: ₹{total:.2f}</h3>", unsafe_allow_html=True)
                            status_placeholder.dataframe(pd.DataFrame(rows))
                        else:
                            pnl_placeholder.text("No open positions")
                            status_placeholder.text("No active position")
                    except Exception as e:
                        status_placeholder.text("Kite positions not available")
                        pnl_placeholder.text("P&L not available")
                else:
                    pnl_placeholder.text("No active position")
                    status_placeholder.text("Mode: " + str(st.session_state.mode))
            logs_placeholder.text_area("Logs", value="\n".join(st.session_state.logger[-200:]), height=300)
        except Exception as e:
            log(f"chart_updater error: {e}")
        time.sleep(CHART_REFRESH_SEC)

# start chart thread once
if 'chart_thread' not in st.session_state:
    st.session_state['chart_thread'] = threading.Thread(target=chart_updater, daemon=True)
    st.session_state['chart_thread'].start()

# ---------------------------- Auto-start logic ----------------------------
def maybe_auto_start():
    if auto_start and access_available and st.session_state.mode == "Live" and kite:
        # If within minutes before start_time, start engine
        now = local_now()
        if now.time() >= start_time and now.time() <= (datetime.combine(now.date(), start_time) + timedelta(minutes=30)).time():
            if not st.session_state.get('engine'):
                cfg = {
                    'exchange': exchange,
                    'tradingsymbol': tradingsymbol,
                    'instrument_token': instrument_token,
                    'exposure': exposure,
                    'leverage': leverage,
                    'sl_pct': sl_pct,
                    'instant_sl_pct': instant_sl_pct,
                    'trail_pct': trail_pct,
                    'start_time': start_time,
                    'squareoff_time': squareoff_time,
                    'sim_ltp': sim_ltp
                }
                eng = TradingEngine(kite if st.session_state.mode=="Live" else None, broker=PaperBroker() if st.session_state.mode=="Paper" else None, cfg=cfg)
                st.session_state.engine = eng
                eng.start()
                log("Auto-started engine")
                send_sms("Engine auto-started")

# call maybe_auto_start on each page load (best effort)
try:
    maybe_auto_start()
except Exception:
    pass

# ---------------------------- Safety Flatten watchdog (15:20) ----------------------------
def safety_flatten_watchdog():
    now = local_now()
    if now.time() >= dtime(15,20):
        log("15:20 safety flatten triggered")
        eng = st.session_state.get('engine')
        if eng:
            try:
                eng.cancel_all_orders()
            except:
                pass
            try:
                eng.exit_all_positions()
            except:
                pass
        send_sms("⚠️ Auto safety flatten executed at 15:20 IST")
        st.session_state.trading_active = False
        # show a message
        st.warning("15:20 Safety Flatten executed - all pending orders cancelled and positions exited.")

# call watchdog once per page load
try:
    safety_flatten_watchdog()
except Exception:
    pass

st.markdown("---")
st.caption("Test first in Paper mode. For Live mode, generate access token using Kite login and paste request_token in sidebar. Always test with small exposure before going live.")
