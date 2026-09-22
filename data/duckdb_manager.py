"""
DuckDB Database Manager for Fyers Trading Bot.
Provides high-performance columnar storage and microsecond time-series queries
for historical candlestick data, replacing individual CSV files.
Optimized for high-concurrency multi-process read/write operations.
Includes recommendations_history table for tracking first-discovered EXCELLENT stocks.
Includes db_metadata table for tracking last-update timestamps with NSE 15:30 boundary logic.
"""

import os
import glob
import re
import duckdb
import pandas as pd
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any, Union

DB_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DB_DIR, "tradingbot.duckdb")
HISTORICAL_DATA_DIR = os.path.join(DB_DIR, "historical_data")

# NSE market close time (IST) — EOD data becomes available after 15:30
MARKET_CLOSE_HOUR   = 15
MARKET_CLOSE_MINUTE = 30


def get_connection(read_only: bool = True) -> duckdb.DuckDBPyConnection:
    """
    Returns a fresh DuckDB connection.
    Defaults to read_only=True to allow unlimited concurrent readers across multiple processes.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return duckdb.connect(DB_PATH, read_only=read_only)


def init_db():
    """
    Initializes the database schema on first boot (idempotent via file existence check).
    Creates:
      - candles     : OHLCV time-series per symbol
      - db_metadata : key-value store for operational metadata (e.g. last_updated)

    NOTE: Only opens a connection when the DB file does not yet exist, so this is
    always safe to call even when a read-only connection is already open elsewhere.
    The db_metadata table is also lazily ensured by _ensure_db_metadata_table().
    """
    if os.path.exists(DB_PATH):
        return  # File already exists — schema was created on a previous boot

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
        con.execute("""
            CREATE TABLE IF NOT EXISTS db_metadata (
                key   VARCHAR NOT NULL PRIMARY KEY,
                value VARCHAR
            );
        """)
    finally:
        con.close()


def _ensure_db_metadata_table(con: duckdb.DuckDBPyConnection) -> None:
    """
    Creates the db_metadata table if it does not exist, using an already-open connection.
    Safe to call repeatedly — CREATE TABLE IF NOT EXISTS is idempotent.
    Follows the same pattern as _ensure_recommendations_table().
    """
    con.execute("""
        CREATE TABLE IF NOT EXISTS db_metadata (
            key   VARCHAR NOT NULL PRIMARY KEY,
            value VARCHAR
        );
    """)


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


def save_candles(
    symbol: str,
    df: pd.DataFrame,
    con: Optional[duckdb.DuckDBPyConnection] = None,
    overwrite: bool = True
) -> int:
    """
    Saves or updates candlestick data for a symbol in DuckDB.
    - overwrite=True: cleans up candidate aliases before inserting full history.
    - overwrite=False: directly inserts/replaces new candles without deleting prior history.
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
        if overwrite:
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


def upsert_candles(
    symbol: str,
    df: pd.DataFrame,
    con: Optional[duckdb.DuckDBPyConnection] = None
) -> int:
    """
    Directly upserts incremental EOD candles into DuckDB without reading or deleting existing history.
    Uses native INSERT OR REPLACE on PRIMARY KEY (symbol, timestamp).
    """
    return save_candles(symbol, df, con=con, overwrite=False)


