Auto Streamlit Trader (Zerodha Kite)

Contents:
- auto_streamlit_trader.py    : Main Streamlit app (Paper mode default)
- config.json                : Default config values
- requirements.txt           : Python dependencies
- access_token.json          : (created by app after you generate session)
- instruments.csv            : optional instrument list for lookup

Quick start:
1) Create a Python environment and install deps:
   pip install -r requirements.txt

2) Run the app locally:
   streamlit run auto_streamlit_trader.py

3) In the sidebar:
   - Toggle Live mode to enable real orders (requires kiteconnect and valid access_token.json).
   - If using Live, get API Key & Secret from developers.kite.trade and use 'Show Kite Login URL' to generate a request token.
   - Paste request token and click 'Generate & Save Access Token' to create access_token.json.

Paper mode:
- Default mode is Paper (simulation). Use Manual Start to test.
- Auto mode will auto-start engine a few minutes before 09:15 on weekdays.

Important safety notes:
- Test with small sizes or paper mode before using live funds.
- Keep API keys secure. Do not share access_token.json publicly.