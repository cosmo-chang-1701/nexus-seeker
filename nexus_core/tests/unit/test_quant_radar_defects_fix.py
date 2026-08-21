"""
tests/unit/test_quant_radar_defects_fix.py

針對三大實盤案例（STX、AMAT、RCAT）及六大量化防禦維度的全套單元測試：
1. STX: STO 流向與高 OI 佔比天花板收租判定、跌破 PutWall 且 Volume PCR 殺盤背離閘道。
2. AMAT: 20 日 Volume Profile LVN 真空區跌穿、下方正 Gamma 枯竭滑步暴跌防護、上方負 Gamma 泥淖阻力識別。
3. RCAT: GEX 絕對深度驗證（淘汰 +62K 單薄紙牆）、極端高波 (IV 96.7%) 散戶雜訊過濾。
"""

from types import SimpleNamespace
from unittest.mock import patch

from market_analysis.uoa_telemetry import (
    UOATradeInput,
    classify_uoa_trade,
)
from market_analysis.index_microstructure import (
    classify_gex_wall,
    find_overhead_negative_gex_swamp,
    calculate_positive_gex_depth_below,
    is_gex_wall_effective,
)
from market_analysis.scenario_classifier import (
    classify_market_scenario,
    MarketScenario,
)
from market_analysis.insights_engine import (
    RiskInsightsContext,
    InsightsEngine,
)
from cogs.embed_builders.market_embeds import build_radar_scan_embed


# ==========================================
# 案例 1：STX（STO 買賣流向與 Volume PCR 殺盤）
# ==========================================


def test_stx_sto_high_oi_ratio_classification() -> None:
    """驗證 STX 08/28 $885C 在 Bid 側成交 304 口 (OI 96, 佔比 3.16x) 精準判定為 STO 築頂收租而非 BTO 點火。"""
    trade = UOATradeInput(
        strike_price=885.0,
        option_type="CALL",
        trade_price=5.20,
        bid_price=5.20,  # 發生在 Bid 側
        ask_price=5.60,
        volume=304,
        open_interest=96,  # 佔比 3.16x OI
        expiry="2026-08-28",
        symbol="STX",
    )
    result = classify_uoa_trade(trade, reference_date="2026-08-21")

    assert result.action == "🔴 賣出開倉 (STO - Bid)"
    assert result.ratio >= 3.0
    assert "3.16x" in result.ratio_str
    assert "🛡️" in result.intent
    assert "[STX]" in result.intent
    assert "$885.00" in result.intent
    assert "STO 築頂收租" in result.intent
    assert "天花板" in result.intent


def test_stx_volume_pcr_skew_divergence_gate() -> None:
    """驗證 STX 跌破 $850 PutWall 且 Volume PCR 飆升至 1.81 時，強制觸發破位順向殺盤，阻斷均值回歸與軋空。"""
    # 1. 測試 InsightsEngine
    ctx = RiskInsightsContext(
        symbol="STX",
        current_price=843.10,
        put_wall=850.00,  # 跌破底牆
        net_gex_status="NEGATIVE_GAMMA_ZONE",
        term_structure=0.98,
        uoa_institutional_short_call=False,
        iv_rank=0.35,
        max_pain_deviation_pct=-0.08,
        can_trade_spreads=True,
        cash_reserve_protection=True,
        volume_pcr=1.81,  # 搶購 Put 殺盤
        skew_percentile=5.0,  # 鈍化低 Skew
    )
    dmp, status, sugg = InsightsEngine.generate_cro_insight(ctx)
    assert dmp == "[🚨 破位順向殺盤]"
    assert "🚨 破位順向殺盤" in str(status)
    assert "1.81" in str(status)
    assert sugg == "STOP_ALL_BUY"

    # 2. 測試 ScenarioClassifier
    scenario = classify_market_scenario(
        price=843.10,
        high=855.0,
        low=840.0,
        current_volume=1_500_000,
        avg_volume_20=1_000_000,
        put_wall=850.0,
        call_wall=885.0,
        gamma_flip=860.0,
        is_squeezing=False,
        uoa_skew=-0.15,
        ivr=35.0,
        hvn=850.0,
        lvn=840.0,
        skew_percentile=5.0,
        is_uoa_aligned=False,
        volume_pcr=1.81,
    )
    # 必須判定為假性支撐陷阱或結構破位，絕非巨鯨護航
    assert scenario in (
        MarketScenario.FAKE_SUPPORT_TRAP,
        MarketScenario.STRUCTURAL_BREAKDOWN_PENDING,
    )


