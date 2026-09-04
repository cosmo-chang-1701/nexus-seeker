from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pandas as pd
import pytest

from cogs.embed_builders.rollover_embeds import (
    RolloverActionView,
    create_dynamic_rollover_embed,
)
from market_analysis.dynamic_rollover import (
    DynamicRolloverEngine,
    RolloverScenario,
)


@pytest.fixture
def engine() -> DynamicRolloverEngine:
    return DynamicRolloverEngine()


@pytest.fixture(autouse=True)
def _mock_target_reference_live_quote() -> Any:
    with patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={},
    ):
        yield


# ==============================================================================
# 情境一：NVDA 轉弱，SPCX 轉強符合轉倉條件 (Opportunity Cost Rotation)
# ==============================================================================


@pytest.mark.asyncio
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value="NORMAL",
)
@patch(
    "database.calendar_cache.get_cached_earnings",
    return_value={
        "earnings_date": (datetime.now() + timedelta(days=45)).strftime("%Y-%m-%d")
    },
)
@patch(
    "services.market_data_service.get_all_option_expiries",
    new_callable=AsyncMock,
    return_value=[(datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")],
)
@patch("services.market_data_service.get_history_df")
@patch("database.market_cache.get_market_cache")
async def test_drill_scenario_1_nvda_decay_spcx_breakout_triggers_rollover(
    mock_market_cache: MagicMock,
    mock_history_df: AsyncMock,
    mock_expiries: AsyncMock,
    mock_earnings: MagicMock,
    mock_regime: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    """
    演練情境一：
    1. NVDA 持倉動能衰退 (PSQ=10.0)，Adjusted EV 偏低 (0.0123)
    2. SPCX 動能突破 (PSQ=95.0)，Adjusted EV 高 (0.235)
    3. SPCX 六重進場過濾全數通過
    4. 期望值利差 > 5.5% 門檻，觸發 30% 機會成本轉倉至 SPCX
    5. 驗證 Discord Embed 渲染
    """

    # 1. Mock Market Cache
    def cache_side_effect(
        symbol: str, expiry: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        sym_u = symbol.upper()
        if sym_u == "NVDA":
            return {
                "reference_spot_price": 195.0,
                "expected_move_upper": 198.0,  # Base EV = 1.54%
                "is_stale": 0,
                "is_degraded": 0,
            }
        if sym_u == "SPCX":
            return {
                "reference_spot_price": 85.0,
                "expected_move_upper": 105.0,  # Base EV = 23.5%
                "is_stale": 0,
                "is_degraded": 0,
            }
        return None

    mock_market_cache.side_effect = cache_side_effect

    # 2. Mock SPCX 15m K 線 (條件一：15m 實體陽線收盤 $85.50 > Gamma Flip $80.00，
    # 成交量 2.5x 均量；open $82.00 < close $85.50 確保為陽線)
    df_data: Dict[str, List[float]] = {
        "Open": [80.0] * 20 + [82.00],
        "Close": [80.0] * 20 + [85.50],
        "Volume": [10000.0] * 20 + [25000.0],
    }
    mock_history_df.return_value = pd.DataFrame(df_data)

    # 3. 設定持倉與候選標的 Radar 數據
    portfolio_assets: List[Dict[str, Any]] = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "spot_price": 195.0,
            "avg_cost": 180.0,  # profit_pct = +8.3% <= 30%
            "quantity": 77.0,
            "current_value": 15000.0,
            "skew_percentile": 40.0,  # < 50% 下行懲罰
            "psq_result": {
                "squeeze_level": "Release",
                "signal_direction": "Neutral",
            },  # Normalized PSQ = 10.0 (< 20 衰退)
        }
    ]

    candidate_radar: Dict[str, Any] = {
        "quote": {"c": 85.0},
        "iv_metrics": {"iv_rank": 25.0},
        "psq_result": {
            "squeeze_level": "High",
            "signal_direction": "Long",
            "is_breakout_long": True,
        },  # Normalized PSQ = 95.0 (> 80 突破)
        "gex_profile_data": {
            "gex_profile": {
                "75.0": -500000.0,  # 累積 < 0
                "80.0": 1500000.0,  # 累積由負轉正 -> Gamma Flip 估算為 80.0，同時為 Support GEX Wall
                "95.0": -500000.0,  # Overhead Call Wall
            },
            "call_wall": 95.0,  # 距現價 11.8% > 5% (條件三通過)
            "put_wall": 80.0,
            "net_gex": 1500000.0,  # 正值 LONG_GAMMA (條件一淨 GEX regime 通過)
        },
        "uoa": [
            {
                "type": "CALL",
                "action": "BTO",
                "strike": 90.0,
                "expiry": (datetime.now() + timedelta(days=21)).strftime(
                    "%Y-%m-%d"
                ),  # DTE = 21 >= 7 (條件四通過)
                "ratio": 1.5,
            }
        ],
    }

    # 4. 執行機會成本評估
    already_flagged: set[str] = set()
    (
        instructions,
        _entry_confirmation,
    ) = await engine.evaluate_opportunity_cost_for_satellites(
        user_id=101,
        portfolio_assets=portfolio_assets,
        already_flagged_symbols=already_flagged,
        candidate_symbol="SPCX",
        candidate_radar=candidate_radar,
    )

    # 5. 驗證指令集
    assert len(instructions) == 1
    ins = instructions[0]
    assert ins["symbol"] == "NVDA"
    assert ins["action"] == "REDUCE"
    assert ins["sell_ratio"] == 0.30
    assert ins["target_core"] == "SPCX"
    assert ins["scenario"] == RolloverScenario.OPPORTUNITY_COST.value
    assert ins["suggested_strategy"] == "Buy Shares"
    assert ins["limit_price"] == 85.00
    assert ins["cash_impact"] == "$4,500"
    assert ins["is_manual_override_required"] is False
    assert "SPCX" in ins["reason"]

    # 6. 驗證 Discord Embed 渲染
    embed = create_dynamic_rollover_embed(
        rollover_type="機會成本轉倉",
        sell_symbol=ins["symbol"],
        sell_ratio=ins["sell_ratio"],
        buy_symbol=ins["target_core"],
        reason=ins["reason"],
        suggested_strategy=ins["suggested_strategy"],
        suggested_price=f"${ins['limit_price']:.2f} (限價)",
        strike="N/A",
        expiry="N/A",
        direction="BUY",
        sell_action="SELL",
        scenario=ins["scenario"],
        cash_impact=ins["cash_impact"],
        asset_class="SPOT",
    )

    assert isinstance(embed, discord.Embed)
    assert embed.title == "💡 機會成本轉倉: NVDA → SPCX"
    assert embed.color == discord.Color.blue()
    assert "NVDA" in str(embed.fields[0].value)
    assert "SELL (賣出現貨)" in str(embed.fields[0].value)
    assert "30%" in str(embed.fields[0].value)
    assert "SPCX" in str(embed.fields[1].value)
    assert "BUY (買入現貨)" in str(embed.fields[1].value)
    assert "Buy Shares" in str(embed.fields[1].value)
    assert "$4,500" in str(embed.fields[2].value)
    assert "$85.00" in str(embed.fields[2].value)
    assert "到期日" not in str(embed.fields[2].value)
    assert "履約價" not in str(embed.fields[2].value)

    # 7. 驗證 Action View
    view = RolloverActionView(target_symbol="NVDA")
    assert len(view.children) == 2
    assert getattr(view.children[0], "label", None) == "執行試算"
    assert getattr(view.children[1], "label", None) == "忽略"


# ==============================================================================
# 情境二：NVDA 轉弱，沒有標的符合轉倉條件 (No Candidate / Hold / Breakdown / Margin)
# ==============================================================================


@pytest.mark.asyncio
async def test_drill_scenario_2a_nvda_decay_no_target_safe_hold(
    engine: DynamicRolloverEngine,
) -> None:
    """
    子情境 2A：
    Watchlist 無高 EV 標的 (回傳 VOO)。
    S2 機會成本早退不發送指令；
    S3 持倉檢驗 NVDA 做市商底牆未破，發出安心防守卡 HOLD。
    """
    # 1. 驗證 S2 機會成本對 VOO 早退
    portfolio_assets: List[Dict[str, Any]] = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 15000.0,
            "quantity": 77.0,
            "spot_price": 195.0,
            "avg_cost": 180.0,
            "put_wall": 190.0,
            "call_wall": 210.0,
            "gamma_flip": 192.0,
            "atr_14": 3.0,
            "atr_15m": 3.0,
            "price_15m_close": 195.0,
            "sqz_mom": 0.5,
            "skew": 0.1,
            "max_allocation_pct": 0.30,
            "target_allocation_pct": 0.20,
        }
    ]

    (
        s2_instructions,
        s2_entry_confirmation,
    ) = await engine.evaluate_opportunity_cost_for_satellites(
        user_id=101,
        portfolio_assets=portfolio_assets,
        already_flagged_symbols=set(),
        candidate_symbol="VOO",
        candidate_radar=None,
    )
    assert s2_instructions == []
    assert s2_entry_confirmation is None

    # 2. 驗證 S3 核心衛星檢驗 -> 未破位，發出 HOLD 安心防守卡
    with patch(
        "market_analysis.dynamic_rollover.get_full_user_context"
    ) as mock_user_ctx, patch(
        "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
        new_callable=AsyncMock,
        return_value=False,
    ):
        mock_user_ctx.return_value = MagicMock(can_trade_spreads=False)

        # 總資產 $60,000，NVDA 佔 $15,000 = 25% (<= max_alloc 30%)
        s3_instructions = await engine.check_satellite_rebalancing(
            user_id=101,
            portfolio_assets=portfolio_assets,
            total_account_value=60000.0,
        )

        assert len(s3_instructions) == 0  # 內部常規未超標，未觸發強制減倉

    # 3. 測試直接生成安心防守 Embed
    embed = create_dynamic_rollover_embed(
        rollover_type="持倉防守 (核心衛星再平衡)",
        sell_symbol="NVDA",
        sell_ratio=0.0,
        buy_symbol="NVDA",
        reason="微結構判定: GEX Wall $190.00 護城河完好，阻力天花板 $210.00\n防守機制: 建議設置防守委託單 停損: $185.50",
        suggested_strategy="HOLD (維持現狀續抱)",
        suggested_price="N/A (維持現狀)",
        strike="N/A",
        expiry="N/A",
        direction="HOLD",
        scenario=RolloverScenario.SATELLITE_REBALANCE.value,
    )
    assert embed.title == "🛡️ 持倉防守評估: 持倉防守 (核心衛星再平衡)"
    assert embed.color == discord.Color.teal()
    assert "🟢【狀態：安全續抱】" in str(embed.description)


