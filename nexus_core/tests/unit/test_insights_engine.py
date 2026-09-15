import pytest
from market_analysis.insights_engine import InsightsEngine, RiskInsightsContext


class RadarRenderer:
    @staticmethod
    def format_row(context: RiskInsightsContext) -> str:
        dmp_label, status_label, suggestion = InsightsEngine.generate_cro_insight(
            context
        )
        # Mocking the radar row output formatting
        return f"{context.symbol} {context.current_price} {context.iv_rank} {context.put_wall} {dmp_label} {status_label}"


@pytest.mark.asyncio
async def test_narrative_trap_override() -> None:
    context = RiskInsightsContext(
        symbol="SPY",
        current_price=254.30,
        put_wall=250.00,
        net_gex_status="NEGATIVE_GAMMA_ZONE",
        term_structure=1.0,
        uoa_institutional_short_call=False,
        iv_rank=0.5,
        max_pain_deviation_pct=0.0912,
        can_trade_spreads=True,
        cash_reserve_protection=True,
        expected_move_lower=240.0,
        has_positive_gamma_support=False,
        cb_triggered=False,
    )
    dmp_label, status_label, suggestion = InsightsEngine.generate_cro_insight(context)
    assert "磁吸回升" not in (status_label or "")
    assert "底牆保衛" in (status_label or "") or "嚴防破位" in (status_label or "")


def test_d_mp_logic_blocking_on_put_wall_breach() -> None:
    # 測試當現價跌破 PutWall 時，D-MP 是否成功阻斷，且防守狀態正確觸發
    context = RiskInsightsContext(
        symbol="RKLB",
        current_price=83.41,
        put_wall=85.00,  # 實質破位
        net_gex_status="UNKNOWN",
        term_structure=1.02,
        uoa_institutional_short_call=True,
        can_trade_spreads=False,
        cash_reserve_protection=True,
        iv_rank=0.588,
        max_pain_deviation_pct=0.1749,
    )

    # 呼叫 UI/雷達渲染格式化邏輯
    radar_row = RadarRenderer.format_row(context)

    # 斷言：原本的超跌磁吸標籤與火箭圖示必須被徹底抹殺阻斷
    assert "超跌磁吸" not in radar_row
    assert "🚀" not in radar_row

    # 斷言：強制替換為底牆破位標籤與鐵律一狀態
    assert "[底牆破位]" in radar_row
    assert "🛑 觸發鐵律一：左側禁區 0%" in radar_row


def test_compute_realtime_insights_with_all_none_values() -> None:
    """測試 compute_realtime_insights 在所有數值欄位為 None 時不拋出異常。"""
    from market_analysis.insight_generator import compute_realtime_insights

    none_data = {
        "symbol": "NONE_SYM",
        "spot": None,
        "max_pain": None,
        "put_wall": None,
        "gex_status": None,
        "uoa_calls_vol": None,
        "uoa_puts_vol": None,
        "skew_percentile": None,
        "iv_rank": None,
    }
    result = compute_realtime_insights(none_data)
    assert isinstance(result, str)
    assert "NONE_SYM" in result


def test_compute_realtime_insights_preserves_zero_values_without_truthiness_fallback() -> (
    None
):
    """測試 0.0 合法值不會被 or 50.0 預設值錯誤覆蓋。"""
    from market_analysis.insight_generator import compute_realtime_insights

    zero_data = {
        "symbol": "ZERO_SYM",
        "spot": 100.0,
        "max_pain": 80.0,  # dist_pct = +25%
        "put_wall": 0.0,
        "gex_status": "POSITIVE",
        "uoa_calls_vol": 0.0,
        "uoa_puts_vol": 0.0,
        "skew_percentile": 0.0,  # 合法 0 分位（<= 30.0，看漲亢奮）
        "iv_rank": 0.0,  # 合法 0 分位（< 15.0，絕對低位）
    }
    result = compute_realtime_insights(zero_data)
    # 若 skew_percentile 被 0.0 or 50.0 篡改為 50.0，將不會觸發 🚀 情緒面呈現 Call 偏斜亢奮
    assert "🚀" in result or "Call 偏斜亢奮" in result
    # 若 iv_rank 被 0.0 or 50.0 篡改為 50.0，將不會觸發 IVR 處於絕對低位
    assert "IVR 處於絕對低位" in result


def test_clean_float_rejects_booleans_and_non_finite_values() -> None:
    """測試 _clean_float 嚴格過濾 boolean 及 non-finite (NaN, Inf, -Inf) 數值。"""
    from market_analysis.insight_generator import _clean_float

    assert _clean_float(None, 50.0) == 50.0
    assert _clean_float(False, 50.0) == 50.0
    assert _clean_float(True, 50.0) == 50.0
    assert _clean_float(float("nan"), 50.0) == 50.0
    assert _clean_float(float("inf"), 50.0) == 50.0
    assert _clean_float(float("-inf"), 50.0) == 50.0
    assert _clean_float("nan", 50.0) == 50.0
    assert _clean_float("inf", 50.0) == 50.0
    assert _clean_float("invalid", 50.0) == 50.0
    assert _clean_float(0.0, 50.0) == 0.0
    assert _clean_float(0, 50.0) == 0.0
    assert _clean_float(75.5, 50.0) == 75.5


def test_compute_realtime_insights_with_non_dict_data() -> None:
    """測試 compute_realtime_insights 遇到非 dict 輸入時安全回傳空字串。"""
    from market_analysis.insight_generator import compute_realtime_insights

    assert compute_realtime_insights(None) == ""  # type: ignore[arg-type]
    assert compute_realtime_insights([]) == ""  # type: ignore[arg-type]
    assert compute_realtime_insights("string") == ""  # type: ignore[arg-type]
