"""自選批次量化雷達呈現邏輯完整性測試 (SOP Radar Embed Integrity)。

驗證項目：
1. 代理 PutWall (MaxPain * 0.9) 僅用於呈現，不汙染負 Gamma、共振磁吸、破位殺盤或風控策略。
2. 代理 PutWall 時 Neg-GEX 欄位距離顯示 N/A。
3. STO 結構腿排除（跨式與借方價差賣出腿排除，貸方價差賣出腿保留）。
4. Rule 1 STO 物理封頂依當下即時現價動態重算否決。
5. 預期區間無 IV 時不誤觸下緣警示、上下緣缺失時優先以前收 pc 置中、皆無時傳入 Context 為 None 且顯示 N/A。
6. 圖例更新（EM Z-Score、⏱️ 動能、移除共振磁吸行）與死碼移除。
7. HVN/LVN 警示安全提取 vp_data。
"""

from typing import Any, Optional
from unittest.mock import patch

from cogs.embed_builders.market_embeds import (
    _radar_sto_eligible,
    build_radar_scan_embed,
)
from market_analysis.insights_engine import RiskInsightsContext


def _scan_row(**overrides: Any) -> dict[str, Any]:
    """建立測試用標準掃描資料列。"""
    base: dict[str, Any] = {
        "symbol": "TEST",
        "quote": {"c": 100.0, "dp": 0.5, "pc": 99.5},
        "iv_metrics": {
            "iv_rank": 40.0,
            "expected_move_weekly": 5.0,
            "term_structure_ratio": 1.0,
        },
        "max_pain": {"max_pain": 100.0},
        "gex_metrics": {
            "put_wall": 80.0,
            "call_wall": 120.0,
            "net_gex": 5_000_000.0,
        },
        "psq_result": {"momentum": 10.0, "direction": "Long", "is_squeezing": False},
        "uoa": [],
        "skew": 0.0,
        "skew_percentile": 50.0,
        "radar_cache": {},
    }
    base.update(overrides)
    return base


def _build(rows: list[dict[str, Any]]) -> tuple[str, str, str]:
    """回傳 (表格文字, 即時聯動警示文字, 圖例與風控指引文字)。"""
    with patch(
        "market_analysis.insights_engine.InsightsEngine.generate_cro_insight",
        return_value=(None, None, None),
    ):
        embeds = build_radar_scan_embed(rows, "WATCHLIST", 12345)
    assert len(embeds) == 1
    table = "\n".join(
        str(f.value) for f in embeds[0].fields if "核心 AI" in (f.name or "")
    )
    insights = "\n".join(
        str(f.value) for f in embeds[0].fields if "即時聯動警示" in (f.name or "")
    )
    legend = "\n".join(
        str(f.value) for f in embeds[0].fields if "雷達圖例" in (f.name or "")
    )
    return table, insights, legend


