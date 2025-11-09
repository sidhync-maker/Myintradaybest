# auto_streamlit_full.py
"""
Full Streamlit Zerodha Auto Trader (Option 1 style: manual login once per day)
Features:
 - Safe token handling (request_token -> saved access_token)
 - Paper / Live modes
 - Scheduled start time (default 09:15) and auto square-off (default 15:15)
 - Initial SL %, breakeven trigger, trailing SL
 - Emergency exit (cancel & close)
 - Live P&L dashboard
 - Fast2SMS SMS alerts
"""

import os
import time
import threading
import traceback
import requests
import json
from datetime import datetime, time as dtime, timedelta

import streamlit as st
import pandas as pd
import numpy as np

# optional import (kiteconnect might not be installed while developing)
try:
    from kiteconnect import KiteConnect
    KITE_AVAILABLE = True
except Exception:
    KiteConnect = None
    KITE_AVAILABLE = False

# ------------------------
# CONFIG (edit / use env vars)
# ------------------------
# Zerodha app credentials (do not hardcode in production; prefer env vars)
API_KEY = os.getenv("ZK_API_KEY", "")      # e.g., "t32mq..."
API_SECRET = os.getenv("ZK_API_SECRET", "")  # e.g., "xf9jf..."
REDIRECT_URL = os.getenv("ZK_REDIRECT_URL", "http://localhost:8501")

# Access token storage (JSON contains kite.generate_session() output)
ACCESS_TOKEN_FILE = os.getenv("ACCESS_TOKEN_FILE", "access_token.json")

# SMS (Fast2SMS) config
FAST2SMS_API_KEY = os.getenv("FAST2SMS_API_KEY", "")  # put your Fast2SMS key or leave blank to disable SMS
USER_PHONE = os.getenv("USER_PHONE", "")  # e.g., "919876543210"
FAST2SMS_URL = "https://www.fast2sms.com/dev/bulkV2"

# Defaults for trading
DEFAULT_START_TIME = dtime(9, 15)   # market entry time
DEFAULT_SQUAREOFF = dtime(15, 15)   # daily squareoff
DEFAULT_EXPOSURE = 50000.0
DEFAULT_LEVERAGE = 1
DEFAULT_SL_PCT = 3.0
TRIGGER_MULTIPLIER = 3.0
DEFAULT_TRAIL_PCT = 3.0

# Poll intervals
POLL_PRICE_SEC = 10
POLL_PNL_SEC = 5

# ------------------------
# UTILITIES: safe json save/load
# ------------------------
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

# ------------------------
# SIMPLE LOGGER
# ------------------------
class SimpleLogger:
    def __init__(self, maxlen=2000):
        self.maxlen = maxlen
        self.lines = []
        self._lock = threading.Lock()

    def add(self, msg):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        with self._lock:
            self.lines.append(line)
            if len(self.lines) > self.maxlen:
                self.lines = self.lines[-self.maxlen:]

    def get(self):
        with self._lock:
            return "\n".join(self.lines)

logger = SimpleLogger()

