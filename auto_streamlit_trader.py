# auto_intraday_streamlit_pro.py
"""
Auto Intraday Streamlit Trader — PRO (final)
- Zerodha Kite Connect (manual request_token once/day; saves access token; SMS alert on expiry)
- Manual stock selector (default RELIANCE)
- Intraday MIS trading with 5x leverage auto-quantity
- Paper / Live mode (manual toggle)
- Manual Start, Manual Stop, Emergency STOP (flatten & cancel)
- Instant SL, Initial SL, Trailing SL
- If SL hit or stop pressed -> cancel all untriggered orders + exit triggered positions
- Live P&L display (color-coded)
- Live 5-minute candlestick chart for last 30 minutes (6 candles) with VWAP + markers
- Digital clock (IST) displayed
- Dark background theme (except entry fields)
- Kite connected & access-token indicators (green/red dot)
- Auto-start option (if enabled & conditions met)
- Fast2SMS alerts (placeholder key included)
- 15:15 square-off and 15:20 safety flatten watchdog
"""

import os
import json
import time
import threading
import traceback
from datetime import datetime, timedelta, time as dtime

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import requests

# optional kite import
try:
    from kiteconnect import KiteConnect
    KITE_AVAILABLE = True
except Exception:
    KiteConnect = None
    KITE_AVAILABLE = False

# -------------------- TIMEZONE (IST) --------------------
try:
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

# -------------------- CONFIG DEFAULTS --------------------
ACCESS_TOKEN_FILE = os.getenv("ACCESS_TOKEN_FILE", "access_token.json")
API_KEY_ENV = os.getenv("ZK_API_KEY", "")
API_SECRET_ENV = os.getenv("ZK_API_SECRET", "")

# Fast2SMS placeholder (user replaces with real key in sidebar)
FAST2SMS_API_KEY_PLACEHOLDER = "your_fast2sms_key_here"
FAST2SMS_DEFAULT_NUMBER = os.getenv("FAST2SMS_NUMBER", "")

DEFAULT_EXPOSURE = 50000.0
DEFAULT_LEVERAGE = 5
DEFAULT_SL_PCT = 3.0
DEFAULT_INSTANT_SL_PCT = 1.5
DEFAULT_TRAIL_PCT = 3.0
DEFAULT_START_TIME = dtime(9,15)
DEFAULT_SQUAREOFF = dtime(15,15)

CANDLES_COUNT = 6  # last 30 minutes -> 6 candles (5-min each)
ENGINE_SLEEP = 3
CHART_REFRESH_SEC = 6
CLOCK_REFRESH_SEC = 1

# -------------------- UTILITIES --------------------
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

# simple logger stored in session_state
if "logs" not in st.session_state:
    st.session_state["logs"] = []

def log(msg):
    ts = now_str()
    entry = f"[{ts}] {msg}"
    st.session_state["logs"].append(entry)
    # keep last 500 lines
    if len(st.session_state["logs"]) > 500:
        st.session_state["logs"] = st.session_state["logs"][-500:]

# -------------------- SMS (Fast2SMS) --------------------
def send_fast2sms(message, api_key, number):
    if not api_key or not number:
        log("Fast2SMS: missing key/number — SMS not sent.")
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
        log(f"Fast2SMS status={r.status_code} for message: {message[:60]}")
        return r.status_code == 200
    except Exception as e:
        log(f"Fast2SMS error: {e}")
        return False

def send_sms(message):
    key = st.session_state.get("fast2sms_key") or FAST2SMS_API_KEY_PLACEHOLDER
    num = st.session_state.get("sms_number") or FAST2SMS_DEFAULT_NUMBER
    ok = send_fast2sms(message, key, num)
    if not ok:
        log("SMS failed — check Fast2SMS key & number.")
    return ok

# -------------------- PAPER BROKER --------------------
class PaperBroker:
    def __init__(self):
        self.positions = {}   # symbol -> {'qty', 'avg'}
        self.orders = {}
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
        cancelled = list(self.orders.keys())
        self.orders.clear()
        log(f"[Paper] Cancelled orders: {cancelled}")
        return cancelled
    def close_all(self):
        closed = []
        for s,p in list(self.positions.items()):
            if p['qty']!=0:
                closed.append({'symbol':s,'qty':p['qty']})
                log(f"[Paper] Closed {s} qty={p['qty']}")
                self.positions[s] = {'qty':0,'avg':0.0}
        return closed

