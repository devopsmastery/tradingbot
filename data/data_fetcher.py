"""
Fyers Historical Data Fetcher (using raw HTTP requests).
Fetches daily candle data and saves as CSV for backtesting.
"""

import os
import sys
import time
import requests
import pandas as pd
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from live_trading.fyers_auth import get_access_token, FYERS_APP_ID

HISTORY_URL = "https://api-t1.fyers.in/data/history"
HISTORICAL_DATA_DIR = os.path.join(os.path.dirname(__file__), "historical_data")
STOCKS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "stocks_to_test.txt")

# Fyers limits: max 366 days per request for daily resolution
MAX_DAYS_PER_REQUEST = 365


def read_stocks(file_path: str) -> list:
    """Reads stock symbols from the stocks_to_test.txt file."""
    stocks = []
    if not os.path.exists(file_path):
        print(f"Error: {file_path} not found.")
        return stocks
    with open(file_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("Active Stocks") or line.startswith("Here are"):
                continue
            stocks.append(line)
    return stocks


def to_fyers_symbol(symbol: str) -> str:
    """Converts a raw symbol like 'RELIANCE' to Fyers format 'NSE:RELIANCE-EQ'."""
    symbol = symbol.strip()
    if symbol.startswith("NSE:"):
        return symbol
    # Handle suffixes like -BE, -SM, -ST (SME/Trade-to-Trade segments)
    if "-" in symbol:
        return f"NSE:{symbol}"
    return f"NSE:{symbol}-EQ"


def fetch_historical_data(
    symbol: str,
    access_token: str,
    days: int = 365,
    resolution: str = "D",
) -> pd.DataFrame:
    """
    Fetches historical candle data for a symbol from the Fyers API.
    
    Args:
        symbol: Fyers-formatted symbol (e.g., NSE:RELIANCE-EQ)
        access_token: Valid Fyers access token
        days: Number of days of history to fetch (default 1 year)
        resolution: Candle resolution ('D' for daily, '1' for 1-min, etc.)
    
    Returns:
        DataFrame with columns: Date, Open, High, Low, Close, Volume
    """
    headers = {
        "Authorization": f"{FYERS_APP_ID}:{access_token}",
    }

    all_candles = []
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)

    # Fetch in chunks to respect API limits
    current_start = start_date
    while current_start < end_date:
        current_end = min(current_start + timedelta(days=MAX_DAYS_PER_REQUEST), end_date)

        params = {
            "symbol": symbol,
            "resolution": resolution,
            "date_format": "1",
            "range_from": current_start.strftime("%Y-%m-%d"),
            "range_to": current_end.strftime("%Y-%m-%d"),
            "cont_flag": "1",
        }

        response = requests.get(HISTORY_URL, params=params, headers=headers)
        data = response.json()

        if data.get("s") == "ok" and "candles" in data:
            all_candles.extend(data["candles"])
        else:
            print(f"  Warning: API returned {data.get('s', 'error')} for {symbol} "
                  f"({current_start.strftime('%Y-%m-%d')} to {current_end.strftime('%Y-%m-%d')}): "
                  f"{data.get('message', 'unknown error')}")

        current_start = current_end + timedelta(days=1)
        time.sleep(0.3)  # Rate limiting

    if not all_candles:
        raise ValueError(f"No candle data returned for {symbol}")

    # Fyers candle format: [epoch, open, high, low, close, volume]
    df = pd.DataFrame(all_candles, columns=["Epoch", "Open", "High", "Low", "Close", "Volume"])
    df["Date"] = pd.to_datetime(df["Epoch"], unit="s")
    df = df[["Date", "Open", "High", "Low", "Close", "Volume"]]
    df.sort_values("Date", inplace=True)
    df.set_index("Date", inplace=True)
    df.drop_duplicates(inplace=True)

    return df


from data.duckdb_manager import (
    save_candles, load_candles, has_symbol, get_latest_candle_date, get_candle_count, DB_PATH
)


def save_historical_data(symbol: str, df: pd.DataFrame, write_csv: bool = True) -> str:
    """
    Saves a DataFrame into DuckDB (and optionally as CSV for backup).
    Maintains full backward compatibility.
    """
    # 1. Save directly into DuckDB
    try:
        save_candles(symbol, df)
    except Exception as e:
        print(f"  Warning: DuckDB save failed for {symbol}: {e}")

    # 2. Save CSV as backup
    clean_name = symbol.replace(":", "_").replace("-", "_")
    file_path = os.path.join(HISTORICAL_DATA_DIR, f"{clean_name}.csv")
    if write_csv:
        try:
            os.makedirs(HISTORICAL_DATA_DIR, exist_ok=True)
            df.to_csv(file_path)
        except Exception:
            pass
    return file_path