# ------------------------
# PAPER BROKER (simulation)
# ------------------------
class PaperBroker:
    def __init__(self):
        self.positions = {}  # symbol -> {'qty':int, 'avg':float}
        self.orders = {}
        self._id = 1

    def _oid(self):
        oid = f"P{self._id}"
        self._id += 1
        return oid

    def place_market_buy(self, symbol, qty, price=None):
        price = price if price is not None else 100.0
        oid = self._oid()
        # update position
        pos = self.positions.get(symbol, {'qty': 0, 'avg': 0.0})
        total_qty = pos['qty'] + qty
        if pos['qty'] == 0:
            avg = price
        else:
            avg = (pos['avg'] * pos['qty'] + price * qty) / total_qty
        self.positions[symbol] = {'qty': total_qty, 'avg': avg}
        self.orders[oid] = {'id': oid, 'type': 'BUY', 'symbol': symbol, 'qty': qty, 'price': price, 'status': 'FILLED'}
        logger.add(f"[Paper] BUY {symbol} qty={qty} @ {price} (id={oid})")
        return oid

    def place_slm(self, symbol, qty, trigger):
        oid = self._oid()
        self.orders[oid] = {'id': oid, 'type': 'SLM', 'symbol': symbol, 'qty': qty, 'trigger': trigger, 'status': 'ACTIVE'}
        logger.add(f"[Paper] SLM {symbol} qty={qty} trigger={trigger} (id={oid})")
        return oid

    def modify_order(self, oid, trigger):
        if oid in self.orders:
            self.orders[oid]['trigger'] = trigger
            logger.add(f"[Paper] Modify {oid} -> trigger {trigger}")
            return True
        return False

    def cancel_all(self):
        cancelled = []
        for oid, o in list(self.orders.items()):
            if o.get('status') in ('ACTIVE','OPEN'):
                o['status'] = 'CANCELLED'
                cancelled.append(oid)
        logger.add(f"[Paper] Cancelled: {cancelled}")
        return cancelled

    def close_all(self):
        closed = []
        for sym, p in list(self.positions.items()):
            if p.get('qty',0) != 0:
                closed.append({'symbol':sym,'qty':p['qty']})
                logger.add(f"[Paper] Closed {sym} qty={p['qty']}")
                self.positions[sym] = {'qty':0,'avg':0.0}
        return closed

    def get_positions(self):
        res = []
        for sym,p in self.positions.items():
            if p.get('qty',0)!=0:
                res.append({'tradingsymbol':sym,'quantity':p['qty'],'avg_price':p['avg'],'pnl':0.0})
        return res

