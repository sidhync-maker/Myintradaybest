# auto_trader_streamlit.py
"""
Full Streamlit intraday MIS trader with:
- Zerodha KiteConnect (manual request_token once/day; saves access_token.json)
- Twilio WhatsApp sandbox alerts (join code already provided)
- Paper / Live mode
- 5x auto-quantity MIS (exposure * leverage / ltp)
- Manual Start / Stop / Emergency
- Instant SL, Initial SL, Breakeven -> Trailing SL
- Reject protection (no retry after order rejected), cancel pending & exit triggered on stop/SL/emergency
- Live P&L (color-coded green/red/blue)
- Last 30 minutes 5-min candle chart (VWAP + price), auto-update every 5 sec
- Digital clock (IST) top-right below P&L
- 15:15 square-off and 15:20 force flatten
- Dark blue theme with sidebar styling
"""

import os
import json
import time
import threading
from datetime import datetime, timedelta, timezone
import math
import traceback

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go

# External libs (KiteConnect, Twilio)
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

# -------------------------
# ============ USER KEYS (you provided these) ============
# NOTE: these are embedded because you asked; keep them private.
# If you put this app on a public server, move them to env variables.
ZK_API_KEY = "t32mq5t5xgnjdtni"
ZK_API_SECRET = "xf9jfyfvmqo408m52l4u2gpyo34fcsfe"

# Twilio sandbox credentials (you provided)
TWILIO_ACCOUNT_SID = "ACb4124c85f9e5d7991e3cf340f844a336"
TWILIO_AUTH_TOKEN  = "ee618c2d4d3a860fa0f7f724d0dd047c"
TWILIO_WHATSAPP_FROM = "+14155238886"  # Twilio sandbox number
# your number as 'to' must be joined using the shown join code in Twilio console:
# join crimson-hawk (you already have that)
# Put your personal WhatsApp number here (with country code, e.g. +9198xxxx)
TWILIO_WHATSAPP_TO = ""  # <-- set in sidebar by user
# ========================================================

# Access token file (save Kite generate_session output)
ACCESS_TOKEN_FILE = "access_token.json"

# App defaults
DEFAULT_EXPOSURE = 50000.0
DEFAULT_LEVERAGE = 5.0
DEFAULT_SL_PCT = 3.0
DEFAULT_INSTANT_SL_PCT = 1.5
DEFAULT_TRAIL_PCT = 3.0
VWAP_CANDLES = 30  # last 30 candles (5-min)
CHART_REFRESH_SEC = 5
PNL_REFRESH_SEC = 5
PRICE_POLL_SEC = 5

# -------------------------
# Utilities
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
    # IST timezone
    return datetime.now(timezone(timedelta(hours=5, minutes=30)))

def now_ist_str():
    return now_ist().strftime("%Y-%m-%d %H:%M:%S IST")

# Simple logger
if 'app_logs' not in st.session_state:
    st.session_state['app_logs'] = []
def log(msg):
    ts = now_ist_str()
    line = f"[{ts}] {msg}"
    st.session_state['app_logs'].append(line)
    # keep tail
    if len(st.session_state['app_logs']) > 2000:
        st.session_state['app_logs'] = st.session_state['app_logs'][-2000:]

# -------------------------
# WhatsApp via Twilio helper
def send_whatsapp(message):
    """Send WhatsApp message using Twilio sandbox - best-effort."""
    to = st.session_state.get('twilio_to') or TWILIO_WHATSAPP_TO
    sid = st.session_state.get('twilio_sid') or TWILIO_ACCOUNT_SID
    auth = st.session_state.get('twilio_auth') or TWILIO_AUTH_TOKEN
    frm = st.session_state.get('twilio_from') or TWILIO_WHATSAPP_FROM
    if not to or not sid or not auth or not frm:
        log("WhatsApp not sent: Twilio credentials or To number missing")
        return False
    try:
        client = TwilioClient(sid, auth)
        msg = client.messages.create(
            from_ = f"whatsapp:{frm}",
            body = message,
            to = f"whatsapp:{to}"
        )
        log(f"WhatsApp sent sid={msg.sid}")
        return True
    except Exception as e:
        log(f"WhatsApp send failed: {e}")
        return False

