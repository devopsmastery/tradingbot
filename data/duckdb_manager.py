"""
DuckDB Database Manager for Fyers Trading Bot.
Provides high-performance columnar storage and microsecond time-series queries
for historical candlestick data, replacing individual CSV files.
Optimized for high-concurrency multi-process read/write operations.
"""

import os
import glob
import duckdb
import pandas as pd
from datetime import datetime
from typing import Optional, List, Dict, Any

DB_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DB_DIR, "tradingbot.duckdb")
HISTORICAL_DATA_DIR = os.path.join(DB_DIR, "historical_data")


def get_connection(read_only: bool = True) -> duckdb.DuckDBPyConnection:
    """
    Returns a fresh DuckDB connection.
    Defaults to read_only=True to allow unlimited concurrent readers across multiple processes.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return duckdb.connect(DB_PATH, read_only=read_only)


def init_db():
    """Initializes the database schema if missing."""
    if os.path.exists(DB_PATH):
        return

    con = duckdb.connect(DB_PATH, read_only=False)
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS candles (
                symbol VARCHAR,
                timestamp TIMESTAMP,
                open DOUBLE,
                high DOUBLE,
                low DOUBLE,
                close DOUBLE,
                volume BIGINT,
                PRIMARY KEY (symbol, timestamp)
            );
        """)
    finally:
        con.close()


def normalize_symbol_candidates(symbol: str) -> List[str]:
    """Generates all possible alias formats for symbol matching."""
    s = symbol.strip().upper()
    candidates = [s]
    
    if not s.startswith("NSE:"):
        if "-" in s:
            candidates.append(f"NSE:{s}")
        else:
            candidates.append(f"NSE:{s}-EQ")
            candidates.append(f"NSE:{s}-BE")
            candidates.append(f"NSE:{s}-SM")
            candidates.append(f"NSE:{s}-ST")
    else:
        core = s[4:]
        candidates.append(core)
        if core.endswith("-EQ") or core.endswith("-BE") or core.endswith("-SM") or core.endswith("-ST"):
            candidates.append(core[:-3])
        if "-" not in core:
            candidates.append(f"NSE:{core}-EQ")
            
    return list(dict.fromkeys(candidates))


def save_candles(symbol: str, df: pd.DataFrame, con: Optional[duckdb.DuckDBPyConnection] = None) -> int:
    """
    Saves or updates candlestick data for a symbol in DuckDB.
    Opens a write connection only for the duration of the write and closes it immediately.
    """
    if df is None or df.empty:
        return 0

    init_db()
    
    s = symbol.strip().upper()
    if not s.startswith("NSE:"):
        sym = f"NSE:{s}" if "-" in s else f"NSE:{s}-EQ"
    else:
        sym = s

    temp_df = df.copy()
    if "Date" in temp_df.columns:
        temp_df["Date"] = pd.to_datetime(temp_df["Date"])
        temp_df.set_index("Date", inplace=True)
    elif not isinstance(temp_df.index, pd.DatetimeIndex):
        temp_df.index = pd.to_datetime(temp_df.index)

    temp_df.reset_index(inplace=True)
    
    col_map = {}
    for col in temp_df.columns:
        c_lower = str(col).lower()
        if c_lower in ['date', 'timestamp', 'epoch', 'time', 'datetime']:
            col_map[col] = 'timestamp'
        elif c_lower == 'open':
            col_map[col] = 'open'
        elif c_lower == 'high':
            col_map[col] = 'high'
        elif c_lower == 'low':
            col_map[col] = 'low'
        elif c_lower == 'close':
            col_map[col] = 'close'
        elif c_lower in ['volume', 'vol']:
            col_map[col] = 'volume'

    temp_df.rename(columns=col_map, inplace=True)
    temp_df['symbol'] = sym
    
    for req in ['timestamp', 'open', 'high', 'low', 'close', 'volume']:
        if req not in temp_df.columns:
            raise ValueError(f"Missing required column '{req}' in DataFrame for {sym}")

    temp_df = temp_df[['symbol', 'timestamp', 'open', 'high', 'low', 'close', 'volume']]
    temp_df.dropna(subset=['timestamp', 'close'], inplace=True)
    temp_df.drop_duplicates(subset=['symbol', 'timestamp'], keep='last', inplace=True)

    close_con = False
    if con is None:
        con = duckdb.connect(DB_PATH, read_only=False)
        close_con = True

    try:
        candidates = normalize_symbol_candidates(sym)
        placeholders = ", ".join(["?"] * len(candidates))
        con.execute(f"DELETE FROM candles WHERE symbol IN ({placeholders})", candidates)
        
        con.register("temp_df_view", temp_df)
        con.execute("""
            INSERT OR REPLACE INTO candles (symbol, timestamp, open, high, low, close, volume)
            SELECT symbol, timestamp, open, high, low, close, volume FROM temp_df_view
        """)
        con.unregister("temp_df_view")
        return len(temp_df)
    finally:
        if close_con:
            con.close()


