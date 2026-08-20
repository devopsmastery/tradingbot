"""
Backtest Orchestrator: Runs all strategies across all stocks in stocks_to_test.txt
and produces a comparative performance report including Max Drawdown.
"""

import sys
import os
import backtrader as bt
import pandas as pd

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.data_fetcher import read_stocks, to_fyers_symbol, load_historical_csv
from strategies import (
    BollingerRSIStrategy,
    KeltnerBreakoutStrategy,
    KeltnerBreakoutTunedStrategy,
    SqueezeBreakoutStrategy,
    KeltnerEMACrossStrategy,
    KeltnerRetracementBreakoutStrategy,
)

STOCKS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "stocks_to_test.txt")
RESULTS_DIR = os.path.dirname(os.path.abspath(__file__))
INITIAL_CASH = 100000.0


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
    pnl = final_value - INITIAL_CASH
    pnl_pct = (pnl / INITIAL_CASH) * 100

    trade_analysis = strat.analyzers.trades.get_analysis()
    total_trades = trade_analysis.get('total', {}).get('total', 0)
    won = trade_analysis.get('won', {}).get('total', 0)
    lost = trade_analysis.get('lost', {}).get('total', 0)
    win_rate = (won / total_trades * 100) if total_trades > 0 else 0

    dd_analysis = strat.analyzers.drawdown.get_analysis()
    max_drawdown_pct = dd_analysis.get('max', {}).get('drawdown', 0)
    max_drawdown_len = dd_analysis.get('max', {}).get('len', 0)

    sharpe_analysis = strat.analyzers.sharpe.get_analysis()
    sharpe = sharpe_analysis.get('sharperatio', None)
    if sharpe is None or (isinstance(sharpe, float) and (sharpe != sharpe)):  # NaN check
        sharpe = 0.0

    return {
        "Strategy": name,
        "Final Value": round(final_value, 2),
        "PnL": round(pnl, 2),
        "PnL %": round(pnl_pct, 2),
        "Total Trades": total_trades,
        "Won": won,
        "Lost": lost,
        "Win Rate %": round(win_rate, 2),
        "Max Drawdown %": round(max_drawdown_pct, 2),
        "Max DD Duration (bars)": max_drawdown_len,
        "Sharpe Ratio": round(float(sharpe), 3),
    }


def print_aggregate_table(agg):
    """Prints a formatted comparison table."""
    print("\n" + "=" * 105)
    print("  STRATEGY COMPARISON (AGGREGATED ACROSS ALL STOCKS IN stocks_to_test.txt)")
    print("=" * 105)
    cols = ["Strategy", "PnL", "PnL %", "Total Trades", "Win Rate %", "Max Drawdown %", "Avg Sharpe"]
    print(f"  {'Strategy':<35} {'PnL':>10} {'PnL%':>8} {'Trades':>8} {'Win%':>8} {'Max DD%':>9} {'Sharpe':>8}")
    print(f"  {'-'*35} {'-'*10} {'-'*8} {'-'*8} {'-'*8} {'-'*9} {'-'*8}")
    for _, row in agg.iterrows():
        print(f"  {row['Strategy']:<35} {row['PnL']:>10,.0f} {row['PnL %']:>7.1f}% "
              f"{row['Total Trades']:>8,.0f} {row['Win Rate %']:>7.1f}% "
              f"{row['Max Drawdown %']:>8.1f}% {row['Sharpe Ratio']:>8.3f}")


def print_per_stock_table(results_df, strategy_name):
    """Prints the top and bottom performers per stock for a given strategy."""
    df = results_df[results_df["Strategy"] == strategy_name].copy()
    df = df.sort_values("PnL %", ascending=False)

    print(f"\n  --- Per-Stock Results for: {strategy_name} ---")
    print(f"  {'Symbol':<20} {'PnL %':>8} {'Trades':>8} {'Win%':>8} {'Max DD%':>9}")
    print(f"  {'-'*20} {'-'*8} {'-'*8} {'-'*8} {'-'*9}")
    for _, row in df.iterrows():
        flag = " <<< BEST" if row["PnL %"] == df["PnL %"].max() else ""
        flag = " <<< WORST" if row["PnL %"] == df["PnL %"].min() else flag
        print(f"  {row['Symbol']:<20} {row['PnL %']:>7.1f}% {row['Total Trades']:>8} "
              f"{row['Win Rate %']:>7.1f}% {row['Max Drawdown %']:>8.1f}%{flag}")


def main():
    stocks = read_stocks(STOCKS_FILE)
    if not stocks:
        print("No stocks found to backtest.")
        return

    print(f"\nBacktesting {len(stocks)} stocks from stocks_to_test.txt...")
    print("Using cached historical CSV data.\n")

    strategies = [
        (KeltnerRetracementBreakoutStrategy, "Keltner 5-Rule Retracement (NEW)"),
        (KeltnerBreakoutTunedStrategy,       "Keltner Tuned (ATR 2.0 + EMA)"),
        (KeltnerEMACrossStrategy,             "Keltner + EMA Cross"),
        (KeltnerBreakoutStrategy,             "Keltner Breakout (ATR 1.5)"),
        (BollingerRSIStrategy,                "Bollinger RSI"),
        (SqueezeBreakoutStrategy,             "Squeeze Breakout"),
    ]

    overall_results = []
    skipped = []
    errors = []

    for i, symbol in enumerate(stocks, 1):
        fyers_symbol = to_fyers_symbol(symbol)
        try:
            df = load_historical_csv(fyers_symbol)
        except FileNotFoundError:
            skipped.append(symbol)
            continue

        if len(df) < 30:
            skipped.append(symbol)
            continue

        print(f"  [{i:2d}/{len(stocks)}] {symbol:<20}", end="", flush=True)

        for StratClass, name in strategies:
            try:
                res = run_backtest_for_strategy(StratClass, df, name)
                res["Symbol"] = symbol
                overall_results.append(res)
            except Exception as e:
                errors.append(f"{symbol}/{name}: {e}")
        print("OK")

    print(f"\n  Skipped {len(skipped)} symbols (no data): {', '.join(skipped[:10])}{'...' if len(skipped) > 10 else ''}")
    if errors:
        print(f"  {len(errors)} errors encountered.")

    if not overall_results:
        print("\nNo results. Run data/data_fetcher.py first to cache historical data.")
        return

    results_df = pd.DataFrame(overall_results)

    # Aggregate by strategy
    agg = results_df.groupby("Strategy").agg(
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

    print_aggregate_table(agg)

    best_strategy = agg.iloc[0]["Strategy"]
    best_pnl = agg.iloc[0]["PnL"]
    best_dd = agg.iloc[0]["Max Drawdown %"]
    best_winrate = agg.iloc[0]["Win Rate %"]

    print(f"\n  => WINNER: {best_strategy}")
    print(f"     Total PnL      : Rs.{best_pnl:,.2f}")
    print(f"     Max Drawdown   : {best_dd:.1f}%")
    print(f"     Win Rate       : {best_winrate:.1f}%")

    # Show per-stock breakdown for the 2 key strategies
    print("\n" + "=" * 105)
    print("  PER-STOCK BREAKDOWN")
    print("=" * 105)
    print_per_stock_table(results_df, "Keltner + EMA Cross (NEW)")
    print_per_stock_table(results_df, "Keltner Tuned (ATR 2.0 + EMA)")

    # Save detailed CSV
    results_path = os.path.join(RESULTS_DIR, "backtest_results.csv")
    results_df.to_csv(results_path, index=False)
    print(f"\n  Detailed results saved to: {results_path}")


if __name__ == "__main__":
    main()