# SMS fallback (simple print/log) - you can extend with Fast2SMS if required
def send_alert(message):
    log("ALERT: " + message)
    # try Twilio WhatsApp first
    ok = send_whatsapp(message)
    if not ok:
        log("WhatsApp alert failed or disabled; check Twilio credentials.")

# -------------------------
# PaperBroker simulation (light)
class PaperBroker:
    def __init__(self):
        self.positions = {}   # symbol -> {'qty','avg'}
        self.orders = {}
        self._id = 1
    def _oid(self):
        oid = f"P{self._id}"
        self._id += 1
        return oid
    def place_market_buy(self, symbol, qty, price):
        oid = self._oid()
        pos = self.positions.get(symbol, {'qty':0,'avg':0.0})
        total_qty = pos['qty'] + qty
        if pos['qty'] == 0:
            avg = price
        else:
            avg = (pos['avg']*pos['qty'] + price*qty) / total_qty
        self.positions[symbol] = {'qty': total_qty, 'avg': avg}
        self.orders[oid] = {'id':oid,'type':'BUY','symbol':symbol,'qty':qty,'price':price,'status':'FILLED'}
        log(f"[Paper] BUY {symbol} qty={qty} @ {price} id={oid}")
        return oid
    def place_market_sell(self, symbol, qty, price):
        oid = self._oid()
        pos = self.positions.get(symbol, {'qty':0,'avg':0.0})
        sell_qty = min(qty, pos['qty'])
        pos['qty'] = pos['qty'] - sell_qty
        if pos['qty'] == 0:
            pos['avg'] = 0.0
        self.positions[symbol] = pos
        self.orders[oid] = {'id':oid,'type':'SELL','symbol':symbol,'qty':sell_qty,'price':price,'status':'FILLED'}
        log(f"[Paper] SELL {symbol} qty={sell_qty} @ {price} id={oid}")
        return oid
    def get_positions(self):
        rows = []
        for sym,p in self.positions.items():
            if p.get('qty',0) != 0:
                rows.append({'tradingsymbol': sym, 'quantity': p['qty'], 'avg_price': p['avg'], 'pnl': 0.0})
        return rows
    def close_all(self):
        closed = []
        for sym,p in list(self.positions.items()):
            if p['qty'] != 0:
                closed.append({'symbol':sym,'qty':p['qty']})
                log(f"[Paper] Closed {sym} qty={p['qty']}")
                self.positions[sym] = {'qty':0,'avg':0.0}
        return closed

