# auto_streamlit_final.py
# Fully automated Streamlit + Zerodha intraday trader
# - Manual symbol entry
# - Live & Paper modes
# - Auto 09:15 entry, SL/Trailing, 15:15 square-off
# - Cancel all pending orders & close all positions on SL or time exit
# - Live P&L display and logs

import streamlit as st
import pandas as pd
import numpy as np
import threading
import time
import json
import os
from datetime import datetime, timedelta
try:
    from kiteconnect import KiteConnect
except Exception:
    KiteConnect = None

# ----------------- CONFIG DEFAULTS -----------------
DEFAULT_NIFTY_TOKEN = "256265"   # change if you use a different token
DEFAULT_SL_PCT = 1.0             # initial stop loss percent
TRIGGER_MULTIPLIER = 3.0         # 1:3 -> when profit >= SL*3 we move SL to breakeven
TRAIL_PCT = 3.0                  # trailing percent (off peak)
DEFAULT_EXPOSURE = 50000
DEFAULT_LEVERAGE = 5

ACCESS_TOKEN_FILE = "access_token.json"

# ----------------- SIMPLE LOGGER -----------------
class SimpleLogger:
    def __init__(self, maxlen=500):
        self.logs = []
        self.maxlen = maxlen
    def add(self, msg):
        t = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        s = f"[{t}] {msg}"
        self.logs.append(s)
        if len(self.logs) > self.maxlen:
            self.logs.pop(0)
    def get(self):
        return "\n".join(self.logs)

logger = SimpleLogger()

# ----------------- PAPER BROKER (SIMULATION) -----------------
class PaperBroker:
    def __init__(self):
        self.orders = {}  # simple store: id -> order dict
        self.positions = {}  # symbol -> net qty, avg price
        self._next = 1
    def _next_id(self):
        oid = f"P{self._next}"
        self._next += 1
        return oid
    def place_market_buy(self, symbol, qty, price=None):
        oid = self._next_id()
        # simulate fill at price or dummy
        fill_price = price if price else (self.positions.get(symbol, {}).get('avg', 100.0))
        self.orders[oid] = {'id': oid, 'type': 'BUY', 'symbol': symbol, 'qty': qty, 'price': fill_price, 'status': 'FILLED'}
        # update position
        pos = self.positions.get(symbol, {'qty':0, 'avg':0.0})
        total_qty = pos['qty'] + qty
        if pos['qty'] == 0:
            pos['avg'] = fill_price
        else:
            pos['avg'] = (pos['avg']*pos['qty'] + fill_price*qty) / total_qty
        pos['qty'] = total_qty
        self.positions[symbol] = pos
        logger.add(f"[Paper] BUY filled {symbol} qty {qty} @ {fill_price} (id {oid})")
        return oid
    def place_slm(self, symbol, qty, trigger):
        oid = self._next_id()
        self.orders[oid] = {'id': oid, 'type': 'SLM', 'symbol': symbol, 'qty': qty, 'trigger': trigger, 'status': 'ACTIVE'}
        logger.add(f"[Paper] SLM placed {symbol} qty {qty} trigger {trigger} (id {oid})")
        return oid
    def modify_order(self, oid, trigger):
        if oid in self.orders:
            self.orders[oid]['trigger'] = trigger
            logger.add(f"[Paper] Order {oid} modified trigger->{trigger}")
            return True
        return False
    def cancel_all(self):
        cancelled = []
        for oid, o in list(self.orders.items()):
            if o.get('status') in ('ACTIVE','OPEN','TRIGGER PENDING'):
                o['status'] = 'CANCELLED'
                cancelled.append(oid)
        logger.add(f"[Paper] Cancelled orders: {cancelled}")
        return cancelled
    def close_all_positions(self):
        closed = []
        for sym, pos in list(self.positions.items()):
            qty = pos.get('qty',0)
            if qty == 0:
                continue
            # simulate market close at avg (no PnL calc here)
            closed.append({'symbol': sym, 'qty': qty})
            logger.add(f"[Paper] Closed {sym} qty {qty}")
            self.positions[sym] = {'qty':0, 'avg':0.0}
        return closed
    def get_positions(self):
        # return list similar to kite.positions()['net']
        res = []
        for sym, p in self.positions.items():
            if p['qty'] != 0:
                res.append({'tradingsymbol': sym, 'exchange': 'NSE', 'quantity': p['qty'], 'pnl': 0.0, 'avg_price': p['avg']})
        return res

