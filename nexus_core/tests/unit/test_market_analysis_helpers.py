from market_analysis.option_guidance import is_spread_illiquid
from database.cache import get_kv_cache_with_age, save_kv_cache
from database.market_cache import (
    get_fundamental_scan_state,
    save_fundamental_scan_state,
)
from market_analysis.pro_management import calculate_financial_runway
from market_analysis.ivr_strategy_gate import (
    is_selling_locked_by_ivr,
    _IVR_SELLING_LOCKOUT,
    get_ivr_lockout_allowed_strategies,
)
from market_analysis.greeks import (
    calculate_greeks,
    calculate_contract_delta,
    calculate_vanna,
)


def test_is_spread_illiquid_wide_spread_flagged() -> None:
    # spread_ratio = (1.20 - 1.00) / 1.10 ≈ 18.2% > 15%
    assert is_spread_illiquid(1.00, 1.20) is True


def test_is_spread_illiquid_tight_spread_not_flagged() -> None:
    # spread_ratio = (1.05 - 1.00) / 1.025 ≈ 4.9% < 15%
    assert is_spread_illiquid(1.00, 1.05) is False


def test_is_spread_illiquid_boundary_exactly_at_threshold_not_flagged() -> None:
    # bid=1.00, ask chosen so spread_ratio == 0.15 exactly -> strictly > required, so False
    bid = 1.00
    ask = bid * (2 + 0.15) / (2 - 0.15)
    assert is_spread_illiquid(bid, ask, threshold=0.15) is False


def test_is_spread_illiquid_zero_or_missing_quotes_not_flagged() -> None:
    assert is_spread_illiquid(0.0, 0.0) is False
    assert is_spread_illiquid(0.0, 1.20) is False
    assert is_spread_illiquid(1.20, 1.20) is False
    assert is_spread_illiquid(1.20, 1.00) is False  # ask < bid: malformed quote, ignore


# database.cache.get_kv_cache_with_age — additive read helper backing the
# options-data freshness/timestamp display work (surfaces kv_cache.updated_at
# as an age-in-seconds alongside the existing value, without changing
# get_kv_cache()'s own behavior or the stored value shape).


async def test_get_kv_cache_with_age_missing_key_returns_none_tuple() -> None:
    value, age_seconds = get_kv_cache_with_age("does_not_exist_key")
    assert value is None
    assert age_seconds is None


async def test_get_kv_cache_with_age_returns_value_and_small_age() -> None:
    assert await save_kv_cache("uoa_TESTSYM", [{"strike": 100.0}]) is True

    value, age_seconds = get_kv_cache_with_age("uoa_TESTSYM")
    assert value == [{"strike": 100.0}]
    assert age_seconds is not None
    # 剛寫入，年齡應接近 0 秒，給予寬鬆容差以避免測試環境時鐘/延遲造成偶發失敗。
    assert 0.0 <= age_seconds < 30.0


async def test_get_kv_cache_with_age_preserves_arbitrary_json_value_shape() -> None:
    await save_kv_cache("dp_poc_TESTSYM", 123.45)

    value, age_seconds = get_kv_cache_with_age("dp_poc_TESTSYM")
    assert value == 123.45
    assert age_seconds is not None


# database.market_cache — fundamental_scan_state CRUD helpers (v062) used by
# the automated daily SEC filing scanner as a dedup cursor (distinct from
# fundamental_cache, which stores the LLM verdict itself and has no
# accession_number column).


def test_get_fundamental_scan_state_missing_returns_none() -> None:
    assert get_fundamental_scan_state("NOPE") is None


def test_save_and_get_fundamental_scan_state_roundtrip() -> None:
    assert save_fundamental_scan_state("amd", "0001-22", "10-Q") is True

    state = get_fundamental_scan_state("AMD")
    assert state is not None
    assert state["last_accession_number"] == "0001-22"
    assert state["last_form_type"] == "10-Q"