def test_proxy_put_wall_displays_star_and_does_not_pollute_strategies() -> None:
    """代理 PutWall (MaxPain * 0.9) 僅在 G/P-Wall 欄附帶 *，不汙染策略與風控判斷。"""
    captured_ctx: list[RiskInsightsContext] = []

    def mock_generate_cro_insight(
        ctx: RiskInsightsContext,
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        captured_ctx.append(ctx)
        return None, None, None

    # 無實測 PutWall，MaxPain = 100.0 -> 代理 PutWall = 90.0
    # 現價 85.0 < 代理 90.0，但 net_gex > 0
    row = _scan_row(
        quote={"c": 85.0, "dp": -2.0, "pc": 87.0},
        max_pain={"max_pain": 100.0},
        gex_metrics={"call_wall": 120.0, "net_gex": 2_000_000.0},
        gex_profile_data={},
        iv_metrics={"iv_rank": 30.0, "expected_move_weekly": 5.0},
        volume_pcr=1.5,  # 順向搶購 Put，但因實測 PutWall 為 0，不應觸發破位殺盤
    )

    with patch(
        "market_analysis.insights_engine.InsightsEngine.generate_cro_insight",
        side_effect=mock_generate_cro_insight,
    ):
        embeds = build_radar_scan_embed([row], "WATCHLIST", 12345)

    assert len(embeds) == 1
    table = "\n".join(
        str(f.value) for f in embeds[0].fields if "核心 AI" in (f.name or "")
    )
    insights = "\n".join(
        str(f.value) for f in embeds[0].fields if "即時聯動警示" in (f.name or "")
    )
    legend = "\n".join(
        str(f.value) for f in embeds[0].fields if "雷達圖例" in (f.name or "")
    )

    # 1. G/P-Wall 顯示 $90.0*，且極性維持 (+)
    assert "$90.0*" in table
    assert "(+) $120.0 / $90.0*" in table

    # 2. Neg-GEX 欄位必須顯示 N/A（代理值不得計算距離）
    assert "| N/A |" in table

    # 3. IV 策略為適宜賣方，不得誤判為 🔴禁賣Put
    assert "🟢適宜賣方" in table
    assert "🔴禁賣Put" not in table

    # 4. 戰術建議不得觸發跌破底牆、破位順向殺盤
    assert "跌破底牆" not in table
    assert "破位殺盤" not in table

    # 5. 警示中不得觸發破位殺盤與跌破底牆
    assert "實體跌破 $90.00 PutWall" not in insights
    assert "跌破 GEX PutWall 做市商底牆" not in insights

    # 6. 圖例中包含代理揭露註記
    assert "PutWall 後綴 * 為 Max Pain×0.9 代理估算" in legend

    # 7. RiskInsightsContext.put_wall 必須為 0.0 而非代理 90.0
    assert len(captured_ctx) == 1
    assert captured_ctx[0].put_wall == 0.0


def test_proxy_put_wall_does_not_trigger_magnetic_resonance() -> None:
    """代理 PutWall 不得觸發共振磁吸判斷。"""
    # 痛點 100，代理 90.0，現價 95 (距痛點偏離 5%)，若痛點 120 (偏離 > 10%)，POC 90.0
    row = _scan_row(
        quote={"c": 95.0, "dp": 0.0, "pc": 95.0},
        max_pain={"max_pain": 120.0},
        gex_metrics={"call_wall": 140.0, "net_gex": 1_000_000.0},
        volume_poc=90.0,
    )
    table, _, _ = _build([row])
    # 不得觸發共振磁吸標籤
    assert "[🧲 共振磁吸]" not in table


def test_radar_sto_eligible_helper_matrix() -> None:
    """測試 _radar_sto_eligible 輔助函式之各類結構判斷。"""
    # 非 dict 防禦
    assert _radar_sto_eligible(None) is False  # type: ignore[arg-type]
    assert _radar_sto_eligible([]) is False  # type: ignore[arg-type]

    # 1. 封頂清單項 (帶 structure_leg 或 spread_credit，或一般單腿項)
    # 一般單腿 STO
    assert _radar_sto_eligible({"strike": 100.0, "type": "CALL"}) is True
    # 結構腿但非貸方（借方或跨式）
    assert (
        _radar_sto_eligible({"strike": 100.0, "type": "CALL", "structure_leg": True})
        is False
    )
    # 結構腿且為貸方價差賣出腿
    assert (
        _radar_sto_eligible(
            {
                "strike": 100.0,
                "type": "CALL",
                "structure_leg": True,
                "spread_credit": True,
            }
        )
        is True
    )

    # 2. UOA 項 (帶 structure 或 spread_role)
    # 一般單腿 UOA STO
    assert (
        _radar_sto_eligible({"strike": 100.0, "type": "CALL", "action": "STO"}) is True
    )
    # 跨式 (STRADDLE) 賣出腿：一律排除
    assert (
        _radar_sto_eligible(
            {
                "strike": 100.0,
                "type": "CALL",
                "action": "STO",
                "structure": "STRADDLE",
            }
        )
        is False
    )
    # 借方價差賣出腿 (SHORT_LEG, spread_credit=False)：排除
    assert (
        _radar_sto_eligible(
            {
                "strike": 100.0,
                "type": "CALL",
                "action": "STO",
                "spread_role": "SHORT_LEG",
                "spread_credit": False,
            }
        )
        is False
    )
    # 貸方價差賣出腿 (SHORT_LEG, spread_credit=True)：保留
    assert (
        _radar_sto_eligible(
            {
                "strike": 100.0,
                "type": "CALL",
                "action": "STO",
                "spread_role": "SHORT_LEG",
                "spread_credit": True,
            }
        )
        is True
    )


def test_sto_cell_filters_straddles_and_debit_spreads() -> None:
    """STO 格應過濾跨式及借方價差賣出腿，保留貸方價差賣出腿與單腿。"""
    uoa_list = [
        # 跨式賣出腿：應排除
        {
            "strike": 110.0,
            "type": "CALL",
            "action": "STO",
            "structure": "STRADDLE",
        },
        # 借方價差賣出腿：應排除
        {
            "strike": 120.0,
            "type": "CALL",
            "action": "STO",
            "spread_role": "SHORT_LEG",
            "spread_credit": False,
        },
        # 貸方價差賣出腿：應保留
        {
            "strike": 130.0,
            "type": "CALL",
            "action": "STO",
            "spread_role": "SHORT_LEG",
            "spread_credit": True,
        },
        # 單腿 STO：應保留
        {
            "strike": 140.0,
            "type": "CALL",
            "action": "STO",
        },
    ]

    row = _scan_row(quote={"c": 100.0, "dp": 0.0}, uoa=uoa_list)
    table, _, _ = _build([row])

    # 應包含 130 與 140
    assert "C$130.0" in table
    assert "C$140.0" in table
    # 不得包含 110 與 120
    assert "C$110.0" not in table
    assert "C$120.0" not in table


def test_rule1_sto_veto_dynamically_recalculated_by_current_spot() -> None:
    """Rule 1 否決應依當下現價重算，現價穿越封頂履約價後應解除否決。"""
    # 封頂清單為 Call $105.0
    sto_caps = [{"strike": 105.0, "type": "CALL"}]

    # 情境 1: 現價 100.0，封頂 105.0 > 100.0 -> Rule 1 否決生效 (禁止進場)
    row_below = _scan_row(
        quote={"c": 100.0, "dp": 0.0},
        gex_metrics={"call_wall": 100.0, "net_gex": 5_000_000.0},
        psq_result={"momentum": 5.0, "direction": "Long", "is_squeezing": False},
        radar_cache={
            "sto_strikes": sto_caps,
            "physical_cap_above_spot": True,  # 快照時在現價之上
        },
    )
    table_below, _, _ = _build([row_below])
    assert "禁止進場：偵測到 STO 物理封頂" in table_below

    # 情境 2: 現價已漲至 110.0，封頂 105.0 <= 110.0 -> Rule 1 否決解除
    # 即使快照時的 physical_cap_above_spot 仍為 True，也必須依現價重算解除否決
    row_above = _scan_row(
        quote={"c": 110.0, "dp": 5.0},
        gex_metrics={"call_wall": 110.0, "net_gex": 5_000_000.0},
        psq_result={"momentum": 5.0, "direction": "Long", "is_squeezing": False},
        radar_cache={
            "sto_strikes": sto_caps,
            "physical_cap_above_spot": True,  # 舊快照殘留 True
        },
    )
    table_above, _, _ = _build([row_above])
    assert "禁止進場：偵測到 STO 物理封頂" not in table_above

    # 情境 3: 封頂清單內為跨式結構腿 (structure_leg=True)，不合資格，不觸發否決
    sto_straddle = [{"strike": 105.0, "type": "CALL", "structure_leg": True}]
    row_straddle = _scan_row(
        quote={"c": 100.0, "dp": 0.0},
        gex_metrics={"call_wall": 100.0, "net_gex": 5_000_000.0},
        psq_result={"momentum": 5.0, "direction": "Long", "is_squeezing": False},
        radar_cache={
            "sto_strikes": sto_straddle,
            "physical_cap_above_spot": True,
        },
    )
    table_straddle, _, _ = _build([row_straddle])
    assert "禁止進場：偵測到 STO 物理封頂" not in table_straddle


def test_no_iv_data_does_not_trigger_weekly_expected_move_lower_alert() -> None:
    """無真實 IV 資料時，不應被 ±5% 合成預期區間誤觸發「本週波動下緣」警示。"""
    # 缺失 iv_metrics
    row = _scan_row(
        quote={"c": 90.0, "dp": -5.0},
        iv_metrics=None,  # 無 IV 資料
        max_pain={"max_pain": 100.0},  # dist_pct = -10% < -3%
    )
    _, insights, _ = _build([row])
    assert "價格已極度逼近本週波動下緣" not in insights


def test_expected_move_recenter_priority_pc_then_ref_then_none() -> None:
    """預期區間上下緣缺失時，中心價依序取 quote.pc -> reference_price，皆無則顯示 N/A。"""
    captured_ctx: list[RiskInsightsContext] = []

    def mock_generate_cro_insight(
        ctx: RiskInsightsContext,
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        captured_ctx.append(ctx)
        return None, None, None

    # 1. 優先取 quote.pc 置中
    row_pc = _scan_row(
        quote={"c": 105.0, "pc": 100.0, "dp": 5.0},
        iv_metrics={
            "iv_rank": 30.0,
            "expected_move_weekly": 10.0,
            "expected_move_lower": 0.0,
            "expected_move_upper": 0.0,
            "reference_price": 98.0,  # 應被 pc=100.0 覆蓋
        },
    )
    table_pc, _, _ = _build([row_pc])
    # 中心 100, EM=10 -> low=90, high=110. 現價 105 -> (105-90)/20 = 75% -> Z-Score +0.50σ
    assert "+0.50σ" in table_pc

    # 2. pc 缺失時取 reference_price 置中
    row_ref = _scan_row(
        quote={"c": 105.0, "dp": 5.0},  # 無 pc
        iv_metrics={
            "iv_rank": 30.0,
            "expected_move_weekly": 10.0,
            "expected_move_lower": 0.0,
            "expected_move_upper": 0.0,
            "reference_price": 100.0,
        },
    )
    table_ref, _, _ = _build([row_ref])
    assert "+0.50σ" in table_ref

    # 3. 兩者皆無時不置中：顯示 N/A，傳入 Context 為 None
    row_none = _scan_row(
        quote={"c": 105.0, "dp": 5.0},  # 無 pc
        iv_metrics={
            "iv_rank": 30.0,
            "expected_move_weekly": 10.0,
            "expected_move_lower": 0.0,
            "expected_move_upper": 0.0,
            "reference_price": None,  # 無 reference_price
        },
    )
    with patch(
        "market_analysis.insights_engine.InsightsEngine.generate_cro_insight",
        side_effect=mock_generate_cro_insight,
    ):
        embeds = build_radar_scan_embed([row_none], "WATCHLIST", 12345)
        table_none = "\n".join(
            str(f.value) for f in embeds[0].fields if "核心 AI" in (f.name or "")
        )

    # Z-Score 顯示 N/A
    assert "| N/A |" in table_none
    # 傳入 RiskInsightsContext 之 expected_move 為 None 而非 0.0
    assert len(captured_ctx) == 1
    assert captured_ctx[0].expected_move_lower is None
    assert captured_ctx[0].expected_move_upper is None


def test_legend_notes_updated_and_dead_code_removed() -> None:
    """驗證圖例說明文字更新且移除共振磁吸行。"""
    row = _scan_row()
    _, _, legend = _build([row])

    # 驗證新圖例文本
    assert "EM Z-Score：(現價−前收)/週預期波動，±1 代表觸及預期區間上下緣。" in legend
    assert (
        "指標: SQZ 🟢多頭動能/🔴空頭動能，⏱️ 代表處於擠壓蓄力期，數值為動能。" in legend
    )

    # 驗證已移除之過期圖例文本
    assert "EM Pos %" not in legend
    assert "🧲 共振磁吸" not in legend
    assert "MOM 顯示數值代表處於擠壓蓄力期" not in legend


def test_hvn_lvn_near_alerts_safely_read_vp_data() -> None:
    """驗證 HVN/LVN 警示安全提取 vp_data 且容忍 None。"""
    # 提供 vp_data，現價 100.0 接近 HVN 100.5 (偏離 0.5%) 與 LVN 99.5 (偏離 0.5%)
    row_with_vp = _scan_row(
        quote={"c": 100.0, "dp": 0.0},
        vp_data={"hvn": 100.5, "lvn": 99.5},
    )
    _, insights_vp, _ = _build([row_with_vp])
    assert "價格接近 HVN 密集區 ($100.50)" in insights_vp
    assert "價格接近 LVN 真空區 ($99.50)" in insights_vp

    # vp_data 為 None 時不拋出異常
    row_none_vp = _scan_row(
        quote={"c": 100.0, "dp": 0.0},
        vp_data=None,
    )
    table, _, _ = _build([row_none_vp])
    assert "TEST" in table