# -------------------------
# Trading Engine
class TradingEngine:
    def __init__(self, kite=None, broker=None, live=False, cfg=None):
        self.kite = kite
        self.broker = broker or PaperBroker()
        self.live = live and (kite is not None)
        self.cfg = cfg or {}
        self.active = False
        self._stop = threading.Event()
        self.entry_price = None
        self.qty = 0
        self.entry_time = None
        self.sl_trigger = None
        self.instant_sl_price = None
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
        if self.live and self.kite and instrument_token:
            try:
                end = datetime.now()
                start = end - timedelta(hours=1)
                data = self.kite.historical_data(int(instrument_token), start, end, "5minute")
                return pd.DataFrame(data)
            except Exception as e:
                log(f"OHLC fetch failed: {e}")
                return pd.DataFrame()
        # simulated df
        now = now_ist()
        base = float(self.cfg.get('sim_ltp', 100.0))
        N = VWAP_CANDLES
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
                log(f"LTP fetch error: {e}")
                return None
        # paper simulate
        if self.entry_price:
            return round(self.entry_price * (1 + np.random.normal(0,0.0015)),2)
        return float(self.cfg.get('sim_ltp', 100.0)) * (1 + np.random.normal(0,0.001))

    def place_market_buy(self, exchange, tradingsymbol, qty, ltp):
        # Reject-protection: after a rejected order, do not repeat
        if self.order_rejected:
            log("Order rejected earlier - not retrying buy")
            return None
        if self.live and self.kite:
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
                log(f"[Live] BUY failed/rejected: {e}")
                self.order_rejected = True
                # cancel any pending and alert
                try:
                    self.cancel_all_pending_orders()
                except:
                    pass
                send_alert(f"Order rejected for {tradingsymbol}. Not retrying. Error: {e}")
                return None
        else:
            return self.broker.place_market_buy(tradingsymbol, qty, ltp)

    def place_market_sell(self, exchange, tradingsymbol, qty, ltp):
        if self.live and self.kite:
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
                send_alert(f"SELL failed: {e}")
                return None
        else:
            return self.broker.place_market_sell(tradingsymbol, qty, ltp)

    def place_slm_try(self, exchange, tradingsymbol, qty, trigger):
        # place sell SLM as protective stop
        if self.live and self.kite:
            try:
                oid = self.kite.place_order(
                    tradingsymbol=tradingsymbol,
                    exchange=exchange,
                    transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                    quantity=qty,
                    order_type=self.kite.ORDER_TYPE_SLM,
                    trigger_price=trigger,
                    product=self.kite.PRODUCT_MIS,
                    variety=self.kite.VARIETY_REGULAR
                )
                log(f"[Live] SLM placed id={oid} trigger={trigger}")
                return oid
            except Exception as e:
                log(f"SL placement error: {e}")
                send_alert(f"SL placement failed: {e}")
                return None
        else:
            # Paper: simulate SLM active
            oid = f"PSLM{int(time.time())}"
            self.sl_order_id = oid
            log(f"[Paper] SLM simulated id={oid} trigger={trigger}")
            return oid

    def modify_slm_try(self, order_id, trigger):
        if self.live and self.kite:
            try:
                self.kite.modify_order(order_id=order_id, trigger_price=trigger)
                log(f"[Live] Modified SL {order_id} -> {trigger}")
                return True
            except Exception as e:
                log(f"Modify SL failed: {e}")
                return False
        else:
            # simulated modify
            log(f"[Paper] Modified SL {order_id} -> {trigger}")
            return True

    def cancel_all_pending_orders(self):
        cancelled = []
        if self.live and self.kite:
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
                            log(f"Failed to cancel {oid}: {e}")
                log(f"[Live] Cancelled pending orders: {cancelled}")
            except Exception as e:
                log(f"[Live] Cancel all failed: {e}")
        else:
            # Paper
            cancelled = []
            for oid,o in list(self.broker.orders.items()):
                if o.get('status') in ('ACTIVE','OPEN','TRIGGER PENDING'):
                    o['status'] = 'CANCELLED'
                    cancelled.append(oid)
            log(f"[Paper] Cancelled orders: {cancelled}")
        return cancelled

    def close_all_open_positions(self):
        closed = []
        if self.live and self.kite:
            try:
                pos = self.kite.positions()
                net = pos.get('net', []) if isinstance(pos, dict) else []
                for p in net:
                    tradingsymbol = p.get('tradingsymbol')
                    exchange = p.get('exchange') or 'NSE'
                    qty = int(p.get('quantity', 0) or 0)
                    if qty == 0: continue
                    if qty > 0:
                        tx = self.kite.TRANSACTION_TYPE_SELL
                    else:
                        tx = self.kite.TRANSACTION_TYPE_BUY
                        qty = abs(qty)
                    try:
                        order = self.kite.place_order(
                            tradingsymbol=tradingsymbol,
                            exchange=exchange,
                            transaction_type=tx,
                            quantity=qty,
                            order_type=self.kite.ORDER_TYPE_MARKET,
                            product=self.kite.PRODUCT_MIS,
                            variety=self.kite.VARIETY_REGULAR
                        )
                        closed.append({'symbol':tradingsymbol,'qty':qty,'order_id':order})
                    except Exception as e:
                        log(f"[Live] Failed to close {tradingsymbol}: {e}")
                log(f"[Live] Close orders placed: {closed}")
            except Exception as e:
                log(f"[Live] Close all failed: {e}")
        else:
            closed = self.broker.close_all()
        return closed

    def emergency_exit(self, reason="emergency"):
        log(f"Emergency exit triggered: {reason}")
        send_alert(f"Emergency exit: {reason}")
        try:
            self.cancel_all_pending_orders()
        except:
            pass
        try:
            self.close_all_open_positions()
        except:
            pass
        self.stop()

    def run(self):
        log("Engine started")
        self.active = True
        self._stop.clear()

        cfg = self.cfg
        exchange = cfg.get('exchange','NSE')
        tradingsymbol = cfg.get('tradingsymbol','RELIANCE')
        instrument_token = cfg.get('instrument_token','')
        sim_ltp = cfg.get('sim_ltp', 100.0)
        start_time = cfg.get('start_time')
        squareoff_time = cfg.get('squareoff_time')
        sl_pct = float(cfg.get('sl_pct', DEFAULT_SL_PCT))
        instant_sl_pct = float(cfg.get('instant_sl_pct', DEFAULT_INSTANT_SL_PCT))
        trail_pct = float(cfg.get('trail_pct', DEFAULT_TRAIL_PCT))

        # ensure start_time and squareoff_time are time objects (they will be)
        first_candle_used = False

        while not self._stop.is_set():
            try:
                now = now_ist()
                if now.weekday() >= 5:
                    time.sleep(10); continue

                # fetch candles
                df = self.get_5min_ohlc(instrument_token)
                if not df.empty:
                    # ensure ascending and last VWAP
                    if 'date' in df.columns:
                        df = df.sort_values('date').tail(VWAP_CANDLES)
                    typical = (df['high'] + df['low'] + df['close'])/3.0
                    df['cum_vol'] = df['volume'].cumsum()
                    df['cum_vp'] = (typical * df['volume']).cumsum()
                    df['vwap'] = df['cum_vp'] / df['cum_vol']

                # identify first candle of the day (approx)
                if len(df) >= 2:
                    first_candle = df.iloc[0]
                    latest = df.iloc[-1]
                    prev = df.iloc[-2]
                else:
                    first_candle = None
                    latest = None
                    prev = None

                # 9:15 - 9:30 first candle bullish immediate buy
                if not self.entry_price and start_time:
                    now_t = now.time()
                    window_end = (datetime.combine(now.date(), start_time) + timedelta(minutes=15)).time()
                    if start_time <= now_t <= window_end and first_candle is not None and not first_candle_used:
                        if float(first_candle['close']) > float(first_candle['open']):
                            ltp = self.get_ltp(f"{exchange}:{tradingsymbol}") or float(first_candle['close'])
                            if ltp > float(first_candle['close']):
                                qty = self.compute_qty(ltp)
                                if qty > 0:
                                    oid = self.place_market_buy(exchange, tradingsymbol, qty, ltp)
                                    if oid:
                                        self.entry_price = ltp
                                        self.qty = qty
                                        self.entry_time = now
                                        self.peak_price = ltp
                                        self.instant_sl_price = round(self.entry_price * (1 - instant_sl_pct/100),2)
                                        self.sl_trigger = round(self.entry_price * (1 - sl_pct/100),2)
                                        # place SLM (best-effort)
                                        try:
                                            self.sl_order_id = self.place_slm_try(exchange, tradingsymbol, qty, self.sl_trigger)
                                        except:
                                            pass
                                        send_alert(f"BUY executed {tradingsymbol} @{self.entry_price} qty={self.qty} SL={self.sl_trigger} InstantSL={self.instant_sl_price}")
                                        first_candle_used = True

                # after 9:30 VWAP + uptrend buy
                if not self.entry_price and latest is not None and prev is not None:
                    if start_time and now.time() > (datetime.combine(now.date(), start_time) + timedelta(minutes=15)).time():
                        try:
                            if float(latest['close']) > float(prev['close']) and float(latest['close']) > float(latest.get('vwap', -1)):
                                ltp = self.get_ltp(f"{exchange}:{tradingsymbol}") or float(latest['close'])
                                qty = self.compute_qty(ltp)
                                if qty > 0:
                                    oid = self.place_market_buy(exchange, tradingsymbol, qty, ltp)
                                    if oid:
                                        self.entry_price = ltp
                                        self.qty = qty
                                        self.entry_time = now
                                        self.peak_price = ltp
                                        self.instant_sl_price = round(self.entry_price * (1 - instant_sl_pct/100),2)
                                        self.sl_trigger = round(self.entry_price * (1 - sl_pct/100),2)
                                        try:
                                            self.sl_order_id = self.place_slm_try(exchange, tradingsymbol, qty, self.sl_trigger)
                                        except:
                                            pass
                                        send_alert(f"BUY executed {tradingsymbol} @{self.entry_price} qty={self.qty} SL={self.sl_trigger} InstantSL={self.instant_sl_price}")
                        except Exception as e:
                            log(f"After 9:30 logic error: {e}")

                # Post-entry management
                if self.entry_price is not None:
                    ltp = self.get_ltp(f"{exchange}:{tradingsymbol}") or self.entry_price
                    if ltp > self.peak_price:
                        self.peak_price = ltp
                    # Instant SL
                    if ltp <= self.instant_sl_price:
                        log(f"Instant SL hit @{ltp}")
                        send_alert(f"Instant SL hit for {tradingsymbol} @{ltp}")
                        self.place_market_sell(exchange, tradingsymbol, self.qty, ltp)
                        self.emergency_exit("Instant SL hit")
                        break
                    # Initial SL
                    if ltp <= self.sl_trigger:
                        log(f"Initial SL hit @{ltp}")
                        send_alert(f"Initial SL hit for {tradingsymbol} @{ltp}")
                        self.place_market_sell(exchange, tradingsymbol, self.qty, ltp)
                        self.emergency_exit("Initial SL hit")
                        break
                    # breakeven and trailing
                    profit_pct = (ltp - self.entry_price)/self.entry_price*100
                    if profit_pct >= (float(sl_pct) * 3.0):
                        try:
                            if self.sl_order_id and self.live:
                                self.modify_slm_try(self.sl_order_id, round(self.entry_price,2))
                                send_alert(f"Moved SL to breakeven for {tradingsymbol}")
                            else:
                                self.sl_trigger = round(self.entry_price,2)
                        except Exception as e:
                            log(f"Breakeven move error: {e}")
                    # trailing
                    trailing_trigger = round(self.peak_price * (1 - float(trail_pct)/100),2)
                    if trailing_trigger > self.sl_trigger:
                        try:
                            if self.sl_order_id and self.live:
                                self.modify_slm_try(self.sl_order_id, trailing_trigger)
                            self.sl_trigger = trailing_trigger
                            log(f"Trailing SL updated -> {trailing_trigger} (peak {self.peak_price})")
                        except Exception as e:
                            log(f"Trailing modify error: {e}")
                    # square-off
                    if squareoff_time and now.time() >= squareoff_time:
                        log("Square-off time reached -> exiting")
                        send_alert(f"Auto square-off for {tradingsymbol}")
                        self.place_market_sell(exchange, tradingsymbol, self.qty, ltp)
                        self.emergency_exit("Square-off")
                        break
                    # safety flatten at 15:20
                    safety_time = (datetime.combine(now.date(), squareoff_time) + timedelta(minutes=5)).time() if squareoff_time else None
                    if safety_time and now.time() >= safety_time:
                        log("Safety flatten time reached -> force exit")
                        send_alert("Safety flatten triggered: force exit all positions")
                        self.close_all_open_positions()
                        self.cancel_all_pending_orders()
                        self.emergency_exit("Safety flatten")
                        break

                time.sleep(PRICE_POLL_SEC)
            except Exception as e:
                log(f"Engine loop error: {e}")
                traceback.print_exc()
                send_alert(f"Engine error: {e}")
                self.stop()
                break

        log("Engine stopped")
        self.active = False

    def stop(self):
        self._stop.set()