@pytest.mark.asyncio
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value="NORMAL",
)
@patch("database.calendar_cache.get_cached_earnings", return_value=None)
@patch(
    "services.market_data_service.get_all_option_expiries",
    new_callable=AsyncMock,
    return_value=[(datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")],
)
@patch("services.market_data_service.get_history_df")
@patch("database.market_cache.get_market_cache")
async def test_drill_scenario_2b_candidate_blocked_by_six_gates(
    mock_market_cache: MagicMock,
    mock_history_df: AsyncMock,
    mock_expiries: AsyncMock,
    mock_earnings: MagicMock,
    mock_regime: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    """
    子情境 2B：
    Watchlist 有 SPCX，但 SPCX 上方有巨量 STO Call 壓頂 (ratio=4.0 > 3.0)，
    防洗盤條件三攔截，S2 機會成本靜默早退。
    """
    mock_market_cache.return_value = {
        "reference_spot_price": 85.0,
        "expected_move_upper": 105.0,
        "is_stale": 0,
        "is_degraded": 0,
    }

    df_data: Dict[str, List[float]] = {
        "Open": [80.0] * 20 + [85.50],
        "Close": [80.0] * 20 + [85.50],
        "Volume": [10000.0] * 20 + [25000.0],
    }
    mock_history_df.return_value = pd.DataFrame(df_data)

    portfolio_assets: List[Dict[str, Any]] = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "spot_price": 195.0,
            "avg_cost": 180.0,
            "quantity": 77.0,
            "current_value": 15000.0,
            "psq_result": {"squeeze_level": "Release", "signal_direction": "Neutral"},
        }
    ]

    # 上方存在 $88.0C STO (ratio=4.0 > 3.0 物理封頂)，本情境重點是條件三攔截，
    # 條件一的 Open 值僅需存在以避免抓取結果缺欄位，不影響本測試斷言。
    candidate_radar_blocked: Dict[str, Any] = {
        "quote": {"c": 85.0},
        "iv_metrics": {"iv_rank": 25.0},
        "psq_result": {
            "squeeze_level": "High",
            "signal_direction": "Long",
            "is_breakout_long": True,
        },
        "gex_profile_data": {
            "gex_profile": {"75.0": -500000.0, "80.0": 1500000.0},
            "call_wall": 95.0,
            "put_wall": 80.0,
        },
        "uoa": [
            {
                "type": "CALL",
                "action": "STO",
                "strike": 88.0,
                "ratio": 4.0,  # 觸發條件三物理封頂
            },
            {
                "type": "CALL",
                "action": "BTO",
                "strike": 90.0,
                "expiry": (datetime.now() + timedelta(days=21)).strftime("%Y-%m-%d"),
                "ratio": 1.5,
            },
        ],
    }

    (
        instructions,
        entry_confirmation,
    ) = await engine.evaluate_opportunity_cost_for_satellites(
        user_id=101,
        portfolio_assets=portfolio_assets,
        already_flagged_symbols=set(),
        candidate_symbol="SPCX",
        candidate_radar=candidate_radar_blocked,
    )

    assert instructions == []
    assert entry_confirmation is not None
    assert entry_confirmation[0] is False


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.get_full_user_context")
@patch(
    "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
    new_callable=AsyncMock,
    return_value=True,
)
async def test_drill_scenario_2c_nvda_structural_breakdown_retreat_to_voo(
    mock_cliff: AsyncMock,
    mock_user_ctx: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    """
    子情境 2C：
    NVDA 15m 實體收盤跌破 Stop Loss ($184.00 < $185.50)，
    觸發結構破位 (Structural Breakdown)。
    機構風控鐵律：強制 100% 清倉 (LIQUIDATE) 撤退回防核心大盤 VOO，嚴禁追逐高波衛星標的。
    """
    mock_user_ctx.return_value = MagicMock(can_trade_spreads=False)

    portfolio_assets: List[Dict[str, Any]] = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "spot_price": 184.0,
            "price_15m_close": 184.0,  # 實體跌破防守線 $185.50
            "avg_cost": 180.0,
            "quantity": 77.0,
            "current_value": 14168.0,
            "put_wall": 190.0,
            "call_wall": 210.0,
            "gamma_flip": 192.0,
            "atr_14": 3.0,
            "atr_15m": 3.0,
            "sqz_mom": -0.8,
            "skew": -0.2,
            "max_allocation_pct": 0.30,
            "target_allocation_pct": 0.20,
        }
    ]

    instructions = await engine.check_satellite_rebalancing(
        user_id=101,
        portfolio_assets=portfolio_assets,
        total_account_value=50000.0,
    )

    assert len(instructions) == 1
    ins = instructions[0]
    assert ins["symbol"] == "NVDA"
    assert ins["action"] == "LIQUIDATE"
    assert ins["sell_ratio"] == 1.0
    assert ins["target_core"] == "VOO"
    assert ins["scenario"] == RolloverScenario.SATELLITE_REBALANCE.value
    assert "15m 實體破位確認" in ins["reason"]

    # 驗證 Embed 渲染
    embed = create_dynamic_rollover_embed(
        rollover_type="核心衛星再平衡",
        sell_symbol=ins["symbol"],
        sell_ratio=ins["sell_ratio"],
        buy_symbol=ins["target_core"],
        reason=ins["reason"],
        suggested_strategy=ins["suggested_strategy"],
        suggested_price="Market",
        strike="N/A",
        expiry="N/A",
        direction="BUY",
        sell_action="SELL",
        scenario=ins["scenario"],
        cash_impact=ins["cash_impact"],
        asset_class="SPOT",
    )

    assert embed.title == "🔄 核心衛星再平衡: NVDA → VOO"
    assert "🚨【執行轉倉指令】" in str(embed.description)
    assert "SELL (賣出現貨)" in str(embed.fields[0].value)
    assert "VOO" in str(embed.fields[1].value)
    assert "到期日" not in str(embed.fields[2].value)
    assert "履約價" not in str(embed.fields[2].value)


