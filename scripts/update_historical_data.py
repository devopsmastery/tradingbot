"""
Update Historical Data — Incremental DuckDB Sync.

Reads last-recorded date per symbol from DuckDB, fetches only missing
end-of-day OHLC candles from Fyers, and appends them.
If data is already up to date, reports the last recorded date and skips.
"""

import os
import sys
import io
import time
import pandas as pd
from datetime import datetime, timedelta

# Force UTF-8 output for Windows cp1252 compatibility
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

# Ensure parent directory is in sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live_trading.fyers_auth import get_access_token
from data.data_fetcher import (
    read_stocks,
    to_fyers_symbol,
    save_historical_data,
    fetch_historical_data,
    STOCKS_FILE,
    HISTORY_URL,
    FYERS_APP_ID,
)
from data.duckdb_manager import (
    get_connection,
    init_db,
    normalize_symbol_candidates,
    load_candles,
)
from data.watchlist_manager import get_active_watchlist, get_sell_watchlist
import requests


WATCHLIST_FILE = os.path.join(os.path.dirname(STOCKS_FILE), "stocks_watchlist.txt")

# Liquid stock used to probe the actual last trading date from Fyers API
PROBE_SYMBOL = "NSE:RELIANCE-EQ"


def probe_last_trading_date(access_token: str) -> datetime:
    """
    Fetches the last 7 days of candles for a liquid stock (RELIANCE)
    to determine the actual last available trading date on the exchange.
    This correctly handles weekends, public holidays, and partial days.
    """
    headers = {"Authorization": f"{FYERS_APP_ID}:{access_token}"}
    today = datetime.now()
    params = {
        "symbol": PROBE_SYMBOL,
        "resolution": "D",
        "date_format": "1",
        "range_from": (today - timedelta(days=7)).strftime("%Y-%m-%d"),
        "range_to": today.strftime("%Y-%m-%d"),
        "cont_flag": "1",
    }
    resp = requests.get(HISTORY_URL, params=params, headers=headers)
    data = resp.json()
    if data.get("s") == "ok" and data.get("candles"):
        last_epoch = data["candles"][-1][0]
        # Fyers daily candle epochs are UTC midnight — same as DuckDB storage
        last_date = datetime.utcfromtimestamp(last_epoch)
        return last_date.replace(hour=0, minute=0, second=0, microsecond=0)
    elif data.get("s") == "error":
        msg = data.get("message", "Unknown error")
        if "authenticate" in msg.lower():
            print(f"\n  ERROR: Fyers token expired or invalid. Please re-authenticate.")
            print(f"         Use the GUI Auth modal or run daily_auth_check().")
            raise RuntimeError(f"Fyers authentication failed: {msg}")
        print(f"\n  WARNING: Probe API returned error: {msg}")

    # Fallback: previous weekday
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def get_duckdb_latest_dates() -> dict:
    """Returns {symbol: latest_timestamp} for all symbols in DuckDB in one query."""
    init_db()
    con = get_connection(read_only=True)
    try:
        rows = con.execute(
            "SELECT symbol, MAX(timestamp) AS latest FROM candles GROUP BY symbol"
        ).fetchall()
        return {row[0]: pd.to_datetime(row[1]) for row in rows}
    finally:
        con.close()


def fetch_missing_data(symbol: str, access_token: str, start_date: datetime, end_date: datetime) -> pd.DataFrame:
    """Fetches daily candle data for a specific date range from Fyers API."""
    headers = {"Authorization": f"{FYERS_APP_ID}:{access_token}"}
    params = {
        "symbol": symbol,
        "resolution": "D",
        "date_format": "1",
        "range_from": start_date.strftime("%Y-%m-%d"),
        "range_to": end_date.strftime("%Y-%m-%d"),
        "cont_flag": "1",
    }
    response = requests.get(HISTORY_URL, params=params, headers=headers)
    data = response.json()

    if data.get("s") == "error":
        msg = data.get("message", "")
        if "authenticate" in msg.lower():
            raise RuntimeError(f"Fyers auth failed: {msg}")

    if data.get("s") == "ok" and "candles" in data and len(data["candles"]) > 0:
        df = pd.DataFrame(data["candles"], columns=["Epoch", "Open", "High", "Low", "Close", "Volume"])
        df["Date"] = pd.to_datetime(df["Epoch"], unit="s")
        df = df[["Date", "Open", "High", "Low", "Close", "Volume"]]
        df.sort_values("Date", inplace=True)
        df.set_index("Date", inplace=True)
        return df
    return pd.DataFrame()