def bulk_upsert_candles(
    records: List[Dict[str, Any]],
    con: Optional[duckdb.DuckDBPyConnection] = None,
) -> int:
    """
    High-performance bulk upsert: writes candles for MANY symbols in a single
    INSERT OR REPLACE transaction instead of one call per symbol.

    Args:
        records: List of dicts, each with keys:
                   'symbol' (str, Fyers format e.g. 'NSE:RELIANCE-EQ')
                   'df'     (pd.DataFrame with Date index and OHLCV columns)
        con:     Optional existing read-write DuckDB connection to reuse.

    Returns:
        Total number of rows written across all symbols.

    Performance vs upsert_candles():
        upsert_candles() = N register + N INSERT + N unregister calls.
        bulk_upsert_candles() = 1 register + 1 INSERT + 1 unregister call.
        For 200 symbols this is ~200× fewer DB round-trips.
    """
    if not records:
        return 0

    prepared = []
    for rec in records:
        symbol = rec["symbol"]
        df = rec["df"]
        if df is None or df.empty:
            continue

        # Normalise symbol to NSE: format
        s = symbol.strip().upper()
        sym = s if s.startswith("NSE:") else (f"NSE:{s}" if "-" in s else f"NSE:{s}-EQ")

        temp = df.copy()
        if "Date" in temp.columns:
            temp["Date"] = pd.to_datetime(temp["Date"])
            temp.set_index("Date", inplace=True)
        elif not isinstance(temp.index, pd.DatetimeIndex):
            temp.index = pd.to_datetime(temp.index)

        temp.reset_index(inplace=True)

        # Normalise column names
        col_map = {}
        for col in temp.columns:
            cl = str(col).lower()
            if cl in ("date", "timestamp", "epoch", "time", "datetime"):
                col_map[col] = "timestamp"
            elif cl == "open":
                col_map[col] = "open"
            elif cl == "high":
                col_map[col] = "high"
            elif cl == "low":
                col_map[col] = "low"
            elif cl == "close":
                col_map[col] = "close"
            elif cl in ("volume", "vol"):
                col_map[col] = "volume"
        temp.rename(columns=col_map, inplace=True)
        temp["symbol"] = sym

        for req in ("timestamp", "open", "high", "low", "close", "volume"):
            if req not in temp.columns:
                continue  # skip malformed symbols silently

        temp = temp[["symbol", "timestamp", "open", "high", "low", "close", "volume"]]
        temp.dropna(subset=["timestamp", "close"], inplace=True)
        temp.drop_duplicates(subset=["symbol", "timestamp"], keep="last", inplace=True)
        prepared.append(temp)

    if not prepared:
        return 0

    bulk_df = pd.concat(prepared, ignore_index=True)

    close_con = False
    if con is None:
        con = duckdb.connect(DB_PATH, read_only=False)
        close_con = True
    try:
        con.register("_bulk_upsert_view", bulk_df)
        con.execute("""
            INSERT OR REPLACE INTO candles (symbol, timestamp, open, high, low, close, volume)
            SELECT symbol, timestamp, open, high, low, close, volume
            FROM _bulk_upsert_view
        """)
        con.unregister("_bulk_upsert_view")
        return len(bulk_df)
    finally:
        if close_con:
            con.close()


def delete_symbol_candles(
    symbol: str,
    con: Optional[duckdb.DuckDBPyConnection] = None
) -> int:
    """
    Deletes all candle records for a symbol (and its alias candidates) from DuckDB.
    Returns the number of deleted rows.
    """
    init_db()
    candidates = normalize_symbol_candidates(symbol)
    placeholders = ", ".join(["?"] * len(candidates))
    close_con = False
    if con is None:
        con = duckdb.connect(DB_PATH, read_only=False)
        close_con = True
    try:
        cnt = con.execute(f"SELECT COUNT(*) FROM candles WHERE symbol IN ({placeholders})", candidates).fetchone()[0]
        if cnt > 0:
            con.execute(f"DELETE FROM candles WHERE symbol IN ({placeholders})", candidates)
        return cnt
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


# ============================================================
# DB Metadata  (last-updated tracking + 15:30 trading boundary)
# ============================================================

_META_LAST_UPDATED_KEY = "last_updated"  # ISO-8601 datetime string stored in db_metadata


def set_last_updated(
    ts: Optional[datetime] = None,
    con: Optional[duckdb.DuckDBPyConnection] = None,
) -> datetime:
    """
    Records the current (or supplied) timestamp as the DuckDB last-updated time.
    Uses INSERT OR REPLACE so it's idempotent.
    Returns the timestamp that was saved.
    """
    if ts is None:
        ts = datetime.now()
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S")

    close_con = False
    if con is None:
        con = duckdb.connect(DB_PATH, read_only=False)
        close_con = True
    try:
        _ensure_db_metadata_table(con)   # lazy table creation — no extra connection
        con.execute(
            "INSERT OR REPLACE INTO db_metadata (key, value) VALUES (?, ?)",
            [_META_LAST_UPDATED_KEY, ts_str],
        )
        return ts
    finally:
        if close_con:
            con.close()


def get_last_updated() -> Optional[datetime]:
    """
    Returns the last time DuckDB was updated, or None if never updated.
    Reads from the db_metadata table. Returns None if table doesn't exist yet
    (first boot before any update has run — is_db_current() handles this gracefully).
    Uses a read-only connection so it never conflicts with any other open connection.
    """
    try:
        con = get_connection(read_only=True)
        try:
            row = con.execute(
                "SELECT value FROM db_metadata WHERE key = ?", [_META_LAST_UPDATED_KEY]
            ).fetchone()
            if row and row[0]:
                return datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%S")
            return None
        finally:
            con.close()
    except Exception:
        return None


def get_current_eod_session(now: Optional[datetime] = None) -> datetime:
    """
    Returns the datetime of the START of the current EOD session boundary.

    NSE closes at 15:30 IST each day.  EOD data for a trading day is finalised
    only AFTER 15:30 of that day.

    Rules:
      - If current time >= 15:30 today  →  EOD session boundary = 15:30 today
      - If current time <  15:30 today  →  EOD session boundary = 15:30 yesterday

    This boundary is the point-in-time AFTER which a DB update is considered
    "fresh" for the current available EOD data.
    """
    if now is None:
        now = datetime.now()
    market_close_today = now.replace(
        hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0, microsecond=0
    )
    if now >= market_close_today:
        # Today's EOD data is available
        return market_close_today
    else:
        # Today's market is still open (or not yet open); last EOD was yesterday
        return market_close_today - timedelta(days=1)


