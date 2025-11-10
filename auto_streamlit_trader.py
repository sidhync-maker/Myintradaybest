# auto_streamlit_mis_leverage.py
"""
Streamlit Zerodha Intraday MIS trader (Option 1: manual token once/day)
Features:
 - MIS intraday trades with leverage (qty = floor(exposure * leverage / LTP))
 - Instant SL (configurable), initial SL, breakeven + trailing SL
 - Auto-start (scheduler) & manual controls
 - Auto square-off at configured time (default 15:15)
 - Live P&L with color: green/profit, red/loss, blue/neutral
 - SMS alerts (Fast2SMS or Twilio) optional
 - Safe access token handling: paste request_token once/day to generate access_token
"""

import os
import time
import json
import threading
import traceback
from datetime import datetime, time as dtime, timedelta

import streamlit as st
import pandas as pd

# optional Kite import
try:
    from kiteconnect import KiteConnect
    KITE_AVAILABLE = True
except Exception:
    KiteConnect = None
    KITE_AVAILABLE = False

# -----------------------
# CONFIG (do NOT hardcode secrets in production)
# -----------------------
# Prefer environment variables:
API_KEY = os.getenv("ZK_API_KEY", "t32mq5t5xgnjdtni")        # set or paste in sidebar
API_SECRET = os.getenv("ZK_API_SECRET", "xf9jfyfvmqo408m52l4u2gpyo34fcsfe")
REDIRECT_URL = os.getenv("ZK_REDIRECT_URL", "http://localhost:8501")
ACCESS_TOKEN_FILE = os.getenv("ACCESS_TOKEN_FILE", "access_token.json")

# SMS provider selection: "fast2sms" or "twilio" or empty to disable
SMS_PROVIDER = os.getenv("SMS_PROVIDER", "fast2sms").lower()
# Fast2SMS config
FAST2SMS_API_KEY = os.getenv("FAST2SMS_API_KEY", "o0EQRX69hWSDnCP2awiTtxvdFMeAZLOgUj1slcNbBrqf3z4GIJBVfSov8laJ7eET160iZCOrHbchKI4G")
# Twilio config
TWILIO_SID = os.getenv("TWILIO_SID", "")
TWILIO_AUTH = os.getenv("TWILIO_AUTH", "")
TWILIO_FROM = os.getenv("TWILIO_FROM", "")
TWILIO_TO = os.getenv("TWILIO_TO", "")

# Defaults
DEFAULT_EXPOSURE = 50000.0
DEFAULT_LEVERAGE = 2.0
DEFAULT_SL_PCT = 3.0
DEFAULT_INSTANT_SL_PCT = 1.5
DEFAULT_TRAIL_PCT = 3.0
DEFAULT_START_TIME = dtime(9, 15)
DEFAULT_SQUAREOFF = dtime(15, 15)

# Polling intervals
PRICE_POLL_SEC = 5
PNL_POLL_SEC = 5

# -----------------------
# Utilities: safe json save/load
# -----------------------
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

# -----------------------
# Simple logger
# -----------------------
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

class SimpleLogger:
    def __init__(self, maxlen=2000):
        self.maxlen = maxlen
        self.lines = []
        self._lock = threading.Lock()
    def add(self, msg):
        line = f"[{now_str()}] {msg}"
        with self._lock:
            self.lines.append(line)
            if len(self.lines) > self.maxlen:
                self.lines = self.lines[-self.maxlen:]
    def get(self):
        with self._lock:
            return "\n".join(self.lines)

logger = SimpleLogger()

# -----------------------
# SMS helpers
# -----------------------
def send_sms_fast2sms(message, api_key, number):
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
        import requests
        resp = requests.post(url, data=payload, headers=headers, timeout=10)
        logger.add(f"SMS sent (fast2sms) status={resp.status_code}")
        return resp.status_code == 200
    except Exception as e:
        logger.add(f"SMS fast2sms error: {e}")
        return False

def send_sms_twilio(message, sid, auth, from_no, to_no):
    try:
        from twilio.rest import Client
        client = Client(sid, auth)
        client.messages.create(body=message, from_=from_no, to=to_no)
        logger.add("SMS sent (twilio)")
        return True
    except Exception as e:
        logger.add(f"SMS twilio error: {e}")
        return False