# -------------------- TRADING ENGINE --------------------
class TradingEngine(threading.Thread):
    def __init__(self, kite=None, broker=None, cfg=None):
        super().__init__(daemon=True)
        self.kite = kite
        self.broker = broker or PaperBroker()
        self.cfg = cfg or {}
        self._stop = threading.Event()
        # runtime
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
            return float(self.cfg.get('sim_ltp', 100.0)) * (1 + np.random.normal(0, 0.001))

    def place_buy(self, exchange, tradingsymbol, qty, ltp):
        # block if global stop
        if not st.session_state.get("trading_active", True):
            log("Order blocked: trading halted (emergency/manual stop).")
            return None
        if st.session_state.get("mode","Paper") == "Paper":
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
                
# --------------------------------------------
# Rejection Handling for Buy Orders
# --------------------------------------------


def handle_order_rejection(kite, order_id, trading_symbol):
    """Cancel all pending orders and notify via SMS if any order is rejected."""
    try:
        orders = kite.orders()
        for o in orders:
            if o["order_id"] == order_id and o["status"].lower() == "rejected":
                # Get rejection reason if available
                reason = o.get("status_message", "Order rejected by exchange")
                
                # Cancel all pending/untriggered orders
                for pending in orders:
                    if pending["status"].lower() in ["trigger pending", "open", "validation pending"]:
                        kite.cancel_order(variety=pending["variety"], order_id=pending["order_id"])
                
                # Send SMS alert
                send_sms(f"⚠️ BUY ORDER REJECTED for {trading_symbol}. Reason: {reason}. "
                         f"All pending orders cancelled.")

                st.error(f"🚫 Order Rejected: {reason}")
                return True  # indicates rejection handled
        return False  # no rejection found
    except Exception as e:
        st.warning(f"Error checking rejection: {e}")
        return False
            

    def place_sell(self, exchange, tradingsymbol, qty, ltp):
        if st.session_state.get("mode","Paper") == "Paper":
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
        if st.session_state.get("mode","Paper") == "Paper":
            return self.broker.cancel_all()
        else:
            try:
                orders = self.kite.orders()
                cancelled = []
                for o in orders:
                    status = (o.get('status') or "").upper()
                    if status in ('OPEN','TRIGGER PENDING','PENDING'):
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
        if st.session_state.get("mode","Paper") == "Paper":
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
        log("Engine started")
        cfg = self.cfg
        exchange = cfg.get('exchange', 'NSE')
        tradingsymbol = cfg.get('tradingsymbol')
        symbol_ref = f"{exchange}:{tradingsymbol}"
        sim_ltp = float(cfg.get('sim_ltp', 100.0))
        sl_pct = float(cfg.get('sl_pct', DEFAULT_SL_PCT))
        instant_pct = float(cfg.get('instant_sl_pct', DEFAULT_INSTANT_SL_PCT))
        trail_pct = float(cfg.get('trail_pct', DEFAULT_TRAIL_PCT))
        start_time = cfg.get('start_time', DEFAULT_START_TIME)
        squareoff = cfg.get('squareoff_time', DEFAULT_SQUAREOFF)

        first_candle_used = False

        while not self.stopped():
            try:
                # skip weekends
                now = local_now()
                if now.weekday() >= 5:
                    time.sleep(5)
                    continue

                # fetch candles (last 30 min: 6 candles)
                df = pd.DataFrame()
                if self.kite and cfg.get('instrument_token') and st.session_state.get("mode") == "Live":
                    try:
                        end = now
                        start = end - timedelta(minutes=5*CANDLES_COUNT)
                        data = self.kite.historical_data(int(cfg.get('instrument_token')), start, end, '5minute')
                        df = pd.DataFrame(data)
                    except Exception as e:
                        log(f"OHLC fetch error: {e}")
                        df = pd.DataFrame()
                else:
                    base = sim_ltp
                    times = [now - timedelta(minutes=5*(CANDLES_COUNT - i)) for i in range(CANDLES_COUNT)]
                    prices = base * (1 + np.random.normal(0, 0.0015, CANDLES_COUNT))
                    df = pd.DataFrame({'date':times,'open':prices,'high':prices*(1+0.001),'low':prices*(1-0.001),'close':prices,'volume':np.random.randint(100,1000,CANDLES_COUNT)})

                if not df.empty and 'volume' in df.columns:
                    typical = (df['high'] + df['low'] + df['close'])/3.0
                    df['cum_vol'] = df['volume'].cumsum()
                    df['cum_vp'] = (typical * df['volume']).cumsum()
                    df['vwap'] = df['cum_vp'] / df['cum_vol']

                # get ltp
                ltp = self.get_ltp(symbol_ref) or (df['close'].iloc[-1] if not df.empty else sim_ltp)
                qty = self.compute_qty(ltp)

                # ENTRY logic
                if len(df) >= 2:
                    first_candle = df.iloc[0]
                    latest = df.iloc[-1]
                    prev = df.iloc[-2]
                    # 9:15-9:30 first candle bullish & rising
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
                                        self.instant_sl = round(self.entry_price * (1 - instant_pct/100), 2)
                                        self.sl_trigger = round(self.entry_price * (1 - sl_pct/100), 2)
                                        send_sms(f"BUY executed {tradingsymbol} @{self.entry_price} qty={self.qty} SL={self.sl_trigger} InstantSL={self.instant_sl}")
                                        first_candle_used = True

                    # after 9:30 uptrend & above VWAP
                    if self.entry_price is None and now.time() > dtime(9,30):
                        try:
                            if float(latest['close']) > float(prev['close']) and float(latest['close']) > float(latest.get('vwap', -1)):
                                oid = self.place_buy(exchange, tradingsymbol, qty, ltp)
                                if oid is not None:
                                    self.entry_price = ltp
                                    self.qty = qty
                                    self.entry_time = now
                                    self.peak = ltp
                                    self.instant_sl = round(self.entry_price * (1 - instant_pct/100), 2)
                                    self.sl_trigger = round(self.entry_price * (1 - sl_pct/100), 2)
                                    send_sms(f"BUY executed {tradingsymbol} @{self.entry_price} qty={self.qty} SL={self.sl_trigger} InstantSL={self.instant_sl}")
                        except Exception as e:
                            log(f"After-9:30 logic error: {e}")

                # POST-entry management
                if self.entry_price is not None:
                    if ltp > self.peak:
                        self.peak = ltp
                    # instant SL
                    if ltp <= self.instant_sl:
                        log(f"Instant SL hit @{ltp}")
                        send_sms(f"Instant SL hit @{ltp} for {tradingsymbol}")
                        self.place_sell(exchange, tradingsymbol, self.qty, ltp)
                        self.cancel_all_orders()
                        self.exit_all_positions()
                        st.session_state["trading_active"] = False
                        break
                    # initial SL
                    if ltp <= self.sl_trigger:
                        log(f"Initial SL hit @{ltp}")
                        send_sms(f"Initial SL hit @{ltp} for {tradingsymbol}")
                        self.place_sell(exchange, tradingsymbol, self.qty, ltp)
                        self.cancel_all_orders()
                        self.exit_all_positions()
                        st.session_state["trading_active"] = False
                        break
                    # breakeven & trailing
                    profit_pct = (ltp - self.entry_price)/self.entry_price*100
                    if profit_pct >= (sl_pct * 3.0):
                        try:
                            self.sl_trigger = round(self.entry_price, 2)
                            send_sms(f"Moved SL to breakeven for {tradingsymbol}")
                            log("Moved SL to breakeven")
                        except Exception as e:
                            log(f"Breakeven error: {e}")
                    trailing_trigger = round(self.peak * (1 - trail_pct/100), 2)
                    if trailing_trigger > self.sl_trigger:
                        self.sl_trigger = trailing_trigger
                        log(f"Trailing SL updated -> {trailing_trigger} (peak {self.peak})")
                        send_sms(f"Trailing SL updated -> {trailing_trigger} (peak {self.peak})")

                # auto square-off at 15:15
                if now.time() >= dtime(15,15):
                    log("Square-off: flattening positions")
                    send_sms("Square-off time reached - flattening positions")
                    self.cancel_all_orders()
                    self.exit_all_positions()
                    st.session_state["trading_active"] = False
                    break

                # sleep small interval
                for _ in range(int(ENGINE_SLEEP/1)):
                    if self.stopped():
                        break
                    time.sleep(1)

            except Exception as e:
                log(f"Engine loop error: {e}")
                traceback.print_exc()
                send_sms(f"Engine error: {e}")
                try:
                    self.cancel_all_orders()
                    self.exit_all_positions()
                except:
                    pass
                st.session_state["trading_active"] = False
                break

        log("Engine ended")

