"""產業鏈交叉驗證與臨近預測決策引擎 (channel_check) 單元測試。"""

from __future__ import annotations

import json

from market_analysis.fundamental_pipeline.channel_check import (
    compute_pearson_correlation,
    evaluate_causal_link,
    evaluate_channel_check,
    evaluate_nowcast_link,
    result_to_log_record,
)
from market_analysis.fundamental_pipeline.supply_chain_map import (
    LINK_AI_CAPEX,
    LINK_AIR_TRAVEL,
    LINK_SPACE_STARLINK_TW_NOWCAST,
)


def test_pearson_correlation_calculation() -> None:
    """測試皮爾森積差相關係數之數學校準與極端防護。"""
    # 完美正相關
    x = [10.0, 20.0, 30.0, 40.0]
    y = [15.0, 25.0, 35.0, 45.0]
    assert compute_pearson_correlation(x, y) == 1.0

    # 完美負相關
    y_neg = [45.0, 35.0, 25.0, 15.0]
    assert compute_pearson_correlation(x, y_neg) == -1.0

    # 樣本期數不足 (N < 4)
    assert compute_pearson_correlation([1.0, 2.0], [1.0, 2.0]) is None

    # 單一常數數列 (變異數為零，除零防護)
    const_series = [5.0, 5.0, 5.0, 5.0]
    assert compute_pearson_correlation(x, const_series) == 0.0

    # 包含 NaN/Inf 的異常數列過濾
    x_nan = [10.0, 20.0, float("nan"), 30.0, 40.0]
    y_nan = [15.0, 25.0, 99.0, 35.0, 45.0]
    corr = compute_pearson_correlation(x_nan, y_nan)
    assert corr is not None
    assert round(corr, 1) == 1.0

    # 包含 None 與非數值型別的序列過濾
    x_none: list[float | None] = [10.0, None, 20.0, 30.0, 40.0]
    y_none: list[float | None] = [15.0, 99.0, 25.0, 35.0, 45.0]
    corr_none = compute_pearson_correlation(x_none, y_none)
    assert corr_none is not None
    assert round(corr_none, 1) == 1.0

    # 兩組數列皆為常數 (雙重零變異數)
    assert (
        compute_pearson_correlation([3.0, 3.0, 3.0, 3.0], [7.0, 7.0, 7.0, 7.0]) == 0.0
    )


def test_causal_link_insufficient_data() -> None:
    """測試因果傳導鏈在數據缺失時判定為 INSUFFICIENT。"""
    # 驅動端缺失
    res1 = evaluate_causal_link(
        LINK_AI_CAPEX, "2026-Q2", driver_growth=None, follower_growth=25.0
    )
    assert res1.verdict == "INSUFFICIENT"
    assert res1.divergence_pp is None

    # 跟隨端缺失
    res2 = evaluate_causal_link(
        LINK_AI_CAPEX, "2026-Q2", driver_growth=30.0, follower_growth=None
    )
    assert res2.verdict == "INSUFFICIENT"


def test_causal_link_confirm_and_diverge() -> None:
    """測試因果傳導鏈之共振確認與幅度背離判定。"""
    # 傳導共振確認 (偏差 5pp <= 25pp)
    res_confirm = evaluate_causal_link(
        LINK_AI_CAPEX, "2026-Q2", driver_growth=25.0, follower_growth=30.0
    )
    assert res_confirm.verdict == "CONFIRM"
    assert res_confirm.divergence_pp == 5.0

    # 同步收縮確認 (偏差 3pp <= 25pp)
    res_contract = evaluate_causal_link(
        LINK_AI_CAPEX, "2026-Q2", driver_growth=-15.0, follower_growth=-12.0
    )
    assert res_contract.verdict == "CONFIRM"
    assert res_contract.divergence_pp == 3.0

    # 幅度顯著背離 (驅動增長 +80% 但跟隨僅 +5%，偏差 -75pp)
    res_diverge_mag = evaluate_causal_link(
        LINK_AI_CAPEX, "2026-Q2", driver_growth=80.0, follower_growth=5.0
    )
    assert res_diverge_mag.verdict == "DIVERGE"
    assert res_diverge_mag.divergence_pp == -75.0

    # 方向完全背離 (驅動增長 +20% 但跟隨衰退 -15%)
    res_diverge_dir = evaluate_causal_link(
        LINK_AI_CAPEX, "2026-Q2", driver_growth=20.0, follower_growth=-15.0
    )
    assert res_diverge_dir.verdict == "DIVERGE"

    # 零軸微幅波動持平 (驅動 +1.2% 與跟隨 -0.8%，偏差 -2.0pp <= 5.0pp)
    res_flat = evaluate_causal_link(
        LINK_AI_CAPEX, "2026-Q2", driver_growth=1.2, follower_growth=-0.8
    )
    assert res_flat.verdict == "CONFIRM"


