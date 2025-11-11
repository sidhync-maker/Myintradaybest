# auto_intraday_trader_theme.py
"""
Full Streamlit intraday trader with custom vibrant theme,
Zerodha KiteConnect (manual request_token/day), Twilio WhatsApp sandbox,
Paper/Live mode, instant/initial/trailing SL, reject-protection, emergency/manual stop,
live P&L color, VWAP chart (last 30 x 5-min candles), IST digital clock,
15:15 square-off & 15:20 safety flatten, auto-update every 5s.
"""

import os
import json
import time
import threading
import traceback
from datetime import datetime, timedelta, timezone, time as dtime

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go

# Optional libs
try:
    from kiteconnect import KiteConnect
    KITE_AVAILABLE = True
except Exception:
    KiteConnect = None
    KITE_AVAILABLE = False

try:
    from twilio.rest import Client as TwilioClient
    TWILIO_AVAILABLE = True
except Exception:
    TwilioClient = None
    TWILIO_AVAILABLE = False

# -------------------- USER KEYS (Paste your keys or set as env vars) --------------------
# You provided earlier; keep them secure. You can also set these via environment variables.
ZK_API_KEY = os.getenv("ZK_API_KEY", "t32mq5t5xgnjdtni")
ZK_API_SECRET = os.getenv("ZK_API_SECRET", "xf9jfyfvmqo408m52l4u2gpyo34fcsfe")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "ACb4124c85f9e5d7991e3cf340f844a336")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "ee618c2d4d3a860fa0f7f724d0dd047c")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "+14155238886")  # Twilio sandbox
# Set TWILIO_WHATSAPP_TO in sidebar after starting app
# ---------------------------------------------------------------------------------------

# Files & defaults
ACCESS_TOKEN_FILE = "access_token.json"
VWAP_CANDLES = 30
CHART_REFRESH_SEC = 5
PNL_REFRESH_SEC = 5
PRICE_POLL_SEC = 5

DEFAULT_EXPOSURE = 50000.0
DEFAULT_LEVERAGE = 5.0
DEFAULT_SL_PCT = 3.0
DEFAULT_INSTANT_SL_PCT = 1.5
DEFAULT_TRAIL_PCT = 3.0

# -------------------- Utilities --------------------
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

def now_ist():
    return datetime.now(timezone(timedelta(hours=5, minutes=30)))

def now_ist_str():
    return now_ist().strftime("%Y-%m-%d %H:%M:%S IST")

# -------------------- Logger --------------------
if 'logs' not in st.session_state:
    st.session_state['logs'] = []
def logger_add(msg):
    ts = now_ist_str()
    st.session_state['logs'].append(f"[{ts}] {msg}")
    if len(st.session_state['logs']) > 2000:
        st.session_state['logs'] = st.session_state['logs'][-2000:]

# -------------------- WhatsApp / Twilio helper --------------------
def send_whatsapp_via_twilio(to_number, message, sid=None, auth=None, frm=None):
    sid = sid or st.session_state.get('twilio_sid') or TWILIO_ACCOUNT_SID
    auth = auth or st.session_state.get('twilio_auth') or TWILIO_AUTH_TOKEN
    frm = frm or st.session_state.get('twilio_from') or TWILIO_WHATSAPP_FROM
    to = to_number or st.session_state.get('twilio_to')
    if not (sid and auth and frm and to):
        logger_add("WhatsApp alert not sent: Twilio credentials or to-number missing.")
        return False
    try:
        client = TwilioClient(sid, auth)
        m = client.messages.create(
            from_=f"whatsapp:{frm}",
            body=message,
            to=f"whatsapp:{to}"
        )
        logger_add(f"WhatsApp sent (sid={m.sid})")
        return True
    except Exception as e:
        logger_add(f"WhatsApp send failed: {e}")
        return False

def send_alert(msg):
    logger_add("ALERT: " + msg)
    # best-effort - still continue
    send_whatsapp_via_twilio(st.session_state.get('twilio_to'), msg)

