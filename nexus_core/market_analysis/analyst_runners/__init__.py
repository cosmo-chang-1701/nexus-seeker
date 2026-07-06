"""Analyst Agent runner sub-modules.

Each module owns one logical reporting domain:
- macro_runner     : macro data fetch + yield-curve scan
- earnings_runner  : pre-market earnings & valuation scan
- sector_runner    : sector rotation, deep research, open-liquidity
- portfolio_runner : portfolio hedging & post-market summary
- strategy_runner  : next-day strategy + FOMC escape-window analysis
- intraday_runner  : intraday execution guide dispatch
"""