def test_nowcast_link_direction_and_hits() -> None:
    """測試高頻臨近預測之方向分類與實績命中檢驗。"""
    # 數據缺失
    res_none = evaluate_nowcast_link(LINK_AIR_TRAVEL, "2026-Q2", driver_growth=None)
    assert res_none.verdict == "INSUFFICIENT"
    assert res_none.nowcast_direction is None

    # NOWCAST_UP 命中實績
    res_hit_up = evaluate_nowcast_link(
        LINK_AIR_TRAVEL, "2026-Q2", driver_growth=6.5, follower_growth=4.2
    )
    assert res_hit_up.nowcast_direction == "NOWCAST_UP"
    assert res_hit_up.nowcast_hit is True
    assert res_hit_up.verdict == "CONFIRM"

    # NOWCAST_UP 方向失誤 (預測擴張但實績衰退)
    res_miss_up = evaluate_nowcast_link(
        LINK_AIR_TRAVEL, "2026-Q2", driver_growth=6.5, follower_growth=-2.5
    )
    assert res_miss_up.nowcast_hit is False
    assert res_miss_up.verdict == "DIVERGE"

    # NOWCAST_DOWN 命中實績
    res_hit_down = evaluate_nowcast_link(
        LINK_AIR_TRAVEL, "2026-Q2", driver_growth=-5.2, follower_growth=-3.8
    )
    assert res_hit_down.nowcast_direction == "NOWCAST_DOWN"
    assert res_hit_down.nowcast_hit is True
    assert res_hit_down.verdict == "CONFIRM"

    # FLAT 持平判定
    res_flat = evaluate_nowcast_link(
        LINK_AIR_TRAVEL, "2026-Q2", driver_growth=0.8, follower_growth=1.1
    )
    assert res_flat.nowcast_direction == "FLAT"
    assert res_flat.nowcast_hit is True
    assert res_flat.verdict == "CONFIRM"


def test_nowcast_link_preview_when_follower_unreported() -> None:
    """測試跟隨端季度財報尚未發布時的高頻臨近在途預測。"""
    # 先行擴張 (NOWCAST_UP) 預覽
    res_preview_up = evaluate_nowcast_link(
        LINK_SPACE_STARLINK_TW_NOWCAST,
        "2026-09",
        driver_growth=8.1,
        follower_growth=None,
        allow_nowcast_preview=True,
    )
    assert res_preview_up.nowcast_direction == "NOWCAST_UP"
    assert res_preview_up.nowcast_hit is None
    assert res_preview_up.verdict == "CONFIRM"
    assert "偏多擴張" in res_preview_up.summary_text

    # 先行衰退 (NOWCAST_DOWN) 預覽
    res_preview_down = evaluate_nowcast_link(
        LINK_SPACE_STARLINK_TW_NOWCAST,
        "2026-09",
        driver_growth=-6.0,
        follower_growth=None,
        allow_nowcast_preview=True,
    )
    assert res_preview_down.nowcast_direction == "NOWCAST_DOWN"
    assert res_preview_down.verdict == "DIVERGE"
    assert "偏空示警" in res_preview_down.summary_text

    # 禁用預覽時應回退至 INSUFFICIENT
    res_no_preview = evaluate_nowcast_link(
        LINK_SPACE_STARLINK_TW_NOWCAST,
        "2026-09",
        driver_growth=8.1,
        follower_growth=None,
        allow_nowcast_preview=False,
    )
    assert res_no_preview.verdict == "INSUFFICIENT"


def test_evaluate_channel_check_delegation() -> None:
    """測試統一決策入口正確分流至 CAUSAL 與 NOWCAST 處理函式。"""
    causal_res = evaluate_channel_check(
        LINK_AI_CAPEX, "2026-Q2", driver_growth=15.0, follower_growth=12.0
    )
    assert causal_res.link_type == "CAUSAL"
    assert causal_res.verdict == "CONFIRM"

    nowcast_res = evaluate_channel_check(
        LINK_AIR_TRAVEL, "2026-Q2", driver_growth=4.0, follower_growth=3.5
    )
    assert nowcast_res.link_type == "NOWCAST"
    assert nowcast_res.verdict == "CONFIRM"


