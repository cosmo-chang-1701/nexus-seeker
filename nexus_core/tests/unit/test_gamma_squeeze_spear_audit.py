"""
test_gamma_squeeze_spear_audit.py — 針對 Gamma 擠壓 SPEAR 進攻訊號 7 大缺陷修復的專項單元測試。

測試覆蓋：
1. 決策邏輯全局風控覆蓋機制 (Global Risk Override / Hard Lock)
2. 目標價與微觀結構校準 (GEX Peak, >= 5% 空間, STO 剛性物理封頂與負 GEX 否決)
3. 波動率體制即時判定 (IVR > 50% 高波劇烈洗盤環境，嚴禁溫和標籤，限制裸買 OTM)
4. Phase A 開盤時段加權修正 (門檻調高 30%，RVOL_15m >= 1.5 右側驗證)
5. 門檻微觀代理指標採樣 (RVOL_15m，DTE >= 7 且 Vol/OI >= 0.8x 主力單)
6. 跨資產對沖模型基準 (標的內部對沖首選，SPY 納入動態 Beta 與價格比率)
7. 分數凱利倉位配比 (3%~5% 絕對硬上限，存活跑道告急強制歸零)
"""

import pytest
from market_analysis.gamma_squeeze_engine import NexusGammaSqueezeEngine
from market_analysis.models.trader_models import (
    TraderAccountState,
    OptionHolding,
    TickerMarketData,
)


@pytest.fixture
def engine() -> NexusGammaSqueezeEngine:
    return NexusGammaSqueezeEngine(base_gate_3_threshold=1000000.0)


@pytest.fixture
def base_account() -> TraderAccountState:
    return TraderAccountState(
        capital=100000.0,
        cash_reserve=25000.0,
        monthly_burn_rate=6000.0,  # daily burn = 200, runway = 125 days
        current_vix=14.0,
    )


@pytest.fixture
def base_market_data() -> TickerMarketData:
    return TickerMarketData(
        ticker="TSLA",
        spot_price=364.36,
        market_cap_billion=1100.0,
        avg_option_volume=80000,
        days_until_earnings=20,
        tomorrow_expiring_otm_calls_premium=1500000.0,
        iv_rank=40.0,
        option_skew=0.08,
    )


# ==============================================================================
# 1. 決策邏輯自我衝突（進攻指令 vs 生存風控熔斷）
# ==============================================================================
def test_item1_global_risk_override_hard_lock(
    engine: NexusGammaSqueezeEngine, base_market_data: TickerMarketData
) -> None:
    """當生存跑道 < 30 天時，強制硬鎖為 SHIELD，遮蔽並阻斷所有方向性期權買方開倉訊號。"""
    critical_account = TraderAccountState(
        capital=5000.0,
        cash_reserve=1400.0,
        monthly_burn_rate=6000.0,  # daily burn = 200, runway = 7 days (< 30)
        current_vix=14.0,
    )
    # Holdings with zero theta
    holdings = [OptionHolding(symbol="TSLA", quantity=1.0, theta=0.0)]
    greeks = {"vanna": 1.0, "beta": 1.5}

    output = engine.analyze_ticker(
        data=base_market_data,
        account_state=critical_account,
        options_holdings=holdings,
        portfolio_greeks=greeks,
        market_phase="Phase B",
    )

    assert output.financial_runway_days == 7
    # 全局風控硬鎖：SDDM 路由強制覆寫為 SHIELD，禁止 SPEAR
    assert output.sddm_route == "SHIELD"
    # 凱利倉位強制歸零
    assert output.kelly_position_scaling == 0.0

    # 建議動作不得輸出買入 OTM Call
    all_actions_text = " ".join(output.recommended_actions)
    assert "建議分批建立 OTM Call" not in all_actions_text
    assert "建議建立 OTM Call" not in all_actions_text
    assert "嚴禁建立 OTM Call" in all_actions_text
    assert "存活風控硬鎖" in all_actions_text
    assert "全局風控覆蓋機制已啟動" in all_actions_text

    # 風控備註標記存活熔斷
    assert "存活風控硬鎖" in output.risk_mitigation_notes
    assert "觸發全局熔斷" in output.risk_mitigation_notes


