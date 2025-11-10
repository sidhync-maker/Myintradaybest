# auto_streamlit_trader_v7_color_pro.py
"""
Auto Intraday Streamlit Trader — v7 Color Pro Edition
- Colorful, high-contrast theme (deep blue/teal/gold)
- Manual daily request_token -> auto-save access token
- Kite connect status dots + access token status
- Manual stock selector, Paper/Live toggle
- Intraday MIS trading with 5x leverage auto-qty
- Manual Start / Stop / Emergency STOP
- Instant SL, Initial SL, Breakeven -> Trailing SL
- If SL hit / Manual Stop / Emergency -> cancel untriggered + exit triggered positions
- Order reject protection (no retries) -> cancel pending + SMS alert
- Live 5-min candle chart (last 30 minutes) with VWAP (below P&L)
- Digital clock (IST) top-right fixed
- Fast2SMS alerts (placeholder key)
- Auto square-off 15:15 & safety flatten 15:20
- Auto-update every 5 seconds (chart, P&L, status)
- Test in PAPER mode first
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

# Optional KiteConnect
try:
    from kiteconnect import KiteConnect
    KITE_AVAILABLE = True
except Exception:
    KiteConnect = None
    KITE_AVAILABLE = False

# timezone IST
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

# --------------- CONFIG ---------------
ACCESS_TOKEN_FILE = "access_token.json"
FAST2SMS_PLACEHOLDER = "PUT_FAST2SMS_KEY_HERE"
FAST2SMS_DEFAULT_NUMBER = ""
DEFAULT_EXPOSURE = 50000.0
DEFAULT_LEVERAGE = 5
DEFAULT_SL_PCT = 3.0
DEFAULT_INSTANT_SL_PCT = 1.5
DEFAULT_TRAIL_PCT = 3.0
DEFAULT_START_TIME = dtime(9,15)
DEFAULT_SQUAREOFF = dtime(15,15)
CANDLES_COUNT = 6        # 6 * 5min = 30 minutes
REFRESH_SEC = 5
CLOCK_REFRESH_SEC = 1
NIFTY_TOKEN = "256265"

# --------------- UTILITIES ---------------
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

# logs in session
if "logs" not in st.session_state:
    st.session_state["logs"] = []
def log(msg):
    st.session_state["logs"].append(f"[{now_str()}] {msg}")
    if len(st.session_state["logs"]) > 3000:
        st.session_state["logs"] = st.session_state["logs"][-3000:]

# --------------- SMS (Fast2SMS) ---------------
def send_fast2sms(message, api_key, number):
    if not api_key or not number:
        log("Fast2SMS missing key/number; SMS not sent.")
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
        log(f"Fast2SMS status={r.status_code} msg={message[:80]}")
        return r.status_code == 200
    except Exception as e:
        log(f"Fast2SMS error: {e}")
        return False

def send_sms(message):
    key = st.session_state.get("fast2sms_key") or FAST2SMS_PLACEHOLDER
    num = st.session_state.get("sms_number") or FAST2SMS_DEFAULT_NUMBER
    ok = send_fast2sms(message, key, num)
    if not ok:
        log("SMS failed to send (check key/number).")
    return ok

# --------------- Paper Broker (simulation) ---------------
class PaperBroker:
    def __init__(self):
        self.positions = {}
        self.orders = {}
        self._next = 1
    def _oid(self):
        oid = f"P{self._next}"
        self._next += 1
        return oid
    def place_market_buy(self, symbol, qty, price):
        oid = self._oid()
        pos = self.positions.get(symbol, {'qty':0,'avg':0.0})
        total = pos['qty'] + qty
        avg = price if pos['qty']==0 else (pos['avg']*pos['qty'] + price*qty)/total
        self.positions[symbol] = {'qty': total, 'avg': avg}
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
                self.positions[s] = {'qty':0,'avg':0.0}
        return closed

# --------------- Rejection handler ---------------
def handle_rejected_and_cleanup(kite, order_id, symbol):
    if not kite:
        return False
    try:
        orders = kite.orders()
        for o in orders:
            if str(o.get("order_id")) == str(order_id):
                status = (o.get("status") or "").upper()
                if "REJECT" in status or ("CANCEL" in status and o.get("status_message")):
                    reason = o.get("status_message") or status
                    # cancel pending
                    for p in orders:
                        pstatus = (p.get("status") or "").upper()
                        if pstatus in ("OPEN","TRIGGER PENDING","PENDING","VALIDATION PENDING"):
                            try:
                                kite.cancel_order(order_id=p['order_id'], variety=p.get('variety', kite.VARIETY_REGULAR))
                            except Exception as e:
                                log(f"Failed cancel {p.get('order_id')}: {e}")
                    send_sms(f"⚠️ ORDER REJECTED for {symbol}. Reason: {reason}. Pending cancelled.")
                    log(f"Order {order_id} rejected: {reason}.")
                    return True
        return False
    except Exception as e:
        log(f"handle_rejected error: {e}")
        return False

# --------------- Trading Engine ---------------
class TradingEngine(threading.Thread):
    def __init__(self, kite=None, broker=None, cfg=None):
        super().__init__(daemon=True)
        self.kite = kite
        self.broker = broker or PaperBroker()
        self.cfg = cfg or {}
        self._stop = threading.Event()
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
            log("Block BUY: trading halted by state.")
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
                send_sms(f"BUY failed for {tradingsymbol}: {e}")
                try:
                    self.cancel_all_pending()
                except Exception:
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
                send_sms(f"SELL failed for {tradingsymbol}: {e}")
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
                    qty = int(p.get('quantity', 0) or 0)
                    if qty == 0:
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

                df = st.session_state.get("chart_df", pd.DataFrame())
                if df.empty:
                    base = sim_ltp
                    times = [now - timedelta(minutes=5*(CANDLES_COUNT - i)) for i in range(CANDLES_COUNT)]
                    prices = base * (1 + np.random.normal(0,0.0015,CANDLES_COUNT))
                    df = pd.DataFrame({'time':times,'open':prices,'high':prices*(1+0.001),'low':prices*(1-0.001),'close':prices,'volume':np.random.randint(100,1000,CANDLES_COUNT)})
                    st.session_state["chart_df"] = df

                ltp = self.get_ltp(symbol_ref) or df['close'].iloc[-1]
                qty = self.compute_qty(ltp)

                # ENTRY logic (first candle 9:15 bullish OR after 9:30 vwap+uptrend)
                if len(df) >= 2:
                    first_candle = df.iloc[0]
                    latest = df.iloc[-1]
                    prev = df.iloc[-2]

                    # 9:15 immediate buy window
                    if (not first_candle_used) and (start_time <= now.time() <= (datetime.combine(now.date(), start_time) + timedelta(minutes=15)).time()):
                        if float(first_candle['close']) > float(first_candle['open']) and ltp > float(first_candle['close']) and self.entry_price is None:
                            order_id = self.place_buy(exchange, tradingsymbol, qty, ltp)
                            if order_id:
                                if st.session_state.get("mode") == "Live" and self.kite:
                                    time.sleep(2)
                                    rejected = handle_rejected_and_cleanup(self.kite, order_id, tradingsymbol)
                                    if rejected:
                                        self.last_buy_order_id = None
                                    else:
                                        self.entry_price = ltp; self.qty = qty; self.entry_time = now; self.peak = ltp
                                        self.instant_sl = round(self.entry_price * (1 - instant_pct/100),2)
                                        self.sl_trigger = round(self.entry_price * (1 - sl_pct/100),2)
                                        send_sms(f"BUY executed {tradingsymbol} @{self.entry_price} qty={self.qty} SL={self.sl_trigger} InstantSL={self.instant_sl}")
                                else:
                                    self.entry_price = ltp; self.qty = qty; self.entry_time = now; self.peak = ltp
                                    self.instant_sl = round(self.entry_price * (1 - instant_pct/100),2)
                                    self.sl_trigger = round(self.entry_price * (1 - sl_pct/100),2)
                                    send_sms(f"[Paper] BUY executed {tradingsymbol} @{self.entry_price} qty={self.qty} SL={self.sl_trigger} InstantSL={self.instant_sl}")
                                first_candle_used = True

                    # after 9:30 uptrend + vwap buy
                    if self.entry_price is None and now.time() > dtime(9,30):
                        try:
                            if float(latest['close']) > float(prev['close']) and float(latest['close']) > float(latest.get('vwap', -1)):
                                order_id = self.place_buy(exchange, tradingsymbol, qty, ltp)
                                if order_id:
                                    if st.session_state.get("mode") == "Live" and self.kite:
                                        time.sleep(2)
                                        rejected = handle_rejected_and_cleanup(self.kite, order_id, tradingsymbol)
                                        if rejected:
                                            self.last_buy_order_id = None
                                        else:
                                            self.entry_price = ltp; self.qty = qty; self.entry_time = now; self.peak = ltp
                                            self.instant_sl = round(self.entry_price * (1 - instant_pct/100),2)
                                            self.sl_trigger = round(self.entry_price * (1 - sl_pct/100),2)
                                            send_sms(f"BUY executed {tradingsymbol} @{self.entry_price} qty={self.qty} SL={self.sl_trigger} InstantSL={self.instant_sl}")
                                    else:
                                        self.entry_price = ltp; self.qty = qty; self.entry_time = now; self.peak = ltp
                                        self.instant_sl = round(self.entry_price * (1 - instant_pct/100),2)
                                        self.sl_trigger = round(self.entry_price * (1 - sl_pct/100),2)
                                        send_sms(f"[Paper] BUY executed {tradingsymbol} @{self.entry_price} qty={self.qty} SL={self.sl_trigger} InstantSL={self.instant_sl}")
                        except Exception as e:
                            log(f"after-9:30 error: {e}")

                # POST ENTRY: SL checks, trailing, breakeven
                if self.entry_price is not None:
                    if ltp > self.peak:
                        self.peak = ltp
                    # instant SL
                    if ltp <= self.instant_sl:
                        log(f"Instant SL hit @{ltp}")
                        send_sms(f"Instant SL hit @{ltp} for {tradingsymbol}")
                        self.place_sell(exchange, tradingsymbol, self.qty, ltp)
                        self.cancel_all_pending()
                        self.exit_all_positions()
                        st.session_state["trading_active"] = False
                        break
                    # initial SL
                    if ltp <= self.sl_trigger:
                        log(f"Initial SL hit @{ltp}")
                        send_sms(f"Initial SL hit @{ltp} for {tradingsymbol}")
                        self.place_sell(exchange, tradingsymbol, self.qty, ltp)
                        self.cancel_all_pending()
                        self.exit_all_positions()
                        st.session_state["trading_active"] = False
                        break
                    # breakeven
                    profit_pct = (ltp - self.entry_price)/self.entry_price*100
                    if profit_pct >= (sl_pct * 3.0):
                        try:
                            self.sl_trigger = round(self.entry_price,2)
                            send_sms(f"Moved SL to breakeven for {tradingsymbol}")
                            log("Moved SL to breakeven")
                        except Exception as e:
                            log(f"breakeven error: {e}")
                    # trailing based on peak
                    trailing_trigger = round(self.peak * (1 - trail_pct/100), 2)
                    if trailing_trigger > self.sl_trigger:
                        self.sl_trigger = trailing_trigger
                        log(f"Trailing SL updated -> {trailing_trigger} (peak {self.peak})")
                        send_sms(f"Trailing SL updated -> {trailing_trigger} (peak {self.peak})")

                # auto square-off at 15:15
                if tz_now().time() >= dtime(15,15):
                    log("Square-off - flattening")
                    send_sms("Square-off reached - flattening positions")
                    self.cancel_all_pending()
                    self.exit_all_positions()
                    st.session_state["trading_active"] = False
                    break

                # small sleeps
                for _ in range(REFRESH_SEC):
                    if self.stopped():
                        break
                    time.sleep(1)

            except Exception as e:
                log(f"Engine loop error: {e}")
                traceback.print_exc()
                send_sms(f"Engine error: {e}")
                try:
                    self.cancel_all_pending()
                    self.exit_all_positions()
                except Exception:
                    pass
                st.session_state["trading_active"] = False
                break

        log("Engine ended")

# --------------- STREAMLIT UI ---------------
st.set_page_config(page_title="Auto Intraday Trader v7 - Color Pro", layout="wide")

# CSS - colorful theme (deep blue -> teal gradient header, gold titles, sidebar dark, fixed clock)
st.markdown("""
<style>
/* page background */
body, .stApp { background: linear-gradient(180deg,#001428 0%, #002B46 60%) !important; color: #ffffff !important; }

/* header */
h1,h2,h3,h4 { color:#FFD966 !important; font-weight:800 !important; }

/* sidebar */
[data-testid="stSidebar"] {
  background: #0b0f1a !important;
  color: #ffffff !important;
  padding: 16px !important;
  border-right: 2px solid rgba(255,255,255,0.03);
}

/* sidebar inputs/buttons - white bg black text */
[data-testid="stSidebar"] input, [data-testid="stSidebar"] select, [data-testid="stSidebar"] textarea {
  background: #ffffff !important;
  color: #000000 !important;
  font-weight:700 !important;
  border-radius:6px !important;
}

/* main inputs/buttons - cyan bg, black bold txt */
input[type="text"], input[type="password"], input[type="number"], textarea,
.stTextInput > div > input, .stNumberInput input, .stSelectbox > div {
    background-color: #7FEFEF !important; color:#000000 !important; font-weight:700 !important; border-radius:8px !important;
}
.stButton>button { background-color:#00C2A8 !important; color:#001F28 !important; font-weight:800 !important; border-radius:8px !important; }

/* fixed clock */
#fixed-clock {
  position: fixed;
  top: 18px;
  right: 28px;
  z-index: 9999;
  color: #FFD966;
  background: rgba(0,0,0,0.18);
  padding: 8px 12px;
  border-radius: 8px;
  font-weight:900;
  font-size:18px;
  box-shadow: 0 2px 10px rgba(0,0,0,0.4);
}

/* P&L styles */
.profit { color:#3CFB8C !important; font-weight:800 !important; }
.loss { color:#FF6B6B !important; font-weight:800 !important; }
.neutral { color:#7FD1FF !important; font-weight:800 !important; }

/* chart container border for visibility */
.css-1d391kg { /* plotly container class sometimes */ border-radius:8px !important; }

/* logs */
textarea[role="textbox"] { color:#ffffff !important; background: rgba(255,255,255,0.06) !important; }
</style>
""", unsafe_allow_html=True)

# Fixed digital clock (top-right)
st.markdown('<div id="fixed-clock">' + tz_now().strftime("%Y-%m-%d %I:%M:%S %p IST") + '</div>', unsafe_allow_html=True)
def update_fixed_clock():
    while True:
        try:
            st.markdown('<div id="fixed-clock">' + tz_now().strftime("%Y-%m-%d %I:%M:%S %p IST") + '</div>', unsafe_allow_html=True)
        except Exception:
            pass
        time.sleep(CLOCK_REFRESH_SEC)
if 'clock_thread_v7' not in st.session_state:
    st.session_state['clock_thread_v7'] = threading.Thread(target=update_fixed_clock, daemon=True)
    st.session_state['clock_thread_v7'].start()

# HEADER
st.title("🟦 Auto Intraday Trader — v7 Color Pro")
st.markdown("**Test in Paper mode first.** Use sidebar to paste Kite request_token & Fast2SMS key. All updates every 5s.")

# SIDEBAR - always visible with controls
with st.sidebar:
    st.header("🔐 Connection & Alerts")
    api_key = st.text_input("Kite API Key", value=os.getenv("ZK_API_KEY",""), type="password")
    api_secret = st.text_input("Kite API Secret", value=os.getenv("ZK_API_SECRET",""), type="password")
    st.markdown("Open Kite Login URL (below) → login → copy `request_token` from redirected URL → paste")
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
            st.error("Provide API Key, Secret and request_token")
        else:
            try:
                tmp = KiteConnect(api_key=api_key)
                data = tmp.generate_session(request_token.strip(), api_secret=api_secret.strip())
                safe_save_json(ACCESS_TOKEN_FILE, data)
                st.success("Access token saved to access_token.json")
                log("Access token saved")
                send_sms("Access token generated & saved.")
            except Exception as e:
                st.error(f"Token gen failed: {e}")
                log(f"Token gen failed: {e}")

    st.markdown("---")
    st.subheader("📲 Fast2SMS")
    fastkey = st.text_input("Fast2SMS API Key", value=st.session_state.get("fast2sms_key", FAST2SMS_PLACEHOLDER), type="password")
    sms_number = st.text_input("Alert mobile (country code, e.g., 9198...)", value=st.session_state.get("sms_number", FAST2SMS_DEFAULT_NUMBER))
    if st.button("Save SMS settings"):
        st.session_state["fast2sms_key"] = fastkey
        st.session_state["sms_number"] = sms_number
        st.success("SMS settings saved")
        log("SMS settings saved")

# Build kite client if possible
saved_access = safe_load_json(ACCESS_TOKEN_FILE)
access_available = bool(saved_access and saved_access.get("access_token"))
kite = None
kite_connected = False
if KITE_AVAILABLE and api_key:
    try:
        kite = KiteConnect(api_key=api_key)
        if access_available:
            try:
                kite.set_access_token(saved_access.get("access_token"))
                kite.profile()
                kite_connected = True
                log("Kite connected from saved token")
            except Exception as e:
                kite_connected = False
                log(f"Kite token invalid/expired: {e}")
                if st.session_state.get("fast2sms_key"):
                    send_fast2sms("Kite access token invalid/expired. Please refresh.", st.session_state.get("fast2sms_key"), st.session_state.get("sms_number"))
    except Exception as e:
        log(f"Kite init error: {e}")

# Status dots
col_a, col_b, col_c = st.columns([1,1,6])
with col_a:
    st.markdown("**Kite**")
    if kite_connected:
        st.markdown("<span style='color:#3CFB8C; font-weight:800'>● Connected</span>", unsafe_allow_html=True)
    else:
        st.markdown("<span style='color:#FF6B6B; font-weight:800'>● Not connected</span>", unsafe_allow_html=True)
with col_b:
    st.markdown("**Access**")
    if access_available:
        st.markdown("<span style='color:#FFD966; font-weight:800'>● Saved</span>", unsafe_allow_html=True)
    else:
        st.markdown("<span style='color:#FF6B6B; font-weight:800'>● Not saved</span>", unsafe_allow_html=True)

st.markdown("---")

# MAIN CONFIG & CONTROLS
left, right = st.columns([2,1])
with left:
    st.subheader("🛠 Strategy & Symbol")
    mode = st.selectbox("Mode", ["Paper","Live"], index=0)
    st.session_state["mode"] = mode
    exchange = st.selectbox("Exchange", ["NSE","BSE"], index=0)
    tradingsymbol = st.text_input("Trading symbol (exact) e.g., RELIANCE", value="RELIANCE").strip().upper()
    instrument_token = st.text_input("Instrument token (numeric) - optional", value="")
    exposure = st.number_input("Exposure (₹)", value=DEFAULT_EXPOSURE)
    leverage = st.number_input("Leverage (auto MIS)", value=DEFAULT_LEVERAGE, step=1)
    sl_pct = st.number_input("Initial SL %", value=DEFAULT_SL_PCT, step=0.1)
    instant_sl_pct = st.number_input("Instant SL %", value=DEFAULT_INSTANT_SL_PCT, step=0.1)
    trail_pct = st.number_input("Trailing % after breakeven", value=DEFAULT_TRAIL_PCT, step=0.1)
    start_time = st.time_input("Auto start time (IST)", value=DEFAULT_START_TIME)
    squareoff_time = st.time_input("Square-off time (IST)", value=DEFAULT_SQUAREOFF)
    sim_ltp = st.number_input("Paper sim LTP", value=100.0)
    auto_start = st.checkbox("Auto-start when token present & within start window", value=False)

with right:
    st.subheader("▶ Controls & Live P&L (chart below)")
    if st.button("Start Engine (manual)"):
        cfg = {'exchange':exchange,'tradingsymbol':tradingsymbol,'instrument_token':instrument_token,'exposure':exposure,'leverage':leverage,'sl_pct':sl_pct,'instant_sl_pct':instant_sl_pct,'trail_pct':trail_pct,'start_time':start_time,'squareoff_time':squareoff_time,'sim_ltp':sim_ltp}
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
                eng.cancel_all_pending()
            except Exception:
                pass
            try:
                eng.exit_all_positions()
            except Exception:
                pass
            eng.stop()
        log("Engine stop requested (manual)")
        send_sms("Engine stop requested (manual) - flatten attempted.")
        st.success("Engine stop requested.")
    if st.button("Emergency STOP (flatten now)"):
        eng = st.session_state.get("engine")
        st.session_state["trading_active"] = False
        if eng:
            try:
                eng.cancel_all_pending()
            except Exception:
                pass
            try:
                eng.exit_all_positions()
            except Exception:
                pass
            eng.stop()
        send_sms("Emergency STOP pressed — all orders cancelled & positions exited.")
        log("Emergency STOP executed")
        st.error("Emergency STOP executed")

    st.markdown("---")
    pnl_box = st.empty()
    status_box = st.empty()
    logs_box = st.empty()

# Chart placeholder (below P&L as requested)
st.subheader("📈 Live 5-minute Candles (last 30 minutes) with VWAP (updates every 5s)")
chart_placeholder = st.empty()

# Chart worker: update chart_df, render chart and P&L
def chart_worker():
    # init chart_df
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
            if st.session_state.get("mode") == "Live" and KITE_AVAILABLE and kite and tradingsymbol:
                try:
                    ref = f"{exchange}:{tradingsymbol}"
                    d = kite.ltp(ref)
                    ltp_val = list(d.values())[0]['last_price']
                except Exception as e:
                    log(f"chart_worker kite.ltp error: {e}")
                    ltp_val = None
            if ltp_val is None:
                ltp_val = float(st.session_state.get("sim_ltp", sim_ltp)) * (1 + np.random.normal(0,0.0008))

            # update df
            if not df.empty and df.iloc[-1]['time'] == bucket:
                i = df.index[-1]
                df.at[i,'high'] = max(df.at[i,'high'], ltp_val)
                df.at[i,'low'] = min(df.at[i,'low'], ltp_val)
                df.at[i,'close'] = ltp_val
                df.at[i,'volume'] = df.at[i,'volume'] + 1
            else:
                new = {'time':bucket,'open':ltp_val,'high':ltp_val,'low':ltp_val,'close':ltp_val,'volume':1}
                df = pd.concat([df, pd.DataFrame([new])], ignore_index=True).tail(CANDLES_COUNT)

            # VWAP
            typical = (df['high'] + df['low'] + df['close'])/3.0
            df['cum_vol'] = df['volume'].cumsum()
            df['cum_vp'] = (typical * df['volume']).cumsum()
            df['vwap'] = df['cum_vp'] / df['cum_vol']
            st.session_state["chart_df"] = df

            # build chart
            fig = go.Figure(data=[go.Candlestick(x=df['time'], open=df['open'], high=df['high'], low=df['low'], close=df['close'], name='candles')])
            fig.add_trace(go.Scatter(x=df['time'], y=df['vwap'], name='VWAP', mode='lines', line=dict(color='#FFD966', width=2)))
            # optional price line for clarity
            fig.add_trace(go.Scatter(x=df['time'], y=df['close'], name='Price', mode='lines', line=dict(color='#7FD1FF', width=1), opacity=0.6))
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
            fig.update_layout(xaxis_rangeslider_visible=False, template='plotly_dark', height=460, margin=dict(l=8,r=8,t=8,b=8), paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)')

            # render P&L above chart and chart below
            eng = st.session_state.get("engine")
            if eng and getattr(eng,'entry_price', None):
                ltp_now = eng.get_ltp(f"{eng.cfg.get('exchange','NSE')}:{eng.cfg.get('tradingsymbol')}")
                if ltp_now is None:
                    ltp_now = eng.entry_price
                unreal = (ltp_now - eng.entry_price) * eng.qty
                color_class = "profit" if unreal>0 else ("loss" if unreal<0 else "neutral")
                # color-coded display using classes
                if unreal>0:
                    pnl_box.markdown(f"<h3 class='profit'>Unreal P&L: ₹{unreal:.2f}</h3>", unsafe_allow_html=True)
                elif unreal<0:
                    pnl_box.markdown(f"<h3 class='loss'>Unreal P&L: ₹{unreal:.2f}</h3>", unsafe_allow_html=True)
                else:
                    pnl_box.markdown(f"<h3 class='neutral'>Unreal P&L: ₹{unreal:.2f}</h3>", unsafe_allow_html=True)
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
                            if total>0:
                                pnl_box.markdown(f"<h3 class='profit'>Total P&L: ₹{total:.2f}</h3>", unsafe_allow_html=True)
                            elif total<0:
                                pnl_box.markdown(f"<h3 class='loss'>Total P&L: ₹{total:.2f}</h3>", unsafe_allow_html=True)
                            else:
                                pnl_box.markdown(f"<h3 class='neutral'>Total P&L: ₹{total:.2f}</h3>", unsafe_allow_html=True)
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

            # render chart
            try:
                chart_placeholder.plotly_chart(fig, use_container_width=True)
            except Exception as e:
                log(f"chart render error: {e}")

            logs_box.text_area("Logs", value="\n".join(st.session_state.get("logs", [])[-400:]), height=240)
        except Exception as e:
            log(f"chart_worker exception: {e}")
        time.sleep(REFRESH_SEC)

if 'chart_thread_v7' not in st.session_state:
    st.session_state['chart_thread_v7'] = threading.Thread(target=chart_worker, daemon=True)
    st.session_state['chart_thread_v7'].start()

# auto-start if enabled & token available
def maybe_auto_start():
    try:
        if auto_start and access_available and st.session_state.get("mode") == "Live" and kite:
            now = tz_now()
            if now.time() >= start_time and now.time() <= (datetime.combine(now.date(), start_time) + timedelta(minutes=30)).time():
                if not st.session_state.get("engine"):
                    cfg = {'exchange':exchange,'tradingsymbol':tradingsymbol,'instrument_token':instrument_token,'exposure':exposure,'leverage':leverage,'sl_pct':sl_pct,'instant_sl_pct':instant_sl_pct,'trail_pct':trail_pct,'start_time':start_time,'squareoff_time':squareoff_time,'sim_ltp':sim_ltp}
                    eng = TradingEngine(kite if st.session_state.get("mode")=="Live" else None, broker=PaperBroker() if st.session_state.get("mode")=="Paper" else None, cfg=cfg)
                    st.session_state["engine"] = eng
                    st.session_state["trading_active"] = True
                    eng.start()
                    log("Auto-started engine")
                    send_sms("Engine auto-started")
    except Exception:
        pass

maybe_auto_start()

# safety flatten at 15:20
def safety_flatten_check():
    try:
        if tz_now().time() >= dtime(15,20):
            log("15:20 safety flatten triggered")
            eng = st.session_state.get("engine")
            if eng:
                try:
                    eng.cancel_all_pending()
                except Exception:
                    pass
                try:
                    eng.exit_all_positions()
                except Exception:
                    pass
                eng.stop()
            st.session_state["trading_active"] = False
            send_sms("⚠️ Auto safety flatten executed at 15:20 IST")
            st.warning("15:20 Safety Flatten executed - all pending orders cancelled and positions exited.")
    except Exception:
        pass

safety_flatten_check()

st.markdown("---")
st.caption("If some elements still appear hidden on your device, tell me which specific UI item (exact name) and I will adjust spacing or CSS. Always test in PAPER mode first.")