@pytest.mark.asyncio
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value="SHORT_GAMMA_CRITICAL",
)
@patch("market_analysis.dynamic_rollover.get_full_user_context")
@patch(
    "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
    new_callable=AsyncMock,
    return_value=True,
)
async def test_drill_scenario_2d_systemic_margin_defense_retreat_to_boxx(
    mock_cliff: AsyncMock,
    mock_user_ctx: MagicMock,
    mock_regime: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    """
    子情境 2D：
    大盤觸發 SHORT_GAMMA_CRITICAL 負 Gamma 踩踏模式，且帳戶存在保證金赤字壓力。
    NVDA 經檢查無邊際優勢 (No-Edge)，強制 100% 清倉並轉入純現金等價物 BOXX。
    """
    mock_user_ctx.return_value = MagicMock(
        has_margin_pressure=True,
        can_trade_spreads=False,
        cash_reserve=1000.0,
    )

    portfolio_assets: List[Dict[str, Any]] = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "spot_price": 184.0,
            "price_15m_close": 184.0,
            "avg_cost": 180.0,
            "quantity": 77.0,
            "current_value": 14168.0,
            "put_wall": 190.0,
            "call_wall": 210.0,
            "gamma_flip": 192.0,
            "atr_14": 3.0,
            "atr_15m": 3.0,
            "sqz_mom": -1.2,
            "skew": -0.35,
        }
    ]

    instructions = await engine.evaluate_margin_defense(
        user_id=101,
        portfolio_assets=portfolio_assets,
        already_flagged_symbols=set(),
    )

    assert len(instructions) == 1
    ins = instructions[0]
    assert ins["symbol"] == "NVDA"
    assert ins["action"] == "LIQUIDATE"
    assert ins["sell_ratio"] == 1.0
    assert ins["target_core"] == "BOXX"
    assert ins["scenario"] == RolloverScenario.MARGIN_DEFENSE.value
    assert ins["buy_action_label"] == "轉入 BOXX（鎖定無風險利息）"

    # 驗證 Embed 渲染 (紅色警戒)
    embed = create_dynamic_rollover_embed(
        rollover_type="槓桿與保證金防禦",
        sell_symbol=ins["symbol"],
        sell_ratio=ins["sell_ratio"],
        buy_symbol=ins["target_core"],
        reason=ins["reason"],
        suggested_strategy=ins["suggested_strategy"],
        suggested_price="Market",
        strike="N/A",
        expiry="N/A",
        direction="BUY",
        sell_action="SELL",
        buy_action_label=ins["buy_action_label"],
        scenario=ins["scenario"],
        cash_impact=ins["cash_impact"],
        asset_class="SPOT",
    )

    assert embed.title == "🚨 保證金防禦強制平倉: 槓桿與保證金防禦"
    assert embed.color == discord.Color.red()
    assert "SELL (賣出現貨)" in str(embed.fields[0].value)
    assert "BOXX" in str(embed.fields[1].value)
    assert "轉入 BOXX" in str(embed.fields[1].value)
    assert "到期日" not in str(embed.fields[2].value)
    assert "履約價" not in str(embed.fields[2].value)
