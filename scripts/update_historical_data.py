"""
Update Historical Data — Incremental DuckDB Sync (Optimised).

Key optimisations vs the previous sequential version:
  1. ThreadPoolExecutor(max_workers=6): fetches N symbols concurrently instead
     of sequentially — ~6x speedup on network-bound Fyers API calls.
  2. Semaphore rate-limit (8 req/s): replaces the blunt time.sleep(0.3) with
     a rolling-window rate limiter that lets the executor run as fast as the
     Fyers API allows without triggering throttling.
  3. bulk_upsert_candles(): all fetched DataFrames are written to DuckDB in a
     single INSERT OR REPLACE transaction — ~200x fewer DB round-trips.
  4. no_data stocks (new symbols needing 365-day history) run sequentially
     after the parallel pass to avoid very large concurrent payloads.
"""

import os
import sys
import io
import time
import threading
import concurrent.futures
import pandas as pd
from datetime import datetime, timedelta, timezone

# Force UTF-8 output for Windows cp1252 compatibility
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

# Ensure parent directory is in sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live_trading.fyers_auth import get_access_token
import duckdb
import requests

from data.data_fetcher import (
    read_stocks,
    to_fyers_symbol,
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
    save_candles,
    upsert_candles,
    bulk_upsert_candles,
    DB_PATH,
    set_last_updated,
    is_db_current,
    get_last_updated,
    MARKET_CLOSE_HOUR,
    MARKET_CLOSE_MINUTE,
)
from data.watchlist_manager import (
    get_active_watchlist, get_sell_watchlist, purge_untracked_or_failed_symbols
)


WATCHLIST_FILE = os.path.join(os.path.dirname(STOCKS_FILE), "stocks_watchlist.txt")

# Liquid stock used to probe the actual last trading date from Fyers API
PROBE_SYMBOL = "NSE:RELIANCE-EQ"

# --- Concurrency / rate-limit settings ---
MAX_WORKERS = 6     # parallel Fyers API workers for incremental updates
MAX_RPS     = 8     # max requests per second (Fyers sustained cap ~10/s)

# Rolling-window rate-limiter state (module-level, shared across workers)
_rate_lock      = threading.Lock()
_last_req_times: list = []    # monotonic timestamps of recent requests


def _acquire_rate_slot() -> None:
    """
    Rolling-window rate limiter: allows at most MAX_RPS requests within any
    1-second window. Each worker calls this before hitting the Fyers API.
    Much more precise than a blanket time.sleep(0.3).
    """
    with _rate_lock:
        now = time.monotonic()
        # Drop timestamps outside the 1-second window
        while _last_req_times and now - _last_req_times[0] > 1.0:
            _last_req_times.pop(0)

        if len(_last_req_times) >= MAX_RPS:
            # At the cap — sleep until the oldest request falls out of the window
            sleep_for = 1.0 - (now - _last_req_times[0]) + 0.01
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.monotonic()
            while _last_req_times and now - _last_req_times[0] > 1.0:
                _last_req_times.pop(0)

        _last_req_times.append(time.monotonic())


