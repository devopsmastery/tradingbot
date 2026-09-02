import os
import sys
import time
import io

# Force UTF-8 output to avoid cp1252 encoding errors on Windows
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

# Ensure parent directory is in sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.data_fetcher import (
    read_stocks, get_access_token, fetch_historical_data,
    save_historical_data, to_fyers_symbol, STOCKS_FILE
)
from data.watchlist_manager import (
    add_to_active_watchlist, get_active_watchlist,
    get_sell_watchlist, get_test_stocks, clean_symbol,
    read_symbols_from_file
)

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEW_STOCKS_FILE = os.path.join(PROJECT_DIR, "Newly_added_stocks.txt")


def add_new_stocks():
    print("=" * 60)
    print("  ADD NEW STOCKS TO ACTIVE WATCHLIST + DuckDB")
    print("=" * 60)

    if not os.path.exists(NEW_STOCKS_FILE):
        print(f"  Error: {NEW_STOCKS_FILE} does not exist.")
        return

    new_stocks = read_symbols_from_file(NEW_STOCKS_FILE)
    if not new_stocks:
        print("  No valid stocks found in Newly_added_stocks.txt.")
        print("  Add symbols (one per line) and re-run.")
        return

    print(f"\n  Found {len(new_stocks)} symbols in Newly_added_stocks.txt")

    # ---- Determine existing universe for deduplication ----
    active = get_active_watchlist()
    sell = get_sell_watchlist()
    test = get_test_stocks()
    existing = set(active + sell + test)

    unique_new = [s for s in new_stocks if s not in existing]
    duplicates = [s for s in new_stocks if s in existing]

    if duplicates:
        print(f"  Skipping {len(duplicates)} already-tracked: {', '.join(duplicates[:10])}"
              + (f" (+{len(duplicates)-10} more)" if len(duplicates) > 10 else ""))

    if not unique_new:
        print("\n  All stocks in Newly_added_stocks.txt are already tracked. Nothing to add.")
        _clear_staging_file()
        return

    print(f"  Adding {len(unique_new)} new unique stocks to Active Watchlist...\n")

    # ---- Step 1: Add to Active Watchlist via watchlist_manager ----
    result = add_to_active_watchlist(unique_new)
    print(f"  ✅ Active Watchlist updated: {result['active_count']} total stocks")
    print(f"     Newly added: {', '.join(result['added'][:15])}"
          + (f" (+{len(result['added'])-15} more)" if len(result['added']) > 15 else ""))

    # ---- Step 2: Fetch historical data for new stocks ----
    print(f"\n  Fetching 365-day historical data for {len(unique_new)} new stocks...")
    print("  " + "-" * 56)

    access_token = get_access_token()
    success = 0
    failed = []

    for i, symbol in enumerate(unique_new, 1):
        fyers_symbol = to_fyers_symbol(symbol)
        print(f"  [{i:3d}/{len(unique_new)}] {fyers_symbol:25s}", end=" ")
        try:
            df = fetch_historical_data(fyers_symbol, access_token, days=365)
            save_historical_data(fyers_symbol, df)
            print(f"✅ {len(df)} rows saved")
            success += 1
        except Exception as e:
            err_msg = str(e)[:50]
            print(f"❌ {err_msg}")
            failed.append(symbol)

        # Rate limiting to avoid Fyers API throttling
        time.sleep(0.3)

    # ---- Summary ----
    print("  " + "-" * 56)
    print(f"\n  📊 SUMMARY:")
    print(f"     Successfully fetched : {success}/{len(unique_new)}")
    if failed:
        print(f"     Failed              : {len(failed)} ({', '.join(failed[:10])})")
    print(f"     Active Watchlist    : {result['active_count']} stocks")

    # ---- Step 3: Clear staging file ----
    _clear_staging_file()

    print()


def _clear_staging_file():
    """Clears Newly_added_stocks.txt to prevent re-processing on next run."""
    with open(NEW_STOCKS_FILE, "w", encoding="utf-8") as f:
        f.write("# Newly Added Stocks - Paste symbols here (one per line)\n")
    print("  🧹 Cleared Newly_added_stocks.txt to prevent duplication.")


if __name__ == "__main__":
    add_new_stocks()
