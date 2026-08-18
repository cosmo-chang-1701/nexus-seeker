from market_analysis.option_guidance import is_spread_illiquid


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
