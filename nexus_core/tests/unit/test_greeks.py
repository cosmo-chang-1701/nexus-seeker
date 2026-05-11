import pytest
import math
from market_analysis.greeks import calculate_greeks, calculate_contract_delta, calculate_vanna

def test_calculate_vanna_basic():
    # Test vanna calculation with some realistic values
    # Call option, S=100, K=100, T=0.1 (36.5 days), IV=0.2, r=0.05, q=0.0
    vanna = calculate_vanna('c', 100, 100, 0.1, 0.2, 0.0)
    assert isinstance(vanna, float)
    # Vanna for ATM option is usually near 0 if T is small, but let's just check it returns a value
    assert vanna != 0.0

def test_greeks_dividend_correction():
    # Test that dividend rate 'q' affects greeks
    stock_price = 100
    strike = 100
    t_years = 0.5
    iv = 0.2

    # Case 1: No dividend
    greeks_no_div = calculate_greeks('call', stock_price, strike, t_years, iv, q=0.0)

    # Case 2: 5% dividend
    greeks_with_div = calculate_greeks('call', stock_price, strike, t_years, iv, q=0.05)

    # Dividend yield reduces the value of calls, so Delta should be lower
    assert greeks_with_div['delta'] < greeks_no_div['delta']
    assert greeks_with_div['delta'] > 0

def test_calculate_contract_delta_merton():
    # Test Merton model correction (q) in calculate_contract_delta
    row = {'strike': 100, 'impliedVolatility': 0.2}
    stock_price = 100
    t_years = 0.5

    delta_no_div = calculate_contract_delta(row, stock_price, t_years, 'c', q=0.0)
    delta_with_div = calculate_contract_delta(row, stock_price, t_years, 'c', q=0.05)

    assert delta_with_div < delta_no_div

def test_greeks_edge_cases():
    # IV = 0
    res = calculate_greeks('call', 100, 100, 0.5, 0.0, 0.0)
    assert res['delta'] == 0.0

    # t_years = 0
    row = {'strike': 100, 'impliedVolatility': 0.2}
    delta_val = calculate_contract_delta(row, 100, 0, 'c', 0.0)
    assert delta_val == 0.0
