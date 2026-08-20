# FYERS Automated Trading Bot & Multi-Strategy Framework

An institutional-grade automated trading, market discovery, backtesting, and dashboard framework for National Stock Exchange (NSE) stocks using the **Fyers API (v3)**. Features an interactive **Web GUI Dashboard (`localhost:8000`)**, high-performance **DuckDB Time-Series Engine**, 6 modular quantitative strategies, automated daily dry-run scanners with signal quality scoring, portfolio risk management, and an exchange-wide discovery scanner for Small & Mid Cap stocks.

---

## 🖥️ Web GUI Dashboard (`http://localhost:8000`)

A high-performance, dark-themed cyber-finance trading terminal accessible from any browser on `localhost:8000`.

```
                  ┌────────────────────────────────────────┐
                  │   FYERS TRADING BOT WEB DASHBOARD      │
                  │   http://localhost:8000                │
   ┌──────────────┼────────────────────────────────────────┼──────────────┐
   │ 🔐 1-Click   │  📊 Recommendations Hub ("Reco")       │ 📈 Live Deep │
   │ Fyers Auth   │  - 1-Click Copy Tickers for Charts     │ Dive Audits  │
   │ Generator &  │  - Quality Filter (80%+ EXCELLENT)     │ & Indicators │
   │ Auto .env    │  - Real-time breakout metrics          │              │
   ├──────────────┼────────────────────────────────────────┼──────────────┤
   │ ⚡ Quick Run │  💼 Portfolio Risk Monitor             │ 🏆 Backtest  │
   │ - Dry Run    │  - HOLD / CAUTION / SELL Status        │ Leaderboard  │
   │ - Full NSE   │  - Real-time P&L % and allocation      │ & 6-Strategy │
   │ - Update DB  │  - Weakness alert triggers             │ Comparisons  │
   └──────────────┴────────────────────────────────────────┴──────────────┘
```

