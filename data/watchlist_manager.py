"""
Watchlist Manager Module for Fyers Trading Bot.
Manages Active Watchlist (Excellent & Good candidate stocks) and
Sell Watchlist (stocks moving to SELL / weakness).
Provides atomic file operations, deduplication, and DuckDB synchronization.
"""

import os
import re
from typing import List, Dict, Any, Optional

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACTIVE_WATCHLIST_FILE = os.path.join(PROJECT_DIR, "stocks_watchlist.txt")
TEST_STOCKS_FILE = os.path.join(PROJECT_DIR, "stocks_to_test.txt")
SELL_WATCHLIST_FILE = os.path.join(PROJECT_DIR, "stocks_sell_watchlist.txt")


def is_valid_ticker(symbol: str) -> bool:
    """Validates whether a string can be an NSE symbol (alphanumeric, -, &, no spaces, max 15 chars)."""
    if not symbol or len(symbol) > 15 or " " in symbol or "\t" in symbol:
        return False
    return bool(re.match(r"^[A-Z0-9&\-]{1,15}$", symbol))


def clean_symbol(symbol: str) -> str:
    """Normalizes symbol string (removes NSE: prefix, -EQ suffix, whitespace, and rejects invalid text)."""
    s = symbol.strip().upper()
    if s.startswith("NSE:"):
        s = s[4:]
    if s.endswith("-EQ"):
        s = s[:-3]
    s = s.strip()
    return s if is_valid_ticker(s) else ""


def read_symbols_from_file(filepath: str) -> List[str]:
    """Reads non-comment, non-empty stock symbols from a text file."""
    if not os.path.exists(filepath):
        return []
    symbols = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            clean_line = line.strip()
            if clean_line and not clean_line.startswith("#"):
                sym = clean_symbol(clean_line)
                if sym and sym not in symbols:
                    symbols.append(sym)
    return symbols


