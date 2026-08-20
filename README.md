# FYERS Automated Trading Bot & Multi-Strategy Framework

An institutional-grade automated trading, discovery, and backtesting framework for National Stock Exchange (NSE) stocks using the **Fyers API (v3)**. Features multi-strategy backtesting, automated daily dry-run scanners with signal quality scoring, portfolio risk management, and a high-speed full-exchange discovery scanner for Small & Mid Cap stocks.

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
   - Evaluates all watchlist and portfolio stocks against live market quotes.
   - Categorizes recommendations into **`ADD MORE`** (held stocks with expanding momentum) and **`NEW BUY`** (fresh breakout setups).
   - Generates a **`CAUTION`** list for held stocks showing weakness or breaking below KC Mid / EMA 21.
   - Auto-dumps recommendations into timestamped files: `Results/DD-MM-HH-dryrun-results.txt`.

2. **Option 2 (Update Data)** (`scripts/update_historical_data.py`):
   - Incrementally appends latest daily candles to local CSVs in `data/historical_data/` without re-downloading entire histories.

3. **Option 3 (Deep Analysis)** (`scripts/deep_analysis.py <SYMBOL>`):
   - Comprehensive technical audit for any stock: Keltner Channels, EMA 10/21, RSI 14, MACD (12, 26, 9), Volume vs. 20-day SMA, and an Overall Strength Score (0 to 5). Supports live simulated LTP via `--ltp <PRICE>`.

4. **Option 4 (Add New Stocks)** (`scripts/add_new_stocks.py`):
   - Reads newly discovered or user-provided tickers from `Newly_added_stocks.txt`, fetches full historical data, deduplicates, and adds them to `stocks_watchlist.txt` and `stocks_to_test.txt`.

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
├── .agents/
│   ├── AGENTS.md                  # Permanent workspace behavior & rules
│   └── skills/                    # Specialized AI agent skills
├── data/
│   ├── historical_data/           # Local cached OHLCV CSV files
│   ├── data_fetcher.py            # Fyers historical & live quote fetcher
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
│   └── execute_trades.py          # Core signal engine & order execution
├── scripts/
│   ├── dry_run.py                 # Watchlist daily scanner wrapper
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
└── .gitignore                     # Git ignore rules for secrets and runtime outputs
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

### 3. Generate Daily Fyers Auth Token
Generate your daily auth code using:
👉 **[Fyers Auth Code Generation URL](https://api-t1.fyers.in/api/v3/generate-authcode?client_id=JXYIZPROWB-100&redirect_uri=https%3A%2F%2Ftrade.fyers.in%2Fapi-login%2Fredirect-uri%2Findex.html&response_type=code&state=fyers_trading_strategy)**

Paste the code into `.env` under `FYERS_AUTH_CODE`.

### 4. Running the Bot
Launch the main menu:
```bash
python main.py
```

Or run standalone commands directly:
- **Daily Watchlist Dry Run:** `python scripts/dry_run.py`
- **Full NSE Discovery Scan:** `python scripts/full_exchange_scan.py`
- **Run Backtest:** `python backtest/run_backtest.py`
- **Update Historical Data:** `python scripts/update_historical_data.py`
- **Deep Analysis on a Stock:** `python scripts/deep_analysis.py RPEL`
- **Sync to GitHub:** `python push_to_git.py`

---

## 🔒 Security & Privacy
- Sensitive credentials (`.env`), token caches (`.fyers_token`), and runtime execution logs are strictly excluded from version control via `.gitignore`.