def is_db_current(now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Checks whether the DuckDB last-updated timestamp is fresh enough to skip
    a re-fetch, based on the NSE 15:30 trading boundary.

    Returns a dict with:
        {
            'current':          bool   — True = DB is already up-to-date, skip fetch
            'last_updated':     datetime | None
            'eod_boundary':     datetime  — the 15:30 session boundary
            'reason':           str   — human-readable explanation
        }

    Logic:
        - If last_updated is None             → not current (never updated)
        - If last_updated >= eod_boundary     → current (already have latest EOD)
        - Else                                → not current (needs update)
    """
    if now is None:
        now = datetime.now()

    last_updated  = get_last_updated()
    eod_boundary  = get_current_eod_session(now)

    if last_updated is None:
        return {
            "current":      False,
            "last_updated": None,
            "eod_boundary": eod_boundary,
            "reason":       "Database has never been updated.",
        }

    if last_updated >= eod_boundary:
        return {
            "current":      True,
            "last_updated": last_updated,
            "eod_boundary": eod_boundary,
            "reason": (
                f"DB last updated {last_updated.strftime('%d %b %Y %H:%M')} — "
                f"already after EOD boundary {eod_boundary.strftime('%d %b %Y %H:%M')}. "
                f"No update needed."
            ),
        }

    return {
        "current":      False,
        "last_updated": last_updated,
        "eod_boundary": eod_boundary,
        "reason": (
            f"DB last updated {last_updated.strftime('%d %b %Y %H:%M')} — "
            f"stale (EOD boundary is {eod_boundary.strftime('%d %b %Y %H:%M')}). "
            f"Update required."
        ),
    }


# ============================================================
# Recommendations History  (tracks first-seen EXCELLENT stock)
# ============================================================

RECOMMENDATIONS_HISTORY_DDL = """
    CREATE TABLE IF NOT EXISTS recommendations_history (
        symbol          VARCHAR NOT NULL,
        first_seen_date DATE    NOT NULL,
        first_seen_price DOUBLE,
        source          VARCHAR,
        PRIMARY KEY (symbol)
    );
"""


def _ensure_recommendations_table(con: duckdb.DuckDBPyConnection) -> None:
    """Creates recommendations_history table if it does not exist (idempotent)."""
    con.execute(RECOMMENDATIONS_HISTORY_DDL)


def upsert_excellent_recommendation(
    symbol: str,
    seen_date: date,
    seen_price: Optional[float],
    source: str = "dryrun",
    con: Optional[duckdb.DuckDBPyConnection] = None,
) -> bool:
    """
    Inserts a new row for an EXCELLENT stock if not already tracked.
    Does NOT update existing rows — preserves the *first* seen date forever.
    Returns True if a new record was inserted, False if already existed.
    """
    close_con = False
    if con is None:
        con = duckdb.connect(DB_PATH, read_only=False)
        close_con = True
    try:
        _ensure_recommendations_table(con)
        existing = con.execute(
            "SELECT 1 FROM recommendations_history WHERE symbol = ?", [symbol]
        ).fetchone()
        if existing:
            return False  # Already tracked — preserve first-seen date
        con.execute(
            """
            INSERT INTO recommendations_history (symbol, first_seen_date, first_seen_price, source)
            VALUES (?, ?, ?, ?)
            """,
            [symbol, seen_date, seen_price, source],
        )
        return True
    finally:
        if close_con:
            con.close()


def batch_upsert_excellent_recommendations(
    records: List[Dict[str, Any]],
    source: str = "dryrun",
) -> int:
    """
    Efficiently upserts a batch of EXCELLENT stocks into recommendations_history.
    Each dict must have: {'symbol': str, 'date': date, 'price': float|None}
    Returns count of NEW records inserted.
    """
    if not records:
        return 0

    con = duckdb.connect(DB_PATH, read_only=False)
    try:
        _ensure_recommendations_table(con)

        # Fetch already-tracked symbols in one query
        existing = {
            row[0]
            for row in con.execute("SELECT symbol FROM recommendations_history").fetchall()
        }

        inserted = 0
        for rec in records:
            sym = rec.get("symbol", "").strip().upper()
            if not sym or sym in existing:
                continue
            con.execute(
                """
                INSERT INTO recommendations_history (symbol, first_seen_date, first_seen_price, source)
                VALUES (?, ?, ?, ?)
                """,
                [sym, rec.get("date", date.today()), rec.get("price"), source],
            )
            existing.add(sym)
            inserted += 1

        return inserted
    finally:
        con.close()


def get_recommendations_history() -> Dict[str, Dict[str, Any]]:
    """
    Returns a dict keyed by symbol with first_seen_date (as DD/MM string) and first_seen_price.
    Example: {'RELIANCE': {'first_seen_date': '04/09', 'first_seen_price': 2450.5, 'source': 'nsescan'}}
    """
    init_db()
    con = get_connection(read_only=True)
    try:
        # Ensure table exists in read path (first boot scenario)
        try:
            rows = con.execute(
                "SELECT symbol, first_seen_date, first_seen_price, source FROM recommendations_history"
            ).fetchall()
        except Exception:
            return {}
        result = {}
        for symbol, fsd, fsp, src in rows:
            if fsd:
                if hasattr(fsd, 'strftime'):
                    date_str = fsd.strftime("%d/%m")
                else:
                    # Handle string dates like '2026-09-04'
                    try:
                        date_str = datetime.strptime(str(fsd)[:10], "%Y-%m-%d").strftime("%d/%m")
                    except Exception:
                        date_str = str(fsd)[:5]
            else:
                date_str = datetime.today().strftime("%d/%m")
            result[symbol] = {
                "first_seen_date": date_str,
                "first_seen_price": round(float(fsp), 2) if fsp else None,
                "source": src,
            }
        return result
    finally:
        con.close()


def _get_close_price_on_date(symbol: str, target_date: date, con: duckdb.DuckDBPyConnection) -> Optional[float]:
    """Returns the closing price for a symbol on (or nearest before) target_date from DuckDB candles."""
    candidates = normalize_symbol_candidates(symbol)
    placeholders = ", ".join(["?"] * len(candidates))
    target_ts = datetime.combine(target_date, datetime.min.time())
    row = con.execute(
        f"""
        SELECT close FROM candles
        WHERE symbol IN ({placeholders})
          AND DATE_TRUNC('day', timestamp) = DATE_TRUNC('day', ?)
        ORDER BY timestamp DESC LIMIT 1
        """,
        candidates + [target_ts],
    ).fetchone()
    return float(row[0]) if row else None


def init_recommendations_history_from_files(results_dir: str) -> Dict[str, Any]:
    """
    Backfills recommendations_history from existing Results/*.txt files.
    Parses all dryrun and nsescan result files chronologically (oldest first)
    so the earliest file wins (first_seen semantics are preserved).

    Returns summary: {'inserted': int, 'files_scanned': int, 'errors': []}
    """
    pattern = re.compile(
        r"^(\d+)\.\s+([A-Z0-9_\-&]+)\s+[—\-]\s+(\d+)%\s+([A-Z]+)", re.MULTILINE
    )
    EXCELLENT_THRESHOLD = 80

    # Gather all result files, sort oldest-first so first_seen is accurate
    all_files = sorted(
        glob.glob(os.path.join(results_dir, "*-dryrun-results.txt"))
        + glob.glob(os.path.join(results_dir, "*-nsescan-results.txt")),
        key=os.path.getmtime,
    )

    if not all_files:
        return {"inserted": 0, "files_scanned": 0, "errors": []}

    errors = []
    # Use ONE read-write connection for everything (DuckDB allows only one connection at a time)
    con = duckdb.connect(DB_PATH, read_only=False)

    try:
        _ensure_recommendations_table(con)

        existing = {
            row[0]
            for row in con.execute(
                "SELECT symbol FROM recommendations_history"
            ).fetchall()
        }

        inserted = 0

        for fpath in all_files:
            fname = os.path.basename(fpath)
            source = "nsescan" if "nsescan" in fname else "dryrun"

            # Derive date from file modification time
            mtime = os.path.getmtime(fpath)
            file_date = datetime.fromtimestamp(mtime).date()

            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
            except Exception as e:
                errors.append(f"{fname}: {e}")
                continue

            # Parse all EXCELLENT symbols from file
            for m in pattern.finditer(content):
                sym = m.group(2).strip().upper()
                score = int(m.group(3))
                if score < EXCELLENT_THRESHOLD:
                    continue
                if sym in existing:
                    continue

                # Try to get price from DuckDB candles on that date (using same rw connection)
                price = None
                try:
                    price = _get_close_price_on_date(sym, file_date, con)
                except Exception:
                    pass

                try:
                    con.execute(
                        """
                        INSERT INTO recommendations_history (symbol, first_seen_date, first_seen_price, source)
                        VALUES (?, ?, ?, ?)
                        """,
                        [sym, file_date, price, source],
                    )
                    existing.add(sym)
                    inserted += 1
                except Exception as e:
                    errors.append(f"{sym} @ {fname}: {e}")

    finally:
        con.close()

    return {"inserted": inserted, "files_scanned": len(all_files), "errors": errors}

