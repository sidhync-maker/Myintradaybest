# auto_zerodha_full.py
import os
import time
import datetime
import requests
import traceback
import streamlit as st
from kiteconnect import KiteConnect
from apscheduler.schedulers.background import BackgroundScheduler
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from urllib.parse import urlparse, parse_qs

# ----------------- CONFIG (use environment variables) -----------------
API_KEY = os.getenv("API_KEY", "t32mq5t5xgnjdtni")
API_SECRET = os.getenv("API_SECRET", "xf9jfyfvmqo408m5214u2gpyo34fcsfe")
REDIRECT_URL = os.getenv("REDIRECT_URL", "http://localhost:8501")
ACCESS_TOKEN_FILE = os.getenv("ACCESS_TOKEN_FILE", "access_token.txt")

# Zerodha login credentials (HIGHLY SENSITIVE) - store as env vars
ZERODHA_USERID = os.getenv("ZERODHA_USERID", "Aboobaker")
ZERODHA_PASSWORD = os.getenv("ZERODHA_PASSWORD", "123143@Ab")
ZERODHA_PIN = os.getenv("ZERODHA_PIN", "")  # optional; if not present and needed, automation may fail

# Fast2SMS (or replace with Twilio) credentials
FAST2SMS_API = os.getenv("FAST2SMS_API", "")   # required for SMS alerts
USER_PHONE = os.getenv("USER_PHONE", "918301844858")       # e.g. "919876543210"

# Chromedriver path (optional). If not set, chromedriver must be in PATH
CHROMEDRIVER_PATH = os.getenv("CHROMEDRIVER_PATH", None)

# Trading defaults
EXCHANGE = os.getenv("EXCHANGE", "NSE")
DEFAULT_EXPOSURE = float(os.getenv("DEFAULT_EXPOSURE", "50000"))
DEFAULT_SL_PCT = float(os.getenv("DEFAULT_SL_PCT", "3.0"))
DEFAULT_TRAIL_PCT = float(os.getenv("DEFAULT_TRAIL_PCT", "3.0"))

# Scheduler config
AUTO_RENEW_HOUR = int(os.getenv("AUTO_RENEW_HOUR", "9"))   # 9 AM
AUTO_RENEW_MINUTE = int(os.getenv("AUTO_RENEW_MINUTE", "0"))

# ----------------- INIT -----------------
kite = KiteConnect(api_key=API_KEY)

st.set_page_config(page_title="Auto Zerodha Trader (Full Automation)", layout="wide")
st.title("⚡ Auto Zerodha Trader — Fully Automated (Selenium + Scheduler + SMS)")

# ----------------- Helpers -----------------
def send_sms(message: str) -> bool:
    """Send SMS via Fast2SMS. Returns True if request returned HTTP 200."""
    if not FAST2SMS_API or not USER_PHONE:
        print("Fast2SMS API or USER_PHONE not configured; cannot send SMS.")
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
        resp = requests.post(url, headers=headers, data=payload, timeout=15)
        print(f"SMS sent status: {resp.status_code}, body: {resp.text}")
        return resp.status_code == 200
    except Exception as e:
        print("SMS send failed:", e)
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
    try:
        kite.set_access_token(token)
        profile = kite.profile()  # will raise if invalid
        print("Verified token for", profile.get("user_name"))
        return True
    except Exception as e:
        print("Token verification failed:", e)
        return False

