"""
Backtest Orchestrator — DuckDB-native.

Data source: DuckDB (tradingbot.duckdb) — the single source of truth.
  - Daily EOD sync appends candles; DuckDB accumulates months of history.
  - No CSV files are read. No Fyers API calls are made here.
  - Runs across all active watchlist stocks (not just stocks_to_test.txt).

Strategies tested (6 total):
  1. Keltner Tuned (ATR 2.0 + EMA 10/21)  ← current live strategy
  2. Keltner 5-Rule Retracement Breakout
  3. Keltner + EMA Cross
  4. Keltner Breakout (ATR 1.5)
  5. Bollinger RSI
  6. Squeeze Breakout

Usage:
    python backtest/run_backtest.py
    python backtest/run_backtest.py --symbols RELIANCE INFY TCS   (subset)
    python backtest/run_backtest.py --min-days 45                  (min history depth)
"""

import sys
import os
import io
import argparse
import backtrader as bt
import pandas as pd
from datetime import date

# Force UTF-8 output for Windows cp1252 compatibility
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.duckdb_manager import get_connection, load_candles
from data.data_fetcher import to_fyers_symbol
from data.watchlist_manager import get_active_watchlist, get_test_stocks
from strategies import (
    BollingerRSIStrategy,
    KeltnerBreakoutStrategy,
    KeltnerBreakoutTunedStrategy,
    SqueezeBreakoutStrategy,
    KeltnerEMACrossStrategy,
    KeltnerRetracementBreakoutStrategy,
)

RESULTS_DIR   = os.path.dirname(os.path.abspath(__file__))
INITIAL_CASH  = 100_000.0
MIN_DAYS_DEFAULT = 30   # Minimum candle rows required to backtest a symbol


def run_backtest_for_strategy(strategy_class, data_feed, name):
    """Runs a single backtrader simulation for a given strategy and data feed."""
    cerebro = bt.Cerebro()
    cerebro.addstrategy(strategy_class)

    data = bt.feeds.PandasData(dataname=data_feed)
    cerebro.adddata(data)

    cerebro.broker.setcash(INITIAL_CASH)
    cerebro.addsizer(bt.sizers.PercentSizer, percents=95)
    cerebro.broker.setcommission(commission=0.001)

    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe', riskfreerate=0.065, annualize=True)

    results = cerebro.run()
    strat = results[0]

    final_value = cerebro.broker.getvalue()
    pnl         = final_value - INITIAL_CASH
    pnl_pct     = (pnl / INITIAL_CASH) * 100

    trade_analysis = strat.analyzers.trades.get_analysis()
    total_trades = trade_analysis.get('total', {}).get('total', 0)
    won          = trade_analysis.get('won', {}).get('total', 0)
    lost         = trade_analysis.get('lost', {}).get('total', 0)
    win_rate     = (won / total_trades * 100) if total_trades > 0 else 0

    dd_analysis      = strat.analyzers.drawdown.get_analysis()
    max_drawdown_pct = dd_analysis.get('max', {}).get('drawdown', 0)
    max_drawdown_len = dd_analysis.get('max', {}).get('len', 0)

    sharpe_analysis = strat.analyzers.sharpe.get_analysis()
    sharpe = sharpe_analysis.get('sharperatio', None)
    if sharpe is None or (isinstance(sharpe, float) and sharpe != sharpe):
        sharpe = 0.0

    return {
        "Strategy":             name,
        "Final Value":          round(final_value, 2),
        "PnL":                  round(pnl, 2),
        "PnL %":                round(pnl_pct, 2),
        "Total Trades":         total_trades,
        "Won":                  won,
        "Lost":                 lost,
        "Win Rate %":           round(win_rate, 2),
        "Max Drawdown %":       round(max_drawdown_pct, 2),
        "Max DD Duration (bars)": max_drawdown_len,
        "Sharpe Ratio":         round(float(sharpe), 3),
    }


