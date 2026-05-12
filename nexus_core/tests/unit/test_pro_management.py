import sys
import os

# Ensure we can import from nexus_core
sys.path.append(os.path.join(os.getcwd(), "nexus_core"))

from market_analysis.pro_management import calculate_financial_runway


def test_calculate_financial_runway():
    # Case 1: Positive burn rate (expenses > theta)
    cash_reserve = 10000.0
    monthly_expense = 3000.0
    daily_theta = 50.0  # Monthly theta = 1500
    # Net burn = 3000 - 1500 = 1500
    # Runway months = 10000 / 1500 = 6.666...
    # Runway days = 6.666 * 30 = 200.0
    assert (
        calculate_financial_runway(cash_reserve, monthly_expense, daily_theta) == 200.0
    )

    # Case 2: Zero burn rate (theta covers expenses exactly)
    cash_reserve = 10000.0
    monthly_expense = 3000.0
    daily_theta = 100.0  # Monthly theta = 3000
    # Net burn = 0
    assert (
        calculate_financial_runway(cash_reserve, monthly_expense, daily_theta) == 9999.0
    )

    # Case 3: Negative burn rate (theta exceeds expenses)
    cash_reserve = 10000.0
    monthly_expense = 3000.0
    daily_theta = 150.0  # Monthly theta = 4500
    # Net burn = -1500
    assert (
        calculate_financial_runway(cash_reserve, monthly_expense, daily_theta) == 9999.0
    )

    # Case 4: No cash reserve but theta covers expenses
    assert calculate_financial_runway(0.0, 3000.0, 150.0) == 9999.0

    # Case 5: No cash reserve and expenses > theta
    assert calculate_financial_runway(0.0, 3000.0, 50.0) == 0.0