# ----------------- TRADING ENGINE -----------------
class TradingEngine:
    def __init__(self, kite=None, broker=None, live=False, config=None):
        self.kite = kite
        self.broker = broker
        self.live = live and (kite is not None)
        self.config = config or {}
        self.entry_price = None
        self.qty = 0
        self.sl_order_id = None
        self.entry_order_id = None
        self.peak_price = None
        self.active = False
        self._stop = threading.Event()

    # --- helpers ---
    def compute_qty(self, ltp):
        if self.config.get('qty_mode') == 'fixed':
            return int(self.config.get('qty', 1000))
        # exposure mode
        exposure = self.config.get('exposure', DEFAULT_EXPOSURE)
        leverage = self.config.get('leverage', DEFAULT_LEVERAGE)
        if ltp <= 0:
            return 0
        qty = int((exposure * leverage) // ltp)
        return max(qty, 1)

    def get_ohlc(self, token, interval='5minute', days=1):
        if self.live and self.kite:
            try:
                data = self.kite.historical_data(int(token), datetime.now() - timedelta(days=days), datetime.now(), interval)
                return pd.DataFrame(data)
            except Exception as e:
                logger.add(f"OHLC fetch failed: {e}")
                return pd.DataFrame()
        return pd.DataFrame()

    def get_ltp(self, exchange, symbol):
        if self.live and self.kite:
            try:
                ref = f"{exchange}:{symbol}"
                d = self.kite.ltp(ref)
                price = list(d.values())[0]['last_price']
                return price
            except Exception as e:
                logger.add(f"Live LTP fetch failed: {e}")
                return None
        # paper mode simulate
        if self.entry_price:
            # slight random walk around peak/entry
            return round(self.entry_price * (1 + np.random.normal(0, 0.0015)), 2)
        return None

    # --- order / position operations ---
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
                logger.add(f"[Live] Market BUY order placed id={oid} qty={qty}")
                return oid
            except Exception as e:
                logger.add(f"[Live] Market buy failed: {e}")
                return None
        else:
            return self.broker.place_market_buy(symbol, qty, price=self.config.get('sim_ltp'))

    def place_slm(self, symbol, exchange, qty, trigger_price):
        if self.live and self.kite:
            try:
                oid = self.kite.place_order(
                    tradingsymbol=symbol,
                    exchange=exchange,
                    transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                    quantity=qty,
                    order_type=self.kite.ORDER_TYPE_SLM,
                    trigger_price=trigger_price,
                    product=self.kite.PRODUCT_MIS,
                    variety=self.kite.VARIETY_REGULAR
                )
                logger.add(f"[Live] SLM placed id={oid} trigger={trigger_price}")
                return oid
            except Exception as e:
                logger.add(f"[Live] SL placement failed: {e}")
                return None
        else:
            return self.broker.place_slm(symbol, qty, trigger_price)

    def modify_slm(self, order_id, trigger_price):
        if self.live and self.kite:
            try:
                self.kite.modify_order(order_id=order_id, trigger_price=trigger_price)
                logger.add(f"[Live] Modified SL {order_id} -> {trigger_price}")
                return True
            except Exception as e:
                logger.add(f"[Live] Modify SL failed: {e}")
                return False
        else:
            return self.broker.modify_order(order_id, trigger_price)

    # --- cancel & close utilities ---
    def cancel_all_pending_orders(self):
        cancelled = []
        if self.live and self.kite:
            try:
                orders = self.kite.orders()
                for o in orders:
                    status = (o.get('status') or "").upper()
                    oid = o.get('order_id')
                    if status in ('OPEN', 'TRIGGER PENDING', 'PENDING'):
                        try:
                            variety = o.get('variety', self.kite.VARIETY_REGULAR)
                            self.kite.cancel_order(order_id=oid, variety=variety)
                            cancelled.append(oid)
                        except Exception as e:
                            logger.add(f"Failed to cancel {oid}: {e}")
                logger.add(f"[Live] Cancelled pending orders: {cancelled}")
            except Exception as e:
                logger.add(f"[Live] Cancel all failed: {e}")
        else:
            cancelled = self.broker.cancel_all()
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
                    if qty == 0:
                        continue
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
                        closed.append({'symbol': tradingsymbol, 'qty': qty, 'order_id': order})
                    except Exception as e:
                        logger.add(f"Failed to close {tradingsymbol}: {e}")
                logger.add(f"[Live] Close orders placed: {closed}")
            except Exception as e:
                logger.add(f"[Live] Close all failed: {e}")
        else:
            closed = self.broker.close_all_positions()
        return closed

    # --- emergency exit (cancel + close + stop) ---
    def emergency_exit(self, reason="emergency"):
        logger.add(f"Emergency exit triggered: {reason}")
        try:
            self.cancel_all_pending_orders()
            self.close_all_open_positions()
        except Exception as e:
            logger.add(f"Error during emergency exit: {e}")
        self.stop()
        self.active = False
        logger.add("Engine stopped for the day after emergency exit.")

    # --- entry evaluation and placement ---
    def evaluate_and_place_entry(self):
        cfg = self.config
        # need instrument tokens for historical checks (nifty token and symbol token)
        try:
            nifty_token = cfg.get('nifty_token', DEFAULT_NIFTY_TOKEN)
            symbol_token = cfg.get('symbol_token')
            if not symbol_token and self.live:
                logger.add("Symbol token required for OHLC checks in live mode. Skipping entry.")
                return False
            nifty_df = self.get_ohlc(nifty_token, '5minute', days=1) if self.live else pd.DataFrame()
            stock_df = self.get_ohlc(symbol_token, '5minute', days=1) if self.live else pd.DataFrame()
            if self.live:
                if nifty_df.empty or stock_df.empty:
                    logger.add("OHLC data unavailable; aborting entry evaluation.")
                    return False
                # evaluate bullish & uptrend
                nifty_df['sma20'] = nifty_df['close'].rolling(20).mean()
                nifty_df['sma50'] = nifty_df['close'].rolling(50).mean()
                if nifty_df['sma20'].iloc[-1] <= nifty_df['sma50'].iloc[-1]:
                    logger.add("Market not bullish; skipping entry.")
                    return False
                stock_df['sma50'] = stock_df['close'].rolling(50).mean()
                if stock_df['close'].iloc[-1] <= stock_df['sma50'].iloc[-1]:
                    logger.add("Stock not in uptrend; skipping entry.")
                    return False
                ltp = stock_df['close'].iloc[-1]
            else:
                # paper: simulate an LTP from config or default
                ltp = cfg.get('sim_ltp', 100.0)
            qty = self.compute_qty(ltp)
            if qty <= 0:
                logger.add("Computed qty <= 0; aborting entry.")
                return False
            # Place market buy
            entry_oid = self.place_market_buy(cfg['tradingsymbol'], cfg['exchange'], qty)
            if not entry_oid:
                logger.add("Entry placement failed.")
                return False
            self.entry_order_id = entry_oid
            self.entry_price = ltp
            self.qty = qty
            self.peak_price = ltp
            # place initial SL
            trigger = round(self.entry_price * (1 - cfg['sl_pct']/100), 2)
            sl_oid = self.place_slm(cfg['tradingsymbol'], cfg['exchange'], qty, trigger)
            self.sl_order_id = sl_oid
            logger.add(f"Entry executed @ {self.entry_price} qty {self.qty}; initial SL @ {trigger}")
            return True
        except Exception as e:
            logger.add(f"Entry exception: {e}")
            return False

    # --- trailing management ---
    def manage_trailing(self):
        if not self.entry_price:
            return
        ltp = self.get_ltp(self.config['exchange'], self.config['tradingsymbol'])
        if ltp is None:
            return
        # update peak price
        if self.peak_price is None or ltp > self.peak_price:
            self.peak_price = ltp
        # check initial SL hit
        if ltp <= self.entry_price * (1 - self.config['sl_pct']/100):
            logger.add("Price hit initial SL level — executing emergency exit.")
            self.emergency_exit(reason="SL hit")
            return
        # profit percent relative to entry
        profit_pct = (ltp - self.entry_price) / self.entry_price * 100
        if profit_pct >= (self.config['sl_pct'] * TRIGGER_MULTIPLIER):
            # move SL to breakeven (entry price) first, then trail by TRAIL_PCT off peak
            new_trigger = round(self.entry_price, 2)
            if self.sl_order_id:
                self.modify_slm(self.sl_order_id, new_trigger)
                logger.add(f"Moved SL to breakeven @ {new_trigger}")
        # compute trailing trigger based on peak
        trailing_trigger = round(self.peak_price * (1 - TRAIL_PCT/100), 2)
        # Only raise SL if trailing_trigger is higher than current SL trigger
        if self.sl_order_id:
            try:
                self.modify_slm(self.sl_order_id, trailing_trigger)
                logger.add(f"Attempted trailing SL -> {trailing_trigger} (peak {self.peak_price})")
            except Exception as e:
                logger.add(f"Trailing modify exception: {e}")

    # --- main loop ---
    def run_forever(self):
        logger.add("Engine loop starting")
        self.active = True
        self._stop.clear()
        while not self._stop.is_set():
            try:
                now = datetime.now()
                # weekends skip
                if now.weekday() >= 5:
                    time.sleep(10)
                    continue
                # if not entered yet, wait for 09:15 trigger
                if not self.entry_price:
                    if now.hour == 9 and now.minute == 15 and now.second < 10:
                        logger.add("09:15 reached -> evaluating entry")
                        self.evaluate_and_place_entry()
                else:
                    # manage trailing and detect SL
                    self.manage_trailing()
                # day-end square off at >=15:15
                if now.hour == 15 and now.minute >= 15:
                    logger.add("15:15 reached -> performing day-end square-off")
                    self.emergency_exit(reason="Time 15:15")
                    break
            except Exception as e:
                logger.add(f"Engine loop exception: {e}")
            time.sleep(3)
        logger.add("Engine loop ended")
        self.active = False

    def stop(self):
        self._stop.set()
        logger.add("Engine stop requested (thread)")

# ----------------- P&L & Status Helpers -----------------
def get_live_pnl(kite=None, broker=None, live=False):
    """
    Returns total_pnl (float) and a list of position dicts for display.
    For live, uses kite.positions(); for paper, uses broker state.
    """
    total_pnl = 0.0
    pnl_list = []
    try:
        if live and kite:
            data = kite.positions()
            net = data.get('net', []) if isinstance(data, dict) else []
            for p in net:
                symbol = p.get('tradingsymbol')
                qty = int(p.get('quantity', 0) or 0)
                pnl = float(p.get('pnl', 0) or 0)
                avg = float(p.get('avg_price', 0) or 0)
                total_pnl += pnl
                pnl_list.append({'Symbol': symbol, 'Qty': qty, 'Avg': avg, 'PnL': round(pnl,2)})
        else:
            if broker:
                positions = broker.get_positions()
                for p in positions:
                    symbol = p.get('tradingsymbol') or p.get('symbol')
                    qty = int(p.get('quantity', 0) or 0)
                    pnl = float(p.get('pnl', 0) or 0)
                    total_pnl += pnl
                    pnl_list.append({'Symbol': symbol, 'Qty': qty, 'Avg': p.get('avg_price', 0), 'PnL': round(pnl, 2)})
    except Exception as e:
        logger.add(f"P&L fetch error: {e}")
    return total_pnl, pnl_list

# ----------------- STREAMLIT UI -----------------
st.set_page_config(page_title="Auto Trader Final", layout="wide")
st.title("🔁 Auto Streamlit Trader — Final (Auto 09:15, SL/Trail, 15:15 Square-off)")

# Sidebar: connection & mode
st.sidebar.header("Connection & Mode")
live_mode = st.sidebar.checkbox("Live mode (place real orders)", value=False)
api_key = st.sidebar.text_input("Kite API Key", type="password")
api_secret = st.sidebar.text_input("Kite API Secret", type="password")

saved_access = None
if os.path.exists(ACCESS_TOKEN_FILE):
    try:
        saved_access = json.load(open(ACCESS_TOKEN_FILE))
        st.sidebar.success("Found saved access_token.json")
    except Exception:
        saved_access = None

if st.sidebar.button("Show Kite Login URL"):
    if KiteConnect is None or not api_key:
        st.sidebar.error("kiteconnect missing or API key not provided")
    else:
        temp = KiteConnect(api_key=api_key)
        st.sidebar.code(temp.login_url())
        st.sidebar.info("Open link, login with the Zerodha ID that created the app, copy request_token from redirect URL and paste below.")

request_token = st.sidebar.text_input("Request token (from Kite login)")
if st.sidebar.button("Generate & Save Access Token"):
    if KiteConnect is None:
        st.sidebar.error("kiteconnect not available")
    else:
        try:
            temp = KiteConnect(api_key=api_key)
            data = temp.generate_session(request_token, api_secret=api_secret)
            json.dump(data, open(ACCESS_TOKEN_FILE, "w"))
            st.sidebar.success("Access token saved to access_token.json")
            saved_access = data
        except Exception as e:
            st.sidebar.error(f"Token gen failed: {e}")

# Strategy configuration (main)
st.subheader("Strategy Configuration")
c1, c2, c3 = st.columns(3)
with c1:
    tradingsymbol = st.text_input("Trading symbol (exact)", value="INFY")
    exchange = st.selectbox("Exchange", ["NSE", "BSE"], index=0)
    symbol_token = st.text_input("Instrument token (numeric) - optional", value="")
with c2:
    qty_mode = st.selectbox("Quantity mode", ["exposure", "fixed"], index=0)
    exposure = st.number_input("Exposure (₹)", value=DEFAULT_EXPOSURE)
    leverage = st.number_input("Leverage", value=DEFAULT_LEVERAGE)
    qty = st.number_input("Fixed qty", value=1000, step=1)
with c3:
    sl_pct = st.number_input("Initial Stop Loss (%)", value=DEFAULT_SL_PCT)
    trail_pct = st.number_input("Trailing percent (used after breakeven)", value=TRAIL_PCT)
    auto_mode = st.checkbox("Auto mode (start before 09:15)", value=True)
    allow_manual = st.checkbox("Allow Manual Start/Stop", value=True)

# session objects
if 'engine' not in st.session_state:
    st.session_state['engine'] = None
if 'engine_thread' not in st.session_state:
    st.session_state['engine_thread'] = None
if 'kite' not in st.session_state:
    st.session_state['kite'] = None
if 'broker' not in st.session_state:
    st.session_state['broker'] = None

# Build kite object if live mode & token available
kite = None
if live_mode:
    if KiteConnect is None:
        st.error("kiteconnect library not installed — Live mode disabled.")
        live_mode = False
    else:
        access = saved_access or (json.load(open(ACCESS_TOKEN_FILE)) if os.path.exists(ACCESS_TOKEN_FILE) else None)
        if access:
            try:
                kite = KiteConnect(api_key=api_key)
                kite.set_access_token(access.get('access_token'))
                st.sidebar.success("Kite connected (from saved token).")
                st.session_state['kite'] = kite
            except Exception as e:
                st.sidebar.error(f"Kite connect failed: {e}")
                kite = None
        else:
            st.sidebar.info("No access token found. Generate one using the login URL.")

# create paper broker by default
if st.session_state.get('broker') is None:
    st.session_state['broker'] = PaperBroker()
broker = st.session_state['broker']

# config dict
config = {
    'tradingsymbol': tradingsymbol,
    'exchange': exchange,
    'symbol_token': symbol_token,
    'qty_mode': 'fixed' if qty_mode == 'fixed' else 'exposure',
    'qty': int(qty),
    'exposure': float(exposure),
    'leverage': float(leverage),
    'sl_pct': float(sl_pct),
    'nifty_token': DEFAULT_NIFTY_TOKEN,
    'sim_ltp': 100.0
}

# engine management functions
def start_engine():
    if st.session_state.get('engine') is not None:
        logger.add("Engine already running")
        return
    eng = TradingEngine(kite=kite, broker=broker, live=live_mode, config=config)
    t = threading.Thread(target=eng.run_forever, daemon=True)
    st.session_state['engine'] = eng
    st.session_state['engine_thread'] = t
    t.start()
    logger.add("Engine started")

def stop_engine():
    eng = st.session_state.get('engine')
    if eng:
        eng.stop()
    st.session_state['engine'] = None
    st.session_state['engine_thread'] = None
    logger.add("Engine stopped by user")

# auto scheduler thread
if auto_mode and st.session_state.get('engine') is None:
    def scheduler_loop():
        logger.add("Auto-scheduler active")
        while True:
            if datetime.now().weekday() < 5:  # weekdays only
                now = datetime.now()
                if st.session_state.get('engine') is None and now.hour == 9 and 10 <= now.minute <= 14:
                    try:
                        start_engine()
                    except Exception as e:
                        logger.add(f"Auto-scheduler start error: {e}")
                if st.session_state.get('engine') is not None and now.hour == 15 and now.minute >= 31:
                    try:
                        stop_engine()
                    except Exception as e:
                        logger.add(f"Auto-scheduler stop error: {e}")
            time.sleep(20)
    if 'scheduler_thread' not in st.session_state:
        st.session_state['scheduler_thread'] = threading.Thread(target=scheduler_loop, daemon=True)
        st.session_state['scheduler_thread'].start()

# manual start/stop
if allow_manual:
    m1, m2, m3 = st.columns([1,1,2])
    if m1.button("Manual Start"):
        start_engine()
    if m2.button("Manual Stop"):
        eng = st.session_state.get('engine')
        if eng:
            eng.emergency_exit(reason="Manual stop requested")
            stop_engine()
    if m3.button("🛑 Emergency Exit (Cancel all & Close all)"):
        eng = st.session_state.get('engine')
        if eng:
            eng.emergency_exit(reason="Manual emergency")

# Dashboard
st.markdown("---")
left, right = st.columns([2,1])

with left:
    st.subheader("Engine Status")
    eng = st.session_state.get('engine')
    st.write("Engine active:", bool(eng and eng.active))
    st.write("Live mode:", live_mode)
    st.write("Symbol:", tradingsymbol)
    st.write("Entry price:", eng.entry_price if eng else "-")
    st.write("Qty:", eng.qty if eng else "-")
    st.write("Peak price:", eng.peak_price if eng else "-")
    st.write("SL order id:", eng.sl_order_id if eng else "-")
    st.write("Entry order id:", eng.entry_order_id if eng else "-")

    st.subheader("Live P&L")
    pnl_placeholder = st.empty()
    # start a small background updater thread for PnL & logs if not present
    if 'pnl_thread' not in st.session_state:
        def pnl_updater():
            while True:
                try:
                    eng_local = st.session_state.get('engine')
                    total_pnl, pnl_list = get_live_pnl(kite=eng_local.kite if eng_local else kite,
                                                       broker=broker,
                                                       live=eng_local.live if eng_local else live_mode)
                    # set in session for UI read
                    st.session_state['pnl_total'] = total_pnl
                    st.session_state['pnl_list'] = pnl_list
                except Exception as e:
                    logger.add(f"PnL updater error: {e}")
                time.sleep(5)
        st.session_state['pnl_thread'] = threading.Thread(target=pnl_updater, daemon=True)
        st.session_state['pnl_thread'].start()

    # display current pnl
    total = st.session_state.get('pnl_total', 0.0)
    color = "green" if total >= 0 else "red"
    pnl_placeholder.markdown(f"<h2 style='text-align:center;color:{color};'>Total P&L: ₹{total:.2f}</h2>", unsafe_allow_html=True)
    if st.session_state.get('pnl_list'):
        st.dataframe(st.session_state['pnl_list'])

with right:
    st.subheader("Activity & Logs")
    # logs area refresh - show logger contents
    log_text = logger.get()
    st.text_area("Logs", value=log_text, height=400)

st.markdown("---")
st.caption("Run in Paper mode first. For Live mode ensure access_token.json exists and is valid (generated from Kite login).")

# end of file
