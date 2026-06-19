import sys

def main():
    filepath = "market_analysis/intraday_pipeline.py"
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # Find the helper block to remove
    start_helpers = -1
    end_helpers = -1
    for i, line in enumerate(lines):
        if line.startswith("def _derive_buy_levels("):
            start_helpers = i
        if line.startswith("async def _estimate_options_wall_metrics("):
            end_helpers = i
            break

    # Find the main block to remove
    start_main = -1
    end_main = -1
    for i, line in enumerate(lines):
        if line.startswith("def calculate_dynamic_trading_signals("):
            start_main = i
        if line.startswith("class IntradayScanPipeline:"):
            end_main = i
            break

    if start_helpers == -1 or end_helpers == -1 or start_main == -1 or end_main == -1:
        print("Error: Could not locate blocks in the file.")
        print(f"helpers: {start_helpers} to {end_helpers}")
        print(f"main: {start_main} to {end_main}")
        sys.exit(1)

    print(f"Removing helpers block: lines {start_helpers} to {end_helpers}")
    print(f"Removing main block: lines {start_main} to {end_main}")

    # Build new imports content
    new_imports = [
        "from market_analysis.models.trader_models import (\n",
        "    TraderAccountState,\n",
        "    OptionHolding,\n",
        "    TickerMarketData,\n",
        "    AdvancedTraderOutput,\n",
        ")\n",
        "from market_analysis.gamma_squeeze_engine import NexusGammaSqueezeEngine\n",
        "from market_analysis.signal_calculator import (\n",
        "    calculate_dynamic_trading_signals,\n",
        "    _derive_buy_levels,\n",
        "    _derive_sell_levels,\n",
        "    _buy_zone_status,\n",
        "    _sell_zone_status,\n",
        "    _extract_pe_ratio,\n",
        ")\n",
        "from market_analysis.option_guidance import (\n",
        "    derive_watchlist_option_guidance,\n",
        "    build_watchlist_option_plan,\n",
        ")\n",
    ]

    # Find where imports end to insert our new imports
    insert_idx = -1
    for i, line in enumerate(lines):
        if "from services.market_data_service import BoundedCache" in line:
            insert_idx = i + 1
            break

    if insert_idx == -1:
        print("Error: Could not find target import line.")
        sys.exit(1)

    # Let's construct the new lines list
    new_lines = []
    # 1. Add top part up to insert_idx
    new_lines.extend(lines[:insert_idx])
    # 2. Add our new imports
    new_lines.extend(new_imports)
    # 3. Add lines from insert_idx to start_helpers
    new_lines.extend(lines[insert_idx:start_helpers])
    # 4. Add lines from end_helpers to start_main
    new_lines.extend(lines[end_helpers:start_main])
    # 5. Add lines from end_main to the end of the file
    new_lines.extend(lines[end_main:])

    with open(filepath, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    print("Successfully refactored intraday_pipeline.py")

if __name__ == "__main__":
    main()
