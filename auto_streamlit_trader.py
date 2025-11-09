# auto_streamlit_except_token.py
import os
import time
import datetime
import threading
import traceback
import requests
import streamlit as st
from kiteconnect import KiteConnect
from apscheduler.schedulers.background import BackgroundScheduler

# -----------------------
# CONFIG - update as needed
# -----------------------
API_KEY = "t32mq5t5xgnjdtni"
API_SECRET = "xf9jfyfvmqo408m5214u2gpyo34fcsfe"
REDIRECT_URL = "http://localhost:8501"
ACCESS_TOKEN_FILE = "access_token.txt"

# Fast2SMS (optional) - set to "" to disable SMS
FAST2SMS_API = os.getenv("FAST2SMS_API", "")   # or paste your key
USER_PHONE = os.getenv("USER_PHONE", "")       # e.g. "919876543210"

# Trading defaults
EXCHANGE = "NSE"
DEFAULT_START_HOUR = 9
DEFAULT_START_MINUTE = 20

# Monitoring / sleep intervals
PRICE_POLL_INTERVAL = 20  # seconds for price polling while monitoring trade

# -----------------------
# INIT
# -----------------------
st.set_page_config(page_title="Auto Trader — Manual Token", layout="wide")
st.title("⚡ Auto Trader — Fully automated except access-token")

kite = KiteConnect(api_key=API_KEY)

# Shared state across scheduler & UI
state = {
    "access_token_loaded": False,
    "token_valid": False,
    "current_trade_running": False,
    "last_job_status": "",
}

# -----------------------
# Utilities
# -----------------------
def send_sms(message: str) -> bool:
    """Send SMS via Fast2SMS (optional). Returns True on 200."""
    if not FAST2SMS_API or not USER_PHONE:
        # SMS disabled
        print("SMS disabled or FAST2SMS_API/USER_PHONE not configured.")
        return False
    try:
        url = "https://www.fast2sms.com/dev/bulkV2"
        payload = {
            "sender_id": "FSTSMS",
            "message": message,
            "language": "english",
            "route": "v3",
            "numbers": USER_PHONE
        }
        headers = {'authorization': FAST2SMS_API}
        resp = requests.post(url, headers=headers, data=payload, timeout=10)
        print("SMS status:", resp.status_code, resp.text)
        return resp.status_code == 200
    except Exception as e:
        print("SMS send error:", e)
        return False

def save_access_token(token: str):
    with open(ACCESS_TOKEN_FILE, "w") as f:
        f.write(token)

def load_access_token() -> str:
    if os.path.exists(ACCESS_TOKEN_FILE):
        with open(ACCESS_TOKEN_FILE, "r") as f:
            return f.read().strip()
    return ""

def verify_token(token: str) -> bool:
    if not token:
        return False
    try:
        kite.set_access_token(token)
        kite.profile()  # will raise if invalid
        return True
    except Exception as e:
        print("Token verify failed:", e)
        return False

# -----------------------
# Token management & resume logic
# -----------------------
def try_load_token_on_start():
    token = load_access_token()
    if token and verify_token(token):
        state["access_token_loaded"] = True
        state["token_valid"] = True
        print("Access token loaded and valid.")
    else:
        state["access_token_loaded"] = False
        state["token_valid"] = False
        print("No valid access token found on start.")

try_load_token_on_start()

def generate_and_store_access_token(request_token: str) -> bool:
    """Exchange request_token for access_token and store; returns True on success."""
    try:
        data = kite.generate_session(request_token, api_secret=API_SECRET)
        access_token = data.get("access_token")
        if not access_token:
            print("generate_session did not return access_token:", data)
            return False
        save_access_token(access_token)
        kite.set_access_token(access_token)
        state["access_token_loaded"] = True
        state["token_valid"] = True
        print("Access token generated & saved.")
        send_sms("🔑 Zerodha access token generated successfully.")
        return True
    except Exception as e:
        print("Error generating access token:", e)
        traceback.print_exc()
        return False

# -----------------------
# Scheduler: 9:00 reminder + trading job
# -----------------------
scheduler = BackgroundScheduler()

def morning_reminder_job():
    token = load_access_token()
    if not verify_token(token):
        msg = "🔔 Reminder: Please log in to Zerodha and paste request_token in the app."
        print(msg)
        send_sms(msg)
        state["last_job_status"] = "Reminder sent: token missing/expired."
    else:
        state["last_job_status"] = "Token valid at 9:00 — no reminder."

