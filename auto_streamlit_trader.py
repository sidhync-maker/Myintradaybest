# auto_intraday_streamlit_full.py
"""
Full Auto Intraday Streamlit Trader
Features:
- Manual request token once/day (auto-saves access token)
- Fast2SMS alerts (placeholder key in sidebar)
- Manual stock selector
- Intraday MIS with 5x leverage auto-quantity
- Paper / Live toggle
- Manual Start / Manual Stop / Emergency Stop (flatten+cancel)
- Instant SL, Initial SL, Breakeven -> Trailing SL
- If SL or stop pressed -> cancel untriggered & exit triggered
- Rejection protection (no retry) and SMS alert
- Live P&L (green/red/blue)
- Live 5-min candle chart (last 30 minutes = 6 candles) built from Kite LTP (auto update every 5s)
- Digital IST clock
- NIFTY trend (sma20 vs sma50) -> Uptrend / Downtrend
- 15:15 square-off & 15:20 safety flatten
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

# Kite
try:
    from kiteconnect import KiteConnect
    KITE_AVAILABLE = True
except Exception:
    KiteConnect = None
    KITE_AVAILABLE = False

# Timezone IST
try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

def tz_now():
    return datetime.now(IST)

def now_str():
    return tz_now().strftime("%Y-%m-%d %H:%M:%S")

# -------------------- CONFIG --------------------
ACCESS_TOKEN_FILE = "access_token.json"
FAST2SMS_KEY_PLACEHOLDER = "your_fast2sms_key_here"
FAST2SMS_DEFAULT_NUMBER = ""  # set in sidebar
DEFAULT_EXPOSURE = 50000.0
DEFAULT_LEVERAGE = 5
DEFAULT_SL_PCT = 3.0
DEFAULT_INSTANT_SL_PCT = 1.5
DEFAULT_TRAIL_PCT = 3.0
DEFAULT_START_TIME = dtime(9,15)
DEFAULT_SQUAREOFF = dtime(15,15)
CANDLES_COUNT = 6   # last 30 minutes => 6 candles (5-min candles)
CHART_INTERVAL_SEC = 5
CLOCK_INTERVAL_SEC = 1
NIFTY_TOKEN = "256265"  # default token used to compute trend

# -------------------- UTIL --------------------
def safe_load_json(p):
    try:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            with open(p,"r") as f:
                return json.load(f)
    except Exception:
        return None
    return None

def safe_save_json(p, data):
    with open(p,"w") as f:
        json.dump(data, f, default=str, indent=2)

# session logger
if "logs" not in st.session_state:
    st.session_state["logs"] = []

def log(msg):
    st.session_state["logs"].append(f"[{now_str()}] {msg}")
    if len(st.session_state["logs"]) > 1000:
        st.session_state["logs"] = st.session_state["logs"][-1000:]

# -------------------- SMS (Fast2SMS) --------------------
def send_fast2sms(msg, key, number):
    if not key or not number:
        log("Fast2SMS key/number missing; SMS not sent.")
        return False
    try:
        url = "https://www.fast2sms.com/dev/bulkV2"
        payload = {
            "sender_id": "FSTSMS",
            "message": msg,
            "language": "english",
            "route": "v3",
            "numbers": number
        }
        headers = {'authorization': key}
        r = requests.post(url, data=payload, headers=headers, timeout=10)
        log(f"Fast2SMS status={r.status_code} msg={msg[:80]}")
        return r.status_code == 200
    except Exception as e:
        log(f"Fast2SMS error: {e}")
        return False

def send_sms_alert(msg):
    key = st.session_state.get("fast2sms_key") or FAST2SMS_KEY_PLACEHOLDER
    num = st.session_state.get("sms_number") or FAST2SMS_DEFAULT_NUMBER
    ok = send_fast2sms(msg, key, num)
    if not ok:
        log("SMS send failed.")

# -------------------- PAPER BROKER --------------------
class PaperBroker:
    def __init__(self):
        self.positions = {}  # symbol -> {'qty','avg'}
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
        self.orders[oid] = {'id':oid,'type':'BUY','symbol':symbol,'qty':qty,'price':price,'status':'FILLED'}
        log(f"[Paper] BUY {symbol} qty={qty} @ {price} (id={oid})")
        return oid
    def place_market_sell(self, symbol, qty, price):
        oid = self._oid()
        pos = self.positions.get(symbol, {'qty':0,'avg':0.0})
        sell_qty = min(qty, pos['qty'])
        pos['qty'] = pos['qty'] - sell_qty
        if pos['qty']==0:
            pos['avg']=0.0
        self.positions[symbol] = pos
        self.orders[oid] = {'id':oid,'type':'SELL','symbol':symbol,'qty':sell_qty,'price':price,'status':'FILLED'}
        log(f"[Paper] SELL {symbol} qty={sell_qty} @ {price} (id={oid})")
        return oid
    def get_positions(self):
        res=[]
        for s,p in self.positions.items():
            if p.get('qty',0)!=0:
                res.append({'tradingsymbol':s,'quantity':p['qty'],'avg_price':p['avg'],'pnl':0.0})
        return res
    def cancel_all(self):
        cancelled = list(self.orders.keys())
        self.orders.clear()
        log(f"[Paper] Cancelled orders: {cancelled}")
        return cancelled
    def close_all(self):
        closed=[]
        for s,p in list(self.positions.items()):
            if p.get('qty',0)!=0:
                closed.append({'symbol':s,'qty':p['qty']})
                log(f"[Paper] Closed {s} qty={p['qty']}")
                self.positions[s]={'qty':0,'avg':0.0}
        return closed

# -------------------- ORDER REJECTION HANDLER --------------------
def handle_rejection_and_cleanup(kite, order_id, symbol):
    """
    If order_id appears rejected in kite.orders(), cancel pending orders and alert via SMS.
    Return True if rejection handled (i.e., found rejected).
    """
    if not kite:
        return False
    try:
        orders = kite.orders()
        for o in orders:
            if str(o.get('order_id')) == str(order_id):
                status = (o.get('status') or "").upper()
                # treat REJECTED or CANCELLED with status_message as rejection
                if "REJECT" in status or ("CANCEL" in status and o.get('status_message')):
                    reason = o.get('status_message') or status
                    # cancel pending
                    for p in orders:
                        pstatus = (p.get('status') or "").upper()
                        if pstatus in ("OPEN","TRIGGER PENDING","PENDING","VALIDATION PENDING"):
                            try:
                                kite.cancel_order(order_id=p['order_id'], variety=p.get('variety', kite.VARIETY_REGULAR))
                            except Exception as e:
                                log(f"Failed cancel {p.get('order_id')}: {e}")
                    send_sms_alert(f"⚠️ ORDER REJECTED for {symbol}. Reason: {reason}. Pending orders cancelled.")
                    log(f"Order {order_id} REJECTED: {reason}. Pending cancelled.")
                    return True
        return False
    except Exception as e:
        log(f"handle_rejection error: {e}")
        return False

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
        self.last_buy_order_id = None

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
        if not st.session_state.get("trading_active", True):
            log("Block BUY: trading halted.")
            return None
        if st.session_state.get("mode","Paper") == "Paper":
            oid = self.broker.place_market_buy(tradingsymbol, qty, ltp)
            self.last_buy_order_id = oid
            return oid
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
                self.last_buy_order_id = oid
                log(f"[Live] BUY placed id={oid} qty={qty}")
                return oid
            except Exception as e:
                log(f"[Live] BUY failed: {e}")
                send_sms_alert(f"BUY failed for {tradingsymbol}: {e}")
                # cancel pending and don't retry
                try:
                    self.cancel_all_pending()
                except:
                    pass
                return None

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
                send_sms_alert(f"SELL failed for {tradingsymbol}: {e}")
                return None

    def cancel_all_pending(self):
        if st.session_state.get("mode","Paper") == "Paper":
            return self.broker.cancel_all()
        else:
            try:
                orders = self.kite.orders()
                cancelled=[]
                for o in orders:
                    status = (o.get('status') or "").upper()
                    if status in ('OPEN','TRIGGER PENDING','PENDING','VALIDATION PENDING'):
                        try:
                            self.kite.cancel_order(order_id=o['order_id'], variety=o.get('variety', self.kite.VARIETY_REGULAR))
                            cancelled.append(o['order_id'])
                        except Exception as e:
                            log(f"Cancel failed {o.get('order_id')}: {e}")
                log(f"Cancelled pending: {cancelled}")
                return cancelled
            except Exception as e:
                log(f"cancel_all_pending error: {e}")
                return []

    def exit_all_positions(self):
        if st.session_state.get("mode","Paper") == "Paper":
            return self.broker.close_all()
        else:
            try:
                pos = self.kite.positions()
                net = pos.get('net', []) if isinstance(pos, dict) else []
                closed=[]
                for p in net:
                    qty = int(p.get('quantity',0) or 0)
                    if qty==0:
                        continue
                    tx = self.kite.TRANSACTION_TYPE_SELL if qty>0 else self.kite.TRANSACTION_TYPE_BUY
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
                        closed.append({'symbol':p.get('tradingsymbol'),'qty':q,'order':order})
                    except Exception as e:
                        log(f"Failed to close {p.get('tradingsymbol')}: {e}")
                log(f"Close orders placed: {closed}")
                return closed
            except Exception as e:
                log(f"exit_all_positions error: {e}")
                return []

    def run(self):
        log("Engine started")
        cfg = self.cfg
        exchange = cfg.get('exchange','NSE')
        tradingsymbol = cfg.get('tradingsymbol')
        symbol_ref = f"{exchange}:{tradingsymbol}"
        sim_ltp = float(cfg.get('sim_ltp', 100.0))
        sl_pct = float(cfg.get('sl_pct', DEFAULT_SL_PCT))
        instant_pct = float(cfg.get('instant_sl_pct', DEFAULT_INSTANT_SL_PCT))
        trail_pct = float(cfg.get('trail_pct', DEFAULT_TRAIL_PCT))
        start_time = cfg.get('start_time', DEFAULT_START_TIME)

        first_candle_used = False

        while not self.stopped():
            try:
                now = tz_now()
                if now.weekday() >= 5:
                    time.sleep(5)
                    continue

                # get chart_df from session (chart thread maintains it)
                df = st.session_state.get("chart_df", pd.DataFrame())
                if df.empty:
                    # fallback sim
                    base = sim_ltp
                    times = [now - timedelta(minutes=5*(CANDLES_COUNT - i)) for i in range(CANDLES_COUNT)]
                    prices = base * (1 + np.random.normal(0,0.0015,CANDLES_COUNT))
                    df = pd.DataFrame({'time':times,'open':prices,'high':prices*(1+0.001),'low':prices*(1-0.001),'close':prices,'volume':np.random.randint(100,1000,CANDLES_COUNT)})
                    st.session_state["chart_df"] = df

                # ltp and qty
                ltp = self.get_ltp(symbol_ref) or df['close'].iloc[-1]
                qty = self.compute_qty(ltp)

                # ENTRY logic (first 9:15 candle or after 9:30 trend+vwap)
                if len(df) >= 2:
                    first_candle = df.iloc[0]
                    latest = df.iloc[-1]
                    prev = df.iloc[-2]

                    if (not first_candle_used) and (start_time <= now.time() <= (datetime.combine(now.date(), start_time) + timedelta(minutes=15)).time()):
                        if float(first_candle['close']) > float(first_candle['open']) and ltp > float(first_candle['close']) and self.entry_price is None:
                            order_id = self.place_buy(exchange, tradingsymbol, qty, ltp)
                            if order_id:
                                # check rejection quickly
                                if st.session_state.get("mode") == "Live" and self.kite:
                                    time.sleep(2)
                                    rejected = handle_rejection_and_cleanup(self.kite, order_id, tradingsymbol)
                                    if rejected:
                                        self.last_buy_order_id = None
                                        # do not set entry
                                    else:
                                        self.entry_price = ltp
                                        self.qty = qty
                                        self.entry_time = now
                                        self.peak = ltp
                                        self.instant_sl = round(self.entry_price * (1 - instant_pct/100),2)
                                        self.sl_trigger = round(self.entry_price * (1 - sl_pct/100),2)
                                        send_sms_alert(f"BUY executed {tradingsymbol} @{self.entry_price} qty={self.qty} SL={self.sl_trigger} InstantSL={self.instant_sl}")
                                else:
                                    # paper immediate fill
                                    self.entry_price = ltp
                                    self.qty = qty
                                    self.entry_time = now
                                    self.peak = ltp
                                    self.instant_sl = round(self.entry_price * (1 - instant_pct/100),2)
                                    self.sl_trigger = round(self.entry_price * (1 - sl_pct/100),2)
                                    send_sms_alert(f"[Paper] BUY executed {tradingsymbol} @{self.entry_price} qty={self.qty} SL={self.sl_trigger} InstantSL={self.instant_sl}")
                                first_candle_used = True

                    if self.entry_price is None and now.time() > dtime(9,30):
                        try:
                            if float(latest['close']) > float(prev['close']) and float(latest['close']) > float(latest.get('vwap', -1)):
                                order_id = self.place_buy(exchange, tradingsymbol, qty, ltp)
                                if order_id:
                                    if st.session_state.get("mode") == "Live" and self.kite:
                                        time.sleep(2)
                                        rejected = handle_rejection_and_cleanup(self.kite, order_id, tradingsymbol)
                                        if rejected:
                                            self.last_buy_order_id = None
                                        else:
                                            self.entry_price = ltp
                                            self.qty = qty
                                            self.entry_time = now
                                            self.peak = ltp
                                            self.instant_sl = round(self.entry_price * (1 - instant_pct/100),2)
                                            self.sl_trigger = round(self.entry_price * (1 - sl_pct/100),2)
                                            send_sms_alert(f"BUY executed {tradingsymbol} @{self.entry_price} qty={self.qty} SL={self.sl_trigger} InstantSL={self.instant_sl}")
                                    else:
                                        self.entry_price = ltp
                                        self.qty = qty
                                        self.entry_time = now
                                        self.peak = ltp
                                        self.instant_sl = round(self.entry_price * (1 - instant_pct/100),2)
                                        self.sl_trigger = round(self.entry_price * (1 - sl_pct/100),2)
                                        send_sms_alert(f"[Paper] BUY executed {tradingsymbol} @{self.entry_price} qty={self.qty} SL={self.sl_trigger} InstantSL={self.instant_sl}")
                        except Exception as e:
                            log(f"after-9:30 logic error: {e}")

                # POST-entry management
                if self.entry_price is not None:
                    if ltp > self.peak:
                        self.peak = ltp
                    # instant SL
                    if ltp <= self.instant_sl:
                        log(f"Instant SL hit @{ltp}")
                        send_sms_alert(f"Instant SL hit @{ltp} for {tradingsymbol}")
                        self.place_sell(exchange, tradingsymbol, self.qty, ltp)
                        self.cancel_all_pending()
                        self.exit_all_positions()
                        st.session_state["trading_active"] = False
                        break
                    # initial SL
                    if ltp <= self.sl_trigger:
                        log(f"Initial SL hit @{ltp}")
                        send_sms_alert(f"Initial SL hit @{ltp} for {tradingsymbol}")
                        self.place_sell(exchange, tradingsymbol, self.qty, ltp)
                        self.cancel_all_pending()
                        self.exit_all_positions()
                        st.session_state["trading_active"] = False
                        break
                    # breakeven & trailing
                    profit_pct = (ltp - self.entry_price)/self.entry_price*100
                    if profit_pct >= (sl_pct * 3.0):
                        try:
                            self.sl_trigger = round(self.entry_price,2)
                            send_sms_alert(f"Moved SL to breakeven for {tradingsymbol}")
                            log("Moved SL to breakeven")
                        except Exception as e:
                            log(f"breakeven error: {e}")
                    trailing_trigger = round(self.peak * (1 - trail_pct/100), 2)
                    if trailing_trigger > self.sl_trigger:
                        self.sl_trigger = trailing_trigger
                        log(f"Trailing SL updated -> {trailing_trigger} (peak {self.peak})")
                        send_sms_alert(f"Trailing SL updated -> {trailing_trigger} (peak {self.peak})")

                # auto square-off at 15:15
                if now.time() >= dtime(15,15):
                    log("Square-off reached - flatten")
                    send_sms_alert("Square-off reached - flattening positions")
                    self.cancel_all_pending()
                    self.exit_all_positions()
                    st.session_state["trading_active"] = False
                    break

                # small sleep
                for _ in range(3):
                    if self.stopped():
                        break
                    time.sleep(1)

            except Exception as e:
                log(f"Engine loop error: {e}")
                traceback.print_exc()
                send_sms_alert(f"Engine error: {e}")
                try:
                    self.cancel_all_pending()
                    self.exit_all_positions()
                except:
                    pass
                st.session_state["trading_active"] = False
                break

        log("Engine ended")

# -------------------- STREAMLIT UI --------------------
st.set_page_config(page_title="Auto Intraday Trader — Full", layout="wide")

# CSS theme: dark main + visible sidebar; clear inputs/buttons
st.markdown("""
<style>
/* MAIN */
.stApp { background-color: #001F3F !important; color: #ffffff !important; }
h1,h2,h3,h4,h5,h6 { color: #FFD700 !important; font-weight:800 !important; }
label, p, span, div, .stMarkdown { color: #ffffff !important; font-weight:700 !important; }
input[type="text"], input[type="password"], input[type="number"], textarea,
.stTextInput > div > input, .stTextInput > div > textarea, .stNumberInput input {
    background-color: #87CEEB !important; color:#000000 !important; font-weight:700 !important;
    border:2px solid #004080 !important; border-radius:6px !important;
}
.stButton>button { background-color:#87CEEB !important; color:#000000 !important; font-weight:800 !important; border-radius:8px !important; }
.stButton>button:hover { background-color:#5dbcd2 !important; }

/* SIDEBAR visible */
[data-testid="stSidebar"] { background-color: #F8F9FA !important; color: #000000 !important; }
[data-testid="stSidebar"] input, [data-testid="stSidebar"] select, [data-testid="stSidebar"] textarea { background: #ffffff !important; color:#000 !important; font-weight:700 !important; border-radius:6px !important; }
[data-testid="stSidebar"] button { background:#ffffff !important; color:#000 !important; font-weight:800 !important; border-radius:6px !important; }

/* P&L colors */
.profit { color:#00FF00 !important; font-weight:700 !important; }
.loss { color:#FF0000 !important; font-weight:700 !important; }
.neutral { color:#1E90FF !important; font-weight:700 !important; }
::placeholder { color:#333333 !important; }
.plotly-graph-div { background: transparent !important; }
</style>
""", unsafe_allow_html=True)

st.title("🔁 Auto Intraday Trader — Full (Live Chart + Clock)")

# Sidebar: Kite + SMS + connection helper
with st.sidebar:
    st.header("Connection & Alerts")
    api_key = st.text_input("Kite API Key", value=os.getenv("ZK_API_KEY",""), type="password")
    api_secret = st.text_input("Kite API Secret", value=os.getenv("ZK_API_SECRET",""), type="password")
    st.markdown("Open Kite Login URL -> login -> copy request_token from redirect -> paste below")
    if KITE_AVAILABLE and api_key:
        try:
            tmp = KiteConnect(api_key=api_key)
            st.code(tmp.login_url())
        except Exception as e:
            st.write("Unable to show login URL:", e)
    else:
        if not KITE_AVAILABLE:
            st.warning("kiteconnect not installed; Live mode disabled.")
    request_token = st.text_input("Paste request_token (from redirect)")
    if st.button("Generate & Save Access Token"):
        if not (api_key and api_secret and request_token):
            st.error("Provide API Key, Secret, and request_token")
        else:
            try:
                tmp = KiteConnect(api_key=api_key)
                data = tmp.generate_session(request_token.strip(), api_secret=api_secret.strip())
                safe_save_json(ACCESS_TOKEN_FILE, data)
                st.success("Access token saved")
                log("Access token saved")
                send_sms_alert("Access token generated and saved.")
            except Exception as e:
                st.error(f"Token gen failed: {e}")
                log(f"Token gen failed: {e}")

    st.markdown("---")
    st.subheader("Fast2SMS Alerts")
    st.text("Put your Fast2SMS key & number to receive SMS")
    fastkey = st.text_input("Fast2SMS API Key", value=FAST2SMS_KEY_PLACEHOLDER, type="password")
    sms_number = st.text_input("Alert mobile (with country code, e.g., 9198...)", value=FAST2SMS_DEFAULT_NUMBER)
    if st.button("Save SMS settings"):
        st.session_state["fast2sms_key"] = fastkey
        st.session_state["sms_number"] = sms_number
        st.success("SMS settings saved")
        log("SMS settings saved in session")

# Build kite client if possible
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
                if st.session_state.get("fast2sms_key"):
                    send_fast2sms("Kite access token invalid/expired. Please refresh.", st.session_state.get("fast2sms_key"), st.session_state.get("sms_number"))

# Top bar: kite / token indicators + clock + NIFTY trend
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
    clock_holder = st.empty()
    def clock_thread():
        while True:
            try:
                clock_holder.markdown(f"<h3 style='text-align:right; color:#00FFFF; font-weight:700'>{tz_now().strftime('%Y-%m-%d %H:%M:%S IST')}</h3>", unsafe_allow_html=True)
            except:
                pass
            time.sleep(CLOCK_INTERVAL_SEC)
    if 'clock_thread' not in st.session_state:
        st.session_state['clock_thread'] = threading.Thread(target=clock_thread, daemon=True)
        st.session_state['clock_thread'].start()

# NIFTY trend status (SMA20 vs SMA50)
def get_nifty_trend(kite):
    try:
        if not kite:
            return "Unknown"
        end = tz_now()
        start = end - timedelta(days=2)
        data = kite.historical_data(int(NIFTY_TOKEN), start, end, '5minute')
        df = pd.DataFrame(data)
        if df.empty:
            return "Unknown"
        df['sma20'] = df['close'].rolling(20).mean()
        df['sma50'] = df['close'].rolling(50).mean()
        if df['sma20'].iloc[-1] > df['sma50'].iloc[-1]:
            return "Uptrend"
        else:
            return "Downtrend"
    except Exception as e:
        log(f"nifty trend error: {e}")
        return "Unknown"

nifty_trend = get_nifty_trend(kite) if KITE_AVAILABLE else "Unknown"
st.markdown(f"**NIFTY Trend:** **{nifty_trend}**")

st.markdown("---")

# Main config
left, right = st.columns([3,1])
with left:
    st.subheader("Strategy & Symbol")
    mode = st.selectbox("Mode", ["Paper","Live"], index=0)
    st.session_state["mode"] = mode
    exchange = st.selectbox("Exchange", ["NSE","BSE"], index=0)
    tradingsymbol = st.text_input("Trading symbol (exact)", value="RELIANCE").strip().upper()
    instrument_token = st.text_input("Instrument token (numeric) - optional", value="")
    exposure = st.number_input("Exposure (₹)", value=DEFAULT_EXPOSURE)
    leverage = st.number_input("Leverage (auto MIS)", value=DEFAULT_LEVERAGE, step=1)
    sl_pct = st.number_input("Initial SL %", value=DEFAULT_SL_PCT, step=0.1)
    instant_sl_pct = st.number_input("Instant SL %", value=DEFAULT_INSTANT_SL_PCT, step=0.1)
    trail_pct = st.number_input("Trailing % after breakeven", value=DEFAULT_TRAIL_PCT, step=0.1)
    start_time = st.time_input("Auto start time (IST)", value=DEFAULT_START_TIME)
    squareoff_time = st.time_input("Square-off time (IST)", value=DEFAULT_SQUAREOFF)
    sim_ltp = st.number_input("Paper sim LTP", value=100.0)
    auto_start = st.checkbox("Auto-start when token present", value=False)

with right:
    st.subheader("Controls")
    if st.button("Start Engine (manual)"):
        cfg = {'exchange':exchange,'tradingsymbol':tradingsymbol,'instrument_token':instrument_token,'exposure':exposure,'leverage':leverage,'sl_pct':sl_pct,'instant_sl_pct':instant_sl_pct,'trail_pct':trail_pct,'start_time':start_time,'squareoff_time':squareoff_time,'sim_ltp':sim_ltp}
        eng = TradingEngine(kite if (mode=="Live" and kite_connected) else None, broker=PaperBroker() if mode=="Paper" else None, cfg=cfg)
        st.session_state["engine"] = eng
        st.session_state["trading_active"] = True
        eng.start()
        log("Engine started (manual)")
        send_sms_alert(f"Engine started for {tradingsymbol} mode={mode}")
    if st.button("Stop Engine (manual)"):
        eng = st.session_state.get("engine")
        st.session_state["trading_active"] = False
        if eng:
            try:
                eng.cancel_all_pending()
            except:
                pass
            try:
                eng.exit_all_positions()
            except:
                pass
            eng.stop()
        log("Engine stop requested (manual)")
        send_sms_alert("Engine stop requested (manual) - flatten attempted.")
        st.success("Engine stop requested.")
    if st.button("Emergency STOP (flatten now)"):
        eng = st.session_state.get("engine")
        st.session_state["trading_active"] = False
        if eng:
            try:
                eng.cancel_all_pending()
            except:
                pass
            try:
                eng.exit_all_positions()
            except:
                pass
            eng.stop()
        send_sms_alert("Emergency STOP pressed — all orders cancelled & positions exited.")
        log("Emergency STOP executed")
        st.error("Emergency STOP executed")

st.markdown("---")

# Chart and P&L area
chart_col, info_col = st.columns([3,1])
with chart_col:
    st.subheader("Live 5-min Candle Chart (last 30 min) — auto updates every 5s")
    chart_placeholder = st.empty()
with info_col:
    st.subheader("Live P&L & Status")
    pnl_box = st.empty()
    status_box = st.empty()
    logs_box = st.empty()

# Chart thread: builds 5-min candles from kite.ltp or sim LTP and updates session_state['chart_df']
def chart_worker():
    # init df if missing
    if "chart_df" not in st.session_state:
        now = tz_now()
        times = [now - timedelta(minutes=5*(CANDLES_COUNT - i)) for i in range(CANDLES_COUNT)]
        base = float(st.session_state.get("sim_ltp", sim_ltp))
        prices = base * (1 + np.random.normal(0,0.0015,CANDLES_COUNT))
        st.session_state["chart_df"] = pd.DataFrame({'time':times,'open':prices,'high':prices*(1+0.001),'low':prices*(1-0.001),'close':prices,'volume':np.random.randint(100,1000,CANDLES_COUNT)})
    while True:
        try:
            df = st.session_state.get("chart_df")
            now = tz_now()
            bucket = now.replace(second=0,microsecond=0,minute=(now.minute//5)*5)
            ltp_val = None
            # fetch live LTP if possible
            if st.session_state.get("mode") == "Live" and kite and tradingsymbol:
                try:
                    ref = f"{exchange}:{tradingsymbol}"
                    d = kite.ltp(ref)
                    ltp_val = list(d.values())[0]['last_price']
                except Exception as e:
                    # network or kite issue - fallback to sim
                    log(f"chart_worker kite.ltp error: {e}")
                    ltp_val = None
            if ltp_val is None:
                ltp_val = float(st.session_state.get("sim_ltp", sim_ltp)) * (1 + np.random.normal(0,0.0008))
            # update or append candle
            if not df.empty and df.iloc[-1]['time'] == bucket:
                i = df.index[-1]
                df.at[i,'high'] = max(df.at[i,'high'], ltp_val)
                df.at[i,'low'] = min(df.at[i,'low'], ltp_val)
                df.at[i,'close'] = ltp_val
                df.at[i,'volume'] = df.at[i,'volume'] + 1
            else:
                new = {'time':bucket,'open':ltp_val,'high':ltp_val,'low':ltp_val,'close':ltp_val,'volume':1}
                df = pd.concat([df, pd.DataFrame([new])], ignore_index=True).tail(CANDLES_COUNT)
            # compute VWAP
            typical = (df['high'] + df['low'] + df['close'])/3.0
            df['cum_vol'] = df['volume'].cumsum()
            df['cum_vp'] = (typical * df['volume']).cumsum()
            df['vwap'] = df['cum_vp'] / df['cum_vol']
            st.session_state["chart_df"] = df

            # build plotly figure
            fig = go.Figure(data=[go.Candlestick(x=df['time'], open=df['open'], high=df['high'], low=df['low'], close=df['close'], name='candles')])
            fig.add_trace(go.Scatter(x=df['time'], y=df['vwap'], name='VWAP', mode='lines'))
            # annotate buy / sl if engine exists
            eng = st.session_state.get("engine")
            if eng:
                ep = getattr(eng,'entry_price', None)
                et = getattr(eng,'entry_time', None)
                sl = getattr(eng,'sl_trigger', None)
                inst = getattr(eng,'instant_sl', None)
                if ep and et:
                    fig.add_trace(go.Scatter(x=[et], y=[ep], mode='markers', marker_symbol='triangle-up', marker_color='green', marker_size=12, name='BUY'))
                if sl:
                    fig.add_hline(y=sl, line_dash="dash", line_color="red", annotation_text="SL", annotation_position="top left")
                if inst:
                    fig.add_hline(y=inst, line_dash="dot", line_color="orange", annotation_text="Instant SL", annotation_position="top left")
            fig.update_layout(xaxis_rangeslider_visible=False, template='plotly_dark', height=520, margin=dict(l=10,r=10,t=30,b=10))
            chart_placeholder.plotly_chart(fig, use_container_width=True)

            # P&L & status
            eng = st.session_state.get("engine")
            if eng and getattr(eng,'entry_price', None):
                ltp_now = eng.get_ltp(f"{eng.cfg.get('exchange','NSE')}:{eng.cfg.get('tradingsymbol')}")
                if ltp_now is None:
                    ltp_now = eng.entry_price
                unreal = (ltp_now - eng.entry_price) * eng.qty
                color = "green" if unreal>0 else ("red" if unreal<0 else "blue")
                pnl_box.markdown(f"<h3 style='color:{color};'>Unreal P&L: ₹{unreal:.2f}</h3>", unsafe_allow_html=True)
                status_box.markdown(f"Mode: **{st.session_state.get('mode')}**  \nSymbol: **{eng.cfg.get('tradingsymbol')}**  \nEntry: ₹{eng.entry_price}  \nQty: {eng.qty}")
            else:
                if KITE_AVAILABLE and kite and st.session_state.get("mode") == "Live":
                    try:
                        pos = kite.positions()
                        net = pos.get('net', []) if isinstance(pos, dict) else []
                        if net:
                            total=0.0
                            rows=[]
                            for p in net:
                                q=int(p.get('quantity',0) or 0)
                                pnl=float(p.get('pnl',0) or 0)
                                total += pnl
                                rows.append({'Symbol':p.get('tradingsymbol'),'Qty':q,'PnL':pnl})
                            clr = "green" if total>0 else ("red" if total<0 else "blue")
                            pnl_box.markdown(f"<h3 style='color:{clr};'>Total P&L: ₹{total:.2f}</h3>", unsafe_allow_html=True)
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

            # logs
            logs_box.text_area("Logs", value="\n".join(st.session_state.get("logs", [])[-400:]), height=300)
        except Exception as e:
            log(f"chart_worker error: {e}")
        time.sleep(CHART_INTERVAL_SEC)

# start chart thread once
if 'chart_thread' not in st.session_state:
    st.session_state['chart_thread'] = threading.Thread(target=chart_worker, daemon=True)
    st.session_state['chart_thread'].start()

# ------------------ Auto-start logic ------------------
def maybe_auto_start():
    if auto_start and access_available and st.session_state.get("mode") == "Live" and kite:
        now = tz_now()
        if now.time() >= start_time and now.time() <= (datetime.combine(now.date(), start_time) + timedelta(minutes=30)).time():
            if not st.session_state.get("engine"):
                cfg = {'exchange':exchange,'tradingsymbol':tradingsymbol,'instrument_token':instrument_token,'exposure':exposure,'leverage':leverage,'sl_pct':sl_pct,'instant_sl_pct':instant_sl_pct,'trail_pct':trail_pct,'start_time':start_time,'squareoff_time':squareoff_time,'sim_ltp':sim_ltp}
                eng = TradingEngine(kite if st.session_state.get("mode")=="Live" else None, broker=PaperBroker() if st.session_state.get("mode")=="Paper" else None, cfg=cfg)
                st.session_state["engine"] = eng
                eng.start()
                log("Auto-started engine")
                send_sms_alert("Engine auto-started")

try:
    maybe_auto_start()
except Exception:
    pass

# ------------------ Safety flatten (15:20) ------------------
def safety_flatten_check():
    now = tz_now()
    if now.time() >= dtime(15,20):
        log("15:20 safety flatten triggered")
        eng = st.session_state.get("engine")
        if eng:
            try:
                eng.cancel_all_pending()
            except:
                pass
            try:
                eng.exit_all_positions()
            except:
                pass
            eng.stop()
        st.session_state["trading_active"] = False
        send_sms_alert("⚠️ Auto safety flatten executed at 15:20 IST")
        st.warning("15:20 Safety Flatten executed - all pending orders cancelled and positions exited.")

try:
    safety_flatten_check()
except Exception:
    pass

st.markdown("---")
st.caption("Test fully in Paper mode first. For Live mode: generate access token via sidebar. Replace Fast2SMS key & number in sidebar to receive SMS.")

# EOF