### How to Launch the Web GUI:
```bash
python run_gui.py
```
Open your browser and navigate to: **[http://localhost:8000](http://localhost:8000)** (or **[http://127.0.0.1:8000](http://127.0.0.1:8000)**).

### Key GUI Features:
1. **🔐 1-Click Fyers Auth Generator & Token Synchronizer**:
   - Direct button to open the Fyers OAuth 2.0 authorization URL.
   - Paste the returned `auth_code` into the GUI modal: it automatically exchanges it for an active access token, updates `.env`, writes `.fyers_token`, and marks authentication as active for the day.
2. **📋 "Reco" (Recommendations Hub)**:
   - Live view of all generated BUY signals categorized into **EXCELLENT (80%+)**, **GOOD (65%+)**, and **MODERATE (50%+)**.
   - **1-Click "Copy Tickers" Button**: Instantly copies all recommended stocks formatted as `NSE:TICKER,` (comma-separated for direct import into TradingView or Fyers Web watchlist).
   - Historical scan explorer with past recommendation logs.
3. **⚡ Central Task Execution & Streaming Terminal**:
   - Trigger **Dry Run**, **Update Data**, **Portfolio Scan**, **Run Backtest**, or **Full NSE Scan** directly from the UI.
   - Real-time Server-Sent Events (SSE) stream live subprocess terminal output with auto-scroll and execution status badges.
4. **🔍 Interactive Deep Analysis Visualizer**:
   - Search any NSE stock to inspect Strength Score (0 to 5), Keltner Channel bounds (Upper, Mid, Lower), EMA 10/21 trend status, RSI (14), MACD momentum, and Volume vs. 20-day SMA.
5. **💼 Portfolio Risk & Weakness Monitor**:
   - Real-time evaluation of all held stocks from `Portfolio.txt` / `data/portfolio_db.json`.
   - Flags holdings breaking below KC Mid or EMA 21 with **`CAUTION`** or **`SELL`** risk warnings and calculates live unrealized P&L %.
6. **🏆 6-Strategy Backtest Leaderboard**:
   - Compares all 6 quantitative strategies with Net P&L, Win Rate %, Total Trades, Max Drawdown %, and Sharpe Ratio metrics.

---

## 🦆 High-Performance DuckDB Time-Series Engine

The framework features an integrated, embedded **DuckDB columnar analytical database** (`data/tradingbot.duckdb`) that replaces hundreds of fragmented CSV files for lightning-fast historical candle queries and high-concurrency multi-process scanning.

```
[1,363 Legacy CSV Files] ──(4.07s Migration)──► [data/tradingbot.duckdb (8.76 MB)]
                                                        │
                      ┌─────────────────────────────────┴─────────────────────────────────┐
                      ▼                                                                   ▼
         Microsecond Time-Series Reads                                     High-Speed Multi-Batch Quotes
       (Single Read-Only Connection <0.2s)                              (50 Symbols per Chunk via Fyers API)
                      │                                                                   │
                      └─────────────────────────────────┬─────────────────────────────────┘
                                                        ▼
                                    In-Memory Indicator Computation
                                (240 Stocks Scanned in ~12-15 Seconds)
```

### Key Performance Benefits:
- **Lightning-Fast Queries**: Queries 240 stock candle histories in **under 0.2 seconds** (over **375x faster** than fetching over the network).
- **Batch Real-Time Quotes**: Fetches live quotes in chunks of 50 symbols per API request, cutting network calls from 480 down to **just 5 batch requests** (99% reduction).
- **High-Speed Daily Scan**: The 240-stock daily scan completes in **~12–15 seconds** (down from 2.5+ minutes).
- **Compact Columnar Storage**: All 96,915 daily OHLCV candles across 1,363 stocks are compressed into a single **8.76 MB** database file.
- **Zero-Lock Multi-Process Concurrency**: Uses non-blocking read-only connections (`read_only=True` default), allowing the Web GUI, background tasks, and CLI commands to query simultaneously without file locks.
- **100% Backward Compatible**: All existing functions (`load_historical_csv()`, `save_historical_data()`) seamlessly query DuckDB with automatic CSV fallback and self-healing migration.

### Run Database Migration Manually:
```bash
python scripts/migrate_to_duckdb.py
```

---

## 🏆 Default Winning Strategy: Keltner Tuned (ATR 2.0 + EMA 10/21)

- **Entry Rule:** Daily Close breaks above the Upper Keltner Channel (20 EMA + 2.0x ATR) **AND** EMA 10 > EMA 21 (confirmed bullish trend).
- **Exit Rule:** Daily Close falls below the Middle Keltner Line (20 EMA) **OR** EMA 10 crosses below EMA 21 (trend breakdown).
- **Signal Quality Metrics:** Ranks buy signals dynamically into **EXCELLENT (80%+)**, **GOOD (65%+)**, **MODERATE (50%+)**, and **WEAK (<50%)** based on breakout magnitude (% ATR above KC), EMA slope expansion, and volume surge vs. 20-day SMA.

---

## 📊 The 6 Implemented Trading Strategies

The framework includes 6 modular Backtrader strategies located in `strategies/`:

| # | Strategy Name | Module | Core Logic & Entry Triggers | Exit Criteria |
|---|---|---|---|---|
| **1** | **Keltner Tuned (ATR 2.0 + EMA)** ★ | [`keltner_breakout_tuned.py`](strategies/keltner_breakout_tuned.py) | **Winning Baseline.** Close > KC Upper (ATR 2.0) with EMA 10 > EMA 21 trend confirmation. | Close < KC Mid OR EMA 10 < EMA 21 |
| **2** | **Keltner Breakout (ATR 1.5)** | [`keltner_breakout.py`](strategies/keltner_breakout.py) | Classic volatility breakout when Close > KC Upper (ATR 1.5). | Close < KC Mid (20 EMA) |
| **3** | **Keltner + EMA Cross** | [`keltner_ema_cross.py`](strategies/keltner_ema_cross.py) | Strict trigger requiring simultaneous KC Upper breakout AND fresh EMA 10/21 bullish crossover. | Close < KC Mid OR EMA 10 < EMA 21 |
| **4** | **Keltner 5-Rule Retracement** | [`keltner_retracement_breakout.py`](strategies/keltner_retracement_breakout.py) | Multi-day setup: 1) EMA 10/21 cross; 2) Breakout Candle (BC) > KC Mid; 3) 2-3% pull-back over 1-4 days; 4) Retracement holds above BC Low & KC Bot; 5) Positive candle breakout confirmation. | Close < KC Mid OR EMA 10 < EMA 21 |
| **5** | **Squeeze Breakout** | [`squeeze_breakout.py`](strategies/squeeze_breakout.py) | John Carter's volatility squeeze: Bollinger Bands (20, 2.0) contract inside Keltner Channels (20, 1.5), entering upon volatility expansion upward. | Close < KC Mid OR Momentum flips negative |
| **6** | **Bollinger RSI Mean Reversion** | [`bollinger_rsi.py`](strategies/bollinger_rsi.py) | Mean-reversion buying when price touches the Lower Bollinger Band (20, 2.0) with RSI (14) < 30 (oversold). | Price reaches BB Middle Band (20 SMA) |