# ----------------- Selenium login to capture request_token -----------------
def selenium_get_request_token(timeout=90) -> str:
    """
    Uses headless Chrome to log in to Kite and capture request_token from redirect URL.
    Returns request_token string or raises Exception.
    """
    login_url = kite.login_url()
    chrome_options = Options()
    # For newest Chrome headless mode use '--headless=new' if supported,
    # fallback to '--headless' if not
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-infobars")

    # If you need to debug, remove headless and add:
    # chrome_options.headless = False

    driver = None
    try:
        if CHROMEDRIVER_PATH:
            driver = webdriver.Chrome(executable_path=CHROMEDRIVER_PATH, options=chrome_options)
        else:
            driver = webdriver.Chrome(options=chrome_options)

        driver.set_page_load_timeout(60)
        driver.get(login_url)

        wait = WebDriverWait(driver, 30)

        # Enter userid
        try:
            userid_el = wait.until(EC.presence_of_element_located((By.ID, "userid")))
            userid_el.clear()
            userid_el.send_keys(ZERODHA_USERID)
        except Exception:
            # locator might change; check common alternatives quickly
            raise RuntimeError("Could not find userid input on login page. Page structure may have changed.")

        # Enter password
        try:
            pwd_el = driver.find_element(By.ID, "password")
            pwd_el.clear()
            pwd_el.send_keys(ZERODHA_PASSWORD)
        except Exception:
            raise RuntimeError("Could not find password input on login page.")

        # Click login
        try:
            submit_btn = driver.find_element(By.XPATH, "//button[@type='submit' or contains(text(),'Login') or contains(text(),'log in')]")
            submit_btn.click()
        except Exception:
            # Try generic button click
            raise RuntimeError("Could not find or click login button.")

        # PIN flow (if shown)
        time.sleep(1)
        try:
            pin_el = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.ID, "pin")))
            if ZERODHA_PIN:
                pin_el.clear()
                pin_el.send_keys(ZERODHA_PIN)
                btn2 = driver.find_element(By.XPATH, "//button[@type='submit' or contains(text(),'Continue') or contains(text(),'Login')]")
                btn2.click()
            else:
                raise RuntimeError("PIN required but ZERODHA_PIN not provided.")
        except Exception:
            # no PIN field; possibly redirected directly or OTP required
            pass

        # Wait for redirect to REDIRECT_URL which should contain request_token param
        end_time = time.time() + timeout
        request_token = None
        while time.time() < end_time:
            current_url = driver.current_url
            if "request_token=" in current_url:
                qs = parse_qs(urlparse(current_url).query)
                request_token = qs.get("request_token", [None])[0]
                break
            time.sleep(1)

        if not request_token:
            # helpful debugging info
            page_source = driver.page_source[:1000]
            raise RuntimeError("Failed to capture request_token from redirect. "
                               "Possible CAPTCHA / extra verification or changed page structure. "
                               f"Page snippet: {page_source}")

        return request_token

    finally:
        if driver:
            driver.quit()

# ----------------- Auto renewal job -----------------
def auto_renew_and_save():
    """Called by scheduler every weekday at configured hour/minute."""
    try:
        print(f"[{datetime.datetime.now()}] Auto-renew: Starting Selenium login to capture request_token...")
        request_token = selenium_get_request_token(timeout=120)
        if not request_token:
            msg = "Auto-renew failed: no request_token captured."
            print(msg)
            send_sms(msg)
            return False

        data = kite.generate_session(request_token, api_secret=API_SECRET)
        access_token = data.get("access_token")
        if not access_token:
            msg = "Auto-renew failed: no access_token returned by generate_session."
            print(msg)
            send_sms(msg)
            return False

        save_access_token(access_token)
        kite.set_access_token(access_token)
        msg = f"🔁 Access token auto-renewed at {datetime.datetime.now().isoformat()}."
        print(msg)
        send_sms(msg)
        return True

    except Exception as e:
        print("Auto-renew error:", e)
        traceback.print_exc()
        send_sms(f"⚠️ Auto-renew error: {e}")
        return False

# ----------------- Scheduler -----------------
scheduler = BackgroundScheduler()
# run Mon-Fri at AUTO_RENEW_HOUR:AUTO_RENEW_MINUTE
scheduler.add_job(auto_renew_and_save, 'cron',
                  day_of_week='mon-fri',
                  hour=AUTO_RENEW_HOUR,
                  minute=AUTO_RENEW_MINUTE,
                  id='zerodha_auto_renew')
scheduler.start()
print(f"Scheduler started: auto-renew scheduled at {AUTO_RENEW_HOUR}:{AUTO_RENEW_MINUTE} Mon-Fri")

# ----------------- Load token and provide UI -----------------
access_token = load_access_token()
token_valid = False
if access_token:
    try:
        token_valid = verify_token(access_token)
    except Exception:
        token_valid = False

if not token_valid:
    st.warning("Access token missing or invalid.")

st.markdown("### Token / Renewal Controls")
col1, col2, col3 = st.columns(3)

with col1:
    if st.button("Run Auto-Renew Now (Selenium)"):
        st.info("Running Selenium auto-renew — check logs. This will run headless Chrome on server.")
        ok = auto_renew_and_save()
        if ok:
            st.success("Auto-renew succeeded.")
            token_valid = True
        else:
            st.error("Auto-renew FAILED. Check server logs and page structure.")

