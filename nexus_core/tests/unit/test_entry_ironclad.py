"""
tests/unit/test_entry_ironclad.py

單元測試：market_analysis/entry_ironclad.py 進場四重嚴格過濾鐵律
(check_entry_ironclad_rules)。純函式、零 I/O，表格式邊界條件驅動。
"""

from datetime import date, timedelta


from market_analysis.entry_ironclad import (
    RuleCheckResult,
    check_entry_ironclad_rules,
)

# gex_profile 依履約價由低到高排序後累積 GEX：
# 95 -> -100 (累積 -100)，100 -> +50 (累積 -50)，105 -> +80 (累積 +30，由負轉正)
# => estimate_symbol_gamma_flip 應回傳 105.0
_GEX_PROFILE = {"95": -100.0, "100": 50.0, "105": 80.0}
_SPOT = 110.0  # > gamma_flip_est(105.0)
_PUT_WALL = 100.0  # < spot，機制二應通過
_CALL_WALL = 120.0  # > spot，機制三第二段應通過


def _future_expiry(days: int) -> str:
    return (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")


def _valid_bto_call(ratio: float = 1.0, dte_days: int = 30) -> dict:
    return {
        "type": "CALL",
        "action": "🟢 買入開倉 (BTO - Ask)",
        "strike": 130.0,
        "ratio": ratio,
        "expiry": _future_expiry(dte_days),
    }


def _base_gex_profile_data(
    put_wall: float = _PUT_WALL, call_wall: float = _CALL_WALL
) -> dict:
    return {
        "put_wall": put_wall,
        "call_wall": call_wall,
        "net_gex": 0.0,
        "gex_profile": _GEX_PROFILE,
    }


def _passing_call_kwargs(**overrides: object) -> dict:
    """建構一組讓四條規則全數通過的標準輸入，供各測試以 overrides 局部覆寫
    單一變因，隔離驗證單一規則的邊界條件。"""
    kwargs = dict(
        candidate_symbol="TEST",
        target_spot=_SPOT,
        gex_profile_data=_base_gex_profile_data(),
        uoa_list=[_valid_bto_call()],
        price_15m_close=110.0,  # > gamma_flip_est(105.0)
        volume_15m=1600.0,
        volume_15m_sma20=1000.0,  # 1600 >= 1000*1.5
    )
    kwargs.update(overrides)
    return kwargs


def test_all_rules_pass() -> None:
    result = check_entry_ironclad_rules(**_passing_call_kwargs())
    assert isinstance(result, RuleCheckResult)
    assert result.all_passed is True
    assert len(result.checks) == 4
    assert all(c.passed for c in result.checks)


def test_as_dict_list_serialization() -> None:
    result = check_entry_ironclad_rules(**_passing_call_kwargs())
    serialized = result.as_dict_list()
    assert len(serialized) == 4
    for row in serialized:
        assert set(row.keys()) == {"name", "label", "passed", "detail"}
        assert row["passed"] is True


# --- 規則一：結構性右側放量突破 ---


def test_rule1_fails_without_breakout_above_gamma_flip() -> None:
    result = check_entry_ironclad_rules(
        **_passing_call_kwargs(price_15m_close=100.0)  # <= gamma_flip_est(105.0)
    )
    assert result.all_passed is False
    rule1 = next(c for c in result.checks if c.name == "rule_1_breakout")
    assert rule1.passed is False


def test_rule1_fails_without_volume_surge() -> None:
    result = check_entry_ironclad_rules(
        **_passing_call_kwargs(volume_15m=1400.0)  # < 1000*1.5 = 1500
    )
    assert result.all_passed is False
    rule1 = next(c for c in result.checks if c.name == "rule_1_breakout")
    assert rule1.passed is False


def test_rule1_fails_when_gamma_flip_undeterminable() -> None:
    """gex_profile 全數為正 (無零交叉點) 時 estimate_symbol_gamma_flip 回傳
    0.0，規則一必須 fail-safe 判定未通過。"""
    result = check_entry_ironclad_rules(
        **_passing_call_kwargs(
            gex_profile_data={
                "put_wall": _PUT_WALL,
                "call_wall": _CALL_WALL,
                "net_gex": 0.0,
                "gex_profile": {"100": 10.0, "105": 20.0},
            }
        )
    )
    rule1 = next(c for c in result.checks if c.name == "rule_1_breakout")
    assert rule1.passed is False


# --- 規則二：做市商正 Gamma 底牆完好 ---


def test_rule2_fails_without_put_wall() -> None:
    result = check_entry_ironclad_rules(
        **_passing_call_kwargs(gex_profile_data=_base_gex_profile_data(put_wall=0.0))
    )
    assert result.all_passed is False
    rule2 = next(c for c in result.checks if c.name == "rule_2_put_wall_floor")
    assert rule2.passed is False


def test_rule2_fails_when_price_below_put_wall() -> None:
    result = check_entry_ironclad_rules(
        **_passing_call_kwargs(
            gex_profile_data=_base_gex_profile_data(put_wall=115.0)  # > spot(110)
        )
    )
    rule2 = next(c for c in result.checks if c.name == "rule_2_put_wall_floor")
    assert rule2.passed is False


# --- 規則三：UOA 無實質物理封頂 ---


def test_rule3_fails_with_sto_call_cap_inside_upside_window() -> None:
    """spot=110，上緣視窗為 (110, 115.5]，STO CALL @112 ratio>=1.0 應觸發封頂。"""
    sto_cap = {
        "type": "CALL",
        "action": "🔴 賣出開倉 (STO - Bid)",
        "strike": 112.0,
        "ratio": 1.2,
    }
    result = check_entry_ironclad_rules(
        **_passing_call_kwargs(uoa_list=[_valid_bto_call(), sto_cap])
    )
    assert result.all_passed is False
    rule3 = next(c for c in result.checks if c.name == "rule_3_no_physical_cap")
    assert rule3.passed is False


def test_rule3_boundary_upside_ceiling_inclusive() -> None:
    """strike 恰為 spot*1.05 (115.5) 時視窗邊界為含等於 (<=)，應觸發封頂。"""
    sto_cap = {
        "type": "CALL",
        "action": "STO",
        "strike": _SPOT * 1.05,
        "ratio": 1.0,
    }
    result = check_entry_ironclad_rules(
        **_passing_call_kwargs(uoa_list=[_valid_bto_call(), sto_cap])
    )
    rule3 = next(c for c in result.checks if c.name == "rule_3_no_physical_cap")
    assert rule3.passed is False


def test_rule3_strike_just_above_ceiling_does_not_cap() -> None:
    """strike 略高於 spot*1.05 上緣視窗之外，不應被判定為封頂。"""
    sto_far = {
        "type": "CALL",
        "action": "STO",
        "strike": _SPOT * 1.05 + 1.0,
        "ratio": 5.0,  # 即使 ratio 很高，超出視窗仍不應觸發
    }
    result = check_entry_ironclad_rules(
        **_passing_call_kwargs(uoa_list=[_valid_bto_call(), sto_far])
    )
    rule3 = next(c for c in result.checks if c.name == "rule_3_no_physical_cap")
    assert rule3.passed is True


def test_rule3_ratio_below_threshold_does_not_cap() -> None:
    """ratio < 1.0 (門檻採 >=) 不構成物理封頂。"""
    sto_weak = {
        "type": "CALL",
        "action": "STO",
        "strike": 112.0,
        "ratio": 0.99,
    }
    result = check_entry_ironclad_rules(
        **_passing_call_kwargs(uoa_list=[_valid_bto_call(), sto_weak])
    )
    rule3 = next(c for c in result.checks if c.name == "rule_3_no_physical_cap")
    assert rule3.passed is True


def test_rule3_fails_when_price_breaches_call_wall() -> None:
    result = check_entry_ironclad_rules(
        **_passing_call_kwargs(
            gex_profile_data=_base_gex_profile_data(call_wall=105.0)  # < spot(110)
        )
    )
    rule3 = next(c for c in result.checks if c.name == "rule_3_no_physical_cap")
    assert rule3.passed is False


# --- 規則四：主力 BTO Call 買盤確認 (DTE >= 7 且 ratio >= 0.8) ---


def test_rule4_fails_without_any_bto_call() -> None:
    result = check_entry_ironclad_rules(**_passing_call_kwargs(uoa_list=[]))
    assert result.all_passed is False
    rule4 = next(c for c in result.checks if c.name == "rule_4_bto_call_conviction")
    assert rule4.passed is False


def test_rule4_fails_when_dte_below_threshold() -> None:
    weak_dte = _valid_bto_call(ratio=1.0, dte_days=3)  # < 7
    result = check_entry_ironclad_rules(**_passing_call_kwargs(uoa_list=[weak_dte]))
    rule4 = next(c for c in result.checks if c.name == "rule_4_bto_call_conviction")
    assert rule4.passed is False


def test_rule4_fails_when_ratio_below_threshold() -> None:
    """刻意的差異點：既有六重鐵律 condition4 只檢查 DTE，不檢查 ratio；
    本鐵律額外要求 ratio >= 0.8，ratio 不足時即使 DTE 合格仍須判定未通過。"""
    weak_ratio = _valid_bto_call(ratio=0.5, dte_days=30)
    result = check_entry_ironclad_rules(**_passing_call_kwargs(uoa_list=[weak_ratio]))
    rule4 = next(c for c in result.checks if c.name == "rule_4_bto_call_conviction")
    assert rule4.passed is False


def test_rule4_passes_at_exact_thresholds() -> None:
    exact = _valid_bto_call(ratio=0.8, dte_days=7)
    result = check_entry_ironclad_rules(**_passing_call_kwargs(uoa_list=[exact]))
    rule4 = next(c for c in result.checks if c.name == "rule_4_bto_call_conviction")
    assert rule4.passed is True


def test_rule4_ignores_sto_and_put_entries() -> None:
    """非 CALL+BTO 的雜訊項目 (STO/PUT) 不應被誤判為有效買盤。"""
    noise = [
        {
            "type": "PUT",
            "action": "BTO",
            "strike": 90.0,
            "ratio": 2.0,
            "expiry": _future_expiry(30),
        },
        {
            "type": "CALL",
            "action": "STO",
            "strike": 130.0,
            "ratio": 2.0,
            "expiry": _future_expiry(30),
        },
    ]
    result = check_entry_ironclad_rules(**_passing_call_kwargs(uoa_list=noise))
    rule4 = next(c for c in result.checks if c.name == "rule_4_bto_call_conviction")
    assert rule4.passed is False


# --- 邊界輸入型別防禦 ---


def test_handles_none_gex_profile_data_and_empty_uoa_list_gracefully() -> None:
    """gex_profile_data=None、uoa_list 空清單時不得拋出例外，應優雅降級為
    全數未通過。"""
    result = check_entry_ironclad_rules(
        candidate_symbol="TEST",
        target_spot=110.0,
        gex_profile_data=None,
        uoa_list=[],
        price_15m_close=110.0,
        volume_15m=1000.0,
        volume_15m_sma20=1000.0,
    )
    assert result.all_passed is False
    assert len(result.checks) == 4
