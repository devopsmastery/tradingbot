"""
Full NSE Exchange Scan -- Discover BUY signals from Small & Mid Cap NSE stocks.

Two-phase approach for efficiency:
  Phase 1: Download NSE CM symbol master (Fyers public URL)
           Filter: EQ segment + Small Cap (2.0-2.9) + Mid Cap (3.0-3.4)
           Exclude: Already in your watchlist/portfolio
           Result: ~500-1000 discovery symbols

  Phase 2: For discovery symbols, fetch live quotes in batches (50/request)
           Pre-filter: Near 52-week high + minimum volume
           For pre-filtered candidates: fetch 60-day history (cached where possible)
           Run Keltner Tuned strategy -> show DISCOVERY BUY signals

Symbol master column layout (NSE_CM.csv):
  Col 0:  Fytoken
  Col 1:  Company name
  Col 9:  Symbol ticker (e.g. NSE:RELIANCE-EQ)
  Col 19: Tradable flag (1=yes, 0=no)
  Col 20: Market cap tier (2.x=smallcap, 3.0-3.4=midcap, 3.5+=largecap, 0=unclassified)

Usage:
    python scripts/full_exchange_scan.py
    python scripts/full_exchange_scan.py --force-refresh   (re-download symbol master)
"""

import os
import sys
import time
import json
import argparse
import requests
import pandas as pd
from datetime import datetime
from io import StringIO

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from dotenv import load_dotenv
load_dotenv()

from live_trading.fyers_auth import get_access_token, FYERS_APP_ID
from data.data_fetcher import (
    to_fyers_symbol, fetch_historical_data, save_historical_data,
    load_historical_csv, read_stocks, append_live_quote
)
from live_trading.execute_trades import (
    compute_indicators, generate_signal, quality_bar, quality_label, Colors, colored
)

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
STOCKS_FILE       = os.path.join(PROJECT_DIR, 'stocks_to_test.txt')
WATCHLIST_FILE    = os.path.join(PROJECT_DIR, 'stocks_watchlist.txt')
PORTFOLIO_DB_FILE = os.path.join(PROJECT_DIR, 'data', 'portfolio_db.json')
NSE_SYMBOLS_CACHE = os.path.join(PROJECT_DIR, 'data', 'nse_smallmid_symbols.txt')

# Fyers public symbol master (no auth required)
NSE_CM_SYMBOL_MASTER_URL = "https://public.fyers.in/sym_details/NSE_CM.csv"

# Cap tier range: 2.0-2.9 = small cap, 3.0-3.4 = mid cap
SMALLCAP_MIN = 2.0
MIDCAP_MAX   = 3.4

# Pre-filter thresholds for live quote screening
MIN_PRICE            = 10.0    # Skip sub-Rs.10 penny stocks
MIN_VOLUME           = 10000   # Min today's traded volume
NEAR_52W_HIGH_RATIO  = 0.88   # Must be within 12% of 52-week high (momentum filter)

BATCH_SIZE = 15  # Fyers quotes API batch size for reliability


# ============================================================
# Helpers
# ============================================================