# ==============================================================================
# 2. 目標價與微觀結構倒置（將阻力牆與負 GEX 斷層誤判為磁吸目標）
# ==============================================================================
def test_item2_target_price_positive_gex_peak(
    engine: NexusGammaSqueezeEngine, base_account: TraderAccountState
) -> None:
    """Gamma 磁吸目標必須為具備實體正 Gamma 深度之節點 (GEX Peak)，且向上空間 >= 5%"""
    data = TickerMarketData(
        ticker="AAPL",
        spot_price=100.0,
        market_cap_billion=2500.0,
        avg_option_volume=100000,
        days_until_earnings=20,
        tomorrow_expiring_otm_calls_premium=1500000.0,
        iv_rank=30.0,
        option_skew=0.06,
        # 102.0: 空間 2% (< 5%) 不得選
        # 105.0: 空間 5%, GEX 10k
        # 110.0: 空間 10%, GEX 50k (Peak)
        # 115.0: 負 GEX 斷層 (-20k)
        gex_profile={
            "102.0": 80000.0,
            "105.0": 10000.0,
            "110.0": 50000.0,
            "115.0": -20000.0,
        },
    )
    output = engine.analyze_ticker(
        data=data,
        account_state=base_account,
        options_holdings=[],
        portfolio_greeks={"vanna": 0.5, "beta": 1.0},
        market_phase="Phase B",
    )
    assert output.sddm_route == "SPEAR"
    assert output.magnet_target == 110.0  # 空間 >= 5% 且正 GEX 最高峰


def test_item2_tsla_microstructure_veto_due_to_sto_and_negative_gex(
    engine: NexusGammaSqueezeEngine, base_account: TraderAccountState
) -> None:
    """實例驗證：TSLA 現價 $364.36，目標 $365 空間不足 0.2%，且為最大負 Gamma 與 5.51x OI STO 剛性封頂，引擎應直接否決進攻"""
    data = TickerMarketData(
        ticker="TSLA",
        spot_price=364.36,
        market_cap_billion=1100.0,
        avg_option_volume=90000,
        days_until_earnings=25,
        tomorrow_expiring_otm_calls_premium=2000000.0,
        iv_rank=30.0,
        option_skew=0.08,
        call_wall=365.0,  # 距離 < 0.2%
        net_gex=-19143083.0,  # 實質負 Gamma
        physical_cap_strikes=[
            {
                "strike": 365.0,
                "type": "CALL",
                "volume": 37831,
                "oi": 6865,
                "ratio": 5.51,  # > 1.0x OI 天量封頂
                "action": "STO",
            }
        ],
    )
    output = engine.analyze_ticker(
        data=data,
        account_state=base_account,
        options_holdings=[],
        portfolio_greeks={"vanna": 1.0, "beta": 1.5},
        market_phase="Phase B",
    )
    # 否決進攻：sddm_route 強制轉為 SHIELD
    assert output.sddm_route == "SHIELD"
    failed_reasons = " ".join(output.failed_gates)
    assert "微觀結構否決" in failed_reasons or "空間不足否決" in failed_reasons


def test_item2_sto_cap_filters_out_candidate_peak(
    engine: NexusGammaSqueezeEngine, base_account: TraderAccountState
) -> None:
    """若最高峰候選履約價遭遇 > 1.0x OI 之 STO 封頂，應自動跳過並選取下一個安全峰值；若皆被封頂則否決"""
    data = TickerMarketData(
        ticker="TEST",
        spot_price=100.0,
        market_cap_billion=50.0,
        avg_option_volume=60000,
        days_until_earnings=20,
        tomorrow_expiring_otm_calls_premium=1200000.0,
        iv_rank=35.0,
        option_skew=0.06,
        gex_profile={"110.0": 100000.0, "115.0": 40000.0},
        physical_cap_strikes=[
            {
                "strike": 110.0,
                "type": "CALL",
                "ratio": 1.8,
                "action": "STO",
            }  # 110 遭遇封頂
        ],
    )
    output = engine.analyze_ticker(
        data=data,
        account_state=base_account,
        options_holdings=[],
        portfolio_greeks={},
        market_phase="Phase B",
    )
    assert output.sddm_route == "SPEAR"
    assert output.magnet_target == 115.0  # 110 被封頂過濾，降至 115 安全峰值