scheduler.add_job(morning_reminder_job, 'cron', hour=9, minute=0, id="morning_reminder")

def scheduled_trading_job(trading_symbol, exchange, exposure, sl_pct, trail_pct):
    """
    Scheduled trade runner. If token invalid, it will wait (sleep & retry) until user provides new token.
    """
    print(f"[{datetime.datetime.now()}] Scheduled trading job started for {trading_symbol}")
    state["last_job_status"] = f"Scheduled job started for {trading_symbol} at {datetime.datetime.now()}"

    # Wait until token is valid (retry loop), but don't block forever — check every 30s.
    wait_start = datetime.datetime.now()
    while not state["token_valid"]:
        elapsed = (datetime.datetime.now() - wait_start).total_seconds()
        if elapsed > 60 * 30:  # after 30 minutes give up for this run
            state["last_job_status"] = "Aborted: token not provided within 30 minutes."
            print("Aborting scheduled job: token not provided within 30 minutes.")
            send_sms("⚠️ Trading aborted: access token not provided (scheduled job).")
            return
        print("Token invalid. Waiting for user to paste request_token...")
        time.sleep(30)

    # token valid — start the trade
    try:
        state["current_trade_running"] = True
        ltp_key = f"{exchange}:{trading_symbol}"
        ltp_data = kite.ltp(ltp_key)
        ltp = ltp_data[ltp_key]["last_price"]
        qty = max(1, int(exposure / ltp))
        print(f"Placing BUY {trading_symbol} qty={qty} ltp={ltp}")
        order_id = kite.place_order(
            tradingsymbol=trading_symbol,
            exchange=exchange,
            transaction_type="BUY",
            quantity=qty,
            order_type="MARKET",
            product="MIS",
            variety="regular"
        )
        print("Buy placed, order id:", order_id)
        send_sms(f"✅ BUY {trading_symbol} @ ₹{ltp} (Qty: {qty})")
        buy_price = ltp
        trail_sl = round(buy_price * (1 - sl_pct / 100), 2)

        # Monitor until square-off
        while datetime.datetime.now().time() < datetime.time(15, 15):
            try:
                ltp_data = kite.ltp(ltp_key)
                current_price = ltp_data[ltp_key]["last_price"]

                if current_price > buy_price:
                    new_sl = round(current_price * (1 - trail_pct / 100), 2)
                    if new_sl > trail_sl:
                        trail_sl = new_sl
                        print(f"Trailing SL moved to {trail_sl}")

                if current_price <= trail_sl:
                    # Exit market
                    kite.place_order(
                        tradingsymbol=trading_symbol,
                        exchange=exchange,
                        transaction_type="SELL",
                        quantity=qty,
                        order_type="MARKET",
                        product="MIS",
                        variety="regular"
                    )
                    send_sms(f"🔻 STOP LOSS hit for {trading_symbol} at ₹{current_price}")
                    print("Stop loss hit; sold at", current_price)
                    state["last_job_status"] = f"SL hit at {current_price}"
                    state["current_trade_running"] = False
                    return

                time.sleep(PRICE_POLL_INTERVAL)
            except Exception as e_inner:
                print("Monitoring error:", e_inner)
                time.sleep(5)

        # square-off at or after 15:15 if still in position
        try:
            kite.place_order(
                tradingsymbol=trading_symbol,
                exchange=exchange,
                transaction_type="SELL",
                quantity=qty,
                order_type="MARKET",
                product="MIS",
                variety="regular"
            )
            send_sms(f"🕒 AUTO SQUARE-OFF executed for {trading_symbol}")
            print("Auto square-off executed.")
            state["last_job_status"] = "Auto square-off executed."
        except Exception as sq_err:
            print("Error during square-off:", sq_err)
            send_sms(f"⚠️ Square-off error: {sq_err}")
            state["last_job_status"] = f"Square-off error: {sq_err}"

    except Exception as e:
        print("Scheduled trading job error:", e)
        traceback.print_exc()
        send_sms(f"⚠️ Trading job error: {e}")
        state["last_job_status"] = f"Error: {e}"
    finally:
        state["current_trade_running"] = False