# -------------------- Paper broker (simulate) --------------------
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
        total = pos['qty'] + qty
        avg = price if pos['qty']==0 else (pos['avg']*pos['qty'] + price*qty)/total
        self.positions[symbol] = {'qty': total, 'avg': avg}
        self.orders[oid] = {'id':oid,'type':'BUY','symbol':symbol,'qty':qty,'price':price,'status':'FILLED'}
        logger_add(f"[Paper] BUY {symbol} qty={qty} @ {price} id={oid}")
        return oid
    def place_market_sell(self, symbol, qty, price):
        oid = self._oid()
        pos = self.positions.get(symbol, {'qty':0,'avg':0.0})
        sell_qty = min(qty, pos['qty'])
        pos['qty'] = pos['qty'] - sell_qty
        if pos['qty']==0:
            pos['avg'] = 0.0
        self.positions[symbol] = pos
        self.orders[oid] = {'id':oid,'type':'SELL','symbol':symbol,'qty':sell_qty,'price':price,'status':'FILLED'}
        logger_add(f"[Paper] SELL {symbol} qty={sell_qty} @ {price} id={oid}")
        return oid
    def get_positions(self):
        res=[]
        for s,p in self.positions.items():
            if p.get('qty',0)!=0:
                res.append({'tradingsymbol':s,'quantity':p['qty'],'avg_price':p['avg'],'pnl':0.0})
        return res
    def close_all(self):
        for s in list(self.positions.keys()):
            if self.positions[s]['qty']!=0:
                logger_add(f"[Paper] Closed {s} qty={self.positions[s]['qty']}")
            self.positions[s]={'qty':0,'avg':0.0}