# ==============================================================================
# 3. 波動率體制判定失真（溫和低波 vs 極端高波）
# ==============================================================================
def test_item3_high_iv_regime_switch(
    engine: NexusGammaSqueezeEngine, base_account: TraderAccountState
) -> None:
    """當 IVR > 50% 時，體制識別必須強制切換為「高波劇烈洗盤環境」，嚴禁使用「溫和」標籤，並限制裸買 OTM 期權"""
    data = TickerMarketData(
        ticker="TSLA",
        spot_price=364.36,
        market_cap_billion=1100.0,
        avg_option_volume=90000,
        days_until_earnings=25,
        tomorrow_expiring_otm_calls_premium=1500000.0,
        iv_rank=69.5,  # IVR > 50%
        realtime_iv=0.427,
        option_skew=0.08,
    )
    output = engine.analyze_ticker(
        data=data,
        account_state=base_account,
        options_holdings=[],
        portfolio_greeks={"vanna": 1.0, "beta": 1.5},
        market_phase="Phase B",
    )
    # 嚴禁使用「溫和」標籤
    assert "溫和" not in output.risk_mitigation_notes
    assert "【高波劇烈洗盤環境】" in output.risk_mitigation_notes
    assert "IV Rank: 69.5%" in output.risk_mitigation_notes
    assert "42.7%" in output.risk_mitigation_notes

    # 推薦動作限制裸買 OTM，改採 Bull Call Spread
    actions_text = " ".join(output.recommended_actions)
    assert "限制裸買 OTM 期權" in actions_text
    assert "牛市認購價差 (Bull Call Spread)" in actions_text


# ==============================================================================
# 4. 開盤時段流動性門檻逆向放寬（放大滑價風險）
# ==============================================================================
def test_item4_phase_a_threshold_increased(engine: NexusGammaSqueezeEngine) -> None:
    """Phase A 開盤時段流動性門檻應調高 30%（嚴禁逆向放寬）"""
    data = TickerMarketData(
        ticker="AAPL",
        spot_price=150.0,
        market_cap_billion=2000.0,
        avg_option_volume=70000,
        days_until_earnings=20,
        tomorrow_expiring_otm_calls_premium=1200000.0,  # $1.2M
        iv_rank=60.0,
        option_skew=0.06,
    )
    # Phase B 門檻 $1.0M -> 通過
    passed_b, _ = engine.validate_gates(data, "Phase B")
    assert passed_b is True

    # Phase A 門檻調高 30% 至 $1.3M -> $1.2M 應判定不通過
    passed_a, failed_a = engine.validate_gates(data, "Phase A")
    assert passed_a is False
    assert any("資金效率不足" in f for f in failed_a)


def test_item4_phase_a_rvol_15m_requirement(engine: NexusGammaSqueezeEngine) -> None:
    """Phase A 要求 15m 實體 K 棒放量驗證 RVOL_15m >= 1.5x SMA20"""
    data_low_rvol = TickerMarketData(
        ticker="AAPL",
        spot_price=150.0,
        market_cap_billion=2000.0,
        avg_option_volume=70000,
        days_until_earnings=20,
        tomorrow_expiring_otm_calls_premium=1500000.0,
        iv_rank=60.0,
        option_skew=0.06,
        rvol_15m=1.2,  # < 1.5x in Phase A
    )
    passed_a, failed_a = engine.validate_gates(data_low_rvol, "Phase A")
    assert passed_a is False
    assert any("RVOL_15m" in f and "1.5x" in f for f in failed_a)

    # 達標 1.6x 應通過
    data_high_rvol = data_low_rvol.model_copy(update={"rvol_15m": 1.6})
    passed_a2, failed_a2 = engine.validate_gates(data_high_rvol, "Phase A")
    assert passed_a2 is True