def _fetch_worker(
    task: dict,
    access_token: str,
    today: datetime,
    counter_lock: threading.Lock,
    counter: list,
    total: int,
) -> dict:
    """
    ThreadPoolExecutor worker: fetches incremental candles for one symbol.

    Returns a result dict:
      { 'symbol', 'fyers_symbol', 'df' (DataFrame | None), 'rows' (int), 'error' (str | None) }
    """
    symbol       = task["symbol"]
    fyers_symbol = task["fyers_symbol"]
    latest       = task["latest"]
    days_behind  = task["days_behind"]
    start_fetch  = latest + timedelta(days=1)

    _acquire_rate_slot()   # honour rate limit before hitting the API

    with counter_lock:
        counter[0] += 1
        idx = counter[0]

    print(f"  [{idx:3d}/{total}] {fyers_symbol:28s} last={latest.strftime('%d-%b')} ({days_behind}d) ...", flush=True)

    try:
        df = fetch_missing_data(fyers_symbol, access_token, start_fetch, today)
        if not df.empty:
            print(f"  [{idx:3d}/{total}] {fyers_symbol:28s} +{len(df)} rows", flush=True)
            return {"symbol": symbol, "fyers_symbol": fyers_symbol, "df": df, "rows": len(df), "error": None}
        else:
            print(f"  [{idx:3d}/{total}] {fyers_symbol:28s} (no new data)", flush=True)
            return {"symbol": symbol, "fyers_symbol": fyers_symbol, "df": None, "rows": 0, "error": None}
    except Exception as e:
        print(f"  [{idx:3d}/{total}] {fyers_symbol:28s} FAILED: {str(e)[:50]}", flush=True)
        return {"symbol": symbol, "fyers_symbol": fyers_symbol, "df": None, "rows": 0, "error": str(e)}


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
        last_date = datetime.fromtimestamp(last_epoch, timezone.utc).replace(tzinfo=None)
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
    print("  (Optimised: concurrent fetch + bulk write)")
    print("=" * 60)

    today = datetime.now()

    # ---- Smart EOD Gate: skip if DB is already current ----
    status       = is_db_current(now=today)
    last_updated = status["last_updated"]
    eod_boundary = status["eod_boundary"]

    print(f"\n  Current time      : {today.strftime('%A, %d %b %Y  %H:%M:%S')}")
    print(f"  Market close      : {MARKET_CLOSE_HOUR:02d}:{MARKET_CLOSE_MINUTE:02d} IST")
    print(f"  EOD boundary      : {eod_boundary.strftime('%d %b %Y  %H:%M')} (start of current session)")
    if last_updated:
        print(f"  DB last updated   : {last_updated.strftime('%d %b %Y  %H:%M:%S')}")
    else:
        print(f"  DB last updated   : Never")

    if status["current"]:
        print(f"\n  [OK] {status['reason']}")
        print(f"\n  DuckDB is already up-to-date for this trading session. Skipping fetch.")
        return

    print(f"\n  [UPDATE NEEDED] {status['reason']}")

    # ---- Build universe from test stocks + active + sell watchlists ----
    main_stocks   = read_stocks(STOCKS_FILE)
    active_stocks = get_active_watchlist()
    sell_stocks   = get_sell_watchlist()
    all_stocks    = list(dict.fromkeys(main_stocks + active_stocks + sell_stocks))

    if not all_stocks:
        print("  No stocks found in any watchlist.")
        return

    print(f"  Total stocks      : {len(all_stocks)}")
    print(f"  Parallel workers  : {MAX_WORKERS}  (rate-limit: {MAX_RPS} req/s)")
    print()

    # ---- Auth + probe actual last trading date ----
    print("  Authenticating...", end=" ", flush=True)
    access_token = get_access_token()
    print("done")

    print("  Probing last trading date from NSE...", end=" ", flush=True)
    last_trading_day = probe_last_trading_date(access_token)
    print(f"{last_trading_day.strftime('%A, %d %b %Y')}")

    # ---- Get all latest dates from DuckDB in one batch query ----
    print("  Loading DuckDB index...", end=" ", flush=True)
    duckdb_latest = get_duckdb_latest_dates()
    print(f"{len(duckdb_latest)} symbols indexed.\n")

    # ---- Categorize stocks ----
    already_current = []
    needs_update    = []   # list of task dicts for parallel phase
    no_data         = []   # list of (symbol, fyers_symbol) for sequential phase

    for symbol in all_stocks:
        fyers_symbol = to_fyers_symbol(symbol)
        candidates   = normalize_symbol_candidates(fyers_symbol)
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
            needs_update.append({
                "symbol":       symbol,
                "fyers_symbol": fyers_symbol,
                "latest":       latest,
                "days_behind":  (last_trading_day.date() - latest.date()).days,
            })

    # ---- Report status ----
    print(f"  Already up-to-date : {len(already_current)} stocks  (last date: {last_trading_day.strftime('%d %b %Y')})")
    print(f"  Needs update       : {len(needs_update)} stocks")
    print(f"  No data (new)      : {len(no_data)} stocks")

    if already_current and not needs_update and not no_data:
        print(f"\n  All {len(already_current)} stocks are already up-to-date!")
        print(f"\n  No fetch required. DuckDB is current.")
        return

    if not needs_update and not no_data:
        print("\n  Nothing to update.")
        return

    updated_count = 0
    skipped_count = 0
    failed        = []
    t_start       = time.monotonic()

    # ================================================================
    # PHASE 1: Concurrent incremental updates (ThreadPoolExecutor)
    # ================================================================
    if needs_update:
        total_inc    = len(needs_update)
        counter      = [0]
        counter_lock = threading.Lock()
        fetch_results = []

        print(f"\n  {'='*56}")
        print(f"  PHASE 1: Incremental update — {total_inc} stocks  ({MAX_WORKERS} workers)")
        print(f"  {'='*56}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(
                    _fetch_worker, task, access_token, today, counter_lock, counter, total_inc
                ): task
                for task in needs_update
            }
            for future in concurrent.futures.as_completed(futures):
                try:
                    fetch_results.append(future.result())
                except Exception as e:
                    task = futures[future]
                    fetch_results.append({
                        "symbol": task["symbol"], "fyers_symbol": task["fyers_symbol"],
                        "df": None, "rows": 0, "error": str(e),
                    })

        # ---- Separate successes from failures ----
        bulk_records = []
        for res in fetch_results:
            if res["error"]:
                failed.append(res["symbol"])
            elif res["df"] is not None:
                bulk_records.append({"symbol": res["fyers_symbol"], "df": res["df"]})
                updated_count += 1
            else:
                skipped_count += 1

        # ---- Bulk write ALL fetched symbols in ONE DuckDB transaction ----
        if bulk_records:
            print(f"\n  Writing {len(bulk_records)} symbols to DuckDB (bulk transaction)...", end=" ", flush=True)
            write_con = duckdb.connect(DB_PATH, read_only=False)
            try:
                total_rows = bulk_upsert_candles(bulk_records, con=write_con)
                print(f"{total_rows} rows written.")
            finally:
                write_con.close()

    # ================================================================
    # PHASE 2: Sequential full-history fetch for new symbols
    # ================================================================
    if no_data:
        total_new = len(no_data)
        print(f"\n  {'='*56}")
        print(f"  PHASE 2: New symbols — {total_new} stocks  (365d history, sequential)")
        print(f"  {'='*56}")

        write_con = duckdb.connect(DB_PATH, read_only=False)
        try:
            for idx, (symbol, fyers_symbol) in enumerate(no_data, 1):
                print(f"  [{idx:3d}/{total_new}] {fyers_symbol:28s} NEW - fetching 365d history ", end="", flush=True)
                try:
                    new_df = fetch_historical_data(fyers_symbol, access_token, days=365)
                    save_candles(fyers_symbol, new_df, con=write_con, overwrite=True)
                    print(f"  {len(new_df)} rows")
                    updated_count += 1
                except Exception as e:
                    print(f"  FAILED: {str(e)[:50]}")
                    failed.append(symbol)
                time.sleep(0.4)   # conservative throttle for large 365-day fetches
        finally:
            write_con.close()

    # ---- Summary ----
    elapsed = time.monotonic() - t_start
    print(f"\n  {'='*56}")
    print(f"  SYNC COMPLETE  ({elapsed:.1f}s)")
    print(f"  {'='*56}")
    print(f"  Updated          : {updated_count}")
    print(f"  No new data      : {skipped_count}")
    if failed:
        print(f"  Failed           : {len(failed)} ({', '.join(failed[:10])})")
        print(f"\n  [PURGE] Auto-removing {len(failed)} un-fetchable/invalid stocks from DuckDB & watchlists...")
        purge_untracked_or_failed_symbols(failed)
        print(f"  Purge complete   : Removed from watchlists & deleted from DuckDB.")
    print(f"  Already current  : {len(already_current)}")
    print(f"  Last trading day : {last_trading_day.strftime('%d %b %Y (%A)')}")

    # ---- Stamp last-updated timestamp in DuckDB ----
    stamp    = set_last_updated()
    next_eod = stamp.replace(
        hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0, microsecond=0
    )
    next_update_str = (
        f"today after {MARKET_CLOSE_HOUR:02d}:{MARKET_CLOSE_MINUTE:02d}"
        if stamp < next_eod
        else f"tomorrow after {MARKET_CLOSE_HOUR:02d}:{MARKET_CLOSE_MINUTE:02d}"
    )
    print(f"  DB last updated  : {stamp.strftime('%d %b %Y  %H:%M:%S')}")
    print(f"  Next update due  : {next_update_str}")
    print()


if __name__ == "__main__":
    main()