# -------------------- Trading Engine --------------------
class TradingEngine:
    def __init__(self, kite=None, broker=None, live=False, cfg=None):
        self.kite = kite
        self.broker = broker or PaperBroker()
        self.live = live and (kite is not None)
        self.cfg = cfg or {}
        self._stop = threading.Event()
        self.active = False
        # runtime
        self.entry_price = None
        self.qty = 0
        self.entry_time = None
        self.sl_trigger = None
        self.instant_sl = None
        self.peak_price = None
        self.sl_order_id = None
        self.order_rejected = False

    def compute_qty(self, ltp):
        if ltp <= 0:
            return 0
        exposure = float(self.cfg.get('exposure', DEFAULT_EXPOSURE))
        leverage = float(self.cfg.get('leverage', DEFAULT_LEVERAGE))
        q = int((exposure * leverage) // ltp)
        return max(1, q)

    def get_5min_ohlc(self, instrument_token):
        # live via kite historical_data if available (best-effort); else simulated candles
        if self.live and self.kite and instrument_token:
            try:
                end = datetime.now()
                start = end - timedelta(hours=1)
                data = self.kite.historical_data(int(instrument_token), start, end, "5minute")
                return pd.DataFrame(data)
            except Exception as e:
                logger_add(f"OHLC fetch error: {e}")
                return pd.DataFrame()
        # simulate
        now = now_ist()
        N = VWAP_CANDLES
        base = float(self.cfg.get('sim_ltp', 100.0))
        prices = base * (1 + np.random.normal(0,0.0015,N))
        times = [now - timedelta(minutes=5*(N-i)) for i in range(N)]
        df = pd.DataFrame({'date':times, 'open':prices, 'high':prices*(1+0.001), 'low':prices*(1-0.001), 'close':prices, 'volume':np.random.randint(100,1000,N)})
        return df

    def get_ltp(self, ref):
        if self.live and self.kite:
            try:
                d = self.kite.ltp(ref)
                return list(d.values())[0]['last_price']
            except Exception as e:
                logger_add(f"LTP fetch error: {e}")
                return None
        # paper simulate small noise around sim_ltp or entry
        if self.entry_price:
            return round(self.entry_price * (1 + np.random.normal(0,0.0015)),2)
        return float(self.cfg.get('sim_ltp', 100.0)) * (1 + np.random.normal(0,0.0015))

    def place_market_buy(self, exchange, symbol, qty, ltp):
        # do not retry after order rejection
        if self.order_rejected:
            logger_add("Buy skipped due to previous rejection (no retry).")
            return None
        if self.live:
            try:
                oid = self.kite.place_order(
                    tradingsymbol=symbol, exchange=exchange,
                    transaction_type=self.kite.TRANSACTION_TYPE_BUY,
                    quantity=qty, order_type=self.kite.ORDER_TYPE_MARKET,
                    product=self.kite.PRODUCT_MIS, variety=self.kite.VARIETY_REGULAR
                )
                logger_add(f"[Live] BUY placed id={oid} qty={qty}")
                return oid
            except Exception as e:
                logger_add(f"[Live] BUY failed/rejected: {e}")
                self.order_rejected = True
                # cancel pending and alert
                try: self.cancel_all_pending_orders()
                except: pass
                send_alert(f"Buy order rejected for {symbol}. No retry. Error: {e}")
                return None
        else:
            return self.broker.place_market_buy(symbol, qty, ltp)

    def place_market_sell(self, exchange, symbol, qty, ltp):
        if self.live:
            try:
                oid = self.kite.place_order(
                    tradingsymbol=symbol, exchange=exchange,
                    transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                    quantity=qty, order_type=self.kite.ORDER_TYPE_MARKET,
                    product=self.kite.PRODUCT_MIS, variety=self.kite.VARIETY_REGULAR
                )
                logger_add(f"[Live] SELL placed id={oid} qty={qty}")
                return oid
            except Exception as e:
                logger_add(f"[Live] SELL failed: {e}")
                send_alert(f"SELL failed: {e}")
                return None
        else:
            return self.broker.place_market_sell(symbol, qty, ltp)

    def place_slm_try(self, exchange, symbol, qty, trigger):
        if self.live:
            try:
                oid = self.kite.place_order(
                    tradingsymbol=symbol, exchange=exchange,
                    transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                    quantity=qty, order_type=self.kite.ORDER_TYPE_SLM,
                    trigger_price=trigger, product=self.kite.PRODUCT_MIS, variety=self.kite.VARIETY_REGULAR
                )
                logger_add(f"[Live] SLM placed id={oid} trigger={trigger}")
                return oid
            except Exception as e:
                logger_add(f"SLM placement error: {e}")
                send_alert(f"SL placement failed: {e}")
                return None
        else:
            oid = f"PSLM{int(time.time())}"
            self.sl_order_id = oid
            logger_add(f"[Paper] Simulated SLM id={oid} trigger={trigger}")
            return oid

    def modify_slm_try(self, order_id, trigger):
        if self.live:
            try:
                self.kite.modify_order(order_id=order_id, trigger_price=trigger)
                logger_add(f"[Live] SL modified {order_id} -> {trigger}")
                return True
            except Exception as e:
                logger_add(f"Modify SL failed: {e}")
                return False
        else:
            logger_add(f"[Paper] Modified SL {order_id} -> {trigger}")
            return True

    def cancel_all_pending_orders(self):
        cancelled=[]
        if self.live:
            try:
                orders = self.kite.orders()
                for o in orders:
                    status = (o.get('status') or "").upper()
                    oid = o.get('order_id')
                    if status in ('OPEN','TRIGGER PENDING','PENDING'):
                        try:
                            variety = o.get('variety', self.kite.VARIETY_REGULAR)
                            self.kite.cancel_order(order_id=oid, variety=variety)
                            cancelled.append(oid)
                        except Exception as e:
                            logger_add(f"Cancel error {oid}: {e}")
                logger_add(f"[Live] Cancelled pending: {cancelled}")
            except Exception as e:
                logger_add(f"Cancel all failed: {e}")
        else:
            for oid,o in list(self.broker.orders.items()):
                if o.get('status') in ('ACTIVE','OPEN','TRIGGER PENDING'):
                    o['status']='CANCELLED'
                    cancelled.append(oid)
            logger_add(f"[Paper] Cancelled orders: {cancelled}")
        return cancelled

    def close_all_open_positions(self):
        closed=[]
        if self.live:
            try:
                pos = self.kite.positions()
                net = pos.get('net',[]) if isinstance(pos,dict) else []
                for p in net:
                    symbol = p.get('tradingsymbol'); exch = p.get('exchange') or 'NSE'
                    qty = int(p.get('quantity',0) or 0)
                    if qty==0: continue
                    if qty>0:
                        tx = self.kite.TRANSACTION_TYPE_SELL
                    else:
                        tx = self.kite.TRANSACTION_TYPE_BUY; qty = abs(qty)
                    try:
                        order = self.kite.place_order(tradingsymbol=symbol, exchange=exch, transaction_type=tx, quantity=qty, order_type=self.kite.ORDER_TYPE_MARKET, product=self.kite.PRODUCT_MIS, variety=self.kite.VARIETY_REGULAR)
                        closed.append({'symbol':symbol,'qty':qty,'order_id':order})
                    except Exception as e:
                        logger_add(f"Failed to close {symbol}: {e}")
                logger_add(f"[Live] Close placed: {closed}")
            except Exception as e:
                logger_add(f"Close all failed: {e}")
        else:
            self.broker.close_all()
            logger_add("[Paper] All positions closed")
        return closed

    def emergency_exit(self, reason="emergency"):
        logger_add(f"Emergency exit: {reason}")
        send_alert(f"Emergency exit: {reason}")
        try:
            self.cancel_all_pending_orders()
        except: pass
        try:
            self.close_all_open_positions()
        except: pass
        self.stop()

    def run(self):
        logger_add("Engine started")
        self.active = True
        self._stop.clear()

        cfg = self.cfg
        exchange = cfg.get('exchange','NSE')
        symbol = cfg.get('tradingsymbol','RELIANCE')
        instrument_token = cfg.get('instrument_token','')
        sim_ltp = float(cfg.get('sim_ltp',100.0))
        start_time = cfg.get('start_time')
        squareoff_time = cfg.get('squareoff_time')
        sl_pct = float(cfg.get('sl_pct', DEFAULT_SL_PCT))
        instant_sl_pct = float(cfg.get('instant_sl_pct', DEFAULT_INSTANT_SL_PCT))
        trail_pct = float(cfg.get('trail_pct', DEFAULT_TRAIL_PCT))

        first_candle_used = False

        while not self._stop.is_set():
            try:
                now = now_ist()
                if now.weekday() >= 5:
                    time.sleep(10); continue

                # fetch candles
                df = self.get_5min_ohlc(instrument_token)
                if not df.empty and 'volume' in df.columns:
                    if 'date' in df.columns:
                        df = df.sort_values('date').tail(VWAP_CANDLES)
                    typical = (df['high'] + df['low'] + df['close'])/3.0
                    df['cum_vol'] = df['volume'].cumsum()
                    df['cum_vp'] = (typical * df['volume']).cumsum()
                    df['vwap'] = df['cum_vp'] / df['cum_vol']
                # identify candles
                latest = df.iloc[-1] if len(df)>=1 else None
                prev = df.iloc[-2] if len(df)>=2 else None
                first_candle = df.iloc[0] if len(df)>=1 else None

                # 9:15-9:30 first candle bullish immediate buy
                if not self.entry_price and start_time:
                    now_t = now.time()
                    window_end = (datetime.combine(now.date(), start_time) + timedelta(minutes=15)).time()
                    if start_time <= now_t <= window_end and first_candle is not None and not first_candle_used:
                        if float(first_candle['close']) > float(first_candle['open']):
                            ltp = self.get_ltp(f"{exchange}:{symbol}") or float(first_candle['close'])
                            if ltp > float(first_candle['close']):
                                qty = self.compute_qty(ltp)
                                if qty > 0:
                                    oid = self.place_market_buy(exchange, symbol, qty, ltp)
                                    if oid:
                                        self.entry_price = ltp; self.qty = qty; self.entry_time = now; self.peak_price = ltp
                                        self.instant_sl = round(self.entry_price * (1 - instant_sl_pct/100),2)
                                        self.sl_trigger = round(self.entry_price * (1 - sl_pct/100),2)
                                        try: self.sl_order_id = self.place_slm_try(exchange, symbol, qty, self.sl_trigger)
                                        except: pass
                                        send_alert(f"BUY executed {symbol} @{self.entry_price} qty={self.qty} SL={self.sl_trigger} InstantSL={self.instant_sl}")
                                        first_candle_used = True

                # after 9:30 VWAP + uptrend buy
                if not self.entry_price and latest is not None and prev is not None:
                    if start_time and now.time() > (datetime.combine(now.date(), start_time) + timedelta(minutes=15)).time():
                        try:
                            if float(latest['close']) > float(prev['close']) and float(latest['close']) > float(latest.get('vwap', -1)):
                                ltp = self.get_ltp(f"{exchange}:{symbol}") or float(latest['close'])
                                qty = self.compute_qty(ltp)
                                if qty > 0:
                                    oid = self.place_market_buy(exchange, symbol, qty, ltp)
                                    if oid:
                                        self.entry_price = ltp; self.qty = qty; self.entry_time = now; self.peak_price = ltp
                                        self.instant_sl = round(self.entry_price * (1 - instant_sl_pct/100),2)
                                        self.sl_trigger = round(self.entry_price * (1 - sl_pct/100),2)
                                        try: self.sl_order_id = self.place_slm_try(exchange, symbol, qty, self.sl_trigger)
                                        except: pass
                                        send_alert(f"BUY executed {symbol} @{self.entry_price} qty={self.qty} SL={self.sl_trigger} InstantSL={self.instant_sl}")
                        except Exception as e:
                            logger_add(f"After9:30 logic error: {e}")

                # post-entry management
                if self.entry_price is not None:
                    ltp = self.get_ltp(f"{exchange}:{symbol}") or self.entry_price
                    if ltp > self.peak_price: self.peak_price = ltp
                    # instant SL
                    if ltp <= self.instant_sl:
                        logger_add(f"Instant SL hit @{ltp}")
                        send_alert(f"Instant SL hit for {symbol} @{ltp}")
                        self.place_market_sell(exchange, symbol, self.qty, ltp)
                        self.emergency_exit("Instant SL hit")
                        break
                    # initial SL
                    if ltp <= self.sl_trigger:
                        logger_add(f"Initial SL hit @{ltp}")
                        send_alert(f"Initial SL hit for {symbol} @{ltp}")
                        self.place_market_sell(exchange, symbol, self.qty, ltp)
                        self.emergency_exit("Initial SL hit")
                        break
                    # breakeven
                    profit_pct = (ltp - self.entry_price)/self.entry_price*100
                    if profit_pct >= (sl_pct * 3.0):
                        try:
                            if self.sl_order_id and self.live:
                                self.modify_slm_try(self.sl_order_id, round(self.entry_price,2))
                                send_alert(f"Moved SL to breakeven for {symbol}")
                            else:
                                self.sl_trigger = round(self.entry_price,2)
                        except Exception as e:
                            logger_add(f"Breakeven error: {e}")
                    # trailing
                    trailing_trigger = round(self.peak_price * (1 - trail_pct/100),2)
                    if trailing_trigger > self.sl_trigger:
                        try:
                            if self.sl_order_id and self.live:
                                self.modify_slm_try(self.sl_order_id, trailing_trigger)
                            self.sl_trigger = trailing_trigger
                            logger_add(f"Trailing updated -> {trailing_trigger}")
                        except Exception as e:
                            logger_add(f"Trailing modify error: {e}")
                    # square-off
                    if squareoff_time and now.time() >= squareoff_time:
                        logger_add("Square-off reached -> exiting")
                        send_alert(f"Auto square-off for {symbol}")
                        self.place_market_sell(exchange, symbol, self.qty, ltp)
                        self.emergency_exit("Square-off")
                        break
                    # safety flatten 5 min after squareoff
                    safety_time = (datetime.combine(now.date(), squareoff_time) + timedelta(minutes=5)).time() if squareoff_time else None
                    if safety_time and now.time() >= safety_time:
                        logger_add("Safety flatten -> force exit")
                        send_alert("Safety flatten triggered")
                        self.close_all_open_positions()
                        self.cancel_all_pending_orders()
                        self.emergency_exit("Safety flatten")
                        break

                time.sleep(PRICE_POLL_SEC)
            except Exception as e:
                logger_add(f"Engine loop error: {e}")
                traceback.print_exc()
                send_alert(f"Engine error: {e}")
                self.stop()
                break

        logger_add("Engine stopped")
        self.active = False

    def stop(self):
        self._stop.set()

# -------------------- Streamlit UI --------------------
st.set_page_config(page_title="Auto Intraday Trader — Vibrant Theme", layout="wide")
# Custom theme CSS (main deep blue gradient, gold title, sky-blue buttons)
st.markdown("""
<style>
/* page */
.stApp {
  background: linear-gradient(180deg,#001f3f 0%, #003366 100%);
  color: #ffffff;
}
/* main content card */
.main-card {
  background: rgba(3,18,32,0.6);
  padding: 12px;
  border-radius: 10px;
  color: #ffffff;
}
/* title */
.app-title {
  color: #FFD700;
  font-weight: 800;
  font-size: 28px;
  margin-bottom: 6px;
}
/* sidebar */
[data-testid="stSidebar"] {
  background-color: #000000;
  color: #ffffff;
}
/* inputs on sidebar */
.stTextInput > label, .stNumberInput > label, .stSelectbox > label {
  color: #000000 !important;
  font-weight:700;
}
/* sky-blue buttons */
.stButton>button {
  background: linear-gradient(180deg,#87ceeb,#00bfff) !important;
  color: #000000 !important;
  font-weight:700;
  border: none;
}
/* high-contrast labels on dark */
label, .css-1v0mbdj-Label {
  color: #ffffff !important;
  font-weight:700;
}
h3, h2 { color: #ffffff !important; }

/* P&L colors will be inline styled */
</style>
""", unsafe_allow_html=True)

st.markdown("<div class='main-card'><div class='app-title'>Auto Intraday Trader — Vibrant Theme</div></div>", unsafe_allow_html=True)

# Sidebar: connection, twilio, kite token
with st.sidebar:
    st.header("Connection & Alerts")
    st.markdown("**Kite (Zerodha)**")
    api_key_in = st.text_input("Kite API Key", value=ZK_API_KEY, type="password")
    api_secret_in = st.text_input("Kite API Secret", value=ZK_API_SECRET, type="password")
    st.markdown("---")
    st.markdown("**Twilio / WhatsApp Sandbox**")
    st.session_state['twilio_sid'] = st.text_input("Twilio SID", value=TWILIO_ACCOUNT_SID, type="password")
    st.session_state['twilio_auth'] = st.text_input("Twilio Auth Token", value=TWILIO_AUTH_TOKEN, type="password")
    st.session_state['twilio_from'] = st.text_input("Twilio From (sandbox)", value=TWILIO_WHATSAPP_FROM)
    st.session_state['twilio_to'] = st.text_input("Your WhatsApp (with +)", value=os.getenv("TWILIO_WHATSAPP_TO",""))
    st.markdown("Join Twilio sandbox: send `join crimson-hawk` to +14155238886 from your WhatsApp.")
    if st.button("Test WhatsApp"):
        to = st.session_state.get('twilio_to')
        ok = send_whatsapp_via_twilio(to, "Test WhatsApp from Auto Intraday Trader (twilio sandbox).")
        if ok: st.success("WhatsApp test sent (check your phone).")
        else: st.error("WhatsApp test failed; check Twilio creds & join sandbox.")
    st.markdown("---")
    st.header("Kite Token (manual daily)")
    st.info("Click Show Kite Login URL -> login -> copy request_token from redirect -> paste -> Generate & Save")
    if st.button("Show Kite Login URL"):
        if not KITE_AVAILABLE or not api_key_in:
            st.error("kiteconnect missing or API key blank")
        else:
            t = KiteConnect(api_key=api_key_in)
            st.code(t.login_url())
            st.caption("Open the URL, login, copy request_token from redirect URL and paste below.")
    request_token = st.text_input("Paste request_token (from redirect)", value="")
    if st.button("Generate & Save Access Token"):
        if not KITE_AVAILABLE:
            st.error("kiteconnect lib missing")
        elif not api_key_in or not api_secret_in or not request_token:
            st.error("Provide API Key, Secret & request_token")
        else:
            try:
                temp = KiteConnect(api_key=api_key_in)
                data = temp.generate_session(request_token.strip(), api_secret=api_secret_in.strip())
                safe_save_json(ACCESS_TOKEN_FILE, data)
                st.success("Saved access_token.json")
                logger_add("Access token saved.")
                send_alert("Kite access token generated & saved.")
            except Exception as e:
                st.error(f"Token gen failed: {e}")
                logger_add(f"Token gen failed: {e}")

# Main layout
left, right = st.columns([3,1])
with left:
    st.subheader("Strategy & Controls")
    c1, c2 = st.columns(2)
    with c1:
        mode = st.selectbox("Mode", ["Paper","Live"], index=0)
        exchange = st.selectbox("Exchange", ["NSE","BSE"], index=0)
        tradingsymbol = st.text_input("Trading Symbol (exact)", value="RELIANCE").strip().upper()
        instrument_token = st.text_input("Instrument token (numeric) - optional", value="")
        sim_ltp = st.number_input("Paper Sim LTP", value=100.0)
    with c2:
        exposure = st.number_input("Exposure (₹)", value=DEFAULT_EXPOSURE)
        leverage = st.number_input("Leverage", value=DEFAULT_LEVERAGE, step=0.5)
        sl_pct = st.number_input("Initial SL %", value=DEFAULT_SL_PCT, step=0.1)
        instant_sl_pct = st.number_input("Instant SL %", value=DEFAULT_INSTANT_SL_PCT, step=0.1)
        trail_pct = st.number_input("Trailing % after breakeven", value=DEFAULT_TRAIL_PCT, step=0.1)
        start_time = st.time_input("Auto Start Time", value=dtime(9,15))
        squareoff_time = st.time_input("Auto Square-off Time", value=dtime(15,15))

    st.markdown("**Controls**")
    b1, b2, b3 = st.columns(3)
    if b1.button("Start Engine"):
        # build kite if needed
        kite = None
        if mode == "Live":
            saved = safe_load_json(ACCESS_TOKEN_FILE)
            if not saved:
                st.error("No saved access token. Generate via sidebar.")
            else:
                try:
                    kite = KiteConnect(api_key=api_key_in)
                    kite.set_access_token(saved.get('access_token'))
                    kite.profile()
                    st.success("Kite connected.")
                except Exception as e:
                    st.error(f"Kite connect failed: {e}")
                    logger_add(f"Kite connect failed: {e}")
                    kite = None
        cfg = {'tradingsymbol':tradingsymbol,'exchange':exchange,'instrument_token':instrument_token,'exposure':exposure,'leverage':leverage,'sl_pct':sl_pct,'instant_sl_pct':instant_sl_pct,'trail_pct':trail_pct,'start_time':start_time,'squareoff_time':squareoff_time,'sim_ltp':sim_ltp}
        eng = TradingEngine(kite=kite, live=(mode=="Live"), cfg=cfg)
        st.session_state['engine'] = eng
        t = threading.Thread(target=eng.run, daemon=True)
        st.session_state['engine_thread'] = t
        t.start()
        logger_add("Engine started by user")
        send_alert(f"Engine started (mode={mode}) for {tradingsymbol}")
    if b2.button("Manual Stop"):
        eng = st.session_state.get('engine')
        if eng:
            eng.stop()
            try: eng.cancel_all_pending_orders()
            except: pass
            try: eng.close_all_open_positions()
            except: pass
            send_alert("Manual stop pressed - cancelled pending & exited triggered positions")
            st.success("Manual stop requested")
            logger_add("Manual stop requested")
    if b3.button("EMERGENCY STOP"):
        eng = st.session_state.get('engine')
        if eng:
            eng.emergency_exit("User emergency stop")
            st.success("Emergency exit executed")
            send_alert("Emergency STOP pressed by user")
            logger_add("Emergency STOP pressed")

    st.markdown("---")
    st.subheader("Live Chart — Last 30 x 5-min (VWAP)")
    chart_placeholder = st.empty()

with right:
    st.subheader("Status & Live P&L")
    status_box = st.empty()
    pnl_box = st.empty()
    clock_box = st.empty()
    logs_box = st.empty()

# UI updater thread: chart, P&L, clock, logs
def ui_updater():
    while True:
        try:
            eng = st.session_state.get('engine')
            # candles
            df = pd.DataFrame()
            if eng:
                df = eng.get_5min_ohlc(eng.cfg.get('instrument_token'))
            else:
                # simulate
                now = now_ist()
                N = VWAP_CANDLES
                base = sim_ltp
                prices = base * (1 + np.random.normal(0,0.0015,N))
                times = [now - timedelta(minutes=5*(N-i)) for i in range(N)]
                df = pd.DataFrame({'date':times,'open':prices,'high':prices*(1+0.001),'low':prices*(1-0.001),'close':prices,'volume':np.random.randint(100,1000,N)})
            if not df.empty:
                if 'date' in df.columns:
                    df = df.sort_values('date').tail(VWAP_CANDLES)
                typical = (df['high']+df['low']+df['close'])/3.0
                df['cum_vol'] = df['volume'].cumsum()
                df['cum_vp'] = (typical * df['volume']).cumsum()
                df['vwap'] = df['cum_vp']/df['cum_vol']
                fig = go.Figure(data=[go.Candlestick(x=df['date'], open=df['open'], high=df['high'], low=df['low'], close=df['close'])])
                fig.add_trace(go.Scatter(x=df['date'], y=df['vwap'], name='VWAP', mode='lines', line=dict(width=1)))
                eng_local = st.session_state.get('engine')
                if eng_local and eng_local.entry_price:
                    fig.add_trace(go.Scatter(x=[eng_local.entry_time], y=[eng_local.entry_price], mode='markers', marker=dict(symbol='triangle-up', size=12, color='green'), name='Entry'))
                    if eng_local.instant_sl:
                        fig.add_hline(y=eng_local.instant_sl, line=dict(color='orange', dash='dash'), annotation_text='InstantSL', annotation_position='top left')
                    if eng_local.sl_trigger:
                        fig.add_hline(y=eng_local.sl_trigger, line=dict(color='red', dash='dash'), annotation_text='SL', annotation_position='top left')
                fig.update_layout(xaxis_rangeslider_visible=False, margin=dict(l=10,r=10,t=30,b=10), height=520, plot_bgcolor='#021323', paper_bgcolor='#021323', font_color='white')
                chart_placeholder.plotly_chart(fig, use_container_width=True)
            # status/pnl/clock
            eng_local = st.session_state.get('engine')
            if eng_local and eng_local.entry_price:
                ltp = eng_local.get_ltp(f"{eng_local.cfg['exchange']}:{eng_local.cfg['tradingsymbol']}") or eng_local.entry_price
                unreal = (ltp - eng_local.entry_price) * eng_local.qty
                color = "green" if unreal>0 else "red" if unreal<0 else "blue"
                status_box.markdown(f"**Mode:** {'Live' if eng_local.live else 'Paper'}  \n**Symbol:** {eng_local.cfg['tradingsymbol']}  \n**Entry:** ₹{eng_local.entry_price}  \n**Qty:** {eng_local.qty}")
                pnl_box.markdown(f"<h3 style='color:{color};'>Unreal P&L: ₹{unreal:.2f}</h3>", unsafe_allow_html=True)
            else:
                # try Kite positions if live selected
                try:
                    saved = safe_load_json(ACCESS_TOKEN_FILE)
                    kite = None
                    if saved and KITE_AVAILABLE and mode=="Live":
                        kite = KiteConnect(api_key=api_key_in); kite.set_access_token(saved.get('access_token'))
                        pos = kite.positions()
                        net = pos.get('net',[]) if isinstance(pos,dict) else []
                        total = 0.0
                        lines=[]
                        for p in net:
                            sym = p.get('tradingsymbol'); q=int(p.get('quantity',0) or 0); pnl = float(p.get('pnl',0) or 0)
                            total += pnl
                            lines.append(f"{sym}: qty={q} pnl={pnl}")
                        color = "green" if total>0 else "red" if total<0 else "blue"
                        pnl_box.markdown(f"<h3 style='color:{color};'>Total P&L: ₹{total:.2f}</h3>", unsafe_allow_html=True)
                        status_box.text("\n".join(lines[:10]) if lines else "No open positions")
                    else:
                        pnl_box.text("No active position")
                        status_box.text("Engine not active")
                except Exception:
                    pnl_box.text("No active position")
                    status_box.text("No kite connection")
            # digital clock IST (top-right)
            clock_box.markdown(f"<div style='text-align:right;font-weight:800;color:#00FFFF;'>⏰ {now_ist_str()}</div>", unsafe_allow_html=True)
            # logs
            logs_box.text_area("Logs (latest)", value="\n".join(st.session_state['logs'][-200:]), height=300)
        except Exception as e:
            logger_add(f"ui_updater error: {e}")
        time.sleep(CHART_REFRESH_SEC)

if 'ui_thread' not in st.session_state:
    st.session_state['ui_thread'] = threading.Thread(target=ui_updater, daemon=True)
    st.session_state['ui_thread'].start()

st.caption("Tip: Test fully in Paper mode. Carefully test Live mode with small exposure first.")
