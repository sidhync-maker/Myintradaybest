# auto_intraday_streamlit_full_option1.py
"""
Full Streamlit Zerodha Intraday MIS trader
- Option 1 chart: last 30 candles (fast)
- Strategy: first-candle bullish immediate buy (9:15-9:30) + after-9:30 VWAP+uptrend buy
- Instant SL, initial SL, breakeven -> trailing SL
- Paper / Live mode
- MIS leverage qty calc: floor(exposure * leverage / ltp)
- SMS alerts (optional Fast2SMS / Twilio)
- Safe token handling: paste request_token once/day to generate access_token
"""

import os
import time
import json
import threading
import traceback
from datetime import datetime, time as dtime, timedelta

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go

# optional imports
try:
    from kiteconnect import KiteConnect
    KITE_AVAILABLE = True
except Exception:
    KiteConnect = None
    KITE_AVAILABLE = False

# optional Twilio
try:
    from twilio.rest import Client as TwilioClient
    TWILIO_AVAILABLE = True
except Exception:
    TwilioClient = None
    TWILIO_AVAILABLE = False

# ---------------------------
# CONFIG - Edit these before running (or set env vars)
# ---------------------------
# Kite (Zerodha) app credentials
API_KEY = os.getenv("ZK_API_KEY", "t32mq5t5xgnjdtni")         # or paste in sidebar
API_SECRET = os.getenv("ZK_API_SECRET", "xf9jfyfvmqo408m52l4u2gpyo34fcsfe")

# Access token storage (kite.generate_session() output)
ACCESS_TOKEN_FILE = os.getenv("ACCESS_TOKEN_FILE", "access_token.json")

# SMS provider - choose "fast2sms", "twilio", or "" to disable
SMS_PROVIDER = os.getenv("SMS_PROVIDER", "fast2sms").lower()
FAST2SMS_KEY = os.getenv("FAST2SMS_KEY", "o0EQRX69hWSDnCP2awiTtxvdFMeAZLOgUj1slcNbBrqf3z4GIJBVfSov8laJ7eET160iZCOrHbchKI4G")
TWILIO_SID = os.getenv("TWILIO_SID", "")
TWILIO_AUTH = os.getenv("TWILIO_AUTH", "")
TWILIO_FROM = os.getenv("TWILIO_FROM", "")
ALERT_MOBILE = os.getenv("ALERT_MOBILE", "918301844858")  # with country code e.g. 9198...

# Strategy defaults
DEFAULT_EXPOSURE = 50000.0
DEFAULT_LEVERAGE = 5.0
DEFAULT_SL_PCT = 2.0
DEFAULT_INSTANT_SL_PCT = 1.5
DEFAULT_TRAIL_PCT = 3.0
DEFAULT_START_TIME = dtime(9, 15)
DEFAULT_SQUAREOFF = dtime(15, 15)
VWAP_CANDLES = 30   # last 30 candles for chart + VWAP calc

# Poll intervals
PRICE_POLL_SEC = 5
CHART_REFRESH_SEC = 6
PNL_POLL_SEC = 5

# ---------------------------
# UTILITIES
# ---------------------------
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

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# Simple logger
class SimpleLogger:
    def __init__(self, maxlen=2000):
        self.maxlen = maxlen
        self.lines = []
        self.lock = threading.Lock()
    def add(self, msg):
        with self.lock:
            ts = now_str()
            self.lines.append(f"[{ts}] {msg}")
            if len(self.lines) > self.maxlen:
                self.lines = self.lines[-self.maxlen:]
    def get(self):
        with self.lock:
            return "\n".join(self.lines)

logger = SimpleLogger()

# ---------------------------
# SMS helpers
# ---------------------------
def send_sms_fast2sms(message, api_key, number):
    try:
        import requests
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
        ok = r.status_code == 200
        logger.add(f"SMS fast2sms status={r.status_code}")
        return ok
    except Exception as e:
        logger.add(f"fast2sms error: {e}")
        return False

def send_sms_twilio(message, sid, auth, from_no, to_no):
    try:
        client = TwilioClient(sid, auth)
        client.messages.create(body=message, from_=from_no, to=to_no)
        logger.add("SMS twilio sent")
        return True
    except Exception as e:
        logger.add(f"twilio error: {e}")
        return False