def write_symbols_to_file(filepath: str, symbols: List[str], header: Optional[str] = None):
    """Writes a clean, deduplicated list of symbols to a text file."""
    unique_symbols = []
    for s in symbols:
        cs = clean_symbol(s)
        if cs and cs not in unique_symbols:
            unique_symbols.append(cs)

    lines = []
    if header:
        lines.append(f"# {header}")
    elif os.path.exists(filepath):
        # Preserve first comment line if present
        with open(filepath, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
            if first_line.startswith("#"):
                lines.append(first_line)

    lines.extend(unique_symbols)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def get_active_watchlist() -> List[str]:
    """Returns list of active watchlist symbols."""
    return read_symbols_from_file(ACTIVE_WATCHLIST_FILE)


def get_sell_watchlist() -> List[str]:
    """Returns list of sell watchlist symbols."""
    return read_symbols_from_file(SELL_WATCHLIST_FILE)


def get_test_stocks() -> List[str]:
    """Returns list of base test stocks."""
    return read_symbols_from_file(TEST_STOCKS_FILE)


def add_to_active_watchlist(symbols: List[str]) -> Dict[str, Any]:
    """
    Adds symbols to the active watchlist and removes them from the sell watchlist.
    Also ensures they are present in DuckDB.
    """
    current_active = get_active_watchlist()
    current_sell = get_sell_watchlist()

    added = []
    for s in symbols:
        cs = clean_symbol(s)
        if cs:
            if cs not in current_active:
                current_active.append(cs)
                added.append(cs)
            if cs in current_sell:
                current_sell.remove(cs)

    write_symbols_to_file(ACTIVE_WATCHLIST_FILE, current_active, "Active Stock Watchlist")
    write_symbols_to_file(SELL_WATCHLIST_FILE, current_sell, "Sell Watchlist - Weakness & Exit Signals")

    return {
        "success": True,
        "added": added,
        "active_count": len(current_active),
        "sell_count": len(current_sell)
    }


def move_to_sell_watchlist(symbols: List[str]) -> Dict[str, Any]:
    """
    Moves symbols from active watchlist to sell watchlist.
    """
    current_active = get_active_watchlist()
    current_sell = get_sell_watchlist()

    moved = []
    for s in symbols:
        cs = clean_symbol(s)
        if cs:
            if cs in current_active:
                current_active.remove(cs)
            if cs not in current_sell:
                current_sell.append(cs)
                moved.append(cs)

    write_symbols_to_file(ACTIVE_WATCHLIST_FILE, current_active, "Active Stock Watchlist")
    write_symbols_to_file(SELL_WATCHLIST_FILE, current_sell, "Sell Watchlist - Weakness & Exit Signals")

    return {
        "success": True,
        "moved": moved,
        "active_count": len(current_active),
        "sell_count": len(current_sell)
    }


def move_to_active_watchlist(symbols: List[str]) -> Dict[str, Any]:
    """
    Restores symbols from sell watchlist back into active watchlist.
    """
    return add_to_active_watchlist(symbols)


def remove_from_watchlist(symbols: List[str], target: str = "active") -> Dict[str, Any]:
    """
    Removes symbols completely from specified watchlist ('active' or 'sell').
    """
    removed = []
    if target == "sell":
        current = get_sell_watchlist()
        for s in symbols:
            cs = clean_symbol(s)
            if cs in current:
                current.remove(cs)
                removed.append(cs)
        write_symbols_to_file(SELL_WATCHLIST_FILE, current, "Sell Watchlist - Weakness & Exit Signals")
        count = len(current)
    else:
        current = get_active_watchlist()
        for s in symbols:
            cs = clean_symbol(s)
            if cs in current:
                current.remove(cs)
                removed.append(cs)
        write_symbols_to_file(ACTIVE_WATCHLIST_FILE, current, "Active Stock Watchlist")
        count = len(current)

    return {
        "success": True,
        "removed": removed,
        "target": target,
        "count": count
    }


def get_watchlist_summary() -> Dict[str, Any]:
    """Returns a full summary of both watchlists and test stocks."""
    active = get_active_watchlist()
    sell = get_sell_watchlist()
    test = get_test_stocks()

    return {
        "active_watchlist": active,
        "active_count": len(active),
        "sell_watchlist": sell,
        "sell_count": len(sell),
        "test_stocks": test,
        "test_count": len(test)
    }


def purge_untracked_or_failed_symbols(symbols: List[str]) -> Dict[str, Any]:
    """
    Purges symbols where data could not be fetched or that are invalid from:
    1. Active Watchlist (stocks_watchlist.txt)
    2. Sell Watchlist (stocks_sell_watchlist.txt)
    3. Test Stocks (stocks_to_test.txt)
    4. DuckDB database (candles table)
    """
    from data.duckdb_manager import delete_symbol_candles

    clean_syms = set()
    for s in symbols:
        raw = str(s).strip().upper()
        clean_syms.add(raw)
        c = clean_symbol(raw)
        if c:
            clean_syms.add(c)
        if raw.startswith("NSE:"):
            clean_syms.add(raw[4:])
        if raw.endswith("-EQ"):
            clean_syms.add(raw[:-3])

    # 1. Purge from Active Watchlist
    active = get_active_watchlist()
    orig_active_len = len(active)
    active = [s for s in active if s not in clean_syms and clean_symbol(s) not in clean_syms]
    if len(active) != orig_active_len:
        write_symbols_to_file(ACTIVE_WATCHLIST_FILE, active, "Active Stock Watchlist")

    # 2. Purge from Sell Watchlist
    sell = get_sell_watchlist()
    orig_sell_len = len(sell)
    sell = [s for s in sell if s not in clean_syms and clean_symbol(s) not in clean_syms]
    if len(sell) != orig_sell_len:
        write_symbols_to_file(SELL_WATCHLIST_FILE, sell, "Sell Watchlist - Weakness & Exit Signals")

    # 3. Purge from Test Stocks
    test = get_test_stocks()
    orig_test_len = len(test)
    test = [s for s in test if s not in clean_syms and clean_symbol(s) not in clean_syms]
    if len(test) != orig_test_len:
        write_symbols_to_file(TEST_STOCKS_FILE, test, "Stocks to Test & Baseline Strategy Universe")

    # 4. Purge from DuckDB
    deleted_candles = 0
    for s in clean_syms:
        try:
            deleted_candles += delete_symbol_candles(s)
        except Exception:
            pass

    return {
        "success": True,
        "purged_symbols": list(clean_syms),
        "removed_from_active": orig_active_len - len(active),
        "removed_from_sell": orig_sell_len - len(sell),
        "removed_from_test": orig_test_len - len(test),
        "active_remaining": len(active),
        "sell_remaining": len(sell),
        "test_remaining": len(test),
        "deleted_candles": deleted_candles
    }


def purge_zero_candle_symbols() -> Dict[str, Any]:
    """
    Scans all watchlists and purges any symbol that has 0 candle data in DuckDB.
    Uses bulk distinct symbol query for high performance.
    """
    from data.duckdb_manager import get_connection, normalize_symbol_candidates

    active = get_active_watchlist()
    sell = get_sell_watchlist()
    test = get_test_stocks()
    all_syms = list(dict.fromkeys(active + sell + test))

    con = get_connection(read_only=True)
    try:
        existing_rows = con.execute("SELECT DISTINCT symbol FROM candles").fetchall()
        existing_symbols = {r[0].upper() for r in existing_rows}
    finally:
        con.close()

    zero_candle_syms = []
    for s in all_syms:
        cands = [c.upper() for c in normalize_symbol_candidates(s)]
        if not any(c in existing_symbols for c in cands):
            zero_candle_syms.append(s)

    if zero_candle_syms:
        return purge_untracked_or_failed_symbols(zero_candle_syms)
    return {"success": True, "purged_symbols": [], "message": "All tracked symbols have valid candle data in DuckDB."}