def load_candles(symbol: str, con: Optional[duckdb.DuckDBPyConnection] = None) -> pd.DataFrame:
    """
    Loads historical candles for a symbol from DuckDB using an existing or new read_only connection.
    """
    init_db()
    candidates = normalize_symbol_candidates(symbol)

    close_con = False
    if con is None:
        con = get_connection(read_only=True)
        close_con = True

    try:
        placeholders = ", ".join(["?"] * len(candidates))
        df = con.execute(f"""
            SELECT 
                timestamp AS "Date", 
                open AS "Open", 
                high AS "High", 
                low AS "Low", 
                close AS "Close", 
                volume AS "Volume"
            FROM candles 
            WHERE symbol IN ({placeholders})
            ORDER BY timestamp ASC
        """, candidates).df()
    finally:
        if close_con:
            con.close()

    if df.empty:
        raise FileNotFoundError(f"No cached data in DuckDB for {symbol}")

    df.drop_duplicates(subset=['Date'], keep='last', inplace=True)
    df.set_index("Date", inplace=True)
    return df


def has_symbol(symbol: str) -> bool:
    """Checks if a symbol has candle records in DuckDB."""
    init_db()
    candidates = normalize_symbol_candidates(symbol)
    con = get_connection(read_only=True)
    try:
        placeholders = ", ".join(["?"] * len(candidates))
        count = con.execute(f"SELECT COUNT(*) FROM candles WHERE symbol IN ({placeholders})", candidates).fetchone()[0]
        return count > 0
    finally:
        con.close()


def get_available_symbols() -> List[str]:
    """Returns list of distinct symbols stored in DuckDB."""
    init_db()
    con = get_connection(read_only=True)
    try:
        rows = con.execute("SELECT DISTINCT symbol FROM candles ORDER BY symbol ASC").fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()


def get_latest_candle_date(symbol: str) -> Optional[datetime]:
    """Returns the latest candle timestamp for a symbol."""
    init_db()
    candidates = normalize_symbol_candidates(symbol)
    con = get_connection(read_only=True)
    try:
        placeholders = ", ".join(["?"] * len(candidates))
        row = con.execute(f"SELECT MAX(timestamp) FROM candles WHERE symbol IN ({placeholders})", candidates).fetchone()
        if row and row[0] is not None:
            return pd.to_datetime(row[0])
        return None
    finally:
        con.close()


def get_candle_count(symbol: Optional[str] = None) -> int:
    """Returns total candle rows count (optionally filtered by symbol)."""
    init_db()
    con = get_connection(read_only=True)
    try:
        if symbol:
            candidates = normalize_symbol_candidates(symbol)
            placeholders = ", ".join(["?"] * len(candidates))
            return con.execute(f"SELECT COUNT(*) FROM candles WHERE symbol IN ({placeholders})", candidates).fetchone()[0]
        return con.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
    finally:
        con.close()


def migrate_csv_directory(dir_path: str = HISTORICAL_DATA_DIR) -> Dict[str, Any]:
    """
    Migrates all CSV files in historical_data directory into DuckDB using fast DataFrame batching.
    """
    init_db()
    if not os.path.exists(dir_path):
        return {"migrated_stocks": 0, "total_rows": 0, "errors": []}

    csv_files = glob.glob(os.path.join(dir_path, "*.csv"))
    if not csv_files:
        return {"migrated_stocks": 0, "total_rows": 0, "errors": []}

    batch = []
    errors = []

    for fpath in csv_files:
        filename = os.path.basename(fpath)
        base = filename[:-4]
        
        parts = base.split('_')
        if len(parts) == 3 and parts[0] == 'NSE':
            sym = f"NSE:{parts[1]}-{parts[2]}"
        elif len(parts) == 2 and parts[0] == 'NSE':
            sym = f"NSE:{parts[1]}"
        else:
            sym = base
            
        try:
            df = pd.read_csv(fpath)
            if not df.empty and 'Date' in df.columns:
                df['symbol'] = sym
                batch.append(df[['symbol', 'Date', 'Open', 'High', 'Low', 'Close', 'Volume']])
        except Exception as e:
            errors.append(f"{filename}: {e}")

    if not batch:
        return {"migrated_stocks": 0, "total_rows": 0, "errors": errors}

    all_df = pd.concat(batch, ignore_index=True)
    all_df['Date'] = pd.to_datetime(all_df['Date'])
    all_df.rename(columns={'Date': 'timestamp', 'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'}, inplace=True)
    all_df.dropna(subset=['timestamp', 'close'], inplace=True)
    all_df.drop_duplicates(subset=['symbol', 'timestamp'], keep='last', inplace=True)

    con = duckdb.connect(DB_PATH, read_only=False)
    try:
        con.register('bulk_view', all_df)
        con.execute('INSERT OR REPLACE INTO candles SELECT symbol, timestamp, open, high, low, close, volume FROM bulk_view;')
        con.unregister('bulk_view')

        total_rows = con.execute('SELECT COUNT(*) FROM candles').fetchone()[0]
        symbols_count = con.execute('SELECT COUNT(DISTINCT symbol) FROM candles').fetchone()[0]

        return {
            "migrated_stocks": symbols_count,
            "total_rows": total_rows,
            "database_path": DB_PATH,
            "database_size_bytes": os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0,
            "errors": errors
        }
    finally:
        con.close()