# -------------------- STREAMLIT UI & THREADS --------------------
st.set_page_config(page_title="Auto Intraday Trader — PRO", layout="wide")
# Dark theme CSS: keep inputs readable (white text on dark backgrounds; inputs remain default)
st.markdown("""
<style>
/* App background and text */
.stApp { background: #0b0f14; color: #e6eef6; }

/* Headings/buttons bold and visible */
h1, h2, h3, .big { color: #ffffff; font-weight:700; }

/* Make sure labels on dark backgrounds are white bold */
.label-dark { color: #ffffff; font-weight:700; }

/* If any widget gets white background, make text black bold */
input, .stButton { font-weight:700; color: #000000; }

/* Logger area look */
.logger-box { background:#071018; color:#d9e6f2; padding:8px; border-radius:6px; }
</style>
""", unsafe_allow_html=True)

st.title("🔁 Auto Intraday MIS Trader — PRO (IST)")

# Sidebar: Kite + Fast2SMS
with st.sidebar:
    st.header("Connection & Alerts")
    api_key = st.text_input("Kite API Key (paste or set env)", value=API_KEY_ENV, type="password")
    api_secret = st.text_input("Kite API Secret", value=API_SECRET_ENV, type="password")
    st.markdown("**Kite login (once/day)** — click URL -> login -> copy request_token from redirect -> paste below")
    if KITE_AVAILABLE and api_key:
        try:
            temp = KiteConnect(api_key=api_key)
            st.write("Kite Login URL (open in new tab):")
            st.code(temp.login_url())
        except Exception as e:
            st.write("Kite login URL unavailable:", e)
    else:
        if not KITE_AVAILABLE:
            st.warning("kiteconnect not installed — Live mode disabled.")
    request_token = st.text_input("Paste request_token (from Kite redirect)", value="")
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
                send_sms("Access token generated and saved.")
            except Exception as e:
                st.error(f"Token gen failed: {e}")
                log(f"Token gen failed: {e}")
    st.markdown("---")
    st.subheader("Fast2SMS (alerts)")
    fastkey = st.text_input("Fast2SMS API Key (paste)", value=FAST2SMS_API_KEY_PLACEHOLDER, type="password")
    sms_number = st.text_input("Alert mobile number (with country code, e.g., 9198...)", value=FAST2SMS_DEFAULT_NUMBER)
    if st.button("Save SMS Settings"):
        st.session_state["fast2sms_key"] = fastkey
        st.session_state["sms_number"] = sms_number
        st.success("SMS settings saved to session")
        log("Fast2SMS settings saved")

