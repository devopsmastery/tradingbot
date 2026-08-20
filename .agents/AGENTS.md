# Custom Rules & Trading Bot Instructions

<RULE[user_global]>
## Fyers Trading Bot Operations & Behavior

### 1. Default Strategy
- The default and winning strategy across the entire system is **Keltner Tuned (ATR 2.0 + EMA 10/21)**.
- All live scans, dry runs, and full exchange scans must use this winning strategy by default.

### 2. Daily Authcode Verification
- Whenever starting the day or beginning a new session (irrespective of which trading option is chosen), check if the Fyers auth token is valid for the current day.
- If expired or not authenticated, provide the user with the Authcode generation link:
  `https://api-t1.fyers.in/api/v3/generate-authcode?client_id=JXYIZPROWB-100&redirect_uri=https%3A%2F%2Ftrade.fyers.in%2Fapi-login%2Fredirect-uri%2Findex.html&response_type=code&state=fyers_trading_strategy`

### 3. Menu Options & Execution Mapping
When the user sends an option number or command, execute the corresponding workflow:
- **Option 1 (Dry Run)**: Run `python -u scripts/dry_run.py`. Wait for the full scan to complete before responding.
- **Option 2 (Update Data)**: Run `python -u scripts/update_historical_data.py`.
- **Option 3 (Deep Analysis)**: Run `python -u scripts/deep_analysis.py <TICKER>`.
- **Option 4 (Add New Stocks)**: Read `Newly_added_stocks.txt`, append unique tickers to `stocks_watchlist.txt` & `stocks_to_test.txt`, fetch history, and clear `Newly_added_stocks.txt`.
- **Option 5 (Portfolio Scan)**: Evaluate portfolio holdings from `Portfolio.txt` / `data/portfolio_db.json`.
- **Option 6 (Run Backtest)**: Run `python -u backtest/run_backtest.py`.
- **Option 7 (Full NSE Scan)**: Run `python -u scripts/full_exchange_scan.py`.
- **Option 8 (Exit)**: Acknowledge session close.

### 4. Output Formatting & Standards
- **Quality Scores**: Always display recommendations ranked by score with labels (e.g. `SYMBOL — 90% EXCELLENT`, `SYMBOL — 75% GOOD`), breakout metrics, and EMA trend details.
- **Categorization**: Group into `ADD MORE` (held stocks) and `NEW BUY` (fresh candidates).
- **Caution List**: List held stocks showing weakness with their current unrealized P&L.
- **Results File**: Output must be saved to `Results/` (e.g. `Results/DD-MM-HH-dryrun-results.txt` or `Results/DD-MM-HH-nsescan-results.txt`) and linked.
- **Main Menu**: Always render the formatted Main Menu at the very end of EVERY response.

### 5. Standard Main Menu Format
```
=============================================================
    FYERS TRADING BOT
  <Current Day, Date Month Year>
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
</RULE[user_global]>