def load_portfolio() -> dict:
    try:
        with open(PORTFOLIO_DB_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_known_universe() -> set:
    """Returns set of raw symbols already tracked (scan lists + portfolio)."""
    known = set()
    for f in [STOCKS_FILE, WATCHLIST_FILE]:
        if os.path.exists(f):
            known.update(read_stocks(f))
    known.update(load_portfolio().keys())
    return known


def fetch_nse_smallmid_symbols(force_refresh: bool = False) -> list:
    """
    Downloads the Fyers NSE_CM symbol master and filters for:
    - EQ segment (-EQ suffix)
    - Small cap (cap tier 2.0-2.9) or Mid cap (cap tier 3.0-3.4)
    - Tradable (col 19 == 1)

    Caches result to data/nse_smallmid_symbols.txt (refreshed daily).
    Returns list of plain symbol names e.g. ['STALLION', 'SKYGOLD', ...].
    """
    # Use cache if fresh (less than 24 hours old)
    if not force_refresh and os.path.exists(NSE_SYMBOLS_CACHE):
        age_hours = (time.time() - os.path.getmtime(NSE_SYMBOLS_CACHE)) / 3600
        if age_hours < 24:
            with open(NSE_SYMBOLS_CACHE, 'r') as f:
                symbols = [line.strip() for line in f if line.strip()]
            print(f"  [cache] {len(symbols)} small+mid cap NSE symbols loaded "
                  f"({age_hours:.0f}h old).")
            return symbols

    print("  Downloading NSE symbol master from Fyers public API...")
    try:
        resp = requests.get(NSE_CM_SYMBOL_MASTER_URL, timeout=30)
        resp.raise_for_status()
        content = resp.text
    except Exception as e:
        print(colored(f"  [!] Download failed: {e}", Colors.RED))
        # Fall back to stale cache if available
        if os.path.exists(NSE_SYMBOLS_CACHE):
            with open(NSE_SYMBOLS_CACHE, 'r') as f:
                symbols = [l.strip() for l in f if l.strip()]
            print(colored(f"  Using stale cache: {len(symbols)} symbols.", Colors.YELLOW))
            return symbols
        return []

    symbols = []
    skipped_notradable = 0
    skipped_largecap   = 0
    skipped_micro      = 0

    for line in content.splitlines():
        parts = line.strip().split(',')
        if len(parts) < 21:
            continue

        ticker   = parts[9].strip()    # e.g. NSE:RELIANCE-EQ
        tradable = parts[19].strip()   # 1 or 0
        cap_str  = parts[20].strip()   # e.g. 2.0, 3.2, 0.0

        # Only EQ segment
        if not ticker.startswith('NSE:') or not ticker.endswith('-EQ'):
            continue

        # Must be tradable
        if tradable != '1':
            skipped_notradable += 1
            continue

        # Parse cap tier
        try:
            cap = float(cap_str)
        except ValueError:
            continue

        # Small cap (2.0-2.9) or Mid cap (3.0-3.4)
        if SMALLCAP_MIN <= cap <= MIDCAP_MAX:
            plain = ticker.replace('NSE:', '').replace('-EQ', '')
            symbols.append(plain)
        elif cap >= 3.5:
            skipped_largecap += 1
        elif cap < SMALLCAP_MIN and cap > 0:
            skipped_micro += 1

    # Cache to file
    os.makedirs(os.path.dirname(NSE_SYMBOLS_CACHE), exist_ok=True)
    with open(NSE_SYMBOLS_CACHE, 'w') as f:
        f.write('\n'.join(symbols))

    print(f"  [OK] Found {len(symbols)} small+mid cap EQ symbols "
          f"(skipped: {skipped_largecap} large, {skipped_micro} micro, "
          f"{skipped_notradable} non-tradable).")
    return symbols


def batch_fetch_quotes(symbols: list, access_token: str) -> dict:
    """
    Batch fetch live quotes for a list of plain symbol names.
    Fyers allows up to 50 symbols per request.

    Returns: {plain_symbol: quote_dict}
    e.g. {'STALLION': {'lp': 243.46, 'volume': 150000, '52_week_high': 280.0, ...}}
    """
    headers = {'Authorization': f'{FYERS_APP_ID}:{access_token}'}
    all_quotes = {}
    total = len(symbols)

    for i in range(0, total, BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        fyers_syms = ','.join(to_fyers_symbol(s) for s in batch)
        url = f'https://api-t1.fyers.in/data/quotes?symbols={fyers_syms}'

        retries = 3
        for attempt in range(retries):
            try:
                resp = requests.get(url, headers=headers, timeout=10)
                if resp.status_code == 429:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                data = resp.json()
                if data.get('s') == 'ok':
                    for item in data.get('d', []):
                        if item.get('s') == 'ok':
                            v = item.get('v', {})
                            raw = item.get('n', '')
                            # Strip NSE: prefix and -EQ suffix
                            plain = raw.replace('NSE:', '').replace('-EQ', '').strip()
                            all_quotes[plain] = v
                    break
            except Exception:
                time.sleep(1.0)

        time.sleep(0.6)

        done = min(i + BATCH_SIZE, total)
        print(f"  Fetching quotes: {done:>4}/{total} ...", end='\r', flush=True)

    print(f"  Fetching quotes: {total}/{total} - done.        ")
    return all_quotes


def pre_filter_candidates(quotes: dict, known_universe: set) -> list:
    """
    Pre-filter live quotes to identify discovery candidates using simple
    momentum signals: near 52-week high + minimum volume + minimum price.

    Returns list of plain symbols that pass the filter.
    """
    candidates = []
    for symbol, v in quotes.items():
        # Skip already-tracked stocks (handled by regular dry run)
        if symbol in known_universe:
            continue

        lp       = v.get('lp') or v.get('close_price') or 0
        volume   = v.get('volume') or 0
        high_52w = v.get('52_week_high') or v.get('high_price') or 0

        if lp < MIN_PRICE:
            continue
        if volume < MIN_VOLUME:
            continue
        # Must be near 52-week high (our Keltner strategy works best at breakouts)
        if high_52w > 0 and (lp / high_52w) >= NEAR_52W_HIGH_RATIO:
            candidates.append(symbol)

    return candidates


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='Full NSE Small+Mid Cap Scan')
    parser.add_argument('--force-refresh', action='store_true',
                        help='Force re-download of NSE symbol master')
    parser.add_argument('--strategy', '-s', type=str, default='1',
                        help='Strategy ID (default: 1)')
    parser.add_argument('strategy_pos', nargs='?', type=str, default=None,
                        help='Positional strategy ID e.g. 6')
    args = parser.parse_args()

    strat_id = args.strategy_pos if args.strategy_pos else args.strategy
    if str(strat_id) == '6':
        os.environ["STRATEGY_ID"] = "6"
        strat_title = "Strategy 6 (Keltner 5-Rule Retracement)"
    else:
        os.environ["STRATEGY_ID"] = "1"
        strat_title = "Strategy 1 (Keltner Tuned - Winning Strategy)"

    print()
    print(colored('=' * 70, Colors.CYAN))
    print(colored(f'  FULL NSE SMALL + MID CAP SCAN  [{strat_title}]', Colors.BOLD + Colors.CYAN))
    print(colored(f'  {datetime.now().strftime("%A, %d %B %Y  %H:%M:%S")}', Colors.CYAN))
    print(colored('=' * 70, Colors.CYAN))
    print()
    print('  Scans small cap + mid cap NSE stocks outside your current universe.')
    print('  Phase 1: Symbol master download + batch live quotes (fast)')
    print('  Phase 2: Strategy evaluation on pre-filtered candidates (moderate)')
    print()

    access_token   = get_access_token()
    portfolio      = load_portfolio()
    known_universe = load_known_universe()

    # ---- Phase 1a: Get small+mid cap NSE symbols ----
    print(colored('  [Phase 1a] Loading NSE small+mid cap symbol list...', Colors.CYAN))
    all_sm_symbols = fetch_nse_smallmid_symbols(force_refresh=args.force_refresh)
    if not all_sm_symbols:
        print(colored('  [!] No symbols loaded. Aborting.', Colors.RED))
        return

    # Discovery = small+mid symbols NOT in known universe
    discovery_symbols = [s for s in all_sm_symbols if s not in known_universe]

    print()
    print(f'  Small+Mid cap universe : {len(all_sm_symbols):>5} stocks')
    print(f'  Already tracked by you : {len(known_universe):>5} (watchlist + portfolio)')
    print(f'  New discovery targets  : {len(discovery_symbols):>5}')
    print()

    if not discovery_symbols:
        print(colored('  All small+mid cap stocks are already in your universe!', Colors.YELLOW))
        return

    # ---- Phase 1b: Batch live quotes ----
    print(colored('  [Phase 1b] Fetching live quotes for discovery symbols...', Colors.CYAN))
    quotes = batch_fetch_quotes(discovery_symbols, access_token)
    print(f'  Got quotes for {len(quotes)} / {len(discovery_symbols)} symbols.')
    print()

    # ---- Phase 1c: Pre-filter momentum candidates ----
    candidates = pre_filter_candidates(quotes, known_universe)
    
    # Fallback: if quotes API rate limited or returned 0, check cached CSVs for discovery symbols
    if not candidates:
        print(colored('  [!] Quote API returned 0 quotes (or rate limited). Checking cached historical data for candidates...', Colors.YELLOW))
        cached_candidates = []
        for symbol in discovery_symbols:
            fyers_sym = to_fyers_symbol(symbol)
            try:
                df = load_historical_csv(fyers_sym)
                if not df.empty and len(df) >= 20:
                    lp = df.iloc[-1]['Close']
                    high_52w = df['High'].max()
                    vol = df.iloc[-1]['Volume']
                    if lp >= MIN_PRICE and (high_52w == 0 or (lp / high_52w) >= NEAR_52W_HIGH_RATIO):
                        cached_candidates.append(symbol)
            except Exception:
                pass
        candidates = cached_candidates

    print(f'  Pre-filter (price > Rs.{MIN_PRICE}, volume > {MIN_VOLUME:,}, '
          f'within {int((1-NEAR_52W_HIGH_RATIO)*100)}% of 52W high):')
    print(f'  {len(candidates)} candidates selected for full Keltner analysis.')
    print()

    if not candidates:
        print(colored('  No momentum candidates found in the small+mid cap universe today.', Colors.YELLOW))
        print('  This usually means the market is in a broad consolidation phase or new discovery symbols need data fetch.')
        print('  Tip: Run Option 2 (Update Data) to fetch fresh historical data for your universe.')
        return

    # ---- Phase 2: Full Keltner scan on candidates ----
    print(colored(f'  [Phase 2] Running Keltner strategy on {len(candidates)} candidates...', Colors.CYAN))
    print()

    discoveries = []  # (symbol, close, score, reasons, quote_data)
    cache_hits  = 0
    api_fetches = 0
    errors      = 0

    for i, symbol in enumerate(candidates, 1):
        fyers_sym = to_fyers_symbol(symbol)
        progress  = colored(f'[{i:3d}/{len(candidates)}]', Colors.DIM)

        try:
            # Try cached historical CSV first (much faster, no API call)
            try:
                df = load_historical_csv(fyers_sym)
                cache_hits += 1
            except FileNotFoundError:
                df = fetch_historical_data(fyers_sym, access_token, days=60)
                save_historical_data(fyers_sym, df)
                api_fetches += 1

            # Overlay today's live quote (real-time price)
            df = append_live_quote(df, fyers_sym, access_token)
            df = compute_indicators(df)
            signal, score, reasons = generate_signal(df)

            latest_close = df.iloc[-1]['Close']

            if signal == 'BUY':
                discoveries.append((symbol, latest_close, score, reasons))
                tag = colored('DISCOVERY', Colors.BOLD + Colors.GREEN)
                print(f'  {progress} [{tag}] {symbol:20s}  Rs.{latest_close:>9.2f}  {quality_bar(score)}')
            else:
                tag = colored(signal, Colors.DIM)
                print(f'  {progress}  {tag:4s}        {symbol:20s}  Rs.{latest_close:>9.2f}')

        except Exception as e:
            errors += 1
            print(f'  {progress}  ERR         {symbol:20s}  {str(e)[:45]}')

        time.sleep(0.1)

    # ---- Discovery Dashboard ----
    print()
    print(colored('=' * 70, Colors.GREEN))
    print(colored('  DISCOVERY BUY SIGNALS  --  New Stocks Outside Your Universe', Colors.BOLD + Colors.GREEN))
    print(colored('=' * 70, Colors.GREEN))
    print(f'  Candidates scanned  : {len(candidates)}')
    print(f'  Cache hits          : {cache_hits} (instant)')
    print(f'  API fetches         : {api_fetches} (downloaded fresh)')
    if errors:
        print(f'  Errors              : {errors}')

    if not discoveries:
        print()
        print(colored('  No discovery BUY signals today.', Colors.YELLOW))
        print('  Small+mid cap momentum stocks are either in consolidation')
        print('  or already in your watchlist/portfolio.')
    else:
        discoveries.sort(key=lambda x: x[2], reverse=True)
        print()
        print(f'  Found {colored(str(len(discoveries)), Colors.BOLD + Colors.GREEN)} discovery signals'
              f' (sorted by quality):\n')

        for idx, (symbol, close, score, reasons) in enumerate(discoveries, 1):
            brief = '  .  '.join(reasons[:3])
            print(f'  {colored(str(idx) + ".", Colors.BOLD)} '
                  f'{colored(symbol, Colors.BOLD + Colors.GREEN):28s} '
                  f'Rs.{close:>9.2f}   {quality_bar(score)}  {quality_label(score)}')
            print(f'     {colored(">", Colors.GREEN)} {brief}')
            print()

        # Save to Results/dd-mm-hh-nsescan-results.txt
        results_dir = os.path.join(PROJECT_DIR, "Results")
        os.makedirs(results_dir, exist_ok=True)
        now = datetime.now()
        results_filename = f"{now.strftime('%d-%m-%H')}-nsescan-results.txt"
        results_filepath = os.path.join(results_dir, results_filename)

        try:
            lines = []
            excellent_good_discoveries = [
                (symbol, score)
                for symbol, close, score, reasons in discoveries
                if score >= 65
            ]
            if excellent_good_discoveries:
                lines.append("DISCOVERY BUY SIGNALS:")
                for idx, (symbol, score) in enumerate(excellent_good_discoveries, 1):
                    label = quality_label(score, plain=True)
                    lines.append(f"{idx}. {symbol} — {score}% {label}")

            with open(results_filepath, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            print(f"  {colored('Results saved:', Colors.GREEN)} Results/{results_filename}")
        except Exception as e:
            print(f"  {colored('Results File Error:', Colors.RED)} {e}")

    print(colored('  ' + '-' * 68, Colors.DIM))
    print(f'  Tip: To track an interesting discovery, add its symbol to')
    print(f'       stocks_watchlist.txt and run Option 2 (Update Data).')
    print()


if __name__ == '__main__':
    main()