# -------------------------
# Streamlit UI starts here
st.set_page_config(page_title="Auto Intraday Trader (Streamlit)", layout="wide")
# Custom styling (dark blue main, sidebar black)
st.markdown(
    """
    <style>
    .stApp { background: linear-gradient(#08203a, #062033); color: white; }
    .stSidebar { background: #000000; color: black; }
    .big-yellow { color: #f1c40f; font-weight:700; font-size:28px; }
    .white-bold { color: white; font-weight:700; }
    .sky-btn { background:#87ceeb !important; color:black !important; font-weight:700; }
    </style>
    """,
    unsafe_allow_html=True
)

st.markdown("<div style='background:#06273a;padding:12px;border-radius:8px'><span class='big-yellow'>Auto Intraday Trader</span></div>", unsafe_allow_html=True)

# Sidebar - connection & creds
with st.sidebar:
    st.header("Connection & Alerts (Twilio)")
    st.markdown("**Kite API (pre-filled)**")
    api_key = st.text_input("Kite API Key", value=ZK_API_KEY, type="password")
    api_secret = st.text_input("Kite API Secret", value=ZK_API_SECRET, type="password")
    st.markdown("---")
    st.markdown("**Twilio / WhatsApp Sandbox**")
    st.session_state['twilio_sid'] = st.text_input("Twilio SID", value=TWILIO_ACCOUNT_SID, type="password")
    st.session_state['twilio_auth'] = st.text_input("Twilio Auth Token", value=TWILIO_AUTH_TOKEN, type="password")
    st.session_state['twilio_from'] = st.text_input("Twilio From (sandbox)", value=TWILIO_WHATSAPP_FROM)
    st.session_state['twilio_to'] = st.text_input("Your WhatsApp number (with + country)", value=TWILIO_WHATSAPP_TO)
    st.markdown("Twilio sandbox number +14155238886 — send the join code (join crimson-hawk) to connect your WhatsApp.")
    st.markdown("---")
    st.header("Kite Token (manual daily)")
    st.info("Click 'Show Kite Login URL' then open it, login and paste request_token here.")
    if st.button("Show Kite Login URL"):
        if not KITE_AVAILABLE or not api_key:
            st.error("kiteconnect library missing or API key blank")
        else:
            t = KiteConnect(api_key=api_key)
            st.code(t.login_url())
            st.caption("Open the URL, login with Zerodha account, copy 'request_token' and paste below.")
    request_token = st.text_input("Paste request_token (from redirect)", value="")
    if st.button("Generate & Save Access Token"):
        if not KITE_AVAILABLE:
            st.error("kiteconnect not installed in environment")
        elif not api_key or not api_secret or not request_token:
            st.error("Provide API Key, Secret and request_token")
        else:
            try:
                temp = KiteConnect(api_key=api_key)
                data = temp.generate_session(request_token.strip(), api_secret=api_secret.strip())
                safe_save_json(ACCESS_TOKEN_FILE, data)
                st.success("Access token saved to access_token.json")
                log("Access token generated & saved")
            except Exception as e:
                st.error(f"Token gen failed: {e}")
                log(f"Token gen failed: {e}")