def main():
    print("=" * 60)
    print("  UPDATE HISTORICAL DATA  -  DuckDB Incremental Sync")
    print("=" * 60)

    # ---- Build universe from test stocks + active + sell watchlists ----
    main_stocks = read_stocks(STOCKS_FILE)
    active_stocks = get_active_watchlist()
    sell_stocks = get_sell_watchlist()
    all_stocks = list(dict.fromkeys(main_stocks + active_stocks + sell_stocks))

    if not all_stocks:
        print("  No stocks found in any watchlist.")
        return

    today = datetime.now()
    print(f"\n  Today             : {today.strftime('%A, %d %b %Y %H:%M')}")
    print(f"  Total stocks      : {len(all_stocks)}")

    # ---- Auth + probe actual last trading date ----
    print("  Authenticating...", end=" ")
    access_token = get_access_token()

    print("  Probing last trading date from NSE...", end=" ")
    last_trading_day = probe_last_trading_date(access_token)
    print(f"{last_trading_day.strftime('%A, %d %b %Y')}")

    # ---- Get all latest dates from DuckDB in one batch query ----
    print("  Loading DuckDB index...", end=" ")
    duckdb_latest = get_duckdb_latest_dates()
    print(f"{len(duckdb_latest)} symbols indexed.\n")

    # ---- Categorize stocks ----
    already_current = []
    needs_update = []
    no_data = []

    for symbol in all_stocks:
        fyers_symbol = to_fyers_symbol(symbol)
        candidates = normalize_symbol_candidates(fyers_symbol)
        latest = None
        for c in candidates:
            if c in duckdb_latest:
                latest = duckdb_latest[c]
                break

        if latest is None:
            no_data.append((symbol, fyers_symbol))
        elif latest.date() >= last_trading_day.date():
            already_current.append((symbol, fyers_symbol, latest))
        else:
            needs_update.append((symbol, fyers_symbol, latest))

    # ---- Report status ----
    print(f"  Already up-to-date : {len(already_current)} stocks  (last date: {last_trading_day.strftime('%d %b %Y')})")
    print(f"  Needs update       : {len(needs_update)} stocks")
    print(f"  No data (new)      : {len(no_data)} stocks")

    if already_current and not needs_update and not no_data:
        print(f"\n  All {len(already_current)} stocks are already up-to-date!")
        print(f"  Last recorded date : {last_trading_day.strftime('%d %b %Y (%A)')}")
        print(f"\n  No fetch required. DuckDB is current.")
        return

    if not needs_update and not no_data:
        print("\n  Nothing to update.")
        return

    # ---- Fetch missing data ----
    total_to_process = len(needs_update) + len(no_data)
    print(f"\n  {'='*56}")
    print(f"  Fetching missing candles for {total_to_process} stocks...")
    print(f"  {'='*56}")

    updated_count = 0
    skipped_count = 0
    failed = []
    counter = 0

    # Process stocks needing incremental updates
    for symbol, fyers_symbol, latest in needs_update:
        counter += 1
        start_fetch = latest + timedelta(days=1)
        days_behind = (last_trading_day.date() - latest.date()).days
        print(f"  [{counter:3d}/{total_to_process}] {fyers_symbol:25s} "
              f"last={latest.strftime('%d-%b')} ({days_behind}d behind) ", end="")

        try:
            new_df = fetch_missing_data(fyers_symbol, access_token, start_fetch, today)
            if not new_df.empty:
                try:
                    existing_df = load_candles(fyers_symbol)
                    combined_df = pd.concat([existing_df, new_df])
                    combined_df = combined_df[~combined_df.index.duplicated(keep='last')]
                    combined_df.sort_index(inplace=True)
                except FileNotFoundError:
                    combined_df = new_df
                save_historical_data(fyers_symbol, combined_df)
                print(f"  +{len(new_df)} rows")
                updated_count += 1
            else:
                print(f"  (no new data)")
                skipped_count += 1
        except Exception as e:
            print(f"  FAILED: {str(e)[:40]}")
            failed.append(symbol)

        time.sleep(0.3)

    # Process stocks with no data at all (fetch full 365-day history)
    for symbol, fyers_symbol in no_data:
        counter += 1
        print(f"  [{counter:3d}/{total_to_process}] {fyers_symbol:25s} "
              f"NEW - fetching 365d history ", end="")
        try:
            new_df = fetch_historical_data(fyers_symbol, access_token, days=365)
            save_historical_data(fyers_symbol, new_df)
            print(f"  {len(new_df)} rows")
            updated_count += 1
        except Exception as e:
            print(f"  FAILED: {str(e)[:40]}")
            failed.append(symbol)
        time.sleep(0.3)

    # ---- Summary ----
    print(f"\n  {'='*56}")
    print(f"  SYNC COMPLETE")
    print(f"  {'='*56}")
    print(f"  Updated          : {updated_count}")
    print(f"  No new data      : {skipped_count}")
    if failed:
        print(f"  Failed           : {len(failed)} ({', '.join(failed[:10])})")
    print(f"  Already current  : {len(already_current)}")
    print(f"  Last trading day : {last_trading_day.strftime('%d %b %Y (%A)')}")
    print()


if __name__ == "__main__":
    main()
