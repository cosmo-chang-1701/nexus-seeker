from market_analysis.insights_engine import RiskInsightsContext, InsightsEngine


class RadarRenderer:
    @staticmethod
    def format_row(context: RiskInsightsContext) -> str:
        dmp_label, status_label, suggestion = InsightsEngine.generate_cro_insight(
            context
        )
        # Mocking the radar row output formatting
        return f"{context.symbol} {context.current_price} {context.iv_rank} {context.put_wall} {dmp_label} {status_label}"


def test_d_mp_logic_blocking_on_put_wall_breach():
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
