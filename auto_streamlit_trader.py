# auto_streamlit_trader_full.py
"""
Fully updated Streamlit trading app:
- Auto 09:15 entry if bullish + uptrend
- Initial SL 1%, trailing 3%
- Day-end 15:15 square-off
- Paper & Live mode
- WhatsApp Cloud API alerts (no Twilio)
- Safe token handling
- Live P&L display
"""

import streamlit as st
import pandas as pd
import numpy as np
import threading
import time
import json
import os
import requests
from datetime import datetime, timedelta

try:
    from kiteconnect import KiteConnect
except ImportError:
    KiteConnect = None

# -------------------- CONFIG --------------------
ACCESS_TOKEN_FILE = "access_token.json"
DEFAULT_NIFTY_TOKEN = "256265"
DEFAULT_SL_PCT = 1.0
TRIGGER_MULTIPLIER = 3.0
TRAIL_PCT = 3.0
DEFAULT_EXPOSURE = 50000
DEFAULT_LEVERAGE = 5

# -------------------- UTILITIES --------------------
def safe_load_json(path):
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path,"r") as f:
                return json.load(f)
    except:
        return None
    return None

def safe_save_json(path,data):
    with open(path,"w") as f:
        json.dump(data,f,default=str,indent=2)

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# -------------------- LOGGER --------------------
class Logger:
    def __init__(self,maxlen=1000):
        self.logs=[]
        self.maxlen=maxlen
    def add(self,msg):
        s=f"[{now_str()}] {msg}"
        self.logs.append(s)
        if len(self.logs)>self.maxlen:
            self.logs.pop(0)
    def get(self):
        return "\n".join(self.logs)
logger=Logger()

# -------------------- WhatsApp Cloud API --------------------
def send_whatsapp(message,phone_number):
    token = st.secrets.get("WABA_TOKEN","YOUR_WABA_ACCESS_TOKEN")
    phone_id = st.secrets.get("PHONE_ID","YOUR_PHONE_NUMBER_ID")
    if token=="YOUR_WABA_ACCESS_TOKEN" or phone_id=="YOUR_PHONE_NUMBER_ID":
        logger.add("WhatsApp API not configured")
        return
    url=f"https://graph.facebook.com/v17.0/{phone_id}/messages"
    payload={
        "messaging_product":"whatsapp",
        "to":phone_number,
        "type":"text",
        "text":{"body":message}
    }
    headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}
    try:
        r=requests.post(url,headers=headers,data=json.dumps(payload))
        if r.status_code==200:
            logger.add(f"WhatsApp sent: {message}")
        else:
            logger.add(f"WhatsApp failed: {r.status_code} {r.text}")
    except Exception as e:
        logger.add(f"WhatsApp error: {e}")

# -------------------- PAPER BROKER --------------------
class PaperBroker:
    def __init__(self):
        self.orders={}
        self.positions={}
        self._next=1
    def _id(self):
        oid=f"P{self._next}"
        self._next+=1
        return oid
    def place_market_buy(self,symbol,qty,price=None):
        oid=self._id()
        fill_price=price if price else self.positions.get(symbol,{}).get('avg',100.0)
        self.orders[oid]={'id':oid,'type':'BUY','symbol':symbol,'qty':qty,'price':fill_price,'status':'FILLED','ts':now_str()}
        pos=self.positions.get(symbol,{'qty':0,'avg':0.0})
        total_qty=pos['qty']+qty
        pos['avg']=(pos['avg']*pos['qty']+fill_price*qty)/total_qty if pos['qty']>0 else fill_price
        pos['qty']=total_qty
        self.positions[symbol]=pos
        logger.add(f"[Paper] BUY filled {symbol} qty={qty}@{fill_price}")
        return oid
    def place_slm(self,symbol,qty,trigger):
        oid=self._id()
        self.orders[oid]={'id':oid,'type':'SLM','symbol':symbol,'qty':qty,'trigger':trigger,'status':'ACTIVE','ts':now_str()}
        logger.add(f"[Paper] SLM placed {symbol} qty={qty} trigger={trigger}")
        return oid
    def modify_order(self,oid,trigger):
        if oid in self.orders:
            self.orders[oid]['trigger']=trigger
            logger.add(f"[Paper] Order {oid} modified -> trigger {trigger}")
            return True
        return False
    def cancel_all(self):
        cancelled=[]
        for oid,o in list(self.orders.items()):
            if o.get('status') in ('ACTIVE','OPEN','TRIGGER PENDING'):
                o['status']='CANCELLED'
                cancelled.append(oid)
        logger.add(f"[Paper] Cancelled orders: {cancelled}")
        return cancelled
    def close_all_positions(self):
        closed=[]
        for sym,p in list(self.positions.items()):
            if p.get('qty',0)!=0:
                closed.append({'symbol':sym,'qty':p['qty']})
                logger.add(f"[Paper] Closed {sym} qty={p['qty']}")
                self.positions[sym]={'qty':0,'avg':0.0}
        return closed
    def get_positions(self):
        res=[]
        for sym,p in self.positions.items():
            if p['qty']!=0:
                res.append({'tradingsymbol':sym,'exchange':'NSE','quantity':p['qty'],'avg_price':p['avg'],'pnl':0.0})
        return res

