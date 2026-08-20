import os
import sys

# Ensure parent directory is in sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live_trading.execute_trades import main

if __name__ == "__main__":
    # Force DRY_RUN mode for this execution
    os.environ["DRY_RUN"] = "True"

    # Default strategy is Strategy 1 (winning strategy) unless specified via argument
    if len(sys.argv) > 1 and sys.argv[1] in ["6", "--strategy-6", "strat6", "-s6"]:
        os.environ["STRATEGY_ID"] = "6"
        strat_name = "Strategy 6 (Keltner 5-Rule Retracement)"
    else:
        os.environ["STRATEGY_ID"] = "1"
        strat_name = "Strategy 1 (Keltner Tuned - Winning Strategy)"

    print("========================================")
    print(f"  STARTING DAILY DRY RUN [{strat_name}]")
    print("========================================")

    # Run the main scanner
    main()