# UI function to schedule job from inputs
def schedule_trade_job(symbol, exchange, exposure, sl_pct, trail_pct, hour, minute):
    job_id = f"trade_{symbol}_{hour}{minute}"
    # Remove existing same job if exists
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    scheduler.add_job(scheduled_trading_job, 'cron',
                      args=[symbol, exchange, exposure, sl_pct, trail_pct],
                      hour=hour, minute=minute, id=job_id)
    print(f"Scheduled trade job {job_id} at {hour}:{minute}")

# start scheduler
scheduler.start()

# -----------------------
# STREAMLIT UI
# -----------------------
st.sidebar.header("Token & Scheduler")
st.sidebar.write("This app is fully automated **except** you must paste `request_token` when asked (one per trading-day).")

# Token area
st.sidebar.subheader("Access token")
st.sidebar.write("If you already have generated token earlier this session it will be loaded automatically.")
current_token = load_access_token()
st.sidebar.write("Token loaded:", bool(current_token))
if current_token:
    st.sidebar.write(f"Saved token file: {ACCESS_TOKEN_FILE}")

req_token_input = st.sidebar.text_input("Paste today's `request_token` (if prompted)", key="req_token")
if st.sidebar.button("Generate & Save Access Token"):
    if req_token_input.strip():
        ok = generate_and_store_access_token(req_token_input.strip())
        if ok:
            st.sidebar.success("Access token saved and validated.")
        else:
            st.sidebar.error("Failed to generate access token. Check request_token and try again.")
    else:
        st.sidebar.error("Please paste a non-empty request_token.")

# Auto-schedule controls
st.sidebar.subheader("Auto-schedule settings")
start_hour = st.sidebar.number_input("Start Hour (24h)", value=DEFAULT_START_HOUR, min_value=0, max_value=23)
start_minute = st.sidebar.number_input("Start Minute", value=DEFAULT_START_MINUTE, min_value=0, max_value=59)
symbol_input = st.sidebar.text_input("Trading symbol", value="RELIANCE")
exchange_input = st.sidebar.selectbox("Exchange", ["NSE", "BSE"])
exposure_input = st.sidebar.number_input("Exposure (₹)", value=50000.0, step=1000.0)
sl_pct_input = st.sidebar.number_input("Stop loss %", value=3.0, step=0.1)
trail_pct_input = st.sidebar.number_input("Trailing SL %", value=3.0, step=0.1)

if st.sidebar.button("Schedule Daily Auto Trade"):
    schedule_trade_job(symbol_input.strip().upper(), exchange_input, exposure_input, sl_pct_input, trail_pct_input, int(start_hour), int(start_minute))
    st.sidebar.success(f"Scheduled daily auto-trade at {int(start_hour):02d}:{int(start_minute):02d}")

if st.sidebar.button("Run Trade Now (manual)"):
    # run scheduled_trading_job in a background thread so UI doesn't block
    t = threading.Thread(target=scheduled_trading_job, args=(symbol_input.strip().upper(), exchange_input, exposure_input, sl_pct_input, trail_pct_input))
    t.daemon = True
    t.start()
    st.sidebar.info("Manual trade started in background thread.")

# Main page
st.header("Status & Controls")
st.write("Token valid:", state["token_valid"])
st.write("Access token loaded:", state["access_token_loaded"])
st.write("Current trade running:", state["current_trade_running"])
st.write("Last job status:", state.get("last_job_status", ""))

st.markdown("---")
st.header("Quick Actions")
col1, col2 = st.columns(2)
with col1:
    if st.button("Check token now"):
        tok = load_access_token()
        valid = verify_token(tok)
        if valid:
            st.success("Token valid.")
            state["token_valid"] = True
            state["access_token_loaded"] = True
        else:
            st.error("Token invalid or missing.")
            state["token_valid"] = False
            state["access_token_loaded"] = False

with col2:
    if st.button("Show next scheduled jobs"):
        jobs = scheduler.get_jobs()
        if not jobs:
            st.info("No scheduled jobs.")
        else:
            for j in jobs:
                st.write(f"{j.id} -> next run: {j.next_run_time}")

st.markdown("---")
st.caption("Notes: You must paste a fresh request_token once per trading day when the token expires. The app will pause scheduled jobs until token is provided and resume automatically.")

# Keep Streamlit session alive and show logs
st.text("Server time: " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))