---

## 🤖 Interactive 8-Option Trading Menu

Run `python main.py` or interact with the AI assistant to access the central trading dashboard:

```
=============================================================
    FYERS TRADING BOT
==============================================================
  What would you like to do next?
  1.  Dry Run            Scan your watchlist -> today's BUY recommendations
  2.  Update Data        Refresh historical data for all stocks
  3.  Deep Analysis      Deep-dive on a specific stock
  4.  Add New Stocks     Import tickers from Newly_added_stocks.txt
  5.  Portfolio Scan     HOLD / CAUTION / SELL status of your holdings
  6.  Run Backtest       Compare all 6 strategies across the stock universe
  7.  Full NSE Scan      Discover BUY signals from ALL small+mid cap NSE stocks
  8.  Exit
```

### Feature Details:
1. **Option 1 (Dry Run)** (`scripts/dry_run.py`):
   - Evaluates all watchlist and portfolio stocks against DuckDB historical candles and batch live quotes in ~12 seconds.
   - Categorizes recommendations into **`ADD MORE`** (held stocks with expanding momentum) and **`NEW BUY`** (fresh breakout setups).
   - Generates a **`CAUTION`** list for held stocks showing weakness or breaking below KC Mid / EMA 21.
   - Auto-dumps recommendations into timestamped files: `Results/DD-MM-HH-dryrun-results.txt`.

2. **Option 2 (Update Data)** (`scripts/update_historical_data.py`):
   - Incrementally updates daily candles in DuckDB and local CSVs without re-downloading entire histories.

3. **Option 3 (Deep Analysis)** (`scripts/deep_analysis.py <SYMBOL>`):
   - Comprehensive technical audit for any stock: Keltner Channels, EMA 10/21, RSI 14, MACD (12, 26, 9), Volume vs. 20-day SMA, and an Overall Strength Score (0 to 5). Supports live simulated LTP via `--ltp <PRICE>`.

4. **Option 4 (Add New Stocks)** (`scripts/add_new_stocks.py`):
   - Reads newly discovered or user-provided tickers from `Newly_added_stocks.txt`, fetches full historical data, deduplicates, and adds them to `stocks_watchlist.txt`, `stocks_to_test.txt`, and DuckDB.

5. **Option 5 (Portfolio Scan)** (`scripts/portfolio_analysis.py`):
   - Directly parses broker-exported holdings from `Portfolio.txt` / `data/portfolio_db.json`.
   - Computes unrealized P&L % and tags every holding with real-time risk status: **`STRONG HOLD`**, **`CAUTION`**, or **`SELL`**.

6. **Option 6 (Run Backtest)** (`backtest/run_backtest.py`):
   - Runs a comparative backtest across all 6 strategies on the full stock universe.
   - Generates performance tables (Net PnL, PnL %, Total Trades, Win Rate %, Max Drawdown %, Sharpe Ratio) and saves per-stock trade logs to `backtest/backtest_results.csv`.

7. **Option 7 (Full NSE Scan)** (`scripts/full_exchange_scan.py`):
   - **Exchange-wide Discovery Engine**: Downloads Fyers official NSE Cash Market symbol master (~2,000+ listings).
   - **Phase 1 (Filtering)**: Filters for EQ segment + Small Cap (tier 2.0-2.9) & Mid Cap (tier 3.0-3.4) (~1,300 stocks), excluding already tracked symbols. Fetches live quote batches to pre-filter stocks trading near 52-week highs with volume liquidity.
   - **Phase 2 (Strategy Evaluation)**: Analyzes pre-filtered candidates against the Keltner Tuned strategy and auto-saves discovery hits to `Results/DD-MM-HH-nsescan-results.txt`.

8. **Option 8 (Exit)**: Clean exit.

---

## 📁 Project Directory Structure

