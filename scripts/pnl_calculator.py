import os
import json
import argparse
from datetime import datetime, timedelta

TRADES_LOG_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'trades_history.json')

def load_history():
    if os.path.exists(TRADES_LOG_PATH):
        try:
            with open(TRADES_LOG_PATH, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            return []
    return []

def calculate_realized_pnl(history):
    """
    Replays the trades chronologically to compute Average Cost and Realized P&L on sells.
    """
    # Sort history chronologically just in case
    history = sorted(history, key=lambda x: x.get('timestamp', x.get('date', '')))
    
    inventory = {}
    realized_pnl_records = []
    
    for trade in history:
        sym = trade['symbol']
        action = trade['action'].upper()
        qty = float(trade['qty'])
        price = float(trade['price'])
        date_str = trade['date']
        
        if sym not in inventory:
            inventory[sym] = {"qty": 0.0, "avg_cost": 0.0}
            
        old_qty = inventory[sym]["qty"]
        old_cost = inventory[sym]["avg_cost"]
        
        if action == "BUY":
            new_qty = old_qty + qty
            if new_qty > 0:
                new_cost = ((old_qty * old_cost) + (qty * price)) / new_qty
            else:
                new_cost = price
            inventory[sym]["qty"] = new_qty
            inventory[sym]["avg_cost"] = new_cost
            
        elif action == "SELL":
            if old_cost > 0.0:
                # Realized P&L
                pnl = (price - old_cost) * qty
                
                realized_pnl_records.append({
                    "date": date_str,
                    "symbol": sym,
                    "qty": qty,
                    "sell_price": price,
                    "avg_cost": old_cost,
                    "pnl": pnl
                })
            
            inventory[sym]["qty"] = old_qty - qty
            
    return realized_pnl_records

def filter_by_date(records, days_ago):
    cutoff_date = datetime.now() - timedelta(days=days_ago)
    cutoff_str = cutoff_date.strftime("%Y-%m-%d")
    
    filtered = []
    for r in records:
        if r.get('date', '') >= cutoff_str:
            filtered.append(r)
    return filtered

def display_pnl(records, title):
    print("=" * 80)
    print(f"  {title}")
    print("-" * 80)
    if not records:
        print("  No sells found with known buy price in this period.")
        print("=" * 80)
        return
        
    # Aggregate by symbol
    aggregated = {}
    for r in records:
        sym = r['symbol']
        if sym not in aggregated:
            aggregated[sym] = {'qty': 0.0, 'cost_basis': 0.0, 'revenue': 0.0, 'pnl': 0.0}
            
        qty = r['qty']
        aggregated[sym]['qty'] += qty
        aggregated[sym]['cost_basis'] += (r['avg_cost'] * qty)
        aggregated[sym]['revenue'] += (r['sell_price'] * qty)
        aggregated[sym]['pnl'] += r['pnl']
        
    print(f"  {'Symbol':<15} {'Qty Sold':>10} {'Avg Buy':>10} {'Avg Sell':>10} {'P&L':>12}")
    print("-" * 80)
    
    total_pnl = 0.0
    for sym, data in aggregated.items():
        qty = data['qty']
        avg_buy = data['cost_basis'] / qty if qty > 0 else 0
        avg_sell = data['revenue'] / qty if qty > 0 else 0
        pnl = data['pnl']
        total_pnl += pnl
        
        print(f"  {sym:<15} {int(qty):>10} {avg_buy:>10.2f} {avg_sell:>10.2f} {pnl:>12.2f}")
        
    print("-" * 80)
    print(f"  Total Realized P&L: Rs. {total_pnl:,.2f}")
    print("=" * 80)

def display_trades(trades, title):
    print("=" * 80)
    print(f"  {title}")
    print("-" * 80)
    if not trades:
        print("  No trades found.")
        print("=" * 80)
        return
        
    print(f"  {'Stock':<15} {'Qty':>8} | {'Buy Date':<12} {'Buy Price':>10} | {'Sell Date':<12} {'Sell Price':>10}")
    print("-" * 80)
    
    for t in trades:
        action = t['action'].upper()
        sym = t['symbol']
        qty = int(t['qty'])
        price = t['price']
        date_str = t['date']
        
        if action == "BUY":
            buy_date, buy_price = date_str, f"{price:.2f}"
            sell_date, sell_price = "-", "-"
        else:
            buy_date, buy_price = "-", "-"
            sell_date, sell_price = date_str, f"{price:.2f}"
            
        print(f"  {sym:<15} {qty:>8} | {buy_date:<12} {buy_price:>10} | {sell_date:<12} {sell_price:>10}")
        
    print("=" * 80)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="P&L and Trades Report")
    parser.add_argument("--pnl", choices=["1w", "1m"], help="Calculate P&L for timeframe")
    parser.add_argument("--trades", choices=["1d", "1w"], help="Show trades for timeframe")
    parser.add_argument("--stock", help="Show all trades for a specific stock")
    args = parser.parse_args()

    history = load_history()
    
    if args.pnl:
        pnl_records = calculate_realized_pnl(history)
        days = 7 if args.pnl == "1w" else 30
        filtered = filter_by_date(pnl_records, days)
        title = f"Realized P&L - Last {args.pnl}"
        display_pnl(filtered, title)
        
    elif args.trades:
        days = 1 if args.trades == "1d" else 7
        filtered = filter_by_date(history, days)
        title = f"Trades - Last {args.trades}"
        display_trades(filtered, title)
        
    elif args.stock:
        sym = args.stock.upper()
        filtered = [t for t in history if t.get('symbol', '').upper() == sym]
        title = f"Trades for {sym}"
        display_trades(filtered, title)
    else:
        parser.print_help()