# Build Kite object if possible
saved = safe_load_json(ACCESS_TOKEN_FILE)
access_available = bool(saved and saved.get("access_token"))
kite = None
kite_connected = False
if KITE_AVAILABLE and api_key:
    try:
        kite = KiteConnect(api_key=api_key)
        if access_available:
            try:
                kite.set_access_token(saved.get("access_token"))
                kite.profile()
                kite_connected = True
                log("Kite connected from saved token")
            except Exception as e:
                kite_connected = False
                log(f"Kite token invalid/expired: {e}")
                # alert by sms
                if st.session_state.get("fast2sms_key"):
                    send_fast2sms("Kite access token invalid/expired. Please refresh.", st.session_state.get("fast2sms_key"), st.session_state.get("sms_number"))
    except Exception as e:
        log(f"KiteConnect init error: {e}")

# Top row: indicators + digital clock
col1, col2, col3 = st.columns([1,1,4])
with col1:
    st.markdown("**Kite**")
    if kite_connected:
        st.markdown("<span style='color:#00ff00; font-weight:700'>● Connected</span>", unsafe_allow_html=True)
    else:
        st.markdown("<span style='color:#ff3333; font-weight:700'>● Not connected</span>", unsafe_allow_html=True)
with col2:
    st.markdown("**Access token**")
    if access_available:
        st.markdown("<span style='color:#00ff00; font-weight:700'>● Saved</span>", unsafe_allow_html=True)
    else:
        st.markdown("<span style='color:#ff3333; font-weight:700'>● Not saved</span>", unsafe_allow_html=True)