# ==============================================================================
# 5. 門檻代理數據採樣漏洞（線性外推誤差與末日雜訊滲透）
# ==============================================================================
def test_item5_rvol_15m_gate1_evaluation(engine: NexusGammaSqueezeEngine) -> None:
    """Gate 1 改用標準微觀指標 RVOL_15m = Volume_15m / SMA20，取消跨時段線性外推"""
    data = TickerMarketData(
        ticker="NVDA",
        spot_price=120.0,
        market_cap_billion=3000.0,
        avg_option_volume=10000,  # 即使日均量欄位不足
        days_until_earnings=20,
        tomorrow_expiring_otm_calls_premium=1500000.0,
        iv_rank=55.0,
        option_skew=0.06,
        rvol_15m=1.25,  # RVOL >= 1.0 (Phase B) 通過
    )
    passed, failed = engine.validate_gates(data, "Phase B")
    assert passed is True
    assert len(failed) == 0


# ==============================================================================
# 6. 跨資產對沖模型基準失真（忽略 Beta 與基差風險）
# ==============================================================================
def test_item6_cross_asset_hedging_beta_and_price_ratio(
    engine: NexusGammaSqueezeEngine, base_account: TraderAccountState
) -> None:
    """跨資產對沖必須納入動態 Beta 與價格比率，且首選標的內部 Delta 平衡"""
    data = TickerMarketData(
        ticker="TSLA",
        spot_price=364.36,
        market_cap_billion=1100.0,
        avg_option_volume=90000,
        days_until_earnings=20,
        tomorrow_expiring_otm_calls_premium=1500000.0,
        iv_rank=40.0,
        option_skew=0.06,
    )
    # Vanna = 2.0, Beta = 1.8, d_vol = 0.10 -> hidden_delta = 0.20 -> hidden_delta_shares = 20.0
    # 首選內部對沖: SELL 20 股 TSLA
    # 次選 SPY (spy_price = 500.0): 20 * 1.8 * (364.36 / 500.0) = 36 * 0.72872 = 26.23 -> SELL 26 單位 SPY
    output = engine.analyze_ticker(
        data=data,
        account_state=base_account,
        options_holdings=[],
        portfolio_greeks={"vanna": 2.0, "beta": 1.8, "spy_price": 500.0},
        market_phase="Phase B",
    )
    instr = output.vanna_hedging_instruction or ""
    # 絕不可為 1:1 的 36 單位或 20 單位 SPY
    assert "SELL 賣出 20 股 TSLA" in instr
    assert "SELL 賣出 26 單位 SPY" in instr
    assert "Beta=1.80" in instr
    assert "價格比=0.73" in instr


# ==============================================================================
# 7. 凱利倉位配比過度激進（單筆 OTM 買方配置 15%）
# ==============================================================================
def test_item7_fractional_kelly_and_hard_caps(
    engine: NexusGammaSqueezeEngine,
    base_market_data: TickerMarketData,
    base_account: TraderAccountState,
) -> None:
    """單筆方向性期權買方倉位硬性限制在 3%~5% 內；存活跑道告急時強制歸零"""
    # VIX < 15: 5.0%
    base_account.current_vix = 13.0
    out_low_vix = engine.analyze_ticker(
        data=base_market_data,
        account_state=base_account,
        options_holdings=[],
        portfolio_greeks={},
        market_phase="Phase B",
    )
    assert out_low_vix.kelly_position_scaling == 0.05  # 5.0%

    # 15 <= VIX < 25: 3.0%
    base_account.current_vix = 18.0
    out_mid_vix = engine.analyze_ticker(
        data=base_market_data,
        account_state=base_account,
        options_holdings=[],
        portfolio_greeks={},
        market_phase="Phase B",
    )
    assert out_mid_vix.kelly_position_scaling == 0.03  # 3.0%

    # 存活跑道告急 (Runway < 30 days): 強制歸零
    critical_account = TraderAccountState(
        capital=5000.0,
        cash_reserve=1000.0,
        monthly_burn_rate=6000.0,  # 5 days
        current_vix=13.0,
    )
    out_critical = engine.analyze_ticker(
        data=base_market_data,
        account_state=critical_account,
        options_holdings=[],
        portfolio_greeks={},
        market_phase="Phase B",
    )
    assert out_critical.kelly_position_scaling == 0.0  # 強制歸零