```
fyers_trading_strategy/
├── gui/
│   ├── server.py                  # FastAPI backend with REST APIs & live SSE terminal
│   └── templates/
│       └── index.html             # Single-page Cyber-Finance dark UI dashboard
├── run_gui.py                     # Web GUI launcher script (localhost:8000)
├── data/
│   ├── duckdb_manager.py          # High-performance DuckDB columnar time-series manager
│   ├── tradingbot.duckdb          # Embedded DuckDB database file (8.76 MB)
│   ├── historical_data/           # Cached OHLCV CSV files (backup)
│   ├── data_fetcher.py            # Fyers historical & batch live quote fetcher
│   ├── portfolio_db.json          # Structured portfolio database
│   ├── dry_run_history.json       # Historical signal tracking log
│   └── nse_smallmid_symbols.txt   # Cached NSE Small & Mid Cap master list
├── strategies/
│   ├── __init__.py                # Strategy exports
│   ├── keltner_breakout_tuned.py  # Strategy 1: Tuned Keltner (ATR 2.0 + EMA) ★
│   ├── keltner_breakout.py        # Strategy 2: Classic Keltner (ATR 1.5)
│   ├── keltner_ema_cross.py       # Strategy 3: Keltner + EMA Cross
│   ├── keltner_retracement_breakout.py # Strategy 4: 5-Rule Retracement Breakout
│   ├── squeeze_breakout.py        # Strategy 5: Bollinger-Keltner Squeeze
│   └── bollinger_rsi.py           # Strategy 6: Bollinger + RSI Mean Reversion
├── backtest/
│   ├── run_backtest.py            # 6-Strategy comparative backtest orchestrator
│   └── backtest_results.csv       # Per-stock backtest results log
├── live_trading/
│   ├── fyers_auth.py              # Raw HTTP OAuth 2.0 Authentication (Python 3.14 compatible)
│   └── execute_trades.py          # Core signal engine with batch quotes & DuckDB
├── scripts/
│   ├── dry_run.py                 # Watchlist daily scanner wrapper
│   ├── migrate_to_duckdb.py       # DuckDB database migration utility
│   ├── update_historical_data.py  # Incremental historical data refresher
│   ├── deep_analysis.py           # Single stock deep dive technical auditor
│   ├── add_new_stocks.py          # Ticker import and deduplication tool
│   ├── portfolio_analysis.py      # Real-time portfolio risk evaluator
│   ├── portfolio_db.py            # Portfolio DB synchronization
│   ├── full_exchange_scan.py      # Full NSE Small & Mid Cap Discovery Scanner
│   └── pnl_calculator.py          # Portfolio P&L calculator utility
├── Results/                       # Auto-saved timestamped recommendations & scans
├── push_to_git.py                 # Automated git commit & push utility
├── main.py                        # Central interactive CLI menu runner
├── Portfolio.txt                  # Broker-exported portfolio holdings file
├── Newly_added_stocks.txt         # Staging file for importing new tickers
├── stocks_to_test.txt             # Primary universe of test stocks
├── stocks_watchlist.txt           # Active tracking watchlist
├── requirements.txt               # Python package dependencies
└── .gitignore                     # Git ignore rules for secrets, DB binaries, & logs
```

---

## ⚡ Quick Start Guide

### 1. Installation
Clone the repository and install required packages:
```bash
git clone https://github.com/devopsmastery/tradingbot.git
cd tradingbot
pip install -r requirements.txt
```

### 2. Configure Credentials (`.env`)
Create a `.env` file in the root directory:
```env
FYERS_APP_ID=YOUR_APP_ID-100
FYERS_SECRET_KEY=YOUR_SECRET_KEY
FYERS_REDIRECT_URI=https://trade.fyers.in/api-login/redirect-uri/index.html
FYERS_AUTH_CODE=PASTE_DAILY_AUTH_CODE_HERE
GIT_TOKEN=ghp_your_github_token
DRY_RUN=True
```

### 3. Launch the Web GUI Dashboard
```bash
python run_gui.py
```
Open **[http://localhost:8000](http://localhost:8000)** in your browser!
- Click **"Generate Auth Code"** in the top navigation bar to log in to Fyers and authenticate in 1 click.
- Click **"Reco"** tab to view today's buy setups and copy formatted tickers directly for TradingView/Fyers.

### 4. CLI Execution (Optional)
Launch the interactive terminal menu:
```bash
python main.py
```

Or run standalone tools directly:
- **Daily Watchlist Dry Run:** `python scripts/dry_run.py`
- **Full NSE Discovery Scan:** `python scripts/full_exchange_scan.py`
- **Run Backtest:** `python backtest/run_backtest.py`
- **Update Historical Data:** `python scripts/update_historical_data.py`
- **Deep Analysis on a Stock:** `python scripts/deep_analysis.py RPEL`
- **Migrate CSVs to DuckDB:** `python scripts/migrate_to_duckdb.py`
- **Sync to GitHub:** `python push_to_git.py`

---

## 🔒 Security & Privacy
- Sensitive credentials (`.env`), token caches (`.fyers_token`), DuckDB binary database files (`*.duckdb*`), and runtime execution logs are strictly excluded from version control via `.gitignore`.