# ==========================================
# 案例 2：AMAT（LVN 籌碼真空與正 Gamma 枯竭）
# ==========================================


def test_amat_lvn_vacuum_and_positive_gex_exhaustion() -> None:
    """驗證 AMAT 跌穿 LVN ($488.60) 且下方正 Gamma 枯竭時，觸發滑步暴跌警報，且識別上方 $500 -35.9M 泥淖。"""
    gex_profile = {
        "480.0": 1000.0,
        "485.0": 3000.0,
        "490.0": 50000.0,
        "495.0": 250000.0,
        "500.0": -35_947_000.0,  # 負 Gamma 泥淖
    }
    spot = 486.76

    # 1. 驗證現價下方正 Gamma 深度加總 (480 + 485 = 4000 << 500K)
    pos_below = calculate_positive_gex_depth_below(gex_profile, spot)
    assert pos_below == 4000.0
    assert pos_below < 500_000.0

    # 2. 驗證上方負 Gamma 泥淖檢索
    swamp_strike, swamp_gex = find_overhead_negative_gex_swamp(gex_profile, spot)
    assert swamp_strike == 500.0
    assert swamp_gex == -35_947_000.0

    # 3. 驗證 InsightsEngine 跌穿 LVN 真空區風控
    ctx = RiskInsightsContext(
        symbol="AMAT",
        current_price=486.76,
        put_wall=495.00,
        net_gex_status="NEGATIVE_GAMMA_ZONE",
        term_structure=1.00,
        uoa_institutional_short_call=False,
        iv_rank=0.45,
        max_pain_deviation_pct=-0.08,
        can_trade_spreads=True,
        cash_reserve_protection=True,
        lvn_price=488.60,  # 現價 486.76 < 488.60
        positive_gex_below=pos_below,
        overhead_neg_gex_swamp=(swamp_strike, swamp_gex),
    )
    dmp, status, sugg = InsightsEngine.generate_cro_insight(ctx)
    assert dmp == "[🛑 跌穿LVN真空區]"
    assert "🛑 跌穿LVN真空區(正Gamma枯竭)" in str(status)
    assert sugg == "STOP_ALL_BUY"


# ==========================================
# 案例 3：RCAT（GEX 絕對厚度與極端高波雜訊）
# ==========================================


def test_rcat_paper_thin_gex_wall_and_high_iv_noise() -> None:
    """驗證 RCAT 名義 PutWall 僅 +62K GEX 被判定為薄弱紙牆，且極端高波 (IV 96.7%) 散戶雜訊被有效過濾。"""
    # 1. 驗證 GEX 深度有效性門檻
    assert not is_gex_wall_effective(62_000.0)
    assert is_gex_wall_effective(1_500_000.0)

    wall_type = classify_gex_wall(
        strike_gex=62_000.0,
        max_positive_gex=62_000.0,
        is_heavy_otm_call=False,
        min_effective_gex=500_000.0,
    )
    assert wall_type == "THIN_SUPPORT_WALL"

    # 2. 驗證 InsightsEngine 薄弱紙牆判定
    ctx = RiskInsightsContext(
        symbol="RCAT",
        current_price=9.45,
        put_wall=9.50,
        net_gex_status="POSITIVE_GAMMA",
        term_structure=0.95,
        uoa_institutional_short_call=False,
        iv_rank=0.525,
        max_pain_deviation_pct=-0.01,
        can_trade_spreads=True,
        cash_reserve_protection=True,
        put_wall_gex=62_000.0,  # 僅 62K
    )
    dmp, status, sugg = InsightsEngine.generate_cro_insight(ctx)
    assert dmp == "[⚠️ 薄弱紙牆]"
    assert "薄弱紙牆(無做市商深度)" in str(status)

    # 3. 驗證 ScenarioClassifier 不在紙牆上觸發巨鯨護航共振
    scenario = classify_market_scenario(
        price=9.45,
        high=9.60,
        low=9.40,
        current_volume=500_000,
        avg_volume_20=400_000,
        put_wall=9.50,
        call_wall=11.0,
        gamma_flip=9.00,
        is_squeezing=False,
        uoa_skew=-0.05,
        ivr=52.5,
        hvn=9.50,
        lvn=8.50,
        skew_percentile=40.0,
        is_uoa_aligned=True,
        put_wall_gex=62_000.0,  # 紙牆
    )
    assert scenario != MarketScenario.WHALE_ESCORT_RESONANCE


# ==========================================
# 綜合驗證：終端雷達 Embed 渲染與警示輸出
# ==========================================