with col3:
    clock_placeholder = st.empty()
    def clock_loop():
        while True:
            try:
                clock_placeholder.markdown(f"<h3 style='text-align:right; color:#ffffff; font-weight:700'>{local_now().strftime('%Y-%m-%d %H:%M:%S IST')}</h3>", unsafe_allow_html=True)
            except:
                pass
            time.sleep(CLOCK_REFRESH_SEC)
    if 'clock_thread' not in st.session_state:
        st.session_state['clock_thread'] = threading.Thread(target=clock_loop, daemon=True)
        st.session_state['clock_thread'].start()

st.markdown("---")

# Main config (left) & controls (right)
left, right = st.columns([3,1])
with left:
    st.subheader("Strategy Configuration")
    mode = st.selectbox("Mode (Paper / Live)", ["Paper", "Live"], index=0)
    st.session_state["mode"] = mode
    exchange = st.selectbox("Exchange", ["NSE", "BSE"], index=0)
    tradingsymbol = st.text_input("Trading symbol (exact)", value="RELIANCE").strip().upper()
    instrument_token = st.text_input("Instrument token (numeric) - optional", value="")
    exposure = st.number_input("Exposure (₹)", value=DEFAULT_EXPOSURE)
    leverage = st.number_input("Leverage (auto 5x MIS)", value=DEFAULT_LEVERAGE, step=1)
    sl_pct = st.number_input("Initial SL %", value=DEFAULT_SL_PCT, step=0.1)
    instant_sl_pct = st.number_input("Instant SL %", value=DEFAULT_INSTANT_SL_PCT, step=0.1)
    trail_pct = st.number_input("Trailing % after breakeven", value=DEFAULT_TRAIL_PCT, step=0.1)
    start_time = st.time_input("Auto-start time (IST)", value=DEFAULT_START_TIME)
    squareoff_time = st.time_input("Square-off time (IST)", value=DEFAULT_SQUAREOFF)
    sim_ltp = st.number_input("Paper sim LTP (if Paper mode or missing LTP)", value=100.0)
    auto_start = st.checkbox("Auto-start (when token present & within window)", value=False)

with right:
    st.subheader("Engine Controls")
    if st.button("Start Engine (manual)"):
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
        st.session_state["engine"] = eng
        st.session_state["trading_active"] = True
        eng.start()
        log("Engine started (manual)")
        send_sms(f"Engine started for {tradingsymbol} mode={mode}")
    if st.button("Stop Engine (manual)"):
        eng = st.session_state.get("engine")
        st.session_state["trading_active"] = False
        if eng:
            try:
                eng.cancel_all_orders()
            except:
                pass
            try:
                eng.exit_all_positions()
            except:
                pass
            eng.stop()
        log("Engine stop requested (manual)")
        send_sms("Engine stop requested (manual) - flattened")
        st.success("Engine stop requested and flatten attempted.")
    if st.button("Emergency STOP (flatten now)"):
        eng = st.session_state.get("engine")
        st.session_state["trading_active"] = False
        if eng:
            try:
                eng.cancel_all_orders()
            except:
                pass
            try:
                eng.exit_all_positions()
            except:
                pass
            eng.stop()
        send_sms("Emergency STOP pressed — all orders cancelled and positions exited.")
        log("Emergency STOP executed")
        st.error("Emergency STOP executed")

st.markdown("---")

# Chart and P&L layout
col_chart, col_info = st.columns([3,1])
with col_chart:
    st.subheader("Live 5-min Candles (last 30 minutes)")
    chart_placeholder = st.empty()
with col_info:
    st.subheader("Live P&L & Status")
    pnl_box = st.empty()
    status_box = st.empty()
    logs_box = st.empty()

