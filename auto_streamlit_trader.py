# auto_streamlit_trader.py
# Streamlit UI + Auto 9:15 + Manual controls
# Paper mode default; toggle to Live to place real orders via Kite Connect.

import streamlit as st
import pandas as pd
import numpy as np
import time
import threading
import json
import os
from datetime import datetime, timedelta
try:
    from kiteconnect import KiteConnect
except Exception:
    KiteConnect = None

st.set_page_config(page_title='Auto Streamlit Trader', layout='wide')
st.title('🔁 Auto Streamlit Trader — UI + Auto 9:15 (Paper mode default)')

# -------------------- Config & Files --------------------
ACCESS_TOKEN_FILE = 'access_token.json'
INSTRUMENTS_CSV = 'instruments.csv'

def now_str(): return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

# Simple logger
class SimpleLogger:
    def __init__(self):
        self.logs = []
    def add(self, s):
        msg = f"[{now_str()}] {s}"
        self.logs.append(msg)
        if len(self.logs) > 500: self.logs.pop(0)
    def get(self): return "\n".join(self.logs)

logger = SimpleLogger()
logger.add("App started. Paper mode is default. Toggle Live mode to enable real orders.")

# -------------------- Minimal strategy helpers --------------------
def sma(series, n): return series.rolling(n).mean()

def is_market_bullish(nifty_df):
    if len(nifty_df) < 60: return False
    nifty_df = nifty_df.copy()
    nifty_df['sma20'] = sma(nifty_df['close'], 20)
    nifty_df['sma50'] = sma(nifty_df['close'], 50)
    return nifty_df['sma20'].iloc[-1] > nifty_df['sma50'].iloc[-1]

def is_stock_uptrend(df):
    if len(df) < 60: return False
    df = df.copy()
    df['sma50'] = sma(df['close'], 50)
    if df['close'].iloc[-1] <= df['sma50'].iloc[-1]: return False
    highs = df['high'].tail(6).values
    return np.all(np.diff(highs) >= -1e-8)

# -------------------- Paper broker (simulation) --------------------
class PaperBroker:
    def __init__(self):
        self.orders = {}
        self.next_id = 1
    def place_market_buy(self, symbol, qty):
        oid = f"P{self.next_id}"
        self.next_id += 1
        self.orders[oid] = {'type':'BUY','symbol':symbol,'qty':qty,'price':None,'status':'FILLED','ts':now_str()}
        logger.add(f"Paper BUY placed id={oid} symbol={symbol} qty={qty}")
        return oid
    def place_slm(self, symbol, qty, trigger):
        oid = f"P{self.next_id}"
        self.next_id += 1
        self.orders[oid] = {'type':'SLM','symbol':symbol,'qty':qty,'trigger':trigger,'status':'ACTIVE','ts':now_str()}
        logger.add(f"Paper SL-M placed id={oid} trigger={trigger}")
        return oid
    def modify_order(self, oid, trigger):
        if oid in self.orders:
            self.orders[oid]['trigger'] = trigger
            logger.add(f"Paper order {oid} modified -> trigger {trigger}")
            return True
        return False

