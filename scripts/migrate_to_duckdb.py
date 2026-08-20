"""
Migration Script: CSV Historical Data to DuckDB
Scans data/historical_data/*.csv and imports all candles into data/tradingbot.duckdb.
"""

import os
import sys
import time

# Ensure parent directory is in sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.duckdb_manager import migrate_csv_directory, get_candle_count, get_available_symbols, DB_PATH

def main():
    print("=" * 65)
    print("  DUCKDB MIGRATION TOOL")
    print("  Importing CSV historical candles into DuckDB...")
    print("=" * 65)
    
    start_time = time.time()
    result = migrate_csv_directory()
    elapsed = round(time.time() - start_time, 2)
    
    total_candles = get_candle_count()
    symbols = get_available_symbols()
    db_size_mb = round(result["database_size_bytes"] / (1024 * 1024), 2)
    
    print(f"\n[SUCCESS] Migration completed in {elapsed}s!")
    print(f"  - Database Location : {DB_PATH}")
    print(f"  - Database Size     : {db_size_mb} MB")
    print(f"  - Stocks Migrated   : {result['migrated_stocks']} stocks ({len(symbols)} unique symbols in DB)")
    print(f"  - Total Candle Rows : {total_candles:,} rows")
    
    if result["errors"]:
        print(f"\n[WARNING] {len(result['errors'])} errors encountered:")
        for err in result["errors"][:5]:
            print(f"  - {err}")
    else:
        print("  - Status            : 100% Clean Import (0 errors)")
    print("=" * 65)

if __name__ == "__main__":
    main()