def test_save_fundamental_scan_state_upserts_on_new_filing() -> None:
    save_fundamental_scan_state("TSLA", "0001-22", "10-Q")
    save_fundamental_scan_state("TSLA", "0002-33", "8-K")

    state = get_fundamental_scan_state("TSLA")
    assert state is not None
    assert state["last_accession_number"] == "0002-33"
    assert state["last_form_type"] == "8-K"


def test_calculate_financial_runway() -> None:
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


def test_ivr_below_lockout_locks_selling() -> None:
    """IVR < 10% → 鎖死賣方策略"""
    assert is_selling_locked_by_ivr(5.0) is True
    assert is_selling_locked_by_ivr(9.9) is True


def test_ivr_at_lockout_does_not_lock() -> None:
    """IVR == 10% → 不鎖死（精確邊界）"""
    assert is_selling_locked_by_ivr(10.0) is False


def test_ivr_above_lockout_does_not_lock() -> None:
    """IVR > 10% → 正常路由，不鎖死"""
    assert is_selling_locked_by_ivr(15.0) is False
    assert is_selling_locked_by_ivr(50.0) is False
    assert is_selling_locked_by_ivr(100.0) is False


def test_ivr_zero_does_not_lock() -> None:
    """IVR == 0.0（數據缺失）→ 不觸發鎖死，由其他降級邏輯處理"""
    assert is_selling_locked_by_ivr(0.0) is False


def test_ivr_negative_does_not_lock() -> None:
    """IVR 負值（異常值）→ 不觸發鎖死"""
    assert is_selling_locked_by_ivr(-1.0) is False


def test_lockout_constant_is_10() -> None:
    """確認硬鎖門檻常數為 10.0"""
    assert _IVR_SELLING_LOCKOUT == 10.0


def test_allowed_strategies_in_lockout() -> None:
    """鎖死狀態下的允許策略列表應包含現貨、ITM Call BTO、Debit Spread"""
    allowed = get_ivr_lockout_allowed_strategies()
    assert "SPOT_BUY" in allowed
    assert "BTO_CALL_ITM" in allowed
    assert "DEBIT_SPREAD" in allowed
    # 不應包含任何賣方策略
    for strategy in allowed:
        assert "STO" not in strategy
        assert "SELL" not in strategy


def test_calculate_vanna_basic() -> None:
    # Test vanna calculation with some realistic values
    # Call option, S=100, K=100, T=0.1 (36.5 days), IV=0.2, r=0.05, q=0.0
    vanna = calculate_vanna("c", 100, 100, 0.1, 0.2, 0.0)
    assert isinstance(vanna, float)
    # Vanna for ATM option is usually near 0 if T is small, but let's just check it returns a value
    assert vanna != 0.0


def test_greeks_dividend_correction() -> None:
    # Test that dividend rate 'q' affects greeks
    stock_price = 100
    strike = 100
    t_years = 0.5
    iv = 0.2

    # Case 1: No dividend
    greeks_no_div = calculate_greeks("call", stock_price, strike, t_years, iv, q=0.0)

    # Case 2: 5% dividend
    greeks_with_div = calculate_greeks("call", stock_price, strike, t_years, iv, q=0.05)

    # Dividend yield reduces the value of calls, so Delta should be lower
    assert greeks_with_div["delta"] < greeks_no_div["delta"]
    assert greeks_with_div["delta"] > 0


def test_calculate_contract_delta_merton() -> None:
    # Test Merton model correction (q) in calculate_contract_delta
    row = {"strike": 100, "impliedVolatility": 0.2}
    stock_price = 100
    t_years = 0.5

    delta_no_div = calculate_contract_delta(row, stock_price, t_years, "c", q=0.0)
    delta_with_div = calculate_contract_delta(row, stock_price, t_years, "c", q=0.05)

    assert delta_with_div < delta_no_div


def test_greeks_edge_cases() -> None:
    # IV = 0
    res = calculate_greeks("call", 100, 100, 0.5, 0.0, 0.0)
    assert res["delta"] == 0.0

    # t_years = 0
    row = {"strike": 100, "impliedVolatility": 0.2}
    delta_val = calculate_contract_delta(row, 100, 0, "c", 0.0)
    assert delta_val == 0.0