# ------------------------
# TRADING ENGINE (live/paper)
# ------------------------
class TradingEngine:
    def __init__(self, kite=None, broker=None, live=False, cfg=None, sms_enabled=False):
        self.kite = kite
        self.broker = broker or PaperBroker()
        self.live = live and (kite is not None)
        self.cfg = cfg or {}
        self.sms_enabled = sms_enabled

        # runtime state
        self.entry_price = None
        self.qty = 0
        self.entry_order_id = None
        self.sl_order_id = None
        self.peak_price = None
        self.active = False
        self._stop = threading.Event()

    def _send_sms(self, msg):
        if self.sms_enabled:
            send_sms(msg)

    def compute_qty(self, ltp):
        if self.cfg.get('qty_mode') == 'fixed':
            return int(self.cfg.get('qty', 1))
        exposure = float(self.cfg.get('exposure', DEFAULT_EXPOSURE))
        leverage = float(self.cfg.get('leverage', DEFAULT_LEVERAGE))
        if ltp <= 0:
            return 0
        q = int((exposure * leverage) // ltp)
        return max(1, q)

    def get_ltp(self, exchange, symbol):
        if self.live and self.kite:
            try:
                key = f"{exchange}:{symbol}"
                d = self.kite.ltp(key)
                return list(d.values())[0]['last_price']
            except Exception as e:
                logger.add(f"[Live] LTP fetch failed: {e}")
                return None
        # Paper: simulate small random walk around sim_ltp
        base = float(self.cfg.get('sim_ltp', 100.0))
        return round(base * (1 + np.random.normal(0, 0.0015)), 2)

    def place_market_buy(self, symbol, exchange, qty):
        if self.live and self.kite:
            try:
                oid = self.kite.place_order(
                    tradingsymbol=symbol,
                    exchange=exchange,
                    transaction_type=self.kite.TRANSACTION_TYPE_BUY,
                    quantity=qty,
                    order_type=self.kite.ORDER_TYPE_MARKET,
                    product=self.kite.PRODUCT_MIS,
                    variety=self.kite.VARIETY_REGULAR
                )
                logger.add(f"[Live] BUY placed id={oid} qty={qty}")
                return oid
            except Exception as e:
                logger.add(f"[Live] BUY failed: {e}")
                return None
        else:
            return self.broker.place_market_buy(symbol, qty, price=self.cfg.get('sim_ltp'))

    def place_slm(self, symbol, exchange, qty, trigger):
        if self.live and self.kite:
            try:
                oid = self.kite.place_order(
                    tradingsymbol=symbol,
                    exchange=exchange,
                    transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                    quantity=qty,
                    order_type=self.kite.ORDER_TYPE_SLM,
                    trigger_price=trigger,
                    product=self.kite.PRODUCT_MIS,
                    variety=self.kite.VARIETY_REGULAR
                )
                logger.add(f"[Live] SLM placed id={oid} trg={trigger}")
                return oid
            except Exception as e:
                logger.add(f"[Live] SLM failed: {e}")
                return None
        else:
            return self.broker.place_slm(symbol, qty, trigger)

    def modify_slm(self, order_id, trigger):
        if self.live and self.kite:
            try:
                self.kite.modify_order(order_id=order_id, trigger_price=trigger)
                logger.add(f"[Live] Modified order {order_id} -> {trigger}")
                return True
            except Exception as e:
                logger.add(f"[Live] Modify failed: {e}")
                return False
        else:
            return self.broker.modify_order(order_id, trigger)

    def cancel_all_pending(self):
        if self.live and self.kite:
            cancelled = []
            try:
                orders = self.kite.orders()
                for o in orders:
                    oid = o.get('order_id') or o.get('order_id')
                    status = (o.get('status') or "").upper()
                    if status in ('OPEN', 'TRIGGER PENDING', 'PENDING'):
                        variety = o.get('variety', self.kite.VARIETY_REGULAR)
                        try:
                            self.kite.cancel_order(order_id=oid, variety=variety)
                            cancelled.append(oid)
                        except Exception as e:
                            logger.add(f"[Live] Cancel {oid} failed: {e}")
                logger.add(f"[Live] Cancelled pending: {cancelled}")
            except Exception as e:
                logger.add(f"[Live] Cancel-all failed: {e}")
            return cancelled
        else:
            return self.broker.cancel_all()

    def close_all_positions(self):
        if self.live and self.kite:
            closed = []
            try:
                pos = self.kite.positions()
                net = pos.get('net', []) if isinstance(pos, dict) else []
                for p in net:
                    sym = p.get('tradingsymbol')
                    qty = int(p.get('quantity', 0) or 0)
                    if qty == 0:
                        continue
                    tx = self.kite.TRANSACTION_TYPE_SELL if qty > 0 else self.kite.TRANSACTION_TYPE_BUY
                    qty_abs = abs(qty)
                    try:
                        oid = self.kite.place_order(
                            tradingsymbol=sym,
                            exchange=p.get('exchange') or 'NSE',
                            transaction_type=tx,
                            quantity=qty_abs,
                            order_type=self.kite.ORDER_TYPE_MARKET,
                            product=self.kite.PRODUCT_MIS,
                            variety=self.kite.VARIETY_REGULAR
                        )
                        closed.append({'symbol': sym, 'qty': qty_abs, 'order_id': oid})
                    except Exception as e:
                        logger.add(f"[Live] Close {sym} failed: {e}")
                logger.add(f"[Live] Close all orders: {closed}")
            except Exception as e:
                logger.add(f"[Live] Close-all failed: {e}")
            return closed
        else:
            return self.broker.close_all()

    def emergency_exit(self, reason="emergency"):
        logger.add(f"Emergency exit: {reason}")
        self.cancel_all_pending()
        self.close_all_positions()
        self.stop()
        self._send_sms(f"EMERGENCY EXIT executed: {reason}")

    # entry conditions: simplified: start at configured start time; add OHLC checks if live and tokens present
    def evaluate_and_place_entry(self):
        try:
            cfg = self.cfg
            sym = cfg['tradingsymbol']
            exch = cfg['exchange']
            # get LTP (paper uses sim)
            ltp = self.get_ltp(exch, sym)
            if ltp is None:
                logger.add("Entry: LTP missing")
                return False
            qty = self.compute_qty(ltp)
            if qty <= 0:
                logger.add("Entry: qty <= 0")
                return False
            oid = self.place_market_buy(sym, exch, qty)
            if not oid:
                logger.add("Entry: buy order failed")
                return False
            self.entry_order_id = oid
            self.entry_price = ltp
            self.qty = qty
            self.peak_price = ltp
            sl_trigger = round(self.entry_price * (1 - cfg['sl_pct']/100), 2)
            sl_oid = self.place_slm(sym, exch, qty, sl_trigger)
            self.sl_order_id = sl_oid
            logger.add(f"Entry executed {sym} qty={qty} entry={self.entry_price} sl={sl_trigger}")
            self._send_sms(f"BUY executed: {sym} @{self.entry_price} (qty {self.qty})")
            return True
        except Exception as e:
            logger.add(f"Entry exception: {e}")
            return False

    def manage_trailing(self):
        if not self.entry_price:
            return
        ltp = self.get_ltp(self.cfg['exchange'], self.cfg['tradingsymbol'])
        if ltp is None:
            return
        if self.peak_price is None or ltp > self.peak_price:
            self.peak_price = ltp
        # initial SL hit check
        if ltp <= self.entry_price * (1 - self.cfg['sl_pct']/100):
            logger.add("Initial SL hit -> emergency exit")
            self._send_sms(f"STOP LOSS hit: {self.cfg['tradingsymbol']} @{ltp}")
            self.emergency_exit(reason="SL hit")
            return
        profit_pct = (ltp - self.entry_price)/self.entry_price*100
        if profit_pct >= (self.cfg['sl_pct'] * TRIGGER_MULTIPLIER):
            # move SL to breakeven
            breakeven = round(self.entry_price, 2)
            if self.sl_order_id:
                self.modify_slm(self.sl_order_id, breakeven)
                logger.add(f"Moved SL to breakeven @ {breakeven}")
        # trailing using peak
        trailing_trigger = round(self.peak_price * (1 - self.cfg['trail_pct']/100), 2)
        if self.sl_order_id:
            ok = self.modify_slm(self.sl_order_id, trailing_trigger)
            if ok:
                logger.add(f"Trailing SL updated -> {trailing_trigger} (peak {self.peak_price})")

    def run_loop(self):
        logger.add("Engine started")
        self.active = True
        self._stop.clear()
        try:
            start_time = self.cfg.get('start_time', DEFAULT_START_TIME)
            squareoff_time = self.cfg.get('squareoff_time', DEFAULT_SQUAREOFF)
            while not self._stop.is_set():
                now = datetime.now()
                if now.weekday() >= 5:  # skip weekends
                    time.sleep(10)
                    continue
                # if no entry yet and it's start time window -> evaluate
                if not self.entry_price:
                    stime = start_time
                    if now.time() >= stime and now.time() <= (datetime.combine(now.date(), stime) + timedelta(minutes=20)).time():
                        logger.add("Start window reached -> evaluating entry")
                        self.evaluate_and_place_entry()
                else:
                    # manage trailing
                    self.manage_trailing()
                # day-end square-off
                if now.time() >= squareoff_time:
                    if self.entry_price:
                        logger.add("Square-off time reached -> closing positions")
                        self._send_sms("Auto square-off executing")
                        self.emergency_exit(reason="Auto square-off")
                    break
                time.sleep(POLL_PRICE_SEC)
        except Exception as e:
            logger.add(f"Engine loop error: {e}")
            traceback.print_exc()
            self._send_sms(f"Engine error: {e}")
        finally:
            self.active = False
            logger.add("Engine stopped")

    def start(self):
        if self.active:
            logger.add("Engine already running")
            return
        t = threading.Thread(target=self.run_loop, daemon=True)
        t.start()

    def stop(self):
        self._stop.set()
        logger.add("Stop requested")

# ------------------------
# SMS helper (Fast2SMS)
# ------------------------
def send_sms(message):
    if not FAST2SMS_API_KEY or not USER_PHONE:
        logger.add("SMS disabled (missing config)")
        return False
    try:
        headers = {"authorization": FAST2SMS_API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
        payload = {
            "sender_id": "FSTSMS",
            "message": message,
            "language": "english",
            "route": "v3",
            "numbers": USER_PHONE
        }
        resp = requests.post(FAST2SMS_URL, data=payload, headers=headers, timeout=10)
        ok = resp.status_code == 200
        logger.add(f"SMS sent: {message} status={resp.status_code}")
        return ok
    except Exception as e:
        logger.add(f"SMS send failed: {e}")
        return False

# ------------------------
# P&L helper
# ------------------------
def get_live_pnl(kite=None, broker=None, live=False):
    total = 0.0
    rows = []
    try:
        if live and kite:
            data = kite.positions()
            net = data.get('net', []) if isinstance(data, dict) else []
            for p in net:
                sym = p.get('tradingsymbol')
                qty = int(p.get('quantity', 0) or 0)
                pnl = float(p.get('pnl', 0) or 0)
                avg = float(p.get('avg_price', 0) or 0)
                total += pnl
                rows.append({'Symbol': sym, 'Qty': qty, 'Avg': avg, 'PnL': round(pnl,2)})
        else:
            if broker:
                pos = broker.get_positions()
                for p in pos:
                    sym = p.get('tradingsymbol') or p.get('symbol')
                    qty = int(p.get('quantity', 0) or 0)
                    pnl = float(p.get('pnl', 0) or 0)
                    total += pnl
                    rows.append({'Symbol': sym, 'Qty': qty, 'Avg': p.get('avg_price',0), 'PnL': round(pnl,2)})
    except Exception as e:
        logger.add(f"P&L fetch error: {e}")
    return total, rows

# ------------------------
# STREAMLIT UI
# ------------------------
st.set_page_config(page_title="Auto Streamlit Full Trader", layout="wide")
st.title("🔁 Auto Streamlit Full Trader (Paper/Live)")

# Sidebar: connection & mode
st.sidebar.header("Connection & SMS")
live_mode = st.sidebar.checkbox("Live Mode (place real orders)", value=False)
api_key_in = st.sidebar.text_input("Kite API Key", value=API_KEY, type="password")
api_secret_in = st.sidebar.text_input("Kite API Secret", value=API_SECRET, type="password")
st.sidebar.markdown("---")
st.sidebar.write("SMS Alerts (Fast2SMS)")
fast2sms_key_in = st.sidebar.text_input("Fast2SMS API Key", value=FAST2SMS_API_KEY, type="password")
user_phone_in = st.sidebar.text_input("Your mobile (with country code, e.g., 9198...)", value=USER_PHONE)

# token / login management
saved = safe_load_json(ACCESS_TOKEN_FILE)
if saved:
    st.sidebar.success("Saved access token found")
else:
    st.sidebar.info("No saved access token yet")

if st.sidebar.button("Show Kite Login URL"):
    if not api_key_in:
        st.sidebar.error("Set API Key first")
    elif not KITE_AVAILABLE:
        st.sidebar.error("kiteconnect library not installed")
    else:
        t = KiteConnect(api_key=api_key_in)
        st.sidebar.code(t.login_url())
        st.sidebar.caption("Open the URL, login with the account that created the app, copy request_token from redirected URL and paste below.")

request_token_in = st.sidebar.text_input("Paste the 'request_token' from Kite redirect (if you have one)")

if st.sidebar.button("Generate & Save Access Token"):
    if not KITE_AVAILABLE:
        st.sidebar.error("kiteconnect not installed")
    elif not api_key_in or not api_secret_in or not request_token_in:
        st.sidebar.error("Provide API Key, API Secret and request_token")
    else:
        try:
            temp = KiteConnect(api_key=api_key_in)
            data = temp.generate_session(request_token_in.strip(), api_secret=api_secret_in.strip())
            safe_save_json(ACCESS_TOKEN_FILE, data)
            saved = data
            st.sidebar.success("Access token saved (access_token.json). You can now enable Live Mode.")
            logger.add("Access token generated and saved")
        except Exception as e:
            st.sidebar.error(f"Token generation failed: {e}")
            logger.add(f"Token gen failed: {e}")

# Build kite instance for live mode if possible
kite = None
if live_mode:
    if not KITE_AVAILABLE:
        st.sidebar.error("kiteconnect not installed. Live disabled.")
        live_mode = False
    else:
        access = saved or safe_load_json(ACCESS_TOKEN_FILE)
        if not access:
            st.sidebar.info("No saved access token. Generate one from login flow.")
            live_mode = False
        else:
            try:
                kite = KiteConnect(api_key=api_key_in)
                kite.set_access_token(access.get('access_token'))
                st.sidebar.success("Kite connected (live).")
            except Exception as e:
                st.sidebar.error(f"Kite connect failed: {e}")
                logger.add(f"Kite connect failed: {e}")
                kite = None
                live_mode = False

# Strategy config
st.sidebar.header("Strategy")
symbol = st.sidebar.text_input("Trading symbol (exact)", value="RELIANCE")
exchange = st.sidebar.selectbox("Exchange", ["NSE","BSE"], index=0)
qty_mode = st.sidebar.selectbox("Qty mode", ["exposure","fixed"], index=0)
exposure = st.sidebar.number_input("Exposure (₹)", value=DEFAULT_EXPOSURE)
fixed_qty = st.sidebar.number_input("Fixed qty (if fixed mode)", value=1, step=1)
sl_pct = st.sidebar.number_input("Initial SL %", value=DEFAULT_SL_PCT, step=0.1)
trail_pct = st.sidebar.number_input("Trailing % after breakeven", value=DEFAULT_TRAIL_PCT, step=0.1)
start_time_in = st.sidebar.time_input("Auto start time", value=DEFAULT_START_TIME)
squareoff_time_in = st.sidebar.time_input("Auto square-off time", value=DEFAULT_SQUAREOFF)
sim_ltp = st.sidebar.number_input("Paper sim LTP", value=100.0)

st.sidebar.markdown("---")
start_immediately = st.sidebar.checkbox("Start engine immediately (when token valid)", value=False)
enable_sms = st.sidebar.checkbox("Enable SMS alerts", value=bool(FAST2SMS_API_KEY and USER_PHONE))
# apply SMS keys from UI if provided
if fast2sms_key_in:
    FAST2SMS_API_KEY = fast2sms_key_in
if user_phone_in:
    USER_PHONE = user_phone_in

# Provide engine config dict
engine_cfg = {
    'tradingsymbol': symbol.strip().upper(),
    'exchange': exchange,
    'qty_mode': 'fixed' if qty_mode=='fixed' else 'exposure',
    'qty': int(fixed_qty),
    'exposure': float(exposure),
    'leverage': float(DEFAULT_LEVERAGE),
    'sl_pct': float(sl_pct),
    'trail_pct': float(trail_pct),
    'start_time': start_time_in,
    'squareoff_time': squareoff_time_in,
    'sim_ltp': float(sim_ltp)
}

# session storage for engine & broker
if 'paper_broker' not in st.session_state:
    st.session_state['paper_broker'] = PaperBroker()
if 'engine' not in st.session_state:
    st.session_state['engine'] = None

broker = st.session_state['paper_broker']

# Buttons
col1, col2, col3 = st.columns(3)
with col1:
    if st.button("Start Engine"):
        # create engine
        eng = TradingEngine(kite=kite, broker=broker, live=live_mode, cfg=engine_cfg, sms_enabled=enable_sms)
        st.session_state['engine'] = eng
        eng.start()
        logger.add("Engine start requested")
with col2:
    if st.button("Stop Engine"):
        eng = st.session_state.get('engine')
        if eng:
            eng.stop()
            st.session_state['engine'] = None
            logger.add("Engine stop requested")
with col3:
    if st.button("Emergency Exit"):
        eng = st.session_state.get('engine')
        if eng:
            eng.emergency_exit(reason="Manual Emergency")
            st.session_state['engine'] = None

# P&L updater thread (background)
if 'pnl_thread' not in st.session_state:
    def pnl_loop():
        while True:
            try:
                eng = st.session_state.get('engine')
                total, lst = get_live_pnl(kite=eng.kite if eng else kite, broker=broker, live=live_mode)
                st.session_state['pnl_total'] = total
                st.session_state['pnl_list'] = lst
            except Exception as e:
                logger.add(f"PnL loop error: {e}")
            time.sleep(POLL_PNL_SEC)
    st.session_state['pnl_thread'] = threading.Thread(target=pnl_loop, daemon=True)
    st.session_state['pnl_thread'].start()

# Dashboard
st.header("Engine & Status")
eng = st.session_state.get('engine')
st.write("Live mode:", live_mode)
st.write("Engine active:", bool(eng and eng.active))
st.write("Symbol:", engine_cfg['tradingsymbol'])
st.write("Start time:", engine_cfg['start_time'])
st.write("Square-off:", engine_cfg['squareoff_time'])
st.write("SL%:", engine_cfg['sl_pct'], "Trail%:", engine_cfg['trail_pct'])

st.markdown("---")
left, right = st.columns([2,1])

with left:
    st.subheader("Live P&L")
    total = st.session_state.get('pnl_total', 0.0)
    color = "green" if total >= 0 else "red"
    st.markdown(f"<h2 style='text-align:center;color:{color};'>Total P&L: ₹{total:.2f}</h2>", unsafe_allow_html=True)
    if st.session_state.get('pnl_list'):
        st.dataframe(pd.DataFrame(st.session_state['pnl_list']), use_container_width=True)

    st.subheader("Manual actions")
    if st.button("Cancel all pending orders"):
        if eng:
            cancelled = eng.cancel_all_pending()
            st.write("Cancelled:", cancelled)
            logger.add(f"User cancelled orders: {cancelled}")
    if st.button("Close all positions"):
        if eng:
            closed = eng.close_all_positions()
            st.write("Close-orders:", closed)
            logger.add("User closed all positions")

with right:
    st.subheader("Activity & Logs")
    st.text_area("Logs", value=logger.get(), height=400)

st.markdown("---")
st.caption("Notes: Run in Paper mode (default) to test thoroughly. Live mode requires valid access_token.json saved by using Kite login flow once per day.")

# auto-start if requested and token valid
if start_immediately and st.session_state.get('engine') is None:
    # attempt to create engine only if token valid (for live), or always for paper
    if live_mode:
        access = safe_load_json(ACCESS_TOKEN_FILE)
        if access and api_key_in and KITE_AVAILABLE:
            try:
                kite_test = KiteConnect(api_key=api_key_in)
                kite_test.set_access_token(access.get('access_token'))
                kite_test.profile()  # verify
                eng = TradingEngine(kite=kite_test, broker=broker, live=True, cfg=engine_cfg, sms_enabled=enable_sms)
                st.session_state['engine'] = eng
                eng.start()
                logger.add("Auto-start engine (live)")
            except Exception as e:
                st.warning(f"Cannot auto-start live engine: {e}")
                logger.add(f"Auto-start live failed: {e}")
        else:
            st.info("No valid token available for live auto-start. Start manually after generating token.")
    else:
        eng = TradingEngine(kite=None, broker=broker, live=False, cfg=engine_cfg, sms_enabled=enable_sms)
        st.session_state['engine'] = eng
        eng.start()
        logger.add("Auto-start engine (paper)")

# keep UI alive
st.write("Server time:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))