def test_result_to_log_record() -> None:
    """測試將 ChannelCheckResult 序列化為可寫入 SQLite 之 ChannelCheckLogRecord。"""
    res = evaluate_channel_check(
        LINK_AI_CAPEX, "2026-Q2", driver_growth=25.0, follower_growth=22.0
    )
    record = result_to_log_record(res)

    assert record.link_key == "AI_CAPEX"
    assert record.as_of_period == "2026-Q2"
    assert record.link_type == "CAUSAL"
    assert record.experimental is False
    assert record.driver_growth == 25.0
    assert record.follower_growth == 22.0
    assert record.divergence_pp == -3.0
    assert record.verdict == "CONFIRM"

    parsed_members = json.loads(record.members_json)
    assert parsed_members["link_key"] == "AI_CAPEX"
    assert "NVDA" in parsed_members["followers"]


def test_channel_check_members_dict_completeness() -> None:
    """測試 evaluate_channel_check 產出之 members_dict 包含完整的 pillar 與 symbols。"""
    res = evaluate_channel_check(
        LINK_AI_CAPEX, "2026-Q2", driver_growth=25.0, follower_growth=22.0
    )
    assert res.members["pillar"] == "MACRO_CORE"
    assert "MSFT" in res.members["symbols"]
    assert "NVDA" in res.members["symbols"]
    assert "2382" in res.members["symbols"]

    # 驗證 NOWCAST 鏈條
    nowcast_res = evaluate_channel_check(
        LINK_AIR_TRAVEL, "2026-Q2", driver_growth=5.0, follower_growth=4.0
    )
    assert nowcast_res.members["pillar"] == "MACRO_CORE"
    assert "DAL" in nowcast_res.members["symbols"]
    assert "UAL" in nowcast_res.members["symbols"]


def test_inverse_polarity_flips_driver_before_verdict() -> None:
    """BRAND_RETAIL_INVENTORY 為反向關係：DIO +10% 與品牌營收 +8% 同號反而是背離。"""
    from market_analysis.fundamental_pipeline.supply_chain_map import (
        LINK_BRAND_RETAIL_INVENTORY,
    )

    same_sign = evaluate_causal_link(
        LINK_BRAND_RETAIL_INVENTORY, "2026-Q2", driver_growth=10.0, follower_growth=8.0
    )
    assert same_sign.verdict == "DIVERGE"
    assert same_sign.driver_growth == 10.0  # 欄位保存原始量測值
    assert same_sign.divergence_pp == 18.0  # 8 - (-10)
    assert same_sign.members["polarity"] == -1

    opposite = evaluate_causal_link(
        LINK_BRAND_RETAIL_INVENTORY, "2026-Q2", driver_growth=10.0, follower_growth=-8.0
    )
    assert opposite.verdict == "CONFIRM"
    assert "反向" in opposite.summary_text

    # 同向鏈條不受影響
    normal = evaluate_causal_link(
        LINK_AI_CAPEX, "2026-Q2", driver_growth=10.0, follower_growth=8.0
    )
    assert normal.verdict == "CONFIRM"
    assert normal.members["polarity"] == 1


def test_insufficient_summary_includes_data_note_and_is_persisted() -> None:
    """資料不足的原因寫入 summary_text，並隨 members_json 存檔供 /fa 顯示。"""
    res = evaluate_channel_check(
        LINK_AI_CAPEX,
        "2026-Q3",
        driver_growth=None,
        follower_growth=5.0,
        data_note="驅動端美股覆蓋 1/5 低於門檻",
    )
    assert res.verdict == "INSUFFICIENT"
    assert "驅動端美股覆蓋 1/5 低於門檻" in res.summary_text
    record = result_to_log_record(res)
    assert "低於門檻" in json.loads(record.members_json)["summary_text"]


def test_chinese_label_maps_cover_all_enum_values() -> None:
    from market_analysis.fundamental_pipeline.channel_check import (
        LINK_TYPE_LABELS_ZH,
        NOWCAST_DIRECTION_LABELS_ZH,
        VERDICT_LABELS_ZH,
    )

    assert set(VERDICT_LABELS_ZH) == {"CONFIRM", "DIVERGE", "INSUFFICIENT"}
    assert set(LINK_TYPE_LABELS_ZH) == {"CAUSAL", "NOWCAST"}
    assert set(NOWCAST_DIRECTION_LABELS_ZH) == {"NOWCAST_UP", "NOWCAST_DOWN", "FLAT"}
    for label in (
        *VERDICT_LABELS_ZH.values(),
        *LINK_TYPE_LABELS_ZH.values(),
        *NOWCAST_DIRECTION_LABELS_ZH.values(),
    ):
        assert label.isascii() is False
