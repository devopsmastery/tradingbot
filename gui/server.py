"""
Fyers Trading Bot - Web GUI Backend Server
Runs on localhost:8000 using FastAPI and Uvicorn.
"""

import os
import sys
import json
import glob
import time
import queue
import re
import asyncio
import subprocess
import threading
from datetime import datetime
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse, parse_qs

from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Project directory
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_DIR, ".env"))

from live_trading.fyers_auth import (
    generate_login_url, generate_access_token, get_access_token,
    FYERS_APP_ID, FYERS_REDIRECT_URI, TOKEN_FILE
)
from data.data_fetcher import (
    to_fyers_symbol, load_historical_csv, read_stocks, fetch_historical_data,
    save_historical_data
)
from live_trading.execute_trades import (
    compute_indicators, generate_signal, quality_label
)

app = FastAPI(title="Fyers Trading Bot GUI", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global task execution state
class TaskManager:
    def __init__(self):
        self.current_process: Optional[subprocess.Popen] = None
        self.current_task_name: Optional[str] = None
        self.log_queue: queue.Queue = queue.Queue(maxsize=10000)
        self.is_running: bool = False
        self.start_time: Optional[float] = None
        self.history_logs: List[str] = []

    def start_task(self, task_name: str, cmd: List[str]) -> bool:
        if self.is_running:
            return False
        
        self.current_task_name = task_name
        self.is_running = True
        self.start_time = time.time()
        self.history_logs = []
        
        # Clear log queue
        while not self.log_queue.empty():
            try:
                self.log_queue.get_nowait()
            except queue.Empty:
                break

        def run_thread():
            try:
                self.log(f"⚡ [START] Running {task_name}...")
                self.log(f"   Command: {' '.join(cmd)}")
                self.log("=" * 65)

                self.current_process = subprocess.Popen(
                    cmd,
                    cwd=PROJECT_DIR,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                    env=dict(os.environ, PYTHONUNBUFFERED="1")
                )

                for line in iter(self.current_process.stdout.readline, ''):
                    clean_line = line.rstrip()
                    if clean_line:
                        self.log(clean_line)

                self.current_process.stdout.close()
                return_code = self.current_process.wait()
                
                elapsed = round(time.time() - self.start_time, 1)
                self.log("=" * 65)
                if return_code == 0:
                    self.log(f"✅ [SUCCESS] {task_name} finished successfully in {elapsed}s.")
                else:
                    self.log(f"❌ [EXIT] {task_name} exited with code {return_code} after {elapsed}s.")
            except Exception as e:
                self.log(f"❌ [ERROR] Task exception: {e}")
            finally:
                self.is_running = False
                self.current_process = None

        thread = threading.Thread(target=run_thread, daemon=True)
        thread.start()
        return True

    def stop_task(self) -> bool:
        if self.current_process and self.is_running:
            try:
                self.current_process.terminate()
                self.log("⚠️ [TERMINATED] Process cancelled by user.")
                self.is_running = False
                return True
            except Exception as e:
                self.log(f"Error terminating process: {e}")
        return False

    def log(self, message: str):
        self.history_logs.append(message)
        if len(self.history_logs) > 3000:
            self.history_logs.pop(0)
        try:
            self.log_queue.put_nowait(message)
        except queue.Full:
            pass

task_manager = TaskManager()


# -------------------------------------------------------------
# API Endpoints
# -------------------------------------------------------------

@app.get("/api/status")
def get_status():
    """Returns general bot health, token status, stock universe counts."""
    token_valid = False
    token_message = "No token file found"
    
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r") as f:
                tok = f.read().strip()
            if tok:
                token_valid = True
                token_message = "Token cached on disk"
        except Exception as e:
            token_message = str(e)

    stocks_to_test = len(read_stocks(os.path.join(PROJECT_DIR, "stocks_to_test.txt")))
    stocks_watchlist = len(read_stocks(os.path.join(PROJECT_DIR, "stocks_watchlist.txt")))
    
    portfolio_count = 0
    try:
        from scripts.portfolio_db import load_db, sync_portfolio
        db = sync_portfolio()
        portfolio_count = len(db)
    except Exception:
        portfolio_count = 0

    return {
        "status": "online",
        "date": datetime.now().strftime("%A, %d %B %Y"),
        "time": datetime.now().strftime("%H:%M:%S"),
        "token_valid": token_valid,
        "token_message": token_message,
        "app_id": FYERS_APP_ID or "Not Configured",
        "is_task_running": task_manager.is_running,
        "active_task": task_manager.current_task_name,
        "counts": {
            "test_stocks": stocks_to_test,
            "watchlist": stocks_watchlist,
            "portfolio": portfolio_count,
            "results_files": len(glob.glob(os.path.join(PROJECT_DIR, "Results", "*.txt")))
        }
    }


@app.get("/api/auth/url")
def get_auth_url():
    """Returns the Fyers login URL."""
    url = generate_login_url()
    return {"login_url": url}


class AuthVerifyRequest(BaseModel):
    auth_input: str

@app.post("/api/auth/verify")
def verify_auth_code(req: AuthVerifyRequest):
    """
    Accepts an auth code or full redirect URL, extracts the code,
    exchanges it with Fyers for an access token, and updates .env and .fyers_token.
    """
    raw_input = req.auth_input.strip()
    if not raw_input:
        raise HTTPException(status_code=400, detail="Auth code or URL cannot be empty")

    auth_code = raw_input
    if "auth_code=" in raw_input or raw_input.startswith("http"):
        try:
            parsed = urlparse(raw_input)
            params = parse_qs(parsed.query)
            if "auth_code" in params:
                auth_code = params["auth_code"][0]
            else:
                frag_params = parse_qs(parsed.fragment)
                if "auth_code" in frag_params:
                    auth_code = frag_params["auth_code"][0]
        except Exception:
            pass

    try:
        access_token = generate_access_token(auth_code)
        
        env_file = os.path.join(PROJECT_DIR, ".env")
        if os.path.exists(env_file):
            with open(env_file, "r") as f:
                env_content = f.read()
            if "FYERS_AUTH_CODE=" in env_content:
                new_env = re.sub(r"FYERS_AUTH_CODE=.*", f"FYERS_AUTH_CODE={auth_code}", env_content)
            else:
                new_env = env_content.strip() + f"\nFYERS_AUTH_CODE={auth_code}\n"
            with open(env_file, "w") as f:
                f.write(new_env)

        return {
            "success": True,
            "message": "Fyers Access Token generated and saved successfully!",
            "token_preview": f"{access_token[:10]}...{access_token[-5:]}" if access_token else ""
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Authentication failed: {e}")


class RunTaskRequest(BaseModel):
    action: str
    symbol: Optional[str] = None
    ltp: Optional[float] = None

@app.post("/api/tasks/run")
def run_task(req: RunTaskRequest):
    """Triggers one of the menu options as an asynchronous task with live output."""
    action = req.action.lower().strip()

    action_map = {
        "dry_run": ("Dry Run Watchlist Scanner", [sys.executable, "-u", os.path.join(PROJECT_DIR, "scripts", "dry_run.py")]),
        "update_data": ("Update Historical Data", [sys.executable, "-u", os.path.join(PROJECT_DIR, "scripts", "update_historical_data.py")]),
        "portfolio_scan": ("Portfolio Holdings Scan", [sys.executable, "-u", os.path.join(PROJECT_DIR, "scripts", "portfolio_analysis.py")]),
        "backtest": ("Run 6-Strategy Backtest", [sys.executable, "-u", os.path.join(PROJECT_DIR, "backtest", "run_backtest.py")]),
        "full_nse_scan": ("Full NSE Small+Mid Cap Scan", [sys.executable, "-u", os.path.join(PROJECT_DIR, "scripts", "full_exchange_scan.py")]),
        "git_push": ("Push to GitHub", [sys.executable, "-u", os.path.join(PROJECT_DIR, "push_to_git.py")]),
        "add_stocks": ("Add New Stocks", [sys.executable, "-u", os.path.join(PROJECT_DIR, "scripts", "add_new_stocks.py")]),
    }

    if action == "deep_analysis":
        if not req.symbol:
            raise HTTPException(status_code=400, detail="Symbol is required for deep analysis")
        cmd = [sys.executable, "-u", os.path.join(PROJECT_DIR, "scripts", "deep_analysis.py"), req.symbol.upper().strip()]
        if req.ltp is not None:
            cmd.extend(["--ltp", str(req.ltp)])
        task_title = f"Deep Analysis: {req.symbol.upper().strip()}"
        started = task_manager.start_task(task_title, cmd)
    elif action in action_map:
        task_title, cmd = action_map[action]
        started = task_manager.start_task(task_title, cmd)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    if not started:
        raise HTTPException(status_code=409, detail="Another task is already running. Please wait or stop it.")

    return {"success": True, "task": task_title}


@app.post("/api/tasks/stop")
def stop_task():
    """Stops the currently running task."""
    stopped = task_manager.stop_task()
    return {"success": stopped}


@app.get("/api/tasks/logs")
def get_logs():
    """Returns all buffered logs from the current/last run."""
    return {
        "is_running": task_manager.is_running,
        "active_task": task_manager.current_task_name,
        "logs": task_manager.history_logs
    }


@app.get("/api/tasks/stream")
async def stream_logs():
    """Server-Sent Events (SSE) streaming real-time terminal output to UI."""
    async def event_generator():
        for line in task_manager.history_logs[-50:]:
            yield f"data: {json.dumps({'line': line, 'running': task_manager.is_running})}\n\n"
        
        while True:
            try:
                line = task_manager.log_queue.get(timeout=0.5)
                yield f"data: {json.dumps({'line': line, 'running': task_manager.is_running})}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'heartbeat': True, 'running': task_manager.is_running})}\n\n"
                await asyncio.sleep(0.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# -------------------------------------------------------------
# Recommendations (Reco) & Results Endpoints
# -------------------------------------------------------------

def parse_results_txt(filepath: str) -> dict:
    """Parses a Results/DD-MM-HH-*.txt file into structured JSON."""
    if not os.path.exists(filepath):
        return {}

    filename = os.path.basename(filepath)
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    lines = [l.strip() for l in content.split("\n") if l.strip()]

    current_section = None
    add_more = []
    new_buy = []
    discoveries = []

    for line in lines:
        if line.startswith("ADD MORE:"):
            current_section = "ADD_MORE"
            continue
        elif line.startswith("NEW BUY:"):
            current_section = "NEW_BUY"
            continue
        elif line.startswith("DISCOVERY BUY SIGNALS:"):
            current_section = "DISCOVERY"
            continue

        match = re.match(r"^\d+\.\s+([A-Z0-9_\-\&]+)\s+[—\-]\s+(\d+)%\s+([A-Z]+)", line)
        if match:
            sym, score, label = match.groups()
            item = {
                "symbol": sym,
                "fyers_symbol": f"NSE:{sym}",
                "score": int(score),
                "label": label
            }
            if current_section == "ADD_MORE":
                add_more.append(item)
            elif current_section == "NEW_BUY":
                new_buy.append(item)
            elif current_section == "DISCOVERY":
                discoveries.append(item)

    all_symbols = [x["symbol"] for x in add_more + new_buy + discoveries]
    excellent_symbols = [x["symbol"] for x in add_more + new_buy + discoveries if x["score"] >= 80]

    all_tickers_str = ",\n".join(f"NSE:{s}" for s in all_symbols)
    excellent_tickers_str = ",\n".join(f"NSE:{s}" for s in excellent_symbols)

    return {
        "filename": filename,
        "filepath": filepath,
        "timestamp": os.path.getmtime(filepath),
        "date_formatted": datetime.fromtimestamp(os.path.getmtime(filepath)).strftime("%A, %d %B %Y %H:%M"),
        "add_more": add_more,
        "new_buy": new_buy,
        "discoveries": discoveries,
        "counts": {
            "total_recos": len(add_more) + len(new_buy) + len(discoveries),
            "add_more": len(add_more),
            "new_buy": len(new_buy),
            "discoveries": len(discoveries),
            "excellent": len(excellent_symbols)
        },
        "all_tickers_formatted": all_tickers_str,
        "excellent_tickers_formatted": excellent_tickers_str,
        "raw_text": content
    }


@app.get("/api/recos/latest")
def get_latest_recos():
    """Returns the latest recommendation results parsed from Results folder."""
    results_dir = os.path.join(PROJECT_DIR, "Results")
    files = glob.glob(os.path.join(results_dir, "*.txt"))
    if not files:
        return {"has_results": False, "message": "No scan results found in Results folder. Please run a Dry Run or Full NSE Scan."}

    files.sort(key=os.path.getmtime, reverse=True)
    latest_file = files[0]
    data = parse_results_txt(latest_file)
    data["has_results"] = True
    return data


@app.get("/api/recos/history")
def get_recos_history():
    """Lists all historical results files in Results directory."""
    results_dir = os.path.join(PROJECT_DIR, "Results")
    files = glob.glob(os.path.join(results_dir, "*.txt"))
    files.sort(key=os.path.getmtime, reverse=True)

    history = []
    for f in files:
        info = parse_results_txt(f)
        if info:
            history.append({
                "filename": info["filename"],
                "timestamp": info["timestamp"],
                "date_formatted": info["date_formatted"],
                "counts": info["counts"]
            })
    return {"files": history}


@app.get("/api/recos/file")
def get_recos_by_file(filename: str = Query(...)):
    """Retrieves and parses a specific Results file."""
    filepath = os.path.join(PROJECT_DIR, "Results", os.path.basename(filename))
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    data = parse_results_txt(filepath)
    data["has_results"] = True
    return data


# -------------------------------------------------------------
# Deep Analysis Endpoint
# -------------------------------------------------------------

def compute_rsi(data, period=14):
    delta = data.diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    avg_gain = gain.rolling(window=period, min_periods=1).mean()
    avg_loss = loss.rolling(window=period, min_periods=1).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

@app.get("/api/deep-analysis")
def get_deep_analysis(symbol: str = Query(...), ltp: Optional[float] = Query(None)):
    """Computes full multi-indicator technical analysis for any ticker symbol."""
    import pandas as pd
    sym = symbol.upper().strip()
    fyers_sym = to_fyers_symbol(sym)

    try:
        df = load_historical_csv(fyers_sym)
    except FileNotFoundError:
        try:
            tok = get_access_token()
            df = fetch_historical_data(fyers_sym, tok, days=90)
            save_historical_data(fyers_sym, df)
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"No cached data for {sym}. Fyers API fetch failed: {e}")

    if df.empty:
        raise HTTPException(status_code=400, detail=f"Data for {sym} is empty.")

    if ltp is not None and ltp > 0:
        last_row = df.iloc[-1].copy()
        last_row['Close'] = ltp
        last_row['High'] = max(last_row['High'], ltp)
        last_row['Low'] = min(last_row['Low'], ltp)
        today = datetime.now()
        if df.index[-1].date() != today.date():
            last_row.name = today
            df = pd.concat([df, pd.DataFrame([last_row])])
        else:
            df.iloc[-1] = last_row

    df = compute_indicators(df)
    df['RSI_14'] = compute_rsi(df['Close'], 14)
    
    # MACD
    ema_12 = df['Close'].ewm(span=12, adjust=False).mean()
    ema_26 = df['Close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = ema_12 - ema_26
    df['MACD_Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
    df['MACD_Hist'] = df['MACD'] - df['MACD_Signal']

    latest = df.iloc[-1]
    vol_ratio = latest['Volume'] / latest['VOL_SMA'] if latest['VOL_SMA'] else 0

    score = 0
    if latest['EMA_FAST'] > latest['EMA_SLOW']: score += 1
    if latest['Close'] > latest['KC_UPPER']: score += 1
    if vol_ratio > 1.2: score += 1
    if latest['MACD_Hist'] > 0: score += 1
    if 40 <= latest['RSI_14'] <= 70: score += 1

    if score >= 4:
        conclusion = "Very Strong Candidate"
        status_tag = "EXCELLENT"
    elif score >= 2:
        conclusion = "Moderate / Mixed Signals"
        status_tag = "MODERATE"
    else:
        conclusion = "Weak / Avoid"
        status_tag = "WEAK"

    return {
        "symbol": sym,
        "fyers_symbol": fyers_sym,
        "date": str(latest.name.date()) if hasattr(latest.name, 'date') else str(latest.name),
        "close": round(float(latest['Close']), 2),
        "volume": int(latest['Volume']),
        "avg_volume": int(latest['VOL_SMA']) if not pd.isna(latest['VOL_SMA']) else 0,
        "vol_ratio": round(float(vol_ratio), 2),
        "ema_10": round(float(latest['EMA_FAST']), 2),
        "ema_21": round(float(latest['EMA_SLOW']), 2),
        "is_bullish_trend": bool(latest['EMA_FAST'] > latest['EMA_SLOW']),
        "rsi_14": round(float(latest['RSI_14']), 2),
        "macd": round(float(latest['MACD']), 2),
        "macd_signal": round(float(latest['MACD_Signal']), 2),
        "macd_hist": round(float(latest['MACD_Hist']), 2),
        "kc_upper": round(float(latest['KC_UPPER']), 2),
        "kc_mid": round(float(latest['KC_MID']), 2),
        "kc_lower": round(float(latest['KC_LOWER']), 2),
        "is_kc_breakout": bool(latest['Close'] > latest['KC_UPPER']),
        "is_kc_breakdown": bool(latest['Close'] < latest['KC_LOWER']),
        "strength_score": score,
        "max_score": 5,
        "conclusion": conclusion,
        "status_tag": status_tag
    }


# -------------------------------------------------------------
# Portfolio Holdings & Backtest APIs
# -------------------------------------------------------------

@app.get("/api/portfolio")
def get_portfolio():
    """Returns parsed portfolio holdings with real-time risk status."""
    try:
        from scripts.portfolio_db import sync_portfolio
        db = sync_portfolio()
        if not db:
            return {"holdings": [], "summary": {}}

        results = []
        total_inv = 0.0
        total_cur = 0.0

        for sym, data in db.items():
            qty = data.get('qty', 0)
            avg_c = data.get('avg_cost', 0.0)
            inv = qty * avg_c
            total_inv += inv

            fyers_sym = to_fyers_symbol(sym)
            close = avg_c
            status = "NO DATA"

            try:
                df = load_historical_csv(fyers_sym)
                if not df.empty:
                    df = compute_indicators(df)
                    latest = df.iloc[-1]
                    close = float(latest['Close'])

                    if close < latest['KC_MID'] or latest['EMA_FAST'] < latest['EMA_SLOW']:
                        status = "CAUTION"
                    if close < latest['KC_LOWER']:
                        status = "SELL"
                    if close >= latest['KC_MID'] and latest['EMA_FAST'] >= latest['EMA_SLOW']:
                        status = "STRONG HOLD"
            except Exception:
                pass

            cur = (close * qty) if close else inv
            total_cur += cur
            pnl_pct = ((close - avg_c) / avg_c * 100) if avg_c > 0 else 0.0

            results.append({
                "symbol": sym,
                "fyers_symbol": f"NSE:{sym}",
                "qty": qty,
                "avg_cost": round(avg_c, 2),
                "cmp": round(close, 2) if close else avg_c,
                "invested": round(inv, 2),
                "current_val": round(cur, 2),
                "pnl_pct": round(pnl_pct, 2),
                "pnl_amount": round(cur - inv, 2),
                "status": status
            })

        results.sort(key=lambda x: (x["status"] == "STRONG HOLD", x["pnl_pct"]))

        total_pnl = total_cur - total_inv
        total_pnl_pct = (total_pnl / total_inv * 100) if total_inv > 0 else 0.0

        return {
            "holdings": results,
            "summary": {
                "total_stocks": len(results),
                "total_invested": round(total_inv, 2),
                "total_current": round(total_cur, 2),
                "total_pnl": round(total_pnl, 2),
                "total_pnl_pct": round(total_pnl_pct, 2),
                "strong_holds": sum(1 for x in results if x["status"] == "STRONG HOLD"),
                "cautions": sum(1 for x in results if x["status"] == "CAUTION"),
                "sells": sum(1 for x in results if x["status"] == "SELL")
            }
        }
    except Exception as e:
        return {"error": str(e), "holdings": [], "summary": {}}


@app.get("/api/backtest/results")
def get_backtest_results():
    """Returns comparative backtest results from backtest_results.csv."""
    csv_path = os.path.join(PROJECT_DIR, "backtest", "backtest_results.csv")
    if not os.path.exists(csv_path):
        return {"has_results": False, "message": "No backtest results found. Run Option 6 (Run Backtest) first."}

    import pandas as pd
    try:
        df = pd.read_csv(csv_path)
        agg = df.groupby("Strategy").agg(
            PnL=("PnL", "sum"),
            **{"PnL %": ("PnL %", "mean")},
            **{"Total Trades": ("Total Trades", "sum")},
            Won=("Won", "sum"),
            Lost=("Lost", "sum"),
            **{"Max Drawdown %": ("Max Drawdown %", "max")},
            **{"Sharpe Ratio": ("Sharpe Ratio", "mean")},
        ).reset_index()
        agg["Win Rate %"] = (agg["Won"] / agg["Total Trades"] * 100).round(2)
        agg = agg.sort_values("PnL", ascending=False)

        strategies = agg.to_dict(orient="records")
        for s in strategies:
            for k, v in s.items():
                if isinstance(v, float):
                    s[k] = round(v, 2)

        return {
            "has_results": True,
            "winner": strategies[0]["Strategy"] if strategies else "",
            "strategies": strategies,
            "total_stocks_evaluated": len(df["Symbol"].unique()) if "Symbol" in df.columns else 0
        }
    except Exception as e:
        return {"has_results": False, "error": str(e)}


# -------------------------------------------------------------
# Frontend HTML Page Serving
# -------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index_page():
    """Serves the single-page application dashboard."""
    template_path = os.path.join(PROJECT_DIR, "gui", "templates", "index.html")
    if os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>GUI Template not found. Check gui/templates/index.html</h1>"