# Main layout
left, right = st.columns([3,1])

with left:
    st.subheader("Strategy / Controls")
    c1, c2 = st.columns(2)
    with c1:
        mode = st.selectbox("Mode", ["Paper","Live"], index=0)
        exchange = st.selectbox("Exchange", ["NSE","BSE"], index=0)
        tradingsymbol = st.text_input("Trading symbol (exact)", value="RELIANCE")
        instrument_token = st.text_input("Instrument token (numeric) - optional", value="")
        sim_ltp = st.number_input("Paper sim LTP", value=100.0)
    with c2:
        exposure = st.number_input("Exposure (₹)", value=DEFAULT_EXPOSURE)
        leverage = st.number_input("Leverage", value=DEFAULT_LEVERAGE, step=0.5)
        sl_pct = st.number_input("Initial SL %", value=DEFAULT_SL_PCT, step=0.1)
        instant_sl_pct = st.number_input("Instant SL %", value=DEFAULT_INSTANT_SL_PCT, step=0.1)
        trail_pct = st.number_input("Trailing % after breakeven", value=DEFAULT_TRAIL_PCT, step=0.1)
        start_time = st.time_input("Auto start time", value=datetime.strptime("09:15","%H:%M").time())
        squareoff_time = st.time_input("Auto square-off time", value=datetime.strptime("15:15","%H:%M").time())

    st.markdown("Controls")
    col1, col2, col3 = st.columns([1,1,1])
    if col1.button("Start Engine (background)"):
        # build kite if needed
        kite = None
        if mode == "Live":
            saved = safe_load_json(ACCESS_TOKEN_FILE)
            if not saved:
                st.error("No saved access token. Generate using request_token.")
            else:
                try:
                    kite = KiteConnect(api_key=api_key)
                    kite.set_access_token(saved.get('access_token'))
                    kite.profile()  # quick verify
                    st.success("Kite connected.")
                except Exception as e:
                    st.error(f"Kite connect failed: {e}")
                    log(f"Kite connect failed: {e}")
                    kite = None
        # create engine
        cfg = {
            'tradingsymbol': tradingsymbol.strip().upper(),
            'exchange': exchange,
            'instrument_token': instrument_token.strip(),
            'exposure': exposure,
            'leverage': leverage,
            'sl_pct': sl_pct,
            'instant_sl_pct': instant_sl_pct,
            'trail_pct': trail_pct,
            'start_time': start_time,
            'squareoff_time': squareoff_time,
            'sim_ltp': sim_ltp
        }
        eng = TradingEngine(kite if mode=="Live" else None, live=(mode=="Live"), cfg=cfg)
        st.session_state['engine'] = eng
        t = threading.Thread(target=eng.run, daemon=True)
        st.session_state['engine_thread'] = t
        t.start()
        log("Engine started by user")
        send_alert("Engine started (mode: {})".format(mode))
    if col2.button("Manual Stop"):
        eng = st.session_state.get('engine')
        if eng:
            eng.stop()
            # cancel & exit
            try:
                eng.cancel_all_pending_orders()
            except:
                pass
            try:
                eng.close_all_open_positions()
            except:
                pass
            send_alert("Manual Stop pressed - cancelled pending and exited triggered positions")
            st.success("Manual stop requested")
            log("Manual stop requested")
    if col3.button("Emergency STOP"):
        eng = st.session_state.get('engine')
        if eng:
            eng.emergency_exit("Manual emergency stop")
            st.success("Emergency exit triggered")
            send_alert("Emergency STOP pressed by user")

    st.markdown("---")
    st.subheader("Live Chart (last 30 5-min candles, VWAP)")
    chart_placeholder = st.empty()