def send_sms(message):
    if SMS_PROVIDER == "fast2sms" and FAST2SMS_API_KEY and st.session_state.get("sms_number"):
        ok = send_sms_fast2sms(message, FAST2SMS_API_KEY, st.session_state.get("sms_number"))
        if not ok:
            logger.add("SMS (fast2sms) failed")
    elif SMS_PROVIDER == "twilio" and TWILIO_SID and TWILIO_AUTH and st.session_state.get("sms_number"):
        ok = send_sms_twilio(message, TWILIO_SID, TWILIO_AUTH, TWILIO_FROM, st.session_state.get("sms_number"))
        if not ok:
            logger.add("SMS (twilio) failed")
    else:
        logger.add("SMS not sent (provider/config missing)")

# -----------------------
# Kite token handling (Option 1)
# -----------------------
def kite_login_url(api_key):
    if not KITE_AVAILABLE:
        return "kiteconnect not installed"
    temp = KiteConnect(api_key=api_key)
    return temp.login_url()

def generate_and_save_session(api_key, api_secret, request_token):
    if not KITE_AVAILABLE:
        raise RuntimeError("kiteconnect not installed")
    temp = KiteConnect(api_key=api_key)
    data = temp.generate_session(request_token.strip(), api_secret=api_secret.strip())
    safe_save_json(ACCESS_TOKEN_FILE, data)
    logger.add("Access token saved")
    return data

def build_kite_instance(api_key, saved_session):
    if not KITE_AVAILABLE:
        return None
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(saved_session.get("access_token"))
    return kite

def verify_saved_token(kite):
    try:
        kite.profile()
        return True
    except Exception as e:
        logger.add(f"Token verify failed: {e}")
        return False