with col2:
    login_url = kite.login_url()
    st.markdown(f"[Manual login flow — open and copy request_token]({login_url})")
    manual_rt = st.text_input("Paste request_token (manual fallback)", key="manual_rt")
    if st.button("Generate Access Token from pasted request_token"):
        try:
            data = kite.generate_session(manual_rt, api_secret=API_SECRET)
            access_token = data.get("access_token")
            if access_token:
                save_access_token(access_token)
                kite.set_access_token(access_token)
                send_sms("🔑 New Zerodha access token generated (manual).")
                st.success("Access token generated and saved.")
                token_valid = True
            else:
                st.error("generate_session did not return access_token.")
        except Exception as e:
            st.error(f"Manual generate failed: {e}")
            traceback.print_exc()

with col3:
    if token_valid:
        st.success("✅ Token is valid and loaded.")
    else:
        st.error("❌ No valid token loaded. Use auto-renew or manual flow above.")

# ----------------- Trading UI & Logic -----------------
st.markdown("## Trading Controls")
trading_symbol = st.text_input("Trading Symbol (exact, e.g. RELIANCE)", value="RELIANCE")
exposure = st.number_input("Exposure (₹)", value=DEFAULT_EXPOSURE, step=1000.0)
sl_pct = st.number_input("Stop Loss %", value=DEFAULT_SL_PCT, step=0.1)
trail_pct = st.number_input("Trailing SL %", value=DEFAULT_TRAIL_PCT, step=0.1)
start_trade = st.button("Start Auto Trade Now")

def place_market_sell(symbol, qty):
    return kite.place_order(
        tradingsymbol=symbol,
        exchange=EXCHANGE,
        transaction_type="SELL",
        quantity=qty,
        order_type="MARKET",
        product="MIS",
        variety="regular"
    )

if start_trade:
    if not token_valid:
        st.error("Cannot start trade: access token not valid. Renew first.")
    else:
        try:
            ltp_key = f"{EXCHANGE}:{trading_symbol}"
            ltp_resp = kite.ltp(ltp_key)
            ltp = ltp_resp[ltp_key]["last_price"]
            qty = max(1, int(exposure / ltp))
            st.write(f"Placing BUY: {trading_symbol} | Qty: {qty} | LTP: ₹{ltp}")

            order_id = kite.place_order(
                tradingsymbol=trading_symbol,
                exchange=EXCHANGE,
                transaction_type="BUY",
                quantity=qty,
                order_type="MARKET",
                product="MIS",
                variety="regular"
            )
            st.success(f"Buy order placed (ID: {order_id})")
            send_sms(f"✅ BUY {trading_symbol} @ ₹{ltp} (Qty: {qty})")

            buy_price = ltp
            trail_sl = round(buy_price * (1 - sl_pct / 100), 2)
            st.info(f"Initial Stop-loss set at: ₹{trail_sl}")

            # Monitor until 15:15
            while datetime.datetime.now().time() < datetime.time(15, 15):
                try:
                    ltp = kite.ltp(ltp_key)[ltp_key]["last_price"]
                    # Update trailing SL
                    if ltp > buy_price:
                        new_sl = round(ltp * (1 - trail_pct / 100), 2)
                        if new_sl > trail_sl:
                            trail_sl = new_sl
                            st.write(f"🔄 Trailing SL moved to ₹{trail_sl}")

                    # Hit SL: sell market
                    if ltp <= trail_sl:
                        place_market_sell(trading_symbol, qty)
                        st.error(f"🔻 Stop-loss hit at ₹{ltp}. Position exited.")
                        send_sms(f"🔻 STOP LOSS hit for {trading_symbol} at ₹{ltp}")
                        break

                    time.sleep(20)
                except Exception as inner_ex:
                    st.warning(f"Price fetch/monitor error: {inner_ex}")
                    time.sleep(5)

            # Square-off at or after 15:15 if still in position
            if datetime.datetime.now().time() >= datetime.time(15, 15):
                place_market_sell(trading_symbol, qty)
                st.warning(f"🕒 Auto square-off executed at {datetime.datetime.now().time()}")
                send_sms(f"🕒 AUTO SQUARE-OFF executed for {trading_symbol} at {datetime.datetime.now().time()}")

        except Exception as e:
            st.error(f"Trading error: {e}")
            traceback.print_exc()
            send_sms(f"⚠️ Trading error: {e}")

st.markdown("---")
st.write("Logs: check server stdout for scheduler and selenium details.")


