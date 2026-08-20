"""
main.py -- Daily Entry Point for the Fyers Trading Bot

Run this every morning before you start trading:
    python main.py

What it does:
  1. Daily auth gate  -- prompts for Fyers auth code once per calendar day
                        (silently skipped on subsequent runs the same day)
  2. Interactive menu -- choose from 7 workflow options
"""

import os
import sys
import subprocess
from datetime import datetime

# Ensure project root is in path
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from dotenv import load_dotenv
load_dotenv()


# ============================================================
# ANSI Colors (minimal set, no dependency on execute_trades)
# ============================================================
CYAN  = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED   = "\033[91m"
DIM   = "\033[90m"
BOLD  = "\033[1m"
RESET = "\033[0m"

if sys.platform == "win32":
    os.system("")  # Enable ANSI on Windows terminal


def c(text, color):
    return f"{color}{text}{RESET}"


# ============================================================
# Header
# ============================================================

def print_header():
    now = datetime.now()
    print()
    print(c("=" * 62, CYAN))
    print(c("    FYERS TRADING BOT", BOLD + CYAN))
    print(c(f"  {now.strftime('%A, %d %B %Y   %H:%M:%S')}", CYAN))
    print(c("=" * 62, CYAN))


# ============================================================
# Menu
# ============================================================

MENU_OPTIONS = {
    "1": ("Dry Run",           "Scan your watchlist -> today's BUY recommendations"),
    "2": ("Update Data",       "Refresh historical data for all stocks"),
    "3": ("Deep Analysis",     "Deep-dive on a specific stock"),
    "4": ("Add New Stocks",    "Import tickers from Newly_added_stocks.txt"),
    "5": ("Portfolio Scan",    "HOLD / CAUTION / SELL status of your holdings"),
    "6": ("Run Backtest",      "Compare all 5 strategies across the stock universe"),
    "7": ("Full NSE Scan",     "Discover BUY signals from ALL small+mid cap NSE stocks"),
    "8": ("Exit",              ""),
}


def show_menu():
    print()
    print(c("  What would you like to do?", BOLD))
    print()
    for key, (name, desc) in MENU_OPTIONS.items():
        if key == "8":
            print(f"  {c(key + '.', DIM)}  {c('Exit', DIM)}")
        elif key == "7":
            # Full NSE Scan gets a visual separator before it
            print()
            print(f"  {c(key + '.', BOLD + CYAN)}  {c(name, BOLD):25s}  {c(desc, DIM)}")
        else:
            print(f"  {c(key + '.', BOLD + CYAN)}  {c(name, BOLD):25s}  {c(desc, DIM)}")
    print()


def run_option(choice: str) -> bool:
    """Execute the selected menu option. Returns False if user chose Exit."""
    scripts = {
        "1": os.path.join(PROJECT_DIR, "scripts", "dry_run.py"),
        "2": os.path.join(PROJECT_DIR, "scripts", "update_historical_data.py"),
        "4": os.path.join(PROJECT_DIR, "scripts", "add_new_stocks.py"),
        "5": os.path.join(PROJECT_DIR, "scripts", "portfolio_analysis.py"),
        "6": os.path.join(PROJECT_DIR, "backtest", "run_backtest.py"),
        "7": os.path.join(PROJECT_DIR, "scripts", "full_exchange_scan.py"),
    }

    if choice == "8":
        return False  # Signal to exit

    elif choice == "3":
        # Deep analysis needs a ticker symbol
        print()
        ticker = input(c("  Enter stock symbol (e.g. RELIANCE): ", BOLD)).strip().upper()
        if not ticker:
            print(c("  No symbol entered. Returning to menu.", YELLOW))
            return True
        ltp_input = input(c("  Live price (LTP) override? Press Enter to skip: ", DIM)).strip()
        script = os.path.join(PROJECT_DIR, "scripts", "deep_analysis.py")
        cmd = [sys.executable, script, ticker]
        if ltp_input:
            cmd += ["--ltp", ltp_input]
        print()
        subprocess.run(cmd, check=False)

    elif choice in scripts:
        print()
        subprocess.run([sys.executable, scripts[choice]], check=False)

    else:
        print(c(f"  Invalid option. Please enter a number between 1 and 8.", RED))

    return True


# ============================================================
# Main
# ============================================================

def main():
    print_header()

    # ---- Daily Auth Gate ----
    # Prompts for Fyers auth code once per calendar day.
    # On subsequent runs the same day, this is a no-op.
    print()
    from live_trading.fyers_auth import daily_auth_check
    daily_auth_check()

    # ---- Interactive Menu Loop ----
    while True:
        show_menu()
        try:
            choice = input(c("  Select option (1-8): ", BOLD)).strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n\n  {c('Interrupted. Goodbye!', YELLOW)}\n")
            break

        keep_going = run_option(choice)
        if not keep_going:
            print(f"\n  {c('Goodbye! Happy trading.', GREEN)}\n")
            break
        # Menu auto-shows again after any option completes (no prompt needed)


if __name__ == "__main__":
    main()