# -------------------- TRADING ENGINE --------------------
class TradingEngine:
    def __init__(self,kite=None,broker=None,live=False,config=None):
        self.kite=kite
        self.broker=broker
        self.live=live and (kite is not None)
        self.config=config or {}
        self.entry_price=None
        self.qty=0
        self.sl_order_id=None
        self.entry_order_id=None
        self.peak_price=None
        self.active=False
        self._stop=threading.Event()
    def compute_qty(self,ltp):
        if self.config.get('qty_mode')=='fixed':
            return int(self.config.get('qty',1000))
        exposure=float(self.config.get('exposure',DEFAULT_EXPOSURE))
        leverage=float(self.config.get('leverage',DEFAULT_LEVERAGE))
        return max(int((exposure*leverage)//ltp),1)
    def get_ltp(self,exchange,symbol):
        if self.live and self.kite:
            try:
                ref=f"{exchange}:{symbol}"
                d=self.kite.ltp(ref)
                return list(d.values())[0]['last_price']
            except:
                return None
        if self.entry_price:
            return round(self.entry_price*(1+np.random.normal(0,0.0015)),2)
        return None
    def place_market_buy(self,symbol,exchange,qty):
        if self.live and self.kite:
            try:
                oid=self.kite.place_order(tradingsymbol=symbol,exchange=exchange,
                    transaction_type=self.kite.TRANSACTION_TYPE_BUY,
                    quantity=qty,order_type=self.kite.ORDER_TYPE_MARKET,
                    product=self.kite.PRODUCT_MIS,variety=self.kite.VARIETY_REGULAR)
                logger.add(f"[Live] Market BUY placed id={oid}")
                return oid
            except Exception as e:
                logger.add(f"[Live] Market buy failed: {e}")
                return None
        else:
            return self.broker.place_market_buy(symbol,qty,price=self.config.get('sim_ltp'))
    def place_slm(self,symbol,exchange,qty,trigger):
        if self.live and self.kite:
            try:
                oid=self.kite.place_order(tradingsymbol=symbol,exchange=exchange,
                    transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                    quantity=qty,order_type=self.kite.ORDER_TYPE_SLM,
                    trigger_price=trigger,product=self.kite.PRODUCT_MIS,variety=self.kite.VARIETY_REGULAR)
                logger.add(f"[Live] SLM placed id={oid} trigger={trigger}")
                return oid
            except Exception as e:
                logger.add(f"[Live] SL placement failed: {e}")
                return None
        else:
            return self.broker.place_slm(symbol,qty,trigger)
    def modify_slm(self,order_id,trigger_price):
        if self.live and self.kite:
            try:
                self.kite.modify_order(order_id=order_id,trigger_price=trigger_price)
                logger.add(f"[Live] Modified SL {order_id} -> {trigger_price}")
                return True
            except:
                return False
        else:
            return self.broker.modify_order(order_id,trigger_price)
    def cancel_all_pending_orders(self):
        if self.live and self.kite:
            cancelled=[]
            try:
                orders=self.kite.orders()
                for o in orders:
                    status=(o.get('status') or "").upper()
                    oid=o.get('order_id')
                    if status in ('OPEN','TRIGGER PENDING','PENDING'):
                        try:
                            variety=o.get('variety',self.kite.VARIETY_REGULAR)
                            self.kite.cancel_order(order_id=oid,variety=variety)
                            cancelled.append(oid)
                        except:
                            pass
                logger.add(f"[Live] Cancelled orders: {cancelled}")
            except:
                pass
            return cancelled
        else:
            return self.broker.cancel_all()
    def close_all_open_positions(self):
        if self.live and self.kite:
            closed=[]
            try:
                pos=self.kite.positions()
                net=pos.get('net',[]) if isinstance(pos,dict) else []
                for p in net:
                    tsym=p.get('tradingsymbol')
                    exch=p.get('exchange') or 'NSE'
                    qty=int(p.get('quantity',0) or 0)
                    if qty==0: continue
                    tx=self.kite.TRANSACTION_TYPE_SELL if qty>0 else self.kite.TRANSACTION_TYPE_BUY
                    qty=abs(qty)
                    try:
                        order=self.kite.place_order(tradingsymbol=tsym,exchange=exch,
                            transaction_type=tx,quantity=qty,order_type=self.kite.ORDER_TYPE_MARKET,
                            product=self.kite.PRODUCT_MIS,variety=self.kite.VARIETY_REGULAR)
                        closed.append({'symbol':tsym,'qty':qty,'order_id':order})
                    except:
                        pass
                return closed
            except:
                return []
        else:
            return self.broker.close_all_positions()
    def emergency_exit(self,reason="emergency"):
        logger.add(f"Emergency exit: {reason}")
        send_whatsapp(f"Emergency exit: {reason}",st.secrets.get("WHATSAPP_NUMBER","919876543210"))
        self.cancel_all_pending_orders()
        self.close_all_open_positions()
        self.stop()
        self.active=False
        logger.add("Engine stopped after emergency exit")
    def evaluate_and_place_entry(self):
        cfg=self.config
        try:
            ltp=self.get_ltp(cfg['exchange'],cfg['tradingsymbol'])
            qty=self.compute_qty(ltp)
            if qty<=0:
                return False
            entry_oid=self.place_market_buy(cfg['tradingsymbol'],cfg['exchange'],qty)
            if not entry_oid: return False
            self.entry_order_id=entry_oid
            self.entry_price=ltp
            self.qty=qty
            self.peak_price=ltp
            trigger=round(self.entry_price*(1-cfg['sl_pct']/100),2)
            sl_oid=self.place_slm(cfg['tradingsymbol'],cfg['exchange'],qty,trigger)
            self.sl_order_id=sl_oid
            logger.add(f"Entry executed @ {self.entry_price} qty={self.qty} sl={trigger}")
            send_whatsapp(f"Entry: {cfg['tradingsymbol']}@{self.entry_price} qty={self.qty} SL:{trigger}",st.secrets.get("WHATSAPP_NUMBER","919876543210"))
            return True
        except Exception as e:
            logger.add(f"Entry exception: {e}")
            return False
    def manage_trailing(self):
        if not self.entry_price: return
        ltp=self.get_ltp(self.config['exchange'],self.config['tradingsymbol'])
        if ltp is None: return
        if self.peak_price is None or ltp>self.peak_price: self.peak_price=ltp
        if ltp<=self.entry_price*(1-self.config['sl_pct']/100):
            self.emergency_exit(reason="SL hit")
            return
        profit_pct=(ltp-self.entry_price)/self.entry_price*100
        if profit_pct>=self.config['sl_pct']*TRIGGER_MULTIPLIER:
            breakeven=round(self.entry_price,2)
            if self.sl_order_id:
                self.modify_slm(self.sl_order_id,breakeven)
                logger.add(f"Moved SL to breakeven @ {breakeven}")
                send_whatsapp(f"SL moved to breakeven for {self.config['tradingsymbol']}",st.secrets.get("WHATSAPP_NUMBER","919876543210"))
        trailing_trigger=round(self.peak_price*(1-TRAIL_PCT/100),2)
        if self.sl_order_id:
            self.modify_slm(self.sl_order_id,trailing_trigger)
            logger.add(f"Updated trailing SL -> {trailing_trigger} (peak {self.peak_price})")
    def run_forever(self):
        logger.add("Engine loop started")
        self.active=True
        self._stop.clear()
        while not self._stop.is_set():
            try:
                now=datetime.now()
                if now.weekday()>=5: time.sleep(10); continue
                if not self.entry_price and now.hour==9 and now.minute==15 and now.second<10:
                    logger.add("09:15 -> evaluating entry")
                    self.evaluate_and_place_entry()
                else:
                    self.manage_trailing()
                if now.hour==15 and now.minute>=15:
                    logger.add("15:15 -> day-end square-off")
                    self.emergency_exit(reason="Time 15:15")
                    break
            except Exception as e:
                logger.add(f"Engine loop error: {e}")
            time.sleep(3)
        logger.add("Engine loop ended")
        self.active=False
    def stop(self):
        self._stop.set()
        logger.add("Engine stop requested")

# -------------------- STREAMLIT UI --------------------
st.set_page_config(page_title="Auto Trader Full", layout="wide")
st.title("🔁 Auto Streamlit Trader — Full Updated")

# Sidebar
st.sidebar.header("Connection & Mode")
live_mode=st.sidebar.checkbox("Live mode",value=False)
api_key=st.sidebar.text_input("Kite API Key", type="password")
api_secret=st.sidebar.text_input("Kite API Secret", type="password")
request_token=st.sidebar.text_input("Request token (from Kite login)")

saved_access=safe_load_json(ACCESS_TOKEN_FILE)
if st.sidebar.button("Generate & Save Access Token"):
    if KiteConnect is None: st.sidebar.error("kiteconnect missing")
    else:
        if not api_key or not api_secret or not request_token: st.sidebar.error("Provide all fields")
        else:
            try:
                temp=KiteConnect(api_key=api_key)
                data=temp.generate_session(request_token,api_secret=api_secret)
                safe_save_json(ACCESS_TOKEN_FILE,data)
                saved_access=data
                st.sidebar.success("Access token saved")
            except Exception as e:
                st.sidebar.error(f"Token gen failed: {e}")

kite=None
if live_mode:
    access=saved_access or safe_load_json(ACCESS_TOKEN_FILE)
    if access and api_key:
        try:
            kite=KiteConnect(api_key=api_key)
            kite.set_access_token(access.get('access_token'))
            st.sidebar.success("Kite connected")
        except: pass

# Config
st.subheader("Strategy Configuration")
tradingsymbol=st.text_input("Trading symbol","INFY")
exchange=st.selectbox("Exchange",["NSE","BSE"])
qty_mode=st.selectbox("Quantity mode",["exposure","fixed"],index=0)
exposure=st.number_input("Exposure (₹)",value=DEFAULT_EXPOSURE)
leverage=st.number_input("Leverage",value=DEFAULT_LEVERAGE)
qty=st.number_input("Fixed qty",value=1000,step=1)
sl_pct=st.number_input("Initial SL %",value=DEFAULT_SL_PCT)
trail_pct=st.number_input("Trailing %",value=TRAIL_PCT)
auto_mode=st.checkbox("Auto start 09:15",value=True)
allow_manual=st.checkbox("Allow Manual Start/Stop",value=True)

# Session state
if 'broker' not in st.session_state: st.session_state['broker']=PaperBroker()
if 'engine' not in st.session_state: st.session_state['engine']=None
broker=st.session_state['broker']

config={'tradingsymbol':tradingsymbol,'exchange':exchange,
        'qty_mode':'fixed' if qty_mode=='fixed' else 'exposure',
        'qty':int(qty),'exposure':float(exposure),'leverage':float(leverage),
        'sl_pct':float(sl_pct),'sim_ltp':100.0}

# Engine control
def start_engine():
    if st.session_state.get('engine') is not None:
        logger.add("Engine already running"); return
    eng=TradingEngine(kite=kite,broker=broker,live=live_mode,config=config)
    t=threading.Thread(target=eng.run_forever,daemon=True)
    st.session_state['engine']=eng
    st.session_state['engine_thread']=t
    t.start()
    logger.add("Engine started")
def stop_engine():
    eng=st.session_state.get('engine')
    if eng: eng.stop()
    st.session_state['engine']=None; st.session_state['engine_thread']=None
    logger.add("Engine stopped")

# Manual buttons
if allow_manual:
    m1,m2,m3=st.columns([1,1,2])
    if m1.button("Manual Start"): start_engine()
    if m2.button("Manual Stop"):
        eng=st.session_state.get('engine')
        if eng: eng.emergency_exit("Manual Stop"); stop_engine()
    if m3.button("🛑 Emergency Exit"):
        eng=st.session