with right:
    st.subheader("Status & Live P&L")
    status_placeholder = st.empty()
    pnl_placeholder = st.empty()
    clock_placeholder = st.empty()
    logs_placeholder = st.empty()

# Chart and status updater thread
def ui_updater():
    while True:
        try:
            eng = st.session_state.get('engine')
            # build df
            df = pd.DataFrame()
            if eng:
                df = eng.get_5min_ohlc(eng.cfg.get('instrument_token'))
            else:
                # simulated
                now = now_ist()
                N = VWAP_CANDLES
                base = sim_ltp
                prices = base * (1 + np.random.normal(0,0.0015,N))
                times = [now - timedelta(minutes=5*(N-i)) for i in range(N)]
                df = pd.DataFrame({'date':times, 'open':prices, 'high':prices*(1+0.001), 'low':prices*(1-0.001), 'close':prices, 'volume':np.random.randint(100,1000,N)})
            if not df.empty:
                if 'date' in df.columns:
                    df = df.sort_values('date').tail(VWAP_CANDLES)
                typical = (df['high'] + df['low'] + df['close'])/3.0
                df['cum_vol'] = df['volume'].cumsum()
                df['cum_vp'] = (typical * df['volume']).cumsum()
                df['vwap'] = df['cum_vp'] / df['cum_vol']

                fig = go.Figure(data=[go.Candlestick(x=df['date'], open=df['open'], high=df['high'], low=df['low'], close=df['close'])])
                fig.add_trace(go.Scatter(x=df['date'], y=df['vwap'], name='VWAP', mode='lines', line=dict(width=1)))
                # add markers if engine entry exists
                if eng and eng.entry_price:
                    fig.add_trace(go.Scatter(x=[eng.entry_time], y=[eng.entry_price], mode='markers', marker=dict(symbol='triangle-up', size=12), name='Entry'))
                    # SL lines
                    if eng.instant_sl_price:
                        fig.add_hline(y=eng.instant_sl_price, line=dict(color='orange', dash='dash'), annotation_text='InstantSL', annotation_position='top left')
                    if eng.sl_trigger:
                        fig.add_hline(y=eng.sl_trigger, line=dict(color='red', dash='dash'), annotation_text='CurrentSL', annotation_position='top left')
                fig.update_layout(xaxis_rangeslider_visible=False, margin=dict(l=10,r=10,t=30,b=10), height=520)
                chart_placeholder.plotly_chart(fig, use_container_width=True)
            # status & pnl & clock
            eng_local = st.session_state.get('engine')
            if eng_local and eng_local.entry_price:
                ltp = eng_local.get_ltp(f"{eng_local.cfg['exchange']}:{eng_local.cfg['tradingsymbol']}") or eng_local.entry_price
                unreal = (ltp - eng_local.entry_price) * eng_local.qty
                color = "green" if unreal > 0 else "red" if unreal < 0 else "blue"
                status_placeholder.markdown(f"**Mode:** {'Live' if eng_local.live else 'Paper'}  \n**Symbol:** {eng_local.cfg['tradingsymbol']}  \n**Entry:** ₹{eng_local.entry_price}  \n**Qty:** {eng_local.qty}")
                pnl_placeholder.markdown(f"<h3 style='color:{color};'>Unreal P&L: ₹{unreal:.2f}</h3>", unsafe_allow_html=True)
            else:
                # show account P&L if kite connected
                try:
                    saved = safe_load_json(ACCESS_TOKEN_FILE)
                    kite = None
                    if saved and KITE_AVAILABLE and mode == "Live":
                        kite = KiteConnect(api_key=api_key); kite.set_access_token(saved.get('access_token'))
                        pos = kite.positions()
                        net = pos.get('net', []) if isinstance(pos, dict) else []
                        total = 0.0
                        lines = []
                        for p in net:
                            sym = p.get('tradingsymbol'); q = int(p.get('quantity',0) or 0); pnl = float(p.get('pnl',0) or 0)
                            total += pnl
                            lines.append(f"{sym}: qty={q} pnl={pnl}")
                        color = "green" if total > 0 else "red" if total < 0 else "blue"
                        pnl_placeholder.markdown(f"<h3 style='color:{color};'>Total P&L: ₹{total:.2f}</h3>", unsafe_allow_html=True)
                        status_placeholder.text("\n".join(lines[:10]) if lines else "No open positions")
                    else:
                        pnl_placeholder.text("No active position")
                        status_placeholder.text("Engine not active")
                except Exception:
                    pnl_placeholder.text("No active position")
                    status_placeholder.text("No kite connection")
            # digital clock IST
            clock_placeholder.markdown(f"<div style='text-align:right;font-weight:700;'>⏰ {now_ist_str()}</div>", unsafe_allow_html=True)
            # logs
            logs_placeholder.text_area("Logs (latest)", value="\n".join(st.session_state['app_logs'][-100:]), height=300)
        except Exception as e:
            log(f"ui_updater error: {e}")
        time.sleep(CHART_REFRESH_SEC)

# start UI updater thread
if 'ui_thread' not in st.session_state:
    st.session_state['ui_thread'] = threading.Thread(target=ui_updater, daemon=True)
    st.session_state['ui_thread'].start()

st.caption("Run in Paper mode to test fully. For Live mode ensure saved access_token.json exists and KiteConnect is installed.")
