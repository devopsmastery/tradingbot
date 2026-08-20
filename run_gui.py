"""
Launcher script for the Fyers Trading Bot Web GUI Dashboard.
Runs on http://localhost:8000
"""

import os
import sys
import uvicorn

# Set project directory in path
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

def main():
    port = 8000
    host = "127.0.0.1"
    url = f"http://localhost:{port}"

    print("=" * 65)
    print("  STARTING FYERS TRADING BOT WEB GUI DASHBOARD")
    print("=" * 65)
    print(f"  URL        : {url}")
    print(f"  Host       : {host}:{port}")
    print("  Live Tasks : Dry Run, Full NSE Scan, Data Update, Backtest")
    print("  Reco Hub   : View recommendations and copy tickers in 1-click")
    print("  Auth Modal : 1-click login and auto-update .env with token")
    print("=" * 65)
    print("  Press Ctrl+C to stop the server.\n")

    uvicorn.run("gui.server:app", host=host, port=port, reload=False, log_level="info")

if __name__ == "__main__":
    main()
