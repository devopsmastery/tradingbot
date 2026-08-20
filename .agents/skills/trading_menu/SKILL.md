---
name: trading-menu
description: Triggers when the user says "Hi" or "Hello" and displays a menu of trading actions. This is the main orchestrator that routes to all sub-agents.
---

# Trading Menu Skill (Main Orchestrator)

When the user says "Hi", "Hello", or asks for the menu, you must reply with the following menu:

```markdown
# 📈 Trading System Menu

1. **Dry Run (Scanner Agent)**: Run the daily scanner — refreshes data, scans for BUY signals, cross-references your portfolio.
2. **Update Historical Data**: Append the latest market data to all existing historical stock records.
3. **Deep Analysis (Fundamental Analyst)**: Perform a combined technical + fundamental analysis on a specific stock.
4. **Add New Stocks**: Import stocks from `Newly_added_stocks.txt` and fetch their history.
5. **Portfolio Scan (Portfolio Watchdog)**: Analyze your active holdings for SELL/CAUTION/HOLD signals with risk report.
6. **Trade History**: View your complete trade audit log.
7. **Trades Bot**: View Trades history over timeframes and calculate P&L.

*Reply with a number to select an option, or describe what you need.*
```

## Option Handlers — Subagent Routing

When the user selects an option, invoke the corresponding subagent using `invoke_subagent`:

### Option 1: Dry Run → Invoke `scanner-agent`
Invoke the `scanner-agent` subagent. It will:
1. Run `python scripts/update_historical_data.py` to refresh data.
2. Run `python scripts/dry_run.py` to scan for signals.
3. Cross-reference BUY signals against Portfolio.txt.
4. Report back with results sorted by quality.

### Option 2: Update Historical Data (Direct — no subagent needed)
1. Run `python scripts/update_historical_data.py` directly.
2. Inform the user how many stocks were updated.

### Option 3: Deep Analysis → Invoke `fundamental-analyst`
1. If the user hasn't specified a ticker symbol, ask them for one first.
2. Invoke the `fundamental-analyst` subagent with the ticker (and optional LTP).
3. It will run technical analysis + web research and report back with a Buy/Hold/Avoid verdict.

### Option 4: Add New Stocks (Direct — no subagent needed)
1. Run `python scripts/add_new_stocks.py` directly.
2. Report the newly added stocks. Do NOT run a backtest or dry run automatically.

### Option 5: Portfolio Scan → Invoke `portfolio-watchdog`
Invoke the `portfolio-watchdog` subagent. It will:
1. Load portfolio and run analysis.
2. Categorize as SELL / CAUTION / STRONG HOLD.
3. Calculate portfolio health percentage.
4. Report back with risk assessment.

### Option 6: Trade History (Direct — no subagent needed)
1. Run `python scripts/trades_history.py` directly.
2. If the user asks to filter: use `--symbol <TICKER>` or `--action BUY/SELL`.

### Option 7: Trades Bot → Invoke `trades-bot`
1. Invoke the `trades-bot` subagent.
2. It will display the sub-menu for P&L calculations and trade reporting over timeframes.

## Trade Execution (Any Context) → Invoke `trade-executor`
When the user says "Buy X shares of Y at Z" or "Sell Y" or pastes a broker trade table, invoke the `trade-executor` subagent. It will:
1. Validate the ticker and parse the order.
2. Execute via portfolio_db.py.
3. Log to trades_history.json audit trail.
4. Report back with confirmation.

## Post-Execution Rule
IMPORTANT: **Always** display the main Trading System Menu again at the very end of your response after completing ANY of the options above. Along with the menu, provide any relevant follow-up suggestions based on the task that was just completed.