def send_alert(message):
    logger.add("Alert: " + message)
    if st.session_state.get("sms_enabled"):
        provider = st.session_state.get("sms_provider", "")
        if provider == "fast2sms":
            key = st.session_state.get("fast2sms_key", FAST2SMS_KEY)
            num = st.session_state.get("sms_number", ALERT_MOBILE)
            if key and num:
                send_sms_fast2sms(message, key, num)
        elif provider == "twilio":
            sid = st.session_state.get("twilio_sid", TWILIO_SID)
            auth = st.session_state.get("twilio_auth", TWILIO_AUTH)
            frm = st.session_state.get("twilio_from", TWILIO_FROM)
            to = st.session_state.get("sms_number", ALERT_MOBILE)
            if sid and auth and frm and to:
                send_sms_twilio(message, sid, auth, frm, to)

# ---------------------------
# Paper broker (simulation)
# ---------------------------
class PaperBroker:
    def __init__(self):
        self.positions = {}   # symbol -> {'qty','avg'}
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
        if pos['qty'] == 0:
            avg = price
        else:
            avg = (pos['avg']*pos['qty'] + price*qty) / total_qty
        self.positions[symbol] = {'qty': total_qty, 'avg': avg}
        self.orders[oid] = {'id':oid, 'type':'BUY', 'symbol':symbol, 'qty':qty, 'price':price, 'status':'FILLED'}
        logger.add(f"[Paper] BUY {symbol} qty={qty} @ {price} (id={oid})")
        return oid
    def place_market_sell(self, symbol, qty, price):
        oid = self._oid()
        pos = self.positions.get(symbol, {'qty':0,'avg':0.0})
        sell_qty = min(qty, pos['qty'])
        pos['qty'] = pos['qty'] - sell_qty
        if pos['qty'] == 0:
            pos['avg'] = 0.0
        self.positions[symbol] = pos
        self.orders[oid] = {'id':oid, 'type':'SELL', 'symbol':symbol, 'qty':sell_qty, 'price':price, 'status':'FILLED'}
        logger.add(f"[Paper] SELL {symbol} qty={sell_qty} @ {price} (id={oid})")
        return oid
    def get_positions(self):
        rows = []
        for sym,p in self.positions.items():
            if p.get('qty',0) != 0:
                rows.append({'tradingsymbol': sym, 'quantity': p['qty'], 'avg_price': p['avg'], 'pnl': 0.0})
        return rows
    def close_all(self):
        closed = []
        for sym, p in list(self.positions.items()):
            if p['qty'] != 0:
                closed.append({'symbol':sym, 'qty': p['qty']})
                logger.add(f"[Paper] Closed {sym} qty={p['qty']}")
                self.positions[sym] = {'qty':0,'avg':0.0}
        return closed