# -----------------------
# Trading engine (runs in background thread)
# -----------------------
class TradingEngine:
    def __init__(self, kite, cfg, sms_enabled=False):
        self.kite = kite
        self.cfg = cfg
        self.sms_enabled = sms_enabled
        self.active = False
        self._stop = threading.Event()

        # runtime
        self.entry_price = None
        self.qty = 0
        self.entry_order_id = None
        self.sl_order_id = None
        self.peak_price = None
        self.instant_sl_price = None

    def compute_qty(self, ltp):
        # qty = floor(exposure * leverage / ltp)
        exposure = float(self.cfg.get("exposure", DEFAULT_EXPOSURE))
        leverage = float(self.cfg.get("leverage", DEFAULT_LEVERAGE))
        if ltp <= 0:
            return 0
        # careful integer division: use floor
        qty = int((exposure * leverage) // ltp)
        return max(1, qty)

    def get_ltp(self, symbol_ref):
        try:
            d = self.kite.ltp(symbol_ref)
            return list(d.values())[0]["last_price"]
        except Exception as e:
            logger.add(f"LTP fetch error: {e}")
            return None

    def place_market_buy_mis(self, exchange, tradingsymbol, qty):
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
            logger.add(f"Live BUY placed id={oid} qty={qty}")
            return oid
        except Exception as e:
            logger.add(f"Place BUY failed: {e}")
            return None

    def place_sell_market(self, exchange, tradingsymbol, qty, reason="sell"):
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
            logger.add(f"Live SELL placed id={oid} qty={qty} reason={reason}")
            return oid
        except Exception as e:
            logger.add(f"Place SELL failed: {e}")
            return None

    def place_slm(self, exchange, tradingsymbol, qty, trigger_price):
        try:
            oid = self.kite.place_order(
                tradingsymbol=tradingsymbol,
                exchange=exchange,
                transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                quantity=qty,
                order_type=self.kite.ORDER_TYPE_SLM,
                trigger_price=trigger_price,
                product=self.kite.PRODUCT_MIS,
                variety=self.kite.VARIETY_REGULAR
            )
            logger.add(f"Live SLM placed id={oid} trg={trigger_price}")
            return oid
        except Exception as e:
            logger.add(f"Place SLM failed: {e}")
            return None

    def modify_slm(self, order_id, trigger_price):
        try:
            self.kite.modify_order(order_id=order_id, trigger_price=trigger_price)
            logger.add(f"Modify order {order_id} -> {trigger_price}")
            return True
        except Exception as e:
            logger.add(f"Modify SL failed: {e}")
            return False

    def cancel_all_pending(self):
        cancelled = []
        try:
            orders = self.kite.orders()
            for o in orders:
                status = (o.get("status") or "").upper()
                oid = o.get("order_id")
                if status in ("OPEN", "TRIGGER PENDING", "PENDING"):
                    variety = o.get("variety", self.kite.VARIETY_REGULAR)
                    try:
                        self.kite.cancel_order(order_id=oid, variety=variety)
                        cancelled.append(oid)
                    except Exception as e:
                        logger.add(f"Cancel {oid} failed: {e}")
            logger.add(f"Cancelled pending orders: {cancelled}")
        except Exception as e:
            logger.add(f"Cancel-all error: {e}")
        return cancelled

    def close_all_positions(self):
        closed = []
        try:
            pos = self.kite.positions()
            net = pos.get("net", []) if isinstance(pos, dict) else []
            for p in net:
                sym = p.get("tradingsymbol")
                qty = int(p.get("quantity", 0) or 0)
                if qty == 0:
                    continue
                tx = self.kite.TRANSACTION_TYPE_SELL if qty > 0 else self.kite.TRANSACTION_TYPE_BUY
                qty_abs = abs(qty)
                try:
                    oid = self.kite.place_order(
                        tradingsymbol=sym,
                        exchange=p.get("exchange") or "NSE",
                        transaction_type=tx,
                        quantity=qty_abs,
                        order_type=self.kite.ORDER_TYPE_MARKET,
                        product=self.kite.PRODUCT_MIS,
                        variety=self.kite.VARIETY_REGULAR
                    )
                    closed.append({"symbol": sym, "qty": qty_abs, "order_id": oid})
                except Exception as e:
                    logger.add(f"Close {sym} failed: {e}")
            logger.add(f"Closed positions: {closed}")
        except Exception as e:
            logger.add(f"Close-all error: {e}")
        return closed

    def emergency_exit(self, reason="emergency"):
        logger.add(f"EMERGENCY: {reason}")
        try:
            self.cancel_all_pending()
            self.close_all_positions()
            send_sms(f"EMERGENCY EXIT executed: {reason}")
        except Exception as e:
            logger.add(f"Emergency exit error: {e}")
        self.stop()

    def run(self):
        logger.add("Engine started")
        self.active = True
        self._stop.clear()

        start_time = self.cfg.get("start_time", DEFAULT_START_TIME)
        squareoff_time = self.cfg.get("squareoff_time", DEFAULT_SQUAREOFF)
        tradingsymbol = self.cfg["tradingsymbol"]
        exchange = self.cfg["exchange"]
        symbol_ref = f"{exchange}:{tradingsymbol}"
        sl_pct = float(self.cfg.get("sl_pct"))
        instant_sl_pct = float(self.cfg.get("instant_sl_pct"))
        trail_pct = float(self.cfg.get("trail_pct"))
        exposure = float(self.cfg.get("exposure"))
        leverage = float(self.cfg.get("leverage"))

        while not self._stop.is_set():
            try:
                now = datetime.now()
                if now.weekday() >= 5:
                    time.sleep(30)
                    continue

                # auto-start entry window
                if self.entry_price is None:
                    # if time reached start window, attempt entry
                    if now.time() >= start_time and now.time() <= (datetime.combine(now.date(), start_time) + timedelta(minutes=20)).time():
                        ltp = self.get_ltp(symbol_ref)
                        if ltp is None:
                            logger.add("Entry: LTP None, retrying")
                            time.sleep(2)
                            continue
                        qty = self.compute_qty(ltp)
                        if qty <= 0:
                            logger.add("Entry: qty computed <=0, abort")
                            time.sleep(2)
                            continue
                        # place market buy MIS
                        oid = self.place_market_buy_mis(exchange, tradingsymbol, qty)
                        if not oid:
                            logger.add("Entry buy failed")
                            time.sleep(2)
                            continue
                        self.entry_order_id = oid
                        self.entry_price = ltp
                        self.qty = qty
                        self.peak_price = ltp
                        # instant and initial sl
                        self.instant_sl_price = round(self.entry_price * (1 - instant_sl_pct / 100), 2)
                        self.sl_trigger = round(self.entry_price * (1 - sl_pct / 100), 2)
                        # attempt SLM order for initial SL
                        try:
                            sl_oid = self.place_slm(exchange, tradingsymbol, qty, self.sl_trigger)
                            self.sl_order_id = sl_oid
                        except Exception as e:
                            logger.add(f"SL placement issue: {e}")
                        msg = f"BUY executed {tradingsymbol} @{self.entry_price} qty={self.qty} SL={self.sl_trigger} InstantSL={self.instant_sl_price}"
                        logger.add(msg)
                        if self.sms_enabled:
                            send_sms(msg)

                else:
                    # manage after entry
                    ltp = self.get_ltp(symbol_ref)
                    if ltp is None:
                        time.sleep(1)
                        continue

                    # immediate instant SL (tight protection right after entry)
                    if ltp <= self.instant_sl_price:
                        logger.add(f"Instant SL triggered @ {ltp}")
                        send_sms(f"Instant SL hit {tradingsymbol} @{ltp}")
                        self.place_sell_market(exchange, tradingsymbol, self.qty, reason="Instant SL")
                        # stop engine after SL
                        self.stop()
                        break

                    # initial SL (if still active) check
                    if ltp <= self.sl_trigger:
                        logger.add(f"Initial SL hit @ {ltp}")
                        send_sms(f"Initial SL hit {tradingsymbol} @{ltp}")
                        self.place_sell_market(exchange, tradingsymbol, self.qty, reason="Initial SL")
                        self.stop()
                        break

                    # breakeven condition
                    profit_pct = (ltp - self.entry_price) / self.entry_price * 100
                    if profit_pct >= (sl_pct * 3.0):  # TRIGGER_MULTIPLIER = 3
                        # move SL to breakeven (entry_price)
                        try:
                            if self.sl_order_id:
                                self.modify_slm(self.sl_order_id, round(self.entry_price, 2))
                                logger.add("Moved SL to breakeven")
                                send_sms(f"Moved SL to breakeven for {tradingsymbol}")
                        except Exception as e:
                            logger.add(f"Move to breakeven failed: {e}")

                    # trailing using peak
                    if ltp > self.peak_price:
                        self.peak_price = ltp
                    trailing_trigger = round(self.peak_price * (1 - trail_pct / 100), 2)
                    if trailing_trigger > self.sl_trigger:
                        try:
                            if self.sl_order_id:
                                self.modify_slm(self.sl_order_id, trailing_trigger)
                                self.sl_trigger = trailing_trigger
                                logger.add(f"Updated trailing SL -> {trailing_trigger} (peak {self.peak_price})")
                        except Exception as e:
                            logger.add(f"Trailing modify error: {e}")

                    # auto square-off at configured time
                    if now.time() >= squareoff_time:
                        logger.add("Square-off time reached")
                        send_sms(f"Square-off executing for {tradingsymbol}")
                        self.place_sell_market(exchange, tradingsymbol, self.qty, reason="Auto Square-off")
                        self.stop()
                        break

                time.sleep(PRICE_POLL_SEC)
            except Exception as e:
                logger.add(f"Engine loop error: {e}")
                traceback.print_exc()
                send_sms(f"Engine error: {e}")
                self.stop()
                break

        logger.add("Engine stopped")
        self.active = False

    def start_background(self):
        self.sms_enabled = self.cfg.get("sms_enabled", False)
        if self.active:
            logger.add("Engine already running")
            return
        t = threading.Thread(target=self.run, daemon=True)
        t.start()

    def stop(self):
        self._stop.set()

# -----------------------
# Streamlit UI
# -----------------------
st.set_page_config(page_title="Auto Streamlit MIS Trader", layout="wide")
st.title("🔁 Auto Streamlit MIS Trader — Leverage & Instant SL")

col1, col2 = st.columns([2,1])
with col2:
    st.sidebar.header("Connection / SMS")
    api_key_in = st.sidebar.text_input("Kite API Key", value=API_KEY, type="password")
    api_secret_in = st.sidebar.text_input("Kite API Secret", value=API_SECRET, type="password")
    provider = st.sidebar.selectbox("SMS provider", ["none", "fast2sms", "twilio"], index=0)
    sms_number = st.sidebar.text_input("Your mobile (with country code, e.g. 9198...)", value=os.getenv("USER_PHONE",""))
    # Twilio fields
    tw_sid = st.sidebar.text_input("Twilio SID", value=TWILIO_SID, type="password")
    tw_auth = st.sidebar.text_input("Twilio Auth", value=TWILIO_AUTH, type="password")
    tw_from = st.sidebar.text_input("Twilio From", value=TWILIO_FROM)
    tw_to = st.sidebar.text_input("Twilio To", value=TWILIO_TO)

    if provider == "fast2sms":
        st.sidebar.info("Make sure FAST2SMS_API_KEY in env or enter below")
        fast2key = st.sidebar.text_input("Fast2SMS API Key", value=FAST2SMS_API_KEY, type="password")
        if fast2key:
            FAST2SMS_API_KEY = fast2key
    elif provider == "twilio":
        if tw_sid and tw_auth and tw_from and tw_to:
            TWILIO_SID = tw_sid; TWILIO_AUTH = tw_auth; TWILIO_FROM = tw_from; TWILIO_TO = tw_to

    st.session_state["sms_number"] = sms_number

    if st.sidebar.button("Show Kite Login URL"):
        if not api_key_in:
            st.sidebar.error("Provide Kite API Key first")
        elif not KITE_AVAILABLE:
            st.sidebar.error("kiteconnect not installed")
        else:
            t = KiteConnect(api_key=api_key_in)
            st.sidebar.code(t.login_url())
            st.sidebar.caption("Open the URL, login, copy request_token from redirected URL and paste below.")

    request_token_in = st.sidebar.text_input("Paste request_token (from Kite redirect)")
    if st.sidebar.button("Generate & Save Access Token"):
        if not api_key_in or not api_secret_in or not request_token_in:
            st.sidebar.error("Provide API Key, Secret and request_token")
        else:
            try:
                session = generate_and_save_session(api_key_in, api_secret_in, request_token_in)
                st.sidebar.success("Saved access token")
            except Exception as e:
                st.sidebar.error(f"Token gen failed: {e}")
                logger.add(f"Token gen failed: {e}")

# Strategy config main area
with col1:
    st.header("Strategy configuration")
    tradingsymbol = st.text_input("Trading symbol (exact)", value="RELIANCE")
    exchange = st.selectbox("Exchange", ["NSE","BSE"], index=0)
    exposure = st.number_input("Exposure (₹)", value=DEFAULT_EXPOSURE)
    leverage = st.number_input("Leverage", value=DEFAULT_LEVERAGE, step=0.5)
    sl_pct = st.number_input("Initial SL (%)", value=DEFAULT_SL_PCT, step=0.1)
    instant_sl_pct = st.number_input("Instant SL (%)", value=DEFAULT_INSTANT_SL_PCT, step=0.1)
    trail_pct = st.number_input("Trailing SL after breakeven (%)", value=DEFAULT_TRAIL_PCT, step=0.1)
    start_time = st.time_input("Auto start time", value=DEFAULT_START_TIME)
    squareoff_time = st.time_input("Auto square-off time", value=DEFAULT_SQUAREOFF)
    sms_enabled = st.checkbox("Enable SMS alerts (as configured in sidebar)", value=(provider != "none"))

# Build kite if access token exists
saved = safe_load_json(ACCESS_TOKEN_FILE)
kite = None
if provider == "fast2sms":
    SMS_PROVIDER = "fast2sms"
elif provider == "twilio":
    SMS_PROVIDER = "twilio"
else:
    SMS_PROVIDER = "none"

if saved and api_key_in and KITE_AVAILABLE:
    try:
        kite = build_kite_instance(api_key_in, saved)
        if not verify_saved_token(kite):
            st.warning("Saved token invalid/expired. Please re-generate.")
            kite = None
    except Exception as e:
        st.warning(f"Kite build failed: {e}")
        kite = None

# Engine controls
engine = st.session_state.get("engine", None)

if st.button("Start Engine (background)"):
    if not kite:
        st.warning("Live trading disabled (no valid token). Starting ONLY if you want to test with real kite, else provide token or test paper flow.")
    cfg = {
        "tradingsymbol": tradingsymbol.strip().upper(),
        "exchange": exchange,
        "exposure": exposure,
        "leverage": leverage,
        "sl_pct": sl_pct,
        "instant_sl_pct": instant_sl_pct,
        "trail_pct": trail_pct,
        "start_time": start_time,
        "squareoff_time": squareoff_time,
        "sms_enabled": sms_enabled
    }
    if not kite:
        st.error("Cannot start engine without valid kite access token (live). Use sidebar to generate token or test via paper (not implemented in this engine).")
    else:
        eng = TradingEngine(kite=kite, cfg=cfg, sms_enabled=sms_enabled)
        st.session_state["engine"] = eng
        eng.start_background()
        logger.add("Engine start requested")

if st.button("Stop Engine"):
    eng = st.session_state.get("engine")
    if eng:
        eng.stop()
        st.session_state["engine"] = None
        logger.add("Engine stop requested")

if st.button("Emergency Exit (cancel & close)"):
    eng = st.session_state.get("engine")
    if eng:
        eng.emergency_exit(reason="Manual Emergency")
        st.session_state["engine"] = None

# Live P&L display (polling)
st.markdown("---")
st.header("Live P&L")
pnl_placeholder = st.empty()

def get_positions_pnl(kite):
    total = 0.0
    rows = []
    try:
        pos = kite.positions()
        net = pos.get("net", []) if isinstance(pos, dict) else []
        for p in net:
            sym = p.get("tradingsymbol")
            qty = int(p.get("quantity", 0) or 0)
            pnl = float(p.get("pnl", 0) or 0)
            avg = float(p.get("avg_price", 0) or 0)
            rows.append({"Symbol": sym, "Qty": qty, "Avg": avg, "PnL": round(pnl,2)})
            total += pnl
    except Exception as e:
        logger.add(f"P&L fetch error: {e}")
    return total, rows

def pnl_updater():
    while True:
        try:
            eng = st.session_state.get("engine")
            if eng and eng.entry_price is not None:
                # compute live unrealized P&L for the active instrument
                symref = f"{eng.cfg['exchange']}:{eng.cfg['tradingsymbol']}"
                ltp = eng.get_ltp(symref)
                if ltp is not None:
                    unreal = (ltp - eng.entry_price) * eng.qty
                    st.session_state['live_unreal'] = unreal
            elif kite:
                total, rows = get_positions_pnl(kite)
                st.session_state['pnl_total'] = total
                st.session_state['pnl_rows'] = rows
        except Exception as e:
            logger.add(f"pnl_updater error: {e}")
        time.sleep(PNL_POLL_SEC)

if 'pnl_thread' not in st.session_state:
    st.session_state['pnl_thread'] = threading.Thread(target=pnl_updater, daemon=True)
    st.session_state['pnl_thread'].start()

# show P&L
if st.session_state.get('live_unreal') is not None:
    unreal = st.session_state['live_unreal']
    color = "green" if unreal > 0 else "red" if unreal < 0 else "blue"
    pnl_placeholder.markdown(f"<h2 style='color:{color};'>Live Unreal P&L: ₹{unreal:.2f}</h2>", unsafe_allow_html=True)
else:
    total = st.session_state.get('pnl_total', 0.0)
    rows = st.session_state.get('pnl_rows', [])
    color = "green" if total > 0 else "red" if total < 0 else "blue"
    pnl_placeholder.markdown(f"<h2 style='color:{color};'>Total P&L: ₹{total:.2f}</h2>", unsafe_allow_html=True)
    if rows:
        st.dataframe(pd.DataFrame(rows))

# logs
st.markdown("---")
st.subheader("Activity Logs")
st.text_area("Logs", value=logger.get(), height=400)

st.caption("Notes: This app executes MIS intraday trades using Kite. You must generate the access token with the Kite login flow once/day and save it via the sidebar. Test in a paper/demo environment before enabling live trading.")