def print_aggregate_table(agg):
    """Prints a formatted strategy comparison table."""
    print("\n" + "=" * 110)
    print("  STRATEGY COMPARISON  (DuckDB — all active watchlist stocks)")
    print("=" * 110)
    print(f"  {'Strategy':<40} {'PnL':>10} {'PnL%':>8} {'Trades':>8} {'Win%':>8} {'Max DD%':>9} {'Sharpe':>8}")
    print(f"  {'-'*40} {'-'*10} {'-'*8} {'-'*8} {'-'*8} {'-'*9} {'-'*8}")
    for _, row in agg.iterrows():
        print(f"  {row['Strategy']:<40} {row['PnL']:>10,.0f} {row['PnL %']:>7.1f}% "
              f"{row['Total Trades']:>8,.0f} {row['Win Rate %']:>7.1f}% "
              f"{row['Max Drawdown %']:>8.1f}% {row['Sharpe Ratio']:>8.3f}")


def print_per_stock_table(results_df, strategy_name):
    """Prints the top and bottom performers per stock for a given strategy."""
    df = results_df[results_df["Strategy"] == strategy_name].copy()
    df = df.sort_values("PnL %", ascending=False)
    if df.empty:
        return

    print(f"\n  --- Per-Stock Results: {strategy_name} ---")
    print(f"  {'Symbol':<22} {'Days':>5} {'PnL %':>8} {'Trades':>8} {'Win%':>8} {'Max DD%':>9}")
    print(f"  {'-'*22} {'-'*5} {'-'*8} {'-'*8} {'-'*8} {'-'*9}")
    for _, row in df.iterrows():
        flag = " <<< BEST"  if row["PnL %"] == df["PnL %"].max() else ""
        flag = " <<< WORST" if row["PnL %"] == df["PnL %"].min() else flag
        days = row.get("Days", "?")
        print(f"  {row['Symbol']:<22} {days:>5} {row['PnL %']:>7.1f}% {row['Total Trades']:>8} "
              f"{row['Win Rate %']:>7.1f}% {row['Max Drawdown %']:>8.1f}%{flag}")