# ---------------------------
# Engine (implements strategy)
# ---------------------------
class TradingEngine:
    def __init__(self, kite=None, broker=None, live=False, cfg=None):
        self.kite = kite
        self.broker = broker or PaperBroker()
        self.live = live and (kite is not None)
        self.cfg = cfg or {}
        self.active = False
        self._stop = threading.Event()
        # runtime
        self.entry_price = None
        self.qty = 0
        self.entry_time = None
        self.sl_trigger = None
        self.instant_sl_price = None
        self.peak_price = None
        self.sl_order_id = None

    def compute_qty(self, ltp):
        exposure = float(self.cfg.get('exposure', DEFAULT_EXPOSURE))
        leverage = float(self.cfg.get('leverage', DEFAULT_LEVERAGE))
        if ltp <= 0:
            return 0
        q = int((exposure * leverage) // ltp)
        return max(1, q)

    def get_5min_ohlc(self, instrument_token, interval='5minute', days=1):
        if not self.live or not self.kite:
            return pd.DataFrame()
        try:
            end = datetime.now()
            start = end - timedelta(days=days)
            data = self.kite.historical_data(int(instrument_token), start, end, interval)
            return pd.DataFrame(data)
        except Exception as e:
            logger.add(f"OHLC fetch failed: {e}")
            return pd.DataFrame()

    def get_ltp(self, ref):
        if self.live and self.kite:
            try:
                d = self.kite.ltp(ref)
                return list(d.values())[0]['last_price']
            except Exception as e:
                logger.add(f"LTP fetch error: {e}")
                return None
        # paper simulation fallback - use sim_ltp if provided
        return float(self.cfg.get('sim_ltp', 100.0)) * (1 + np.random.normal(0, 0.001))

    def place_market_buy(self, exchange, tradingsymbol, qty, ltp):
        if self.live:
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
                logger.add(f"[Live] BUY placed id={oid} qty={qty}")
                return oid
            except Exception as e:
                logger.add(f"[Live] BUY failed: {e}")
                return None
        else:
            return self.broker.place_market_buy(tradingsymbol, qty, ltp)

    def place_market_sell(self, exchange, tradingsymbol, qty, ltp):
        if self.live:
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
                logger.add(f"[Live] SELL placed id={oid} qty={qty}")
                return oid
            except Exception as e:
                logger.add(f"[Live] SELL failed: {e}")
                return None
        else:
            return self.broker.place_market_sell(tradingsymbol, qty, ltp)

    def run(self):
        logger.add("Engine started")
        self.active = True
        self._stop.clear()

        cfg = self.cfg
        exchange = cfg['exchange']
        tradingsymbol = cfg['tradingsymbol']
        symbol_ref = f"{exchange}:{tradingsymbol}"
        instrument_token = cfg.get('instrument_token')  # optional numeric token for historical
        sim_ltp = cfg.get('sim_ltp', 100.0)
        start_time = cfg.get('start_time', DEFAULT_START_TIME)
        squareoff_time = cfg.get('squareoff_time', DEFAULT_SQUAREOFF)
        sl_pct = float(cfg.get('sl_pct', DEFAULT_SL_PCT))
        instant_sl_pct = float(cfg.get('instant_sl_pct', DEFAULT_INSTANT_SL_PCT))
        trail_pct = float(cfg.get('trail_pct', DEFAULT_TRAIL_PCT))

        first_candle_used = False

        while not self._stop.is_set():
            try:
                now = datetime.now()
                if now.weekday() >= 5:
                    time.sleep(10)
                    continue

                # Fetch last 30 5-min candles fast via historical_data using instrument_token if provided
                df = pd.DataFrame()
                if self.live and self.kite and instrument_token:
                    df = self.get_5min_ohlc(instrument_token, '5minute', days=1)
                else:
                    # can't fetch live OHLC for paper or missing token; simulate simple candles around sim_ltp
                    base = float(sim_ltp)
                    prices = base * (1 + np.random.normal(0, 0.002, VWAP_CANDLES))
                    times = [now - timedelta(minutes=5*i) for i in range(VWAP_CANDLES)][::-1]
                    df = pd.DataFrame({
                        'date': times,
                        'open': prices,
                        'high': prices * (1 + np.abs(np.random.normal(0,0.002, VWAP_CANDLES))),
                        'low': prices * (1 - np.abs(np.random.normal(0,0.002, VWAP_CANDLES))),
                        'close': prices,
                        'volume': np.random.randint(100,1000,size=VWAP_CANDLES)
                    })

                # compute VWAP on df
                if not df.empty and 'volume' in df.columns:
                    typical = (df['high'] + df['low'] + df['close']) / 3.0
                    cum_vol = df['volume'].cumsum()
                    cum_vp = (typical * df['volume']).cumsum()
                    df['vwap'] = cum_vp / cum_vol

                # strategy entry checks
                # 1) 9:15-9:30 first candle bullish and rising -> immediate buy
                # identify first 5-min candle of day: assume df sorted ascending by time
                if len(df) >= 3:
                    first_candle = df.iloc[0]
                    latest = df.iloc[-1]
                    prev = df.iloc[-2]

                    # 9:15 window condition
                    if not first_candle_used and start_time <= now.time() <= (datetime.combine(now.date(), start_time) + timedelta(minutes=15)).time():
                        # first candle bullish?
                        if float(first_candle['close']) > float(first_candle['open']):
                            # and current/latest price > first candle close -> rising
                            ltp = self.get_ltp(symbol_ref) or sim_ltp
                            if ltp > float(first_candle['close']):
                                # execute immediate buy if not already in position
                                if self.entry_price is None:
                                    qty = self.compute_qty(ltp)
                                    if qty > 0:
                                        oid = self.place_market_buy(exchange, tradingsymbol, qty, ltp)
                                        if oid is not None:
                                            self.entry_price = ltp
                                            self.qty = qty
                                            self.entry_time = now
                                            self.peak_price = ltp
                                            self.instant_sl_price = round(self.entry_price * (1 - instant_sl_pct/100), 2)
                                            self.sl_trigger = round(self.entry_price * (1 - sl_pct/100), 2)
                                            # attempt SLM initial SL (live) - best effort
                                            try:
                                                if self.live:
                                                    self.sl_order_id = self.place_slm_try(exchange, tradingsymbol, qty, self.sl_trigger)
                                            except Exception as e:
                                                logger.add(f"SL placement error: {e}")
                                            msg = f"BUY executed {tradingsymbol} @{self.entry_price} qty={self.qty} SL={self.sl_trigger} InstantSL={self.instant_sl_price}"
                                            logger.add(msg)
                                            send_alert(msg)
                                            first_candle_used = True

                    # 2) after 9:30 uptrend: latest close > prev close & latest close > vwap
                    if self.entry_price is None:
                        entry_window_after = (datetime.combine(now.date(), dtime(9,30))).time()
                        if now.time() > entry_window_after:
                            try:
                                if float(latest['close']) > float(prev['close']) and float(latest['close']) > float(latest.get('vwap', -1)):
                                    # buy
                                    ltp = self.get_ltp(symbol_ref) or float(latest['close'])
                                    qty = self.compute_qty(ltp)
                                    if qty > 0:
                                        oid = self.place_market_buy(exchange, tradingsymbol, qty, ltp)
                                        if oid is not None:
                                            self.entry_price = ltp
                                            self.qty = qty
                                            self.entry_time = now
                                            self.peak_price = ltp
                                            self.instant_sl_price = round(self.entry_price * (1 - instant_sl_pct/100), 2)
                                            self.sl_trigger = round(self.entry_price * (1 - sl_pct/100), 2)
                                            try:
                                                if self.live:
                                                    self.sl_order_id = self.place_slm_try(exchange, tradingsymbol, qty, self.sl_trigger)
                                            except Exception as e:
                                                logger.add(f"SL placement error: {e}")
                                            msg = f"BUY executed {tradingsymbol} @{self.entry_price} qty={self.qty} SL={self.sl_trigger} InstantSL={self.instant_sl_price}"
                                            logger.add(msg)
                                            send_alert(msg)
                            except Exception as e:
                                logger.add(f"After-9:30 logic error: {e}")

                # Post-entry management
                if self.entry_price is not None:
                    # get live LTP
                    ltp = self.get_ltp(symbol_ref) or self.entry_price
                    # update peak
                    if ltp > self.peak_price:
                        self.peak_price = ltp
                    # instant SL check (immediate protection)
                    if ltp <= self.instant_sl_price:
                        logger.add(f"Instant SL hit @{ltp}")
                        send_alert(f"Instant SL hit for {tradingsymbol} @{ltp}")
                        self.place_market_sell(exchange, tradingsymbol, self.qty, ltp)
                        self.stop()
                        break
                    # initial SL check
                    if ltp <= self.sl_trigger:
                        logger.add(f"Initial SL hit @{ltp}")
                        send_alert(f"Initial SL hit for {tradingsymbol} @{ltp}")
                        self.place_market_sell(exchange, tradingsymbol, self.qty, ltp)
                        self.stop()
                        break
                    # breakeven and trailing
                    profit_pct = (ltp - self.entry_price)/self.entry_price*100
                    if profit_pct >= (sl_pct * 3.0):
                        # move SL to breakeven
                        try:
                            if self.sl_order_id and self.live:
                                self.modify_slm_try(self.sl_order_id, round(self.entry_price,2))
                                logger.add("Moved SL to breakeven")
                                send_alert(f"Moved SL to breakeven for {tradingsymbol}")
                            else:
                                # update local sl_trigger
                                self.sl_trigger = round(self.entry_price,2)
                        except Exception as e:
                            logger.add(f"Breakeven move error: {e}")
                    # trailing
                    trailing_trigger = round(self.peak_price * (1 - trail_pct/100), 2)
                    if trailing_trigger > self.sl_trigger:
                        try:
                            if self.sl_order_id and self.live:
                                self.modify_slm_try(self.sl_order_id, trailing_trigger)
                            self.sl_trigger = trailing_trigger
                            logger.add(f"Trailing SL updated -> {trailing_trigger} (peak {self.peak_price})")
                        except Exception as e:
                            logger.add(f"Trailing modify error: {e}")
                    # auto square-off
                    if now.time() >= squareoff_time:
                        logger.add("Square-off time reached -> exiting")
                        send_alert(f"Auto square-off for {tradingsymbol}")
                        self.place_market_sell(exchange, tradingsymbol, self.qty, ltp)
                        self.stop()
                        break

                time.sleep(PRICE_POLL_SEC)
            except Exception as e:
                logger.add(f"Engine loop error: {e}")
                traceback.print_exc()
                send_alert(f"Engine error: {e}")
                self.stop()
                break

        logger.add("Engine stopped")
        self.active = False

    def stop(self):
        self._stop.set()

    # Helper wrappers for live sl modification attempt - best effort
    def place_slm_try(self, exchange, tradingsymbol, qty, trigger):
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
            return oid
        except Exception as e:
            logger.add(f"place_slm_try error: {e}")
            return None
    def modify_slm_try(self, order_id, trigger):
        try:
            self.kite.modify_order(order_id=order_id, trigger_price=trigger)
            return True
        except Exception as e:
            logger.add(f"modify_slm_try error: {e}")
            return False

# ---------------------------
# Streamlit UI & threads
# ---------------------------
st.set_page_config(page_title="Auto Intraday Trader — Option1 Chart", layout="wide")
st.title("Auto Intraday MIS Trader — Last 30 5-min candles (Option 1)")

# Sidebar: connection & mode & SMS
with st.sidebar:
    st.header("Connection & Mode")
    api_key_in = st.text_input("Kite API Key (or set env ZK_API_KEY)", value=API_KEY, type="password")
    api_secret_in = st.text_input("Kite API Secret (or set env ZK_API_SECRET)", value=API_SECRET, type="password")
    provider = st.selectbox("SMS provider", ["none","fast2sms","twilio"], index=0)
    sms_number = st.text_input("SMS number (with country code, e.g., 9198...)", value=ALERT_MOBILE)
    fast2key = st.text_input("Fast2SMS API Key", value=FAST2SMS_KEY, type="password")
    tw_sid = st.text_input("Twilio SID", value=TWILIO_SID, type="password")
    tw_auth = st.text_input("Twilio Auth", value=TWILIO_AUTH, type="password")
    tw_from = st.text_input("Twilio From", value=TWILIO_FROM)
    # token flow helpers
    if st.button("Show Kite Login URL"):
        if not api_key_in:
            st.error("Provide Kite API Key")
        elif not KITE_AVAILABLE:
            st.error("kiteconnect not installed")
        else:
            t = KiteConnect(api_key=api_key_in)
            st.code(t.login_url())
            st.caption("Open the URL, login, copy request_token from redirected URL and paste below.")
    request_token_in = st.text_input("Paste request_token (from Kite redirect)")

    if st.button("Generate & Save Access Token"):
        if not api_key_in or not api_secret_in or not request_token_in:
            st.error("Provide API Key, API Secret and request_token")
        else:
            try:
                temp = KiteConnect(api_key=api_key_in)
                data = temp.generate_session(request_token_in.strip(), api_secret=api_secret_in.strip())
                safe_save_json(ACCESS_TOKEN_FILE, data)
                st.success("Saved access_token.json")
                logger.add("Saved access token")
            except Exception as e:
                st.error(f"Token gen failed: {e}")
                logger.add(f"Token gen failed: {e}")

# Mode & strategy params
col_cfg1, col_cfg2 = st.columns([2,2])
with col_cfg1:
    mode = st.selectbox("Mode", ["Paper","Live"], index=0)
    exchange = st.selectbox("Exchange", ["NSE","BSE"], index=0)
    tradingsymbol = st.text_input("Trading symbol (exact, e.g. RELIANCE or NIFTY25NOV...)", value="RELIANCE")
    instrument_token = st.text_input("Instrument token (numeric) - optional", value="")
with col_cfg2:
    exposure = st.number_input("Exposure (₹)", value=DEFAULT_EXPOSURE)
    leverage = st.number_input("Leverage", value=DEFAULT_LEVERAGE, step=0.5)
    sl_pct = st.number_input("Initial SL %", value=DEFAULT_SL_PCT, step=0.1)
    instant_sl_pct = st.number_input("Instant SL %", value=DEFAULT_INSTANT_SL_PCT, step=0.1)
    trail_pct = st.number_input("Trailing % after breakeven", value=DEFAULT_TRAIL_PCT, step=0.1)
    start_time = st.time_input("Auto start time", value=DEFAULT_START_TIME)
    squareoff_time = st.time_input("Auto square-off time", value=DEFAULT_SQUAREOFF)
    sim_ltp = st.number_input("Paper sim LTP (used when Paper mode or no LTP)", value=100.0)

# apply SMS config to session state
st.session_state['sms_enabled'] = (provider != "none")
st.session_state['sms_provider'] = provider
st.session_state['sms_number'] = sms_number
st.session_state['fast2sms_key'] = fast2key
st.session_state['twilio_sid'] = tw_sid
st.session_state['twilio_auth'] = tw_auth
st.session_state['twilio_from'] = tw_from

# Build kite instance if possible
saved = safe_load_json(ACCESS_TOKEN_FILE)
kite = None
if saved and api_key_in and KITE_AVAILABLE and mode == "Live":
    try:
        kite = KiteConnect(api_key=api_key_in)
        kite.set_access_token(saved.get('access_token'))
        # quick verify
        try:
            kite.profile()
            st.sidebar.success("Kite connected (live).")
        except Exception as e:
            st.sidebar.warning("Saved token invalid/expired. Generate a fresh token.")
            kite = None
    except Exception as e:
        st.sidebar.error(f"Kite build failed: {e}")
        kite = None

# paper broker
if 'paper_broker' not in st.session_state:
    st.session_state['paper_broker'] = PaperBroker()

# engine holder
if 'engine' not in st.session_state:
    st.session_state['engine'] = None

# create engine config
engine_cfg = {
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
    'sim_ltp': sim_ltp,
    'sms_enabled': st.session_state['sms_enabled']
}

# start/stop buttons
c1, c2, c3 = st.columns(3)
with c1:
    if st.button("Start Engine (background)"):
        if mode == "Live" and kite is None:
            st.error("Live mode requires valid saved access_token.json (generate via sidebar).")
        else:
            eng = TradingEngine(kite if mode=="Live" else None, cfg=engine_cfg, live=(mode=="Live"))
            st.session_state['engine'] = eng
            t = threading.Thread(target=eng.run, daemon=True)
            t.start()
            st.success("Engine started (background).")
            logger.add("Engine started")
with c2:
    if st.button("Stop Engine"):
        eng = st.session_state.get('engine')
        if eng:
            eng.stop()
            st.session_state['engine'] = None
            st.success("Engine stop requested")
            logger.add("Engine stop requested")
with c3:
    if st.button("Emergency Exit (cancel & close)"):
        eng = st.session_state.get('engine')
        if eng:
            eng.stop()
            st.session_state['engine'] = None
            logger.add("Emergency exit requested")
            send_alert("Emergency exit requested by user")

# Chart + P&L area
st.markdown("---")
left, right = st.columns([3,1])
with left:
    st.subheader("Live 5-min Candles (last 30) — chart updates fast")
    chart_placeholder = st.empty()
with right:
    st.subheader("Live P&L & Status")
    status_box = st.empty()
    pnl_box = st.empty()
    logs_box = st.empty()

# Chart updater thread (last 30 candles)
def chart_updater():
    while True:
        try:
            eng = st.session_state.get('engine')
            # choose source for candles
            df = pd.DataFrame()
            if eng and eng.live and eng.kite and eng.cfg.get('instrument_token'):
                df = eng.get_5min_ohlc(eng.cfg['instrument_token'], '5minute', days=1)
            else:
                # try to fetch by using kite.ltp and historical_data if kite available
                if kite and KITE_AVAILABLE and engine_cfg.get('instrument_token'):
                    df = eng.get_5min_ohlc(engine_cfg['instrument_token'], '5minute', days=1)
                # fallback to simulated candles using sim_ltp
            if df.empty:
                # simulate last 30 candles
                base = engine_cfg.get('sim_ltp', 100.0)
                prices = base * (1 + np.random.normal(0,0.0015, VWAP_CANDLES))
                times = [datetime.now() - timedelta(minutes=5*(VWAP_CANDLES-i)) for i in range(VWAP_CANDLES)]
                df = pd.DataFrame({'date':times, 'open':prices, 'high':prices*(1+0.001), 'low':prices*(1-0.001), 'close':prices, 'volume':np.random.randint(100,1000,VWAP_CANDLES)})
            # ensure sorted ascending
            if 'date' in df.columns:
                df = df.sort_values('date').tail(VWAP_CANDLES)
            else:
                # if times are indexed differently, just take tail
                df = df.tail(VWAP_CANDLES)
            # VWAP
            typical = (df['high'] + df['low'] + df['close']) / 3.0
            df['cum_vol'] = df['volume'].cumsum()
            df['cum_vp'] = (typical * df['volume']).cumsum()
            df['vwap'] = df['cum_vp'] / df['cum_vol']
            # build candlestick chart with plotly
            fig = go.Figure(data=[go.Candlestick(x=df['date'],
                                                 open=df['open'], high=df['high'], low=df['low'], close=df['close'],
                                                 name='candles')])
            # VWAP line
            fig.add_trace(go.Scatter(x=df['date'], y=df['vwap'], name='VWAP', mode='lines', line=dict(width=1)))
            # Markers for buy/exit if engine has recorded events
            eng_local = st.session_state.get('engine')
            buys = []
            exits = []
            sl_lines = []
            if eng_local:
                if eng_local.entry_price:
                    buys = [{'x': eng_local.entry_time, 'y': eng_local.entry_price}]
                    sl_lines.append(('InstantSL', eng_local.instant_sl_price))
                    sl_lines.append(('CurrentSL', eng_local.sl_trigger))
                # if engine stopped but had last order, nothing else
            # Draw buy / exit markers
            for b in buys:
                fig.add_trace(go.Scatter(x=[b['x']], y=[b['y']], mode='markers', marker_symbol='triangle-up', marker_color='green', marker_size=14, name='BUY'))
            for label,val in sl_lines:
                fig.add_hline(y=val, line=dict(color='orange' if 'Instant' in label else 'red', dash='dash'), annotation_text=label, annotation_position='top left')
            fig.update_layout(xaxis_rangeslider_visible=False, margin=dict(l=10,r=10,t=30,b=10), height=520)
            chart_placeholder.plotly_chart(fig, use_container_width=True)
            # status/pnl/logs
            if eng_local and eng_local.entry_price:
                ltp = eng_local.get_ltp(f"{eng_local.cfg['exchange']}:{eng_local.cfg['tradingsymbol']}") or eng_local.entry_price
                unreal = (ltp - eng_local.entry_price) * eng_local.qty
                color = "green" if unreal > 0 else "red" if unreal < 0 else "blue"
                status_box.markdown(f"**Engine:** {'Live' if eng_local.live else 'Paper'}  \n**Symbol:** {eng_local.cfg['tradingsymbol']}  \n**Entry:** ₹{eng_local.entry_price}  \n**Qty:** {eng_local.qty}")
                pnl_box.markdown(f"<h3 style='color:{color};'>Unreal P&L: ₹{unreal:.2f}</h3>", unsafe_allow_html=True)
            else:
                # show account positions (if kite)
                if kite:
                    try:
                        pos = kite.positions()
                        net = pos.get('net', []) if isinstance(pos, dict) else []
                        total = 0.0
                        lines = []
                        for p in net:
                            sym = p.get('tradingsymbol'); q = int(p.get('quantity',0) or 0); pnl = float(p.get('pnl',0) or 0)
                            total += pnl
                            lines.append(f"{sym}: qty={q} pnl={pnl}")
                        color = "green" if total > 0 else "red" if total < 0 else "blue"
                        pnl_box.markdown(f"<h3 style='color:{color};'>Total P&L: ₹{total:.2f}</h3>", unsafe_allow_html=True)
                        status_box.text("\n".join(lines[:10]) if lines else "No open positions")
                    except Exception as e:
                        status_box.text("No kite connection / positions")
                else:
                    status_box.text("No active position")
                    pnl_box.text("P&L not available (no engine/kite)")
            logs_box.text_area("Logs", value=logger.get(), height=300)
        except Exception as e:
            logger.add(f"chart_updater error: {e}")
        time.sleep(CHART_REFRESH_SEC)

# start chart updater thread once
if 'chart_thread' not in st.session_state:
    st.session_state['chart_thread'] = threading.Thread(target=chart_updater, daemon=True)
    st.session_state['chart_thread'].start()

st.caption("Chart shows last 30 5-minute candles (Option 1).")

# final note
st.markdown("---")
st.caption("Run in Paper mode to test fully. For Live mode: generate access token via sidebar then Start Engine. Always test carefully with small exposure before using real capital.")