# -------------------- Trading engine --------------------
class TradingEngine:
    def __init__(self, broker, config, kite=None, live=False):
        self.broker = broker
        self.config = config
        self.kite = kite
        self.live = live and (kite is not None)
        self.entry_price = None
        self.qty = 0
        self.sl_order_id = None
        self.active = False
        self.peak_price = None
        self._stop = threading.Event()

    def compute_qty(self, ltp):
        if self.config['qty_mode']=='fixed':
            return int(self.config['qty'])
        if ltp<=0: return 0
        return int((self.config['exposure'] * self.config['leverage']) // ltp)

    def fetch_ohlc(self, token):
        if self.live and self.kite:
            return pd.DataFrame(self.kite.historical_data(int(token), datetime.now()-timedelta(days=1), datetime.now(), '5minute'))
        # Paper mode: load sample or return empty -> user will test
        return pd.DataFrame()

    def get_ltp(self):
        if self.live and self.kite:
            ref = f"{self.config['exchange']}:{self.config['tradingsymbol']}"
            l = list(self.kite.ltp(ref).values())[0]['last_price']
            return l
        # Paper: simulate small random walk around entry or return entry
        if self.entry_price:
            # simple slight drift
            return round(self.entry_price * (1 + np.random.normal(0, 0.001)),2)
        return None

    def try_entry(self):
        cfg = self.config
        logger.add("Evaluating entry conditions (paper mode may not have OHLC).")
        # In live mode, fetch data and evaluate
        if self.live:
            try:
                nifty_df = pd.DataFrame(self.kite.historical_data(int(cfg['nifty_token']), datetime.now()-timedelta(days=1), datetime.now(), '5minute'))
                stock_df = pd.DataFrame(self.kite.historical_data(int(cfg['symbol_token']), datetime.now()-timedelta(days=1), datetime.now(), '5minute'))
            except Exception as e:
                logger.add(f"OHLC fetch failed: {e}")
                return False
            if nifty_df.empty or stock_df.empty:
                logger.add("OHLC empty, skipping")
                return False
            if not is_market_bullish(nifty_df) or not is_stock_uptrend(stock_df):
                logger.add("Conditions not met")
                return False
            ltp = stock_df['close'].iloc[-1]
        else:
            # Paper mode: simulate an LTP for decision-making
            ltp = self.config.get('sim_ltp') or 100.0
        qty = self.compute_qty(ltp)
        if qty<=0:
            logger.add("Qty computed <=0, abort")
            return False
        if self.live:
            order_id = self.kite.place_order(tradingsymbol=cfg['tradingsymbol'], exchange=cfg['exchange'],
                                             transaction_type=self.kite.TRANSACTION_TYPE_BUY, quantity=qty,
                                             order_type=self.kite.ORDER_TYPE_MARKET, product='MIS', variety='regular')
            self.entry_price = ltp
            self.qty = qty
            # place SL-M
            trigger = round(self.entry_price * (1 - cfg['sl_pct']/100),2)
            slid = self.kite.place_order(tradingsymbol=cfg['tradingsymbol'], exchange=cfg['exchange'],
                                         transaction_type=self.kite.TRANSACTION_TYPE_SELL, quantity=qty,
                                         order_type=self.kite.ORDER_TYPE_SLM, trigger_price=trigger, product='MIS', variety='regular')
            self.sl_order_id = slid
            logger.add(f"Live BUY placed id={order_id} ltp={ltp} qty={qty} sl_trigger={trigger}")
        else:
            order_id = self.broker.place_market_buy(cfg['tradingsymbol'], qty)
            self.entry_price = ltp
            self.qty = qty
            trigger = round(self.entry_price * (1 - cfg['sl_pct']/100),2)
            slid = self.broker.place_slm(cfg['tradingsymbol'], qty, trigger)
            self.sl_order_id = slid
            logger.add(f"Paper BUY placed id={order_id} ltp={ltp} qty={qty} sl_trigger={trigger}")
        self.peak_price = self.entry_price
        self.active = True
        return True

    def manage_trailing(self):
        if not self.entry_price: return
        ltp = self.get_ltp()
        if ltp is None: return
        if ltp > self.peak_price: self.peak_price = ltp
        # if price falls to initial SL level
        if ltp <= self.entry_price * (1 - self.config['sl_pct']/100):
            logger.add("Price dropped to SL level -> assume exit")
            self.active = False
            return
        profit_pct = (ltp - self.entry_price) / self.entry_price * 100
        if profit_pct >= self.config['sl_pct'] * 3:
            new_trigger = round(self.entry_price,2)
            # modify SL to breakeven
            if self.live and self.kite:
                try:
                    self.kite.modify_order(order_id=self.sl_order_id, trigger_price=new_trigger)
                    logger.add(f"Modified live SL to breakeven {new_trigger}")
                except Exception as e:
                    logger.add(f"Modify SL failed: {e}")
            else:
                self.broker.modify_order(self.sl_order_id, new_trigger)
        # basic trailing: set SL = peak * (1 - sl_pct)
        trailing = round(self.peak_price * (1 - self.config['sl_pct']/100),2)
        if self.live and self.kite:
            try:
                self.kite.modify_order(order_id=self.sl_order_id, trigger_price=trailing)
            except Exception:
                pass
        else:
            self.broker.modify_order(self.sl_order_id, trailing)

    def run_loop(self):
        logger.add("Engine loop started")
        while not self._stop.is_set():
            now = datetime.now()
            if now.weekday() >= 5:
                time.sleep(10); continue
            if not self.entry_price:
                if now.hour == 9 and now.minute == 15 and now.second < 5:
                    logger.add("09:15 triggered — attempting entry")
                    self.try_entry()
            else:
                self.manage_trailing()
            if now.hour == 15 and now.minute >= 30:
                logger.add("Market closed — engine will stop for the day")
                break
            time.sleep(5)
        logger.add("Engine loop ended")

    def stop(self):
        self._stop.set()

# -------------------- Streamlit UI --------------------

st.sidebar.header("Mode & Connection")
live_mode = st.sidebar.checkbox("Live mode (place real orders)", value=False)
api_key = st.sidebar.text_input("Kite API Key", type="password")
api_secret = st.sidebar.text_input("Kite API Secret", type="password")
access_token = None
if os.path.exists(ACCESS_TOKEN_FILE):
    try:
        access_token = json.load(open(ACCESS_TOKEN_FILE)).get('access_token')
        st.sidebar.success("Found saved access_token.json")
    except Exception:
        pass
show_login = st.sidebar.button("Show Kite Login URL")
if show_login and KiteConnect is not None and api_key:
    kite_temp = KiteConnect(api_key=api_key)
    st.sidebar.code(kite_temp.login_url())
    st.sidebar.info("Open link, login, paste request_token in 'Request token' below and click 'Generate'")

request_token = st.sidebar.text_input("Request token (from Kite login)")
if st.sidebar.button("Generate & Save Access Token"):
    if KiteConnect is None:
        st.sidebar.error("kiteconnect not installed in this environment")
    else:
        try:
            kite_temp = KiteConnect(api_key=api_key)
            data = kite_temp.generate_session(request_token, api_secret=api_secret)
            json.dump(data, open(ACCESS_TOKEN_FILE,'w'))
            st.sidebar.success("Access token saved to access_token.json")
        except Exception as e:
            st.sidebar.error(f"Failed to generate session: {e}")

st.header("Strategy Configuration")
cols = st.columns(3)
with cols[0]:
    tradingsymbol = st.text_input("Trading symbol", value="INFY")
    exchange = st.selectbox("Exchange", ["NSE","BSE"])
    symbol_token = st.text_input("Instrument token (numeric) - optional", value="")
with cols[1]:
    qty_mode = st.radio("Quantity mode", ["exposure","fixed"], index=0)
    exposure = st.number_input("Exposure (₹)", value=50000)
    leverage = st.number_input("Leverage", value=5)
    qty = st.number_input("Fixed qty", value=1000, step=1)
with cols[2]:
    sl_pct = st.number_input("Initial Stop Loss (%)", value=1.0)
    auto_mode = st.checkbox("Auto mode (start before 09:15)", value=True)
    allow_manual = st.checkbox("Allow Manual Start/Stop", value=True)

st.markdown("---")
if 'engine_thread' not in st.session_state: st.session_state['engine_thread'] = None
if 'engine' not in st.session_state: st.session_state['engine'] = None
broker = PaperBroker()

# Build kite if live_mode
kite = None
if live_mode:
    if KiteConnect is None:
        st.error("kiteconnect library not available. Live mode disabled.")
        live_mode = False
    elif api_key and api_secret and access_token:
        kite = KiteConnect(api_key=api_key); kite.set_access_token(access_token)
        st.sidebar.success("Kite connected (from saved token)")

config = {'tradingsymbol':tradingsymbol,'exchange':exchange,'symbol_token':symbol_token,'qty_mode': 'fixed' if qty_mode=='fixed' else 'exposure','qty':qty,'exposure':exposure,'leverage':leverage,'sl_pct':sl_pct,'nifty_token':'256265'}

def start_engine():
    if st.session_state['engine'] is not None:
        logger.add("Engine already running")
        return
    engine = TradingEngine(broker, config, kite=kite, live=live_mode)
    t = threading.Thread(target=engine.run_loop, daemon=True)
    st.session_state['engine'] = engine
    st.session_state['engine_thread'] = t
    t.start()
    logger.add("Engine started")

def stop_engine():
    eng = st.session_state.get('engine')
    if eng:
        eng.stop()
    st.session_state['engine'] = None
    st.session_state['engine_thread'] = None
    logger.add("Engine stopped")

if auto_mode and st.session_state.get('engine') is None:
    # start a background scheduler thread that will start engine before 09:15
    def scheduler():
        logger.add("Auto scheduler running")
        while True:
            now = datetime.now()
            if now.weekday() < 5:
                if st.session_state.get('engine') is None and now.hour==9 and 10<=now.minute<=14:
                    start_engine()
                if st.session_state.get('engine') is not None and now.hour==15 and now.minute>=31:
                    stop_engine()
            time.sleep(15)
    if 'scheduler_thread' not in st.session_state:
        st.session_state['scheduler_thread'] = threading.Thread(target=scheduler, daemon=True)
        st.session_state['scheduler_thread'].start()

if allow_manual:
    c1, c2 = st.columns(2)
    if c1.button("Manual Start"): start_engine()
    if c2.button("Manual Stop"): stop_engine()

st.subheader("Status & Logs")
eng = st.session_state.get('engine')
st.write("Engine active:", bool(eng and eng.active))
st.write("Entry price:", eng.entry_price if eng else '-')
st.write("Qty:", eng.qty if eng else '-')
st.write("Peak:", eng.peak_price if eng else '-')
st.write("SL order id:", eng.sl_order_id if eng else '-')

st.text_area("Activity logs", value=logger.get(), height=300)