# Chart updater thread
def chart_updater():
    while True:
        try:
            eng = st.session_state.get("engine")
            cfg = {
                'exchange': exchange,
                'tradingsymbol': tradingsymbol,
                'instrument_token': instrument_token,
                'sim_ltp': sim_ltp
            }
            df = pd.DataFrame()
            # Try engine's kite or global kite for OHLC
            if eng and eng.kite and cfg.get('instrument_token') and st.session_state.get("mode") == "Live":
                try:
                    end = local_now()
                    start = end - timedelta(minutes=5*CANDLES_COUNT)
                    data = eng.kite.historical_data(int(cfg.get('instrument_token')), start, end, '5minute')
                    df = pd.DataFrame(data)
                except Exception as e:
                    log(f"Chart OHLC (engine) error: {e}")
                    df = pd.DataFrame()
            elif KITE_AVAILABLE and kite and cfg.get('instrument_token') and st.session_state.get("mode") == "Live":
                try:
                    end = local_now()
                    start = end - timedelta(minutes=5*CANDLES_COUNT)
                    data = kite.historical_data(int(cfg.get('instrument_token')), start, end, '5minute')
                    df = pd.DataFrame(data)
                except Exception as e:
                    df = pd.DataFrame()
            if df.empty:
                # simulate candles
                base = float(sim_ltp)
                times = [local_now() - timedelta(minutes=5*(CANDLES_COUNT - i)) for i in range(CANDLES_COUNT)]
                prices = base * (1 + np.random.normal(0, 0.0015, CANDLES_COUNT))
                df = pd.DataFrame({'date':times, 'open':prices, 'high':prices*(1+0.001), 'low':prices*(1-0.001), 'close':prices, 'volume':np.random.randint(100,1000,CANDLES_COUNT)})
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
            eng_local = st.session_state.get("engine")
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

            # P&L & status
            if eng_local and getattr(eng_local, 'entry_price', None):
                ltp = eng_local.get_ltp(f"{eng_local.cfg.get('exchange','NSE')}:{eng_local.cfg.get('tradingsymbol')}")
                if ltp is None:
                    ltp = eng_local.entry_price
                unreal = (ltp - eng_local.entry_price) * eng_local.qty
                color = "green" if unreal > 0 else ("red" if unreal < 0 else "blue")
                pnl_box.markdown(f"<h3 style='color:{color};'>Unreal P&L: ₹{unreal:.2f}</h3>", unsafe_allow_html=True)
                status_box.markdown(f"Mode: **{st.session_state.get('mode')}**  \nSymbol: **{eng_local.cfg.get('tradingsymbol')}**  \nEntry: ₹{eng_local.entry_price}  \nQty: {eng_local.qty}")
            else:
                # show kite positions in live
                if KITE_AVAILABLE and kite and st.session_state.get("mode") == "Live":
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
                            pnl_box.markdown(f"<h3 style='color:{color};'>Total P&L: ₹{total:.2f}</h3>", unsafe_allow_html=True)
                            status_box.dataframe(pd.DataFrame(rows))
                        else:
                            pnl_box.text("No open positions")
                            status_box.text("No active position")
                    except Exception as e:
                        pnl_box.text("P&L not available")
                        status_box.text("Kite positions not available")
                else:
                    pnl_box.text("No active position")
                    status_box.text("Mode: " + str(st.session_state.get("mode")))
            logs_box.text_area("Logs", value="\n".join(st.session_state.get("logs", [])[-300:]), height=300)
        except Exception as e:
            log(f"chart_updater error: {e}")
        time.sleep(CHART_REFRESH_SEC)

if 'chart_thread' not in st.session_state:
    st.session_state['chart_thread'] = threading.Thread(target=chart_updater, daemon=True)
    st.session_state['chart_thread'].start()

# -------------------- Auto-start logic --------------------
def maybe_auto_start():
    if auto_start and access_available and st.session_state.get("mode") == "Live" and kite:
        now = local_now()
        if now.time() >= start_time and now.time() <= (datetime.combine(now.date(), start_time) + timedelta(minutes=30)).time():
            if not st.session_state.get("engine"):
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
                eng = TradingEngine(kite if st.session_state.get("mode")=="Live" else None, broker=PaperBroker() if st.session_state.get("mode")=="Paper" else None, cfg=cfg)
                st.session_state["engine"] = eng
                eng.start()
                log("Auto-started engine")
                send_sms("Engine auto-started")

try:
    maybe_auto_start()
except Exception:
    pass

# -------------------- Safety Flatten Watchdog (15:20) --------------------
def safety_flatten():
    now = local_now()
    if now.time() >= dtime(15,20):
        log("15:20 safety flatten triggered")
        eng = st.session_state.get("engine")
        if eng:
            try:
                eng.cancel_all_orders()
            except:
                pass
            try:
                eng.exit_all_positions()
            except:
                pass
            eng.stop()
        st.session_state["trading_active"] = False
        send_sms("⚠️ Auto safety flatten executed at 15:20 IST")
        st.warning("15:20 Safety Flatten executed - all pending orders cancelled and positions exited.")

try:
    safety_flatten()
except Exception:
    pass

st.markdown("---")
st.caption("Test fully in Paper mode before going Live. For Live: paste request_token from Kite redirect (once/day) and Save Access Token. Replace Fast2SMS key & mobile in sidebar.")

# EOF
