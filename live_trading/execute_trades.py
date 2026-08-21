"""
Fyers Live Trading Script: Keltner Tuned (ATR 2.0 + EMA 10/21)

This script:
1. Authenticates with Fyers API
2. Fetches latest daily candle data for stocks in stocks_to_test.txt + stocks_watchlist.txt
   (also includes all portfolio holdings so ADD MORE signals are never missed)
3. Computes Keltner Channel (ATR 2.0) and EMA 10/21 indicators using live prices
4. Generates color-coded BUY/SELL signals with quality scores
5. Labels BUY signals as ADD MORE (already held) or NEW BUY (fresh entry)
6. Places orders via Fyers API only when DRY_RUN=False

Usage:
    python live_trading/execute_trades.py
    python scripts/dry_run.py          (forces DRY_RUN=True)
"""

import os
import sys
import json
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# Enable ANSI colors on Windows
if sys.platform == "win32":
    os.system("")  # Enables ANSI escape sequences in Windows terminal

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from live_trading.fyers_auth import get_access_token, FYERS_APP_ID
from data.data_fetcher import (
    read_stocks, to_fyers_symbol, fetch_historical_data,
    save_historical_data, HISTORICAL_DATA_DIR, append_live_quote,
    load_historical_csv, fetch_batch_quotes, apply_live_quote
)
from data.watchlist_manager import (
    get_active_watchlist, get_sell_watchlist, get_test_stocks,
    add_to_active_watchlist, move_to_sell_watchlist
)

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOCKS_FILE = os.path.join(PROJECT_DIR, "stocks_to_test.txt")
WATCHLIST_FILE = os.path.join(PROJECT_DIR, "stocks_watchlist.txt")
PORTFOLIO_DB_FILE = os.path.join(PROJECT_DIR, "data", "portfolio_db.json")
ORDER_URL = "https://api-t1.fyers.in/api/v3/orders/sync"

# Strategy Parameters
KC_PERIOD = 20
KC_ATR_MULTIPLIER = 2.0
EMA_FAST = 10
EMA_SLOW = 21
QUANTITY = 1  # Default quantity per order; adjust per stock


# ============================================================
# ANSI Color Codes
# ============================================================
class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    DIM = "\033[90m"
    BOLD = "\033[1m"
    RESET = "\033[0m"
    BG_GREEN = "\033[42m"
    BG_RED = "\033[41m"
    BG_YELLOW = "\033[43m"


def colored(text, color):
    return f"{color}{text}{Colors.RESET}"