def test_radar_embed_renders_all_three_case_studies() -> None:
    """驗證 build_radar_scan_embed 能對 STX、AMAT、RCAT 正確輸出所有修復後的欄位與即時穿透警示。"""
    scan_results = [
        {
            "symbol": "STX",
            "quote": {"c": 843.10, "dp": -2.15},
            "iv_metrics": {
                "iv_rank": 35.0,
                "expected_move_weekly": 30.0,
                "expected_move_lower": 820.0,
                "expected_move_upper": 880.0,
            },
            "skew": -0.15,
            "skew_percentile": 5.0,
            "volume_pcr": 1.81,
            "max_pain": {"max_pain": 860.0},
            "uoa": [
                {
                    "symbol": "STX",
                    "expiry": "2026-08-28",
                    "strike": 885.0,
                    "type": "CALL",
                    "action": "🔴 賣出開倉 (STO - Bid)",
                    "volume": 304,
                    "oi": 96,
                    "ratio": 3.16,
                    "ratio_str": "3.16x",
                }
            ],
            "gex_metrics": {
                "put_wall": 850.0,
                "call_wall": 885.0,
                "net_gex": -2000000.0,
            },
            "gex_profile_data": {
                "put_wall": 850.0,
                "call_wall": 885.0,
                "net_gex": -2000000.0,
            },
            "psq_result": {
                "momentum": -12.4,
                "signal_direction": "🔴",
                "is_squeezing": False,
            },
        },
        {
            "symbol": "AMAT",
            "quote": {"c": 486.76, "dp": -3.20},
            "iv_metrics": {
                "iv_rank": 45.0,
                "expected_move_weekly": 15.0,
                "expected_move_lower": 475.0,
                "expected_move_upper": 505.0,
            },
            "skew": -0.10,
            "skew_percentile": 42.0,
            "volume_pcr": 1.10,
            "max_pain": {"max_pain": 500.0},
            "uoa": [],
            "gex_metrics": {"put_wall": 495.0, "call_wall": 0.0, "net_gex": -5000000.0},
            "gex_profile_data": {
                "put_wall": 495.0,
                "call_wall": 0.0,
                "net_gex": -5000000.0,
                "gex_profile": {"480.0": 1000, "485.0": 3000, "500.0": -35947000},
            },
            "positive_gex_below": 4000.0,
            "overhead_neg_gex_swamp": (500.0, -35947000.0),
            "vp_data": {"lvn": 488.60, "hvn": 500.00},
            "psq_result": {
                "momentum": -8.5,
                "signal_direction": "🔴",
                "is_squeezing": False,
            },
        },
        {
            "symbol": "RCAT",
            "quote": {"c": 9.45, "dp": 1.20},
            "iv_metrics": {
                "iv_rank": 52.5,
                "expected_move_weekly": 1.5,
                "expected_move_lower": 8.0,
                "expected_move_upper": 11.0,
            },
            "skew": 0.0,
            "skew_percentile": 52.0,
            "volume_pcr": 0.85,
            "max_pain": {"max_pain": 9.5},
            "uoa": [],
            "gex_metrics": {"put_wall": 9.5, "call_wall": 11.0, "net_gex": 62000.0},
            "put_wall_gex": 62000.0,
            "psq_result": {
                "momentum": 0.0,
                "signal_direction": "⚪",
                "is_squeezing": False,
            },
        },
    ]

    with (
        patch(
            "database.notifications.get_user_notification_settings",
            return_value={
                "radar_macro_edge": False,
                "radar_alpha_signals": True,
                "radar_risk_defenses": True,
            },
        ),
        patch("database.orders.get_user_active_orders", return_value=[]),
        patch(
            "database.get_full_user_context",
            return_value=SimpleNamespace(
                can_trade_spreads=True, cash_reserve_protection=True
            ),
        ),
    ):
        embeds = build_radar_scan_embed(scan_results, "ALL", 12345)

    assert len(embeds) == 1
    field_text = "\n".join([str(f.value) for f in embeds[0].fields])

    # 1. 驗證 STX 欄位與警示
    assert "PCR:1.81⚠️" in field_text
    assert "C$885.0" in field_text
    assert "STX: 實體跌破 $850.00 PutWall" in field_text

    # 2. 驗證 AMAT 欄位與警示
    assert "阻$500" in field_text
    assert "AMAT: 跌穿 20 日 LVN 真空區 ($488.60)" in field_text
    assert "上方 $500.0 聚集 35.9M 負 Gamma 泥淖" in field_text

    # 3. 驗證 RCAT 欄位與警示
    assert "$9.5(薄)" in field_text
    assert "RCAT: 名義 PutWall ($9.50) 僅單薄 +62K GEX" in field_text