def load_historical_csv(symbol: str, con: Optional[Any] = None) -> pd.DataFrame:
    """
    Loads historical candlestick data for a symbol.
    First loads from DuckDB; if not found, falls back to CSV and auto-migrates into DuckDB.
    """
    # 1. Try loading from DuckDB
    try:
        df = load_candles(symbol, con=con)
        if not df.empty:
            return df
    except FileNotFoundError:
        pass

    # 2. Fallback to legacy CSV file
    clean_name = symbol.replace(":", "_").replace("-", "_")
    file_path = os.path.join(HISTORICAL_DATA_DIR, f"{clean_name}.csv")
    if os.path.exists(file_path):
        try:
            df = pd.read_csv(file_path, index_col="Date", parse_dates=True)
            if not df.empty:
                save_candles(symbol, df, con=con)
                return df
        except Exception:
            pass

    raise FileNotFoundError(f"No cached data in DuckDB or CSV for {symbol}. Run fetch first.")


def get_data_for_symbol(symbol: str, access_token: str = None, days: int = 365) -> pd.DataFrame:
    """
    Returns historical data for a symbol.
    First checks DuckDB / CSV; if not found, fetches from Fyers API and stores in DuckDB.
    """
    fyers_symbol = to_fyers_symbol(symbol)
    try:
        return load_historical_csv(fyers_symbol)
    except FileNotFoundError:
        if access_token is None:
            raise ValueError(f"No cached data for {symbol} and no access_token provided to fetch.")
        df = fetch_historical_data(fyers_symbol, access_token, days=days)
        save_historical_data(fyers_symbol, df)
        return df


def fetch_batch_quotes(symbols: list, access_token: str, chunk_size: int = 50) -> dict:
    """
    Fetches real-time market quotes in high-speed batches of up to 50 symbols.
    Returns: dict mapping symbol -> {'open': ..., 'high': ..., 'low': ..., 'close': ..., 'volume': ...}
    """
    if not symbols or not access_token:
        return {}

    headers = {"Authorization": f"{FYERS_APP_ID}:{access_token}"}
    quotes = {}

    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i + chunk_size]
        symbols_param = ",".join(chunk)
        url = f"https://api-t1.fyers.in/data/quotes?symbols={symbols_param}"
        try:
            response = requests.get(url, headers=headers, timeout=5)
            data = response.json()
            if data.get("s") == "ok" and "d" in data:
                for item in data["d"]:
                    s_name = item.get("n", "")
                    v = item.get("v", {})
                    if v and "lp" in v:
                        quotes[s_name] = {
                            "open": v.get("open_price", v.get("lp")),
                            "high": v.get("high_price", v.get("lp")),
                            "low": v.get("low_price", v.get("lp")),
                            "close": v.get("lp"),
                            "volume": v.get("volume", 0)
                        }
        except Exception:
            pass

    return quotes


def apply_live_quote(df: pd.DataFrame, quote: dict) -> pd.DataFrame:
    """
    Applies a pre-fetched live quote dict onto a DataFrame in memory in microseconds.
    """
    if not quote or df is None or df.empty:
        return df

    today = pd.Timestamp.now().normalize()
    row = {
        "Open": quote.get("open", quote.get("close")),
        "High": quote.get("high", quote.get("close")),
        "Low": quote.get("low", quote.get("close")),
        "Close": quote.get("close"),
        "Volume": quote.get("volume", 0)
    }
    
    df_copy = df.copy()
    if df_copy.index[-1].normalize() == today:
        for col in row:
            df_copy.at[df_copy.index[-1], col] = row[col]
    else:
        new_row = pd.DataFrame([row], index=[today])
        new_row.index.name = "Date"
        df_copy = pd.concat([df_copy, new_row])
    return df_copy


def append_live_quote(df: pd.DataFrame, fyers_symbol: str, access_token: str) -> pd.DataFrame:
    """
    Fetches the real-time quote for a symbol and appends or updates today's candle 
    in the DataFrame, ensuring indicators use the absolute latest price and volume.
    """
    quotes = fetch_batch_quotes([fyers_symbol], access_token)
    quote = quotes.get(fyers_symbol)
    if quote:
        return apply_live_quote(df, quote)
    return df


def fetch_all_stocks(days: int = 365):
    """
    Main entry point: Authenticates and fetches historical data for 
    all stocks listed in stocks_to_test.txt.
    """
    stocks = read_stocks(STOCKS_FILE)
    if not stocks:
        print("No stocks found in stocks_to_test.txt")
        return

    print(f"Found {len(stocks)} stocks to fetch.")
    access_token = get_access_token()

    success_count = 0
    fail_count = 0

    for i, symbol in enumerate(stocks, 1):
        fyers_symbol = to_fyers_symbol(symbol)
        print(f"[{i}/{len(stocks)}] Fetching {fyers_symbol}...", end=" ")
        try:
            df = fetch_historical_data(fyers_symbol, access_token, days=days)
            path = save_historical_data(fyers_symbol, df)
            print(f"OK ({len(df)} rows saved to {os.path.basename(path)})")
            success_count += 1
        except Exception as e:
            print(f"FAILED: {e}")
            fail_count += 1

    print(f"\nDone! {success_count} succeeded, {fail_count} failed.")


if __name__ == "__main__":
    fetch_all_stocks(days=365)