# ============================================================
# Indicator Computation
# ============================================================

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute Keltner Channel (ATR 2.0) and EMA 10/21 on a DataFrame."""
    df = df.copy()

    # EMA for Keltner mid-line
    df["KC_MID"] = df["Close"].ewm(span=KC_PERIOD, adjust=False).mean()

    # ATR
    df["TR"] = np.maximum(
        df["High"] - df["Low"],
        np.maximum(
            abs(df["High"] - df["Close"].shift(1)),
            abs(df["Low"] - df["Close"].shift(1))
        )
    )
    df["ATR"] = df["TR"].ewm(span=KC_PERIOD, adjust=False).mean()

    # Keltner Channels
    df["KC_UPPER"] = df["KC_MID"] + (KC_ATR_MULTIPLIER * df["ATR"])
    df["KC_LOWER"] = df["KC_MID"] - (KC_ATR_MULTIPLIER * df["ATR"])

    # EMA Fast / Slow
    df["EMA_FAST"] = df["Close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["EMA_SLOW"] = df["Close"].ewm(span=EMA_SLOW, adjust=False).mean()

    # Volume SMA for volume confirmation
    df["VOL_SMA"] = df["Volume"].rolling(window=20).mean()

    # Bollinger Band Upper (20-period, 2.0 std dev) for Strategy 6
    df["BB_STD"] = df["Close"].rolling(window=20).std()
    df["BB_UPPER"] = df["KC_MID"] + (2.0 * df["BB_STD"])

    return df


# ============================================================
# Signal Generation with Quality Score
# ============================================================

def generate_signal(df: pd.DataFrame, strategy_id: int = None) -> tuple:
    """
    Generate a trading signal. Default is Strategy 1 (Keltner Tuned - winning strategy).
    Optionally supports Strategy 6 (5-Rule Retracement Strategy).

    Returns: (signal, quality_score, reasons)
    """
    if strategy_id is None:
        try:
            strategy_id = int(os.getenv("STRATEGY_ID", "1"))
        except ValueError:
            strategy_id = 1

    if len(df) < KC_PERIOD + 1:
        return "HOLD", 0, ["Insufficient data"]

    # ---- STRATEGY 6: 5-RULE RETRACEMENT BREAKOUT ----
    if strategy_id == 6:
        today = df.iloc[-1]
        prev = df.iloc[-2]
        today_open = today["Open"]
        today_close = today["Close"]
        today_high = today["High"]
        today_low = today["Low"]
        kc_upper = today["KC_UPPER"]
        kc_mid = today["KC_MID"]
        kc_lower = today["KC_LOWER"]
        bb_upper = today.get("BB_UPPER", kc_upper)
        ema_fast = today["EMA_FAST"]
        ema_slow = today["EMA_SLOW"]
        prev_ema_fast = prev["EMA_FAST"]
        prev_ema_slow = prev["EMA_SLOW"]

        # Check SELL signal
        ema_cross_down = (prev_ema_fast >= prev_ema_slow) and (ema_fast < ema_slow)
        if today_close < kc_mid or ema_cross_down:
            score = 50
            reasons = []
            if today_close < kc_mid:
                reasons.append("Price below KC mid-line")
            if ema_cross_down:
                reasons.append("EMA10 crossed below EMA21")
            return "SELL", min(score, 100), reasons

        # Check BUY signal (Rule 5: positive candle touching upper band)
        rule5_pos = today_close > today_open
        rule5_touch = (today_high >= kc_upper) or (today_high >= bb_upper)

        if rule5_pos and rule5_touch:
            for days_back in range(1, 4):
                if len(df) <= days_back + 1:
                    continue
                bc_idx = -(days_back + 1)
                bc = df.iloc[bc_idx]

                bc_close = bc["Close"]
                bc_high = bc["High"]
                bc_low = bc["Low"]
                bc_kc_mid = bc["KC_MID"]
                bc_ema_fast = bc["EMA_FAST"]
                bc_ema_slow = bc["EMA_SLOW"]

                # Rule 1 & 2 on BC candle
                if bc_ema_fast > bc_ema_slow and bc_close > bc_kc_mid:
                    retrace_ok = False
                    support_ok = True

                    for sub_idx in range(bc_idx + 1, 0):
                        bar = df.iloc[sub_idx]
                        if bar["Low"] < bc_low or bar["Low"] < bar["KC_LOWER"]:
                            support_ok = False
                            break
                        depth = (bc_high - bar["Low"]) / bc_high if bc_high > 0 else 0
                        if depth >= 0.03:
                            retrace_ok = True

                    if support_ok and retrace_ok:
                        score = 75
                        reasons = [
                            "Rule 1 & 2: BC candle confirmed (EMA10 > EMA21 & Close > KC Mid)",
                            f"Rule 3 & 4: Retracement >=3% held above BC low ({bc_low:.2f})",
                            "Rule 5: Green candle touched Upper Band"
                        ]
                        if today_close > kc_upper:
                            score += 15
                            reasons.append("Breakout above KC Upper")
                        return "BUY", min(score, 100), reasons

        return "HOLD", 0, ["Does not satisfy 5-rule entry sequence"]

    # ---- DEFAULT: STRATEGY 1 (KELTNER TUNED - WINNING STRATEGY) ----
    latest = df.iloc[-1]
    prev   = df.iloc[-2]

    close         = latest["Close"]
    kc_upper      = latest["KC_UPPER"]
    kc_mid        = latest["KC_MID"]
    kc_lower      = latest["KC_LOWER"]
    atr           = latest["ATR"]
    ema_fast      = latest["EMA_FAST"]
    ema_slow      = latest["EMA_SLOW"]
    prev_ema_fast = prev["EMA_FAST"]
    prev_ema_slow = prev["EMA_SLOW"]
    volume        = latest["Volume"]
    vol_sma       = latest["VOL_SMA"]
    vol_ratio     = (volume / vol_sma) if vol_sma > 0 else 0

    # ---- BUY SIGNAL ----
    # Core: Price above KC Upper AND EMA10 > EMA21 (Keltner Tuned)
    if close > kc_upper and ema_fast > ema_slow:
        score = 40
        reasons = []

        # Factor 1: Breakout strength (how far above KC Upper)
        breakout_pct = ((close - kc_upper) / atr) * 100 if atr > 0 else 0
        if breakout_pct > 50:
            score += 15
            reasons.append(f"Strong breakout ({breakout_pct:.0f}% of ATR above KC)")
        elif breakout_pct > 20:
            score += 10
            reasons.append(f"Moderate breakout ({breakout_pct:.0f}% of ATR above KC)")
        else:
            score += 5
            reasons.append(f"Marginal breakout ({breakout_pct:.0f}% of ATR above KC)")

        # Factor 2: EMA trend strength
        ema_gap_pct = ((ema_fast - ema_slow) / ema_slow) * 100
        if ema_gap_pct > 2:
            score += 15
            reasons.append(f"Strong EMA trend (EMA10 {ema_gap_pct:.1f}% above EMA21)")
        elif ema_gap_pct > 0.5:
            score += 10
            reasons.append(f"Moderate EMA trend (EMA10 {ema_gap_pct:.1f}% above EMA21)")
        else:
            score += 5
            reasons.append(f"Weak EMA trend (EMA10 {ema_gap_pct:.1f}% above EMA21)")

        # Factor 3: Fresh EMA golden cross today (bonus signal quality)
        if prev_ema_fast <= prev_ema_slow and ema_fast > ema_slow:
            score += 10
            reasons.append("Fresh EMA golden cross (today)")

        # Factor 4: Volume -- scoring bonus only (not a hard gate)
        if vol_ratio >= 2.0:
            score += 15
            reasons.append(f"High volume ({vol_ratio:.1f}x avg)")
        elif vol_ratio >= 1.5:
            score += 10
            reasons.append(f"Above-avg volume ({vol_ratio:.1f}x avg)")
        elif vol_ratio >= 1.0:
            score += 5
            reasons.append(f"Normal volume ({vol_ratio:.1f}x avg)")
        else:
            reasons.append(f"Low volume ({vol_ratio:.1f}x avg) -- caution")

        # Factor 5: Full trend alignment
        if close > ema_fast > ema_slow:
            score += 5
            reasons.append("Price > EMA10 > EMA21 (aligned uptrend)")

        return "BUY", min(score, 100), reasons

    # ---- SELL SIGNAL ----
    ema_crossover_down = (prev_ema_fast >= prev_ema_slow) and (ema_fast < ema_slow)
    if close < kc_mid or ema_crossover_down:
        score = 50
        reasons = []

        if close < kc_mid:
            reasons.append("Price below KC mid-line")
        if ema_crossover_down:
            reasons.append("EMA10 crossed below EMA21 (bearish)")
            score += 20
        if close < kc_lower:
            reasons.append("Price below KC lower band (strong sell)")
            score += 20
        if ema_fast < ema_slow:
            reasons.append("EMA10 < EMA21 (downtrend)")

        return "SELL", min(score, 100), reasons

    # ---- HOLD ----
    reasons = []
    if close < kc_upper:
        reasons.append("Price inside Keltner Channel")
    if ema_fast <= ema_slow:
        reasons.append("EMA10 <= EMA21 (no bullish trend)")
    return "HOLD", 0, reasons


def quality_bar(score):
    """Create a visual quality bar: [======    ] 60%"""
    filled = int(score / 10)
    empty = 10 - filled
    if score >= 75:
        color = Colors.GREEN
    elif score >= 50:
        color = Colors.YELLOW
    else:
        color = Colors.RED
    return f"{color}[{'=' * filled}{' ' * empty}] {score}%{Colors.RESET}"


def quality_label(score, plain=False):
    """Return a text label for the score."""
    if score >= 80:
        return "EXCELLENT" if plain else colored("EXCELLENT", Colors.BOLD + Colors.GREEN)
    elif score >= 65:
        return "GOOD" if plain else colored("GOOD", Colors.GREEN)
    elif score >= 50:
        return "MODERATE" if plain else colored("MODERATE", Colors.YELLOW)
    else:
        return "WEAK" if plain else colored("WEAK", Colors.RED)


# ============================================================
# Order Placement
# ============================================================

def place_order(access_token: str, symbol: str, side: int, qty: int = QUANTITY):
    """
    Places an order on Fyers.

    Args:
        access_token: Fyers access token
        symbol: Fyers symbol (e.g., NSE:RELIANCE-EQ)
        side: 1 = BUY, -1 = SELL
        qty: Number of shares
    """
    headers = {
        "Authorization": f"{FYERS_APP_ID}:{access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "symbol": symbol,
        "qty": qty,
        "type": 2,           # Market order
        "side": side,         # 1=Buy, -1=Sell
        "productType": "CNC", # Cash and Carry (delivery)
        "limitPrice": 0,
        "stopPrice": 0,
        "validity": "DAY",
        "disclosedQty": 0,
        "offlineOrder": False,
        "stopLoss": 0,
        "takeProfit": 0,
    }

    response = requests.post(ORDER_URL, json=payload, headers=headers)
    data = response.json()

    if data.get("s") == "ok":
        side_text = colored("BUY", Colors.GREEN) if side == 1 else colored("SELL", Colors.RED)
        print(f"    Order placed: {symbol} {side_text} x{qty} - Order ID: {data.get('id', 'N/A')}")
    else:
        print(colored(f"    Order FAILED: {symbol} - {data.get('message', data)}", Colors.RED))

    return data


def get_positions(access_token: str) -> dict:
    """Fetches current open positions from Fyers (used in live mode only)."""
    headers = {"Authorization": f"{FYERS_APP_ID}:{access_token}"}
    url = "https://api-t1.fyers.in/api/v3/positions"
    response = requests.get(url, headers=headers)
    data = response.json()
    positions = {}
    if data.get("s") == "ok":
        for pos in data.get("netPositions", []):
            positions[pos["symbol"]] = pos.get("netQty", 0)
    return positions


def load_portfolio() -> dict:
    """
    Load portfolio_db.json for local portfolio lookups.

    Returns:
        dict: {symbol: {qty, avg_cost}}
        e.g. {"STALLION": {"qty": 292, "avg_cost": 209.61}}
    """
    try:
        with open(PORTFOLIO_DB_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ============================================================
# Main
# ============================================================

def main():
    print()
    print(colored("=" * 70, Colors.CYAN))
    print(colored("  KELTNER TUNED LIVE SCANNER  (ATR 2.0 + EMA 10/21)", Colors.BOLD + Colors.CYAN))
    print(colored(f"  {datetime.now().strftime('%A, %d %B %Y  %H:%M:%S')}", Colors.CYAN))
    print(colored("=" * 70, Colors.CYAN))

    # Determine run mode upfront so all logic below can branch on it
    dry_run = os.getenv("DRY_RUN", "True").lower() != "false"
    mode_label = colored("DRY RUN (recommendations only)", Colors.YELLOW + Colors.BOLD) if dry_run else colored("LIVE MODE", Colors.RED + Colors.BOLD)
    print(f"\n  Mode: {mode_label}")

    access_token = get_access_token()

    # Load local portfolio -- used for ADD MORE vs NEW BUY labelling
    # and to ensure every held stock is included in the scan
    portfolio = load_portfolio()

    # ---- Build scanning universe ----
    watchlist_mode = os.getenv("WATCHLIST_MODE", "active").lower()

    if watchlist_mode == "sell":
        sell_stocks = get_sell_watchlist()
        if not sell_stocks:
            print(colored("  [INFO] No stocks currently in Sell Watchlist.", Colors.YELLOW))
            print(colored("  To populate Sell Watchlist, run a regular Dry Run and move SELL stocks.\n", Colors.DIM))
            return
        all_stocks = sell_stocks
        stock_source = {s: "SELL_WATCHLIST" for s in all_stocks}
        print(f"\n  Scanning {colored(str(len(all_stocks)), Colors.BOLD)} stocks from {colored('SELL WATCHLIST', Colors.YELLOW)} (Weakness & Recovery Audit)\n")
    else:
        main_stocks = get_test_stocks()
        watchlist_stocks = get_active_watchlist()

        # Track source tag for display
        stock_source = {}
        for s in main_stocks:
            stock_source[s] = "MAIN"
        for s in watchlist_stocks:
            if s not in stock_source:
                stock_source[s] = "ACTIVE_WATCHLIST"

        # Merge and deduplicate (preserving order)
        all_stocks = list(dict.fromkeys(main_stocks + watchlist_stocks))

        # Ensure every portfolio holding is included even if not in scan lists
        # so ADD MORE signals are never missed
        for ps in portfolio.keys():
            if ps not in stock_source:
                stock_source[ps] = "PORTFOLIO"
                all_stocks.append(ps)

        if not all_stocks:
            print(colored("No stocks found to scan in Active Watchlist.", Colors.RED))
            return

        main_count  = len(main_stocks)
        wl_count    = len([s for s in all_stocks if stock_source.get(s) == "ACTIVE_WATCHLIST"])
        port_extra  = len([s for s in all_stocks if stock_source.get(s) == "PORTFOLIO"])

        print(f"\n  Scanning {colored(str(len(all_stocks)), Colors.BOLD)} stocks ({colored('ACTIVE WATCHLIST', Colors.GREEN)}) -- "
              f"{main_count} main . {wl_count} watchlist . {port_extra} portfolio-only\n")

    # Fetch live positions only in live mode (not needed for dry run)
    # Fetch live positions only in live mode (not needed for dry run)
    positions = {}
    if not dry_run:
        positions = get_positions(access_token)

    # 1. Pre-fetch real-time market quotes in high-speed batches of 50
    all_fyers_symbols = [to_fyers_symbol(s) for s in all_stocks]
    print(f"  Fetching real-time quotes in high-speed batches for {len(all_fyers_symbols)} stocks...", end=" ")
    try:
        live_quotes = fetch_batch_quotes(all_fyers_symbols, access_token)
        print(colored(f"Done ({len(live_quotes)} quotes active).", Colors.GREEN))
    except Exception as q_err:
        live_quotes = {}
        print(colored(f"Warning: Batch quotes fallback - {q_err}", Colors.YELLOW))

    print()

    from data.duckdb_manager import get_connection

    # Tuples stored as:
    #   buy_signals:  (fyers_sym, raw_sym, close, score, reasons, in_portfolio, avg_cost, portfolio_qty)
    #   sell_signals: (fyers_sym, raw_sym, close, held_qty, score, reasons)
    buy_signals  = []
    sell_signals = []
    hold_count   = 0
    error_count  = 0

    # Open single high-performance read-only connection for entire scan loop
    db_con = get_connection(read_only=True)

    try:
        for i, symbol in enumerate(all_stocks, 1):
            fyers_symbol = to_fyers_symbol(symbol)
            progress = colored(f"[{i:3d}/{len(all_stocks)}]", Colors.DIM)

            # Portfolio lookup -- raw symbol keys match portfolio_db.json
            portfolio_data = portfolio.get(symbol, {})
            portfolio_qty  = portfolio_data.get("qty", 0)
            avg_cost       = portfolio_data.get("avg_cost", 0.0)
            in_portfolio   = portfolio_qty > 0

            try:
                # 1. Load historical candles from DuckDB (microsecond speed)
                try:
                    df = load_historical_csv(fyers_symbol, con=db_con)
                except FileNotFoundError:
                    # Fallback to API if not in DuckDB
                    df = fetch_historical_data(fyers_symbol, access_token, days=60)
                    save_historical_data(fyers_symbol, df)

                # 2. Overlay real-time quote from pre-fetched batch dictionary
                quote = live_quotes.get(fyers_symbol)
                if quote:
                    df = apply_live_quote(df, quote)

                df = compute_indicators(df)
                signal, score, reasons = generate_signal(df)

                latest_close = df.iloc[-1]["Close"]

                if signal == "BUY":
                    buy_signals.append((fyers_symbol, symbol, latest_close, score, reasons,
                                        in_portfolio, avg_cost, portfolio_qty))
                    if in_portfolio:
                        tag = colored("ADD MORE", Colors.BOLD + Colors.CYAN)
                    else:
                        tag = colored(" NEW BUY", Colors.BOLD + Colors.GREEN)
                    print(f"  {progress} [{tag}] {symbol:22s}  Rs.{latest_close:>9.2f}  {quality_bar(score)}")

                elif signal == "SELL":
                    # In dry run: use portfolio_db qty; in live: use API positions
                    held_qty = portfolio_qty if dry_run else positions.get(fyers_symbol, 0)
                    if held_qty > 0:
                        sell_signals.append((fyers_symbol, symbol, latest_close, held_qty, score, reasons))
                        tag = colored(" CAUTION", Colors.BOLD + Colors.YELLOW)
                        print(f"  {progress} [{tag}] {symbol:22s}  Rs.{latest_close:>9.2f}  (held {held_qty} @ Rs.{avg_cost:.2f})")
                    else:
                        hold_count += 1
                        tag = colored("HOLD", Colors.DIM)
                        print(f"  {progress}  {tag}   {symbol:22s}  Rs.{latest_close:>9.2f}")
                else:
                    hold_count += 1
                    tag = colored("HOLD", Colors.DIM)
                    print(f"  {progress}  {tag}   {symbol:22s}  Rs.{latest_close:>9.2f}")

            except Exception as e:
                error_count += 1
                tag = colored(" ERR ", Colors.BOLD + Colors.RED)
                print(f"  {progress} {tag} {symbol:22s}  {e}")
    finally:
        db_con.close()

    # ============================================================
    # TODAY'S RECOMMENDATIONS DASHBOARD
    # ============================================================
    print()
    print(colored("=" * 70, Colors.CYAN))
    print(colored("  TODAY'S BUY RECOMMENDATIONS", Colors.BOLD + Colors.CYAN))
    print(colored(f"  {datetime.now().strftime('%A, %d %B %Y')}", Colors.CYAN))
    print(colored("=" * 70, Colors.CYAN))
    print(f"  Stocks scanned  : {len(all_stocks)}")
    print(f"  {colored('BUY signals  :', Colors.GREEN)}   {len(buy_signals)}")
    print(f"  {colored('SELL/Caution :', Colors.YELLOW)}  {len(sell_signals)} held stocks showing weakness")
    print(f"  {colored('HOLD         :', Colors.DIM)}  {hold_count}")
    if error_count:
        print(f"  {colored('Errors       :', Colors.RED)}  {error_count}")

    if not buy_signals and not sell_signals:
        print(f"\n  {colored('No actionable signals today. Market may be in consolidation.', Colors.YELLOW)}")
    else:
        # Sort all buy signals by score descending (best first)
        buy_signals.sort(key=lambda x: x[3], reverse=True)

        # Split into ADD MORE (already held) and NEW BUY (fresh entry)
        add_more = [
            (fs, rs, cl, sc, reas, ac, qty)
            for fs, rs, cl, sc, reas, inp, ac, qty in buy_signals if inp
        ]
        new_buy = [
            (fs, rs, cl, sc, reas)
            for fs, rs, cl, sc, reas, inp, ac, qty in buy_signals if not inp
        ]

        # ---- ADD MORE ----
        if add_more:
            print()
            print(colored("  >> ADD MORE  --  Stocks you already hold with continuing momentum:", Colors.BOLD + Colors.CYAN))
            print(colored("  " + "-" * 68, Colors.CYAN))
            for idx, (fyers_sym, raw_sym, close, score, reasons, avg_cost, qty) in enumerate(add_more, 1):
                unrealised_pct = ((close - avg_cost) / avg_cost * 100) if avg_cost > 0 else 0.0
                pnl_color = Colors.GREEN if unrealised_pct >= 0 else Colors.RED
                pnl_str   = colored(f"{unrealised_pct:+.1f}%", pnl_color)
                brief     = "  .  ".join(reasons[:3])
                print()
                print(f"  {colored(str(idx) + '.', Colors.BOLD)} {colored(raw_sym, Colors.BOLD + Colors.CYAN):30s} "
                      f"Rs.{close:>9.2f}   {quality_bar(score)}  {quality_label(score)}")
                print(f"     Avg Cost: Rs.{avg_cost:>9.2f}  |  Qty held: {qty}  |  Unrealised P&L: {pnl_str}")
                print(f"     {colored('>', Colors.CYAN)} {brief}")

        # ---- NEW BUY ----
        if new_buy:
            print()
            print(colored("  >> NEW BUY  --  Fresh entry opportunities:", Colors.BOLD + Colors.GREEN))
            print(colored("  " + "-" * 68, Colors.GREEN))
            for idx, (fyers_sym, raw_sym, close, score, reasons) in enumerate(new_buy, 1):
                brief = "  .  ".join(reasons[:3])
                print()
                print(f"  {colored(str(idx) + '.', Colors.BOLD)} {colored(raw_sym, Colors.BOLD + Colors.GREEN):30s} "
                      f"Rs.{close:>9.2f}   {quality_bar(score)}  {quality_label(score)}")
                print(f"     {colored('>', Colors.GREEN)} {brief}")

        # ---- CAUTION: HELD STOCKS WEAKENING ----
        if sell_signals:
            print()
            print(colored("  [!]  CAUTION  --  Held stocks showing weakness (consider reviewing):", Colors.BOLD + Colors.YELLOW))
            print(colored("  " + "-" * 68, Colors.YELLOW))
            for fyers_sym, raw_sym, close, held_qty, score, reasons in sell_signals:
                pd_data       = portfolio.get(raw_sym, {})
                avg_c         = pd_data.get("avg_cost", 0.0)
                unrealised    = ((close - avg_c) / avg_c * 100) if avg_c > 0 else 0.0
                pnl_color     = Colors.GREEN if unrealised >= 0 else Colors.RED
                pnl_str       = colored(f"{unrealised:+.1f}%", pnl_color)
                brief         = "  .  ".join(reasons[:2])
                print(f"  {colored(raw_sym, Colors.YELLOW + Colors.BOLD):20s}  Rs.{close:>9.2f}  "
                      f"Qty: {held_qty:>5}  P&L: {pnl_str}")
                print(f"     {colored('>', Colors.YELLOW)} {brief}")

    # ---- QUALITY GUIDE ----
    print()
    print(colored("  " + "-" * 68, Colors.DIM))
    print(colored("  SIGNAL QUALITY GUIDE:", Colors.BOLD))
    print(f"  {colored('EXCELLENT (80+):', Colors.GREEN)}  All factors aligned -- strong breakout, trend & volume")
    print(f"  {colored('GOOD      (65+):', Colors.GREEN)}  Most factors present -- solid entry candidate")
    print(f"  {colored('MODERATE  (50+):', Colors.YELLOW)} Some factors -- enter with caution")
    print(f"  {colored('WEAK      (<50):', Colors.RED)}  Few factors -- consider skipping")
    print(colored("  " + "-" * 68, Colors.DIM))

    # ---- ORDER PLACEMENT (LIVE MODE ONLY) ----
    if not dry_run:
        print(f"\n  {colored('** LIVE MODE - PLACING ORDERS **', Colors.RED + Colors.BOLD)}")
        # Sell orders first to free up capital
        for fyers_sym, raw_sym, close, held_qty, score, reasons in sell_signals:
            place_order(access_token, fyers_sym, side=-1, qty=held_qty)
        # Then buy orders
        for fyers_sym, raw_sym, close, score, reasons, in_portfolio, avg_cost, qty in buy_signals:
            place_order(access_token, fyers_sym, side=1, qty=QUANTITY)
    else:
        print(f"\n  {colored('DRY RUN -- No orders placed. Review recommendations above.', Colors.YELLOW)}")

    # ---- LOG SIGNALS FOR HISTORICAL TRACKING & RESULTS FILE ----
    history_file = os.path.join(PROJECT_DIR, "data", "dry_run_history.json")
    today_str    = datetime.now().strftime('%Y-%m-%d')

    # Save to Results/dd-mm-hh-dryrun-results.txt or sellscan-results.txt
    results_dir = os.path.join(PROJECT_DIR, "Results")
    os.makedirs(results_dir, exist_ok=True)
    now = datetime.now()
    if watchlist_mode == "sell":
        results_filename = f"{now.strftime('%d-%m-%H')}-sellscan-results.txt"
    else:
        results_filename = f"{now.strftime('%d-%m-%H')}-dryrun-results.txt"
    results_filepath = os.path.join(results_dir, results_filename)

    try:
        lines = []
        excellent_good_add_more = [
            (raw_sym, score) for _, raw_sym, close, score, reasons, inp, avg_cost, qty in buy_signals
            if inp and score >= 65
        ]
        excellent_good_new_buy = [
            (raw_sym, score) for _, raw_sym, close, score, reasons, inp, avg_cost, qty in buy_signals
            if not inp and score >= 65
        ]

        if excellent_good_add_more:
            lines.append("ADD MORE:")
            for idx, (raw_sym, score) in enumerate(excellent_good_add_more, 1):
                label = quality_label(score, plain=True)
                lines.append(f"{idx}. {raw_sym} — {score}% {label}")
            lines.append("")

        if excellent_good_new_buy:
            lines.append("NEW BUY:")
            for idx, (raw_sym, score) in enumerate(excellent_good_new_buy, 1):
                label = quality_label(score, plain=True)
                lines.append(f"{idx}. {raw_sym} — {score}% {label}")
            lines.append("")

        if sell_signals:
            lines.append("CAUTION / SELL:")
            for idx, (fyers_sym, raw_sym, close, held_qty, score, reasons) in enumerate(sell_signals, 1):
                reason_str = " . ".join(reasons) if reasons else "Weakness"
                lines.append(f"{idx}. {raw_sym} — {score}% SELL (held {held_qty} @ Rs.{close:.2f}) > {reason_str}")

        with open(results_filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"  {colored('Results saved:', Colors.GREEN)} Results/{results_filename}")
    except Exception as e:
        print(f"  {colored('Results File Error:', Colors.RED)} {e}")

    daily_record = {
        "date": today_str,
        "buy_signals": [
            {
                "symbol": raw_sym,
                "close": close,
                "score": score,
                "action": "ADD_MORE" if inp else "NEW_BUY"
            }
            for _, raw_sym, close, score, reasons, inp, avg_cost, qty in buy_signals
        ],
        "sell_signals": [
            {"symbol": raw_sym, "close": close, "qty": held_qty, "score": score}
            for _, raw_sym, close, held_qty, score, reasons in sell_signals
        ]
    }

    try:
        if os.path.exists(history_file):
            with open(history_file, "r") as f:
                history_data = json.load(f)
        else:
            history_data = []

        # Replace today's record if already exists, else append
        existing_idx = next((i for i, d in enumerate(history_data) if d["date"] == today_str), -1)
        if existing_idx >= 0:
            history_data[existing_idx] = daily_record
        else:
            history_data.append(daily_record)

        with open(history_file, "w") as f:
            json.dump(history_data, f, indent=4)
        print(f"  {colored('History:', Colors.DIM)} Saved today's signals -> data/dry_run_history.json")
    except Exception as e:
        print(f"  {colored('History Error:', Colors.RED)} {e}")

    print()


if __name__ == "__main__":
    main()