def main():
    parser = argparse.ArgumentParser(description="DuckDB-native backtest orchestrator")
    parser.add_argument("--symbols",   nargs="*", help="Run on specific symbols only (raw names)")
    parser.add_argument("--min-days",  type=int, default=MIN_DAYS_DEFAULT,
                        help=f"Minimum trading days required (default: {MIN_DAYS_DEFAULT})")
    parser.add_argument("--strategy",  help="Run only one strategy by name substring")
    args = parser.parse_args()

    # ---- Build stock universe ----
    if args.symbols:
        stocks = args.symbols
        print(f"\n  Running on {len(stocks)} specified symbol(s).")
    else:
        main_stocks  = get_test_stocks()
        watch_stocks = get_active_watchlist()
        stocks = list(dict.fromkeys(main_stocks + watch_stocks))
        print(f"\n  Stock universe: {len(stocks)} symbols "
              f"({len(main_stocks)} main + {len(watch_stocks)} active watchlist)")

    # ---- DuckDB health check ----
    con = get_connection(read_only=True)
    try:
        db_rows  = con.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
        db_syms  = con.execute("SELECT COUNT(DISTINCT symbol) FROM candles").fetchone()[0]
        db_stats = con.execute("SELECT MIN(timestamp), MAX(timestamp) FROM candles").fetchone()
        print(f"  DuckDB         : {db_rows:,} rows | {db_syms:,} symbols")
        print(f"  History span   : {str(db_stats[0])[:10]} -> {str(db_stats[1])[:10]}")
        print(f"  Min days gate  : {args.min_days} trading days\n")
    finally:
        con.close()

    # ---- Define strategy suite ----
    all_strategies = [
        (KeltnerBreakoutStrategy,             "Keltner Breakout (ATR 1.5)  [LIVE]"),
        (KeltnerBreakoutTunedStrategy,       "Keltner Tuned (ATR 2.0 + EMA)"),
        (KeltnerRetracementBreakoutStrategy,  "Keltner 5-Rule Retracement"),
        (KeltnerEMACrossStrategy,             "Keltner + EMA Cross"),
        (BollingerRSIStrategy,                "Bollinger RSI"),
        (SqueezeBreakoutStrategy,             "Squeeze Breakout"),
    ]

    strategies = all_strategies
    if args.strategy:
        strategies = [(cls, nm) for cls, nm in all_strategies
                      if args.strategy.lower() in nm.lower()]
        if not strategies:
            print(f"  ERROR: No strategy matched '{args.strategy}'")
            return
        print(f"  Filtering to strategy: {strategies[0][1]}")

    # ---- Run backtests ----
    overall_results = []
    skipped  = []
    errors   = []

    # Single DuckDB read-only connection for the entire backtest loop
    con = get_connection(read_only=True)
    try:
        for i, symbol in enumerate(stocks, 1):
            fyers_symbol = to_fyers_symbol(symbol)

            try:
                df = load_candles(fyers_symbol, con=con)
            except (FileNotFoundError, Exception):
                skipped.append(symbol)
                continue

            if df is None or df.empty or len(df) < args.min_days:
                skipped.append(symbol)
                continue

            days    = len(df)
            first_d = df.index[0].strftime('%d-%b-%y')
            last_d  = df.index[-1].strftime('%d-%b-%y')
            print(f"  [{i:3d}/{len(stocks)}] {symbol:<22} {days:>4}d  {first_d} -> {last_d} ", end="", flush=True)

            for StratClass, name in strategies:
                try:
                    res = run_backtest_for_strategy(StratClass, df.copy(), name)
                    res["Symbol"] = symbol
                    res["Days"]   = days
                    overall_results.append(res)
                except Exception as e:
                    errors.append(f"{symbol}/{name}: {e}")
            print("OK")
    finally:
        con.close()

    # ---- Results ----
    print(f"\n  Skipped {len(skipped)} symbols (no DuckDB data or <{args.min_days}d)")
    if skipped:
        print(f"  Skipped: {', '.join(skipped[:20])}{'...' if len(skipped) > 20 else ''}")
    if errors:
        print(f"  Errors : {len(errors)}")

    if not overall_results:
        print("\n  No results. Ensure DuckDB is populated (run 'Update DuckDB Data' first).")
        return

    results_df = pd.DataFrame(overall_results)

    # Aggregate by strategy
    agg = results_df.groupby("Strategy").agg(
        PnL         = ("PnL",           "sum"),
        **{"PnL %": ("PnL %",          "mean")},
        **{"Total Trades": ("Total Trades", "sum")},
        Won         = ("Won",           "sum"),
        Lost        = ("Lost",          "sum"),
        **{"Max Drawdown %": ("Max Drawdown %", "max")},
        **{"Sharpe Ratio":   ("Sharpe Ratio",   "mean")},
    ).reset_index()
    agg["Win Rate %"] = (agg["Won"] / agg["Total Trades"].replace(0, 1) * 100).round(2)
    agg = agg.sort_values("PnL", ascending=False)

    print_aggregate_table(agg)

    best_strategy = agg.iloc[0]["Strategy"]
    best_pnl      = agg.iloc[0]["PnL"]
    best_dd       = agg.iloc[0]["Max Drawdown %"]
    best_winrate  = agg.iloc[0]["Win Rate %"]
    best_sharpe   = agg.iloc[0]["Sharpe Ratio"]

    print(f"\n  => WINNER: {best_strategy}")
    print(f"     Total PnL      : Rs.{best_pnl:,.2f}")
    print(f"     Max Drawdown   : {best_dd:.1f}%")
    print(f"     Win Rate       : {best_winrate:.1f}%")
    print(f"     Avg Sharpe     : {best_sharpe:.3f}")

    # Per-stock breakdown for the live strategy and the winner (if different)
    live_strat_name = "Keltner Breakout (ATR 1.5)  [LIVE]"
    print("\n" + "=" * 110)
    print("  PER-STOCK BREAKDOWN")
    print("=" * 110)
    print_per_stock_table(results_df, live_strat_name)
    if best_strategy != live_strat_name:
        print_per_stock_table(results_df, best_strategy)

    # Save detailed CSV
    results_path = os.path.join(RESULTS_DIR, "backtest_results.csv")
    results_df.to_csv(results_path, index=False)
    print(f"\n  Detailed results saved to: {results_path}")


if __name__ == "__main__":
    main()

