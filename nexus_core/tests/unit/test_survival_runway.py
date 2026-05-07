import unittest
from market_analysis.pro_management import calculate_survival_runway

class TestSurvivalRunway(unittest.TestCase):
    def test_standard_runway(self):
        # Cash: 40000, Monthly Expense: 5000, Daily Theta: 10
        # Net Monthly Burn = 5000 - (10 * 30) = 4700
        # Months = 40000 / 4700 = 8.51
        # Days = 8.51 * 30 = 255.3
        result = calculate_survival_runway(40000, 5000, 10)
        self.assertEqual(result, 255.3)

    def test_high_theta_infinity_runway(self):
        # Theta covers all expenses
        # Monthly Expense: 2000, Daily Theta: 100 -> Monthly Theta: 3000
        result = calculate_survival_runway(10000, 2000, 100)
        self.assertEqual(result, 9999.0)

    def test_zero_cash_zero_runway(self):
        result = calculate_survival_runway(0, 5000, 10)
        self.assertEqual(result, 0.0)

    def test_negative_net_burn_infinity(self):
        # Monthly Expense: 3000, Monthly Theta: 3001
        result = calculate_survival_runway(5000, 3000, 100.1)
        self.assertEqual(result, 9999.0)
