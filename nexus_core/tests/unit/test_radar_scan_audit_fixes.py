"""/x 自選雷達線上稽查修正：SQZ 單一來源、雙牆互斥、UOA 封頂方向、
跌破 PutWall 的護航網判定、STO 鎖死欄格式化／去重與表格長度防線。"""

from typing import Any
from unittest.mock import patch

from cogs.embed_builders.market_embeds import (
    _clip_cell,
    _format_sto_cell,
    build_radar_scan_embed,
)


def _scan_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": "TEST",
        "quote": {"c": 100.0, "dp": 0.5},
        "iv_metrics": {"iv_rank": 40.0, "term_structure_ratio": 1.0},
        "max_pain": {"max_pain": 100.0},
        "gex_metrics": {"put_wall": 80.0, "call_wall": 120.0, "net_gex": 5_000_000.0},
        "psq_result": {"momentum": 10.0, "direction": "Long", "is_squeezing": False},
        "uoa": [],
        "skew": 0.0,
        "skew_percentile": 50.0,
    }
    base.update(overrides)
    return base


def _build(rows: list[dict[str, Any]]) -> tuple[str, str]:
    """回傳 (表格文字, 即時聯動警示文字)。"""
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
    return table, insights


def _whale_call(strike: float) -> dict[str, Any]:
    return {
        "expiry": "2026-12-18",
        "strike": strike,
        "type": "CALL",
        "action": "BTO",
        "volume": 60000,
        "oi": 1000,
    }


def test_sqz_value_identical_in_table_and_insights_when_capped() -> None:
    """UOA 封頂只降級方向，不再對動能 −1.0；表格與警示讀同一個值。"""
    row = _scan_row(
        symbol="META",
        quote={"c": 725.93, "dp": 1.0},
        max_pain={"max_pain": 725.0},
        gex_metrics={"put_wall": 650.0, "call_wall": 730.0, "net_gex": 5_000_000.0},
        psq_result={"momentum": 101.9, "direction": "Long", "is_squeezing": True},
        uoa=[_whale_call(750.0)],
    )
    table, insights = _build([row])
    assert "硬封頂" in insights
    assert "⚪+101.9" in table
    assert "+101.9" in insights
    assert "+100.9" not in table + insights


def test_uoa_cap_ignores_strike_below_spot() -> None:
    """AMD：CALL 履約價 615 已在現價 615.73 之下 (價內)，不構成上方封頂。"""
    row = _scan_row(
        symbol="AMD",
        quote={"c": 615.73, "dp": 1.0},
        max_pain={"max_pain": 615.0},
        gex_metrics={"put_wall": 550.0, "call_wall": 700.0, "net_gex": 5_000_000.0},
        uoa=[_whale_call(615.0)],
    )
    table, insights = _build([row])
    assert "硬封頂" not in insights
    assert "🟢+10.0" in table


def test_uoa_cap_picks_nearest_strike_above_spot() -> None:
    row = _scan_row(
        quote={"c": 100.0, "dp": 0.0},
        uoa=[_whale_call(130.0), _whale_call(105.0), _whale_call(95.0)],
    )
    _, insights = _build([row])
    assert "上方 $105.00 存在實質硬封頂" in insights


def test_dual_walls_near_call_side_emits_only_call_wall() -> None:
    """AAPL：現價 330.32 / PW 327.5 / CW 330，區間位置 >= 50% → 只發 CallWall。"""
    row = _scan_row(
        symbol="AAPL",
        quote={"c": 330.32, "dp": 0.5},
        max_pain={"max_pain": 330.0},
        gex_metrics={"put_wall": 327.5, "call_wall": 330.0, "net_gex": 5_000_000.0},
    )
    _, insights = _build([row])
    assert "Call Wall ($330.0，區間位置 113%)" in insights
    assert "逼近 GEX PutWall" not in insights


def test_dual_walls_near_put_side_emits_only_put_wall() -> None:
    """META：現價 725.93 / PW 720 / CW 740，區間位置 30% → 只發 PutWall。"""
    row = _scan_row(
        symbol="META",
        quote={"c": 725.93, "dp": 0.5},
        max_pain={"max_pain": 725.0},
        gex_metrics={"put_wall": 720.0, "call_wall": 740.0, "net_gex": 5_000_000.0},
    )
    _, insights = _build([row])
    assert "逼近 GEX PutWall 做市商底牆 ($720.00，區間位置 30%)" in insights
    assert "Call Wall" not in insights


def test_single_wall_near_unchanged() -> None:
    """只有一道牆成立時行為不變，也不附區間位置。"""
    row = _scan_row(
        quote={"c": 81.0, "dp": 0.0},
        max_pain={"max_pain": 81.0},
        gex_metrics={"put_wall": 80.0, "call_wall": 120.0, "net_gex": 5_000_000.0},
    )
    _, insights = _build([row])
    assert "逼近 GEX PutWall 做市商底牆 ($80.00)" in insights


def _mrna_row(**overrides: Any) -> dict[str, Any]:
    row = _scan_row(
        symbol="MRNA",
        quote={"c": 188.94, "dp": -1.5},
        max_pain={"max_pain": 190.0},
        gex_metrics={"put_wall": 190.0, "call_wall": 210.0, "net_gex": 2_000_000.0},
        gex_profile_data={
            "put_wall": 190.0,
            "call_wall": 210.0,
            "net_gex": 2_000_000.0,
            "gex_profile": {"185.0": 1_000_000.0, "190.0": 1_000_000.0},
        },
        atr_15m=0.5,  # 防守位 = 190 − 0.5 × 0.5 = 189.75 → 現價已跌破
    )
    row.update(overrides)
    return row


def test_below_put_wall_without_support_is_not_escort_net() -> None:
    """MRNA：跌破 PutWall、無正 Gamma 深度、無 DTE≥7 機構買盤 → 不得建議續抱。"""
    table, _ = _build([_mrna_row(atr_15m=0.4)])  # 防守位 189.80
    assert "護航網" not in table
    assert "Delta 拋售" in table


def test_below_put_wall_within_stop_without_support_shows_stop() -> None:
    table, _ = _build([_mrna_row(atr_15m=3.0)])  # 防守位 188.50 < 現價
    assert "護航網" not in table
    assert "負 Gamma Delta 拋售風險，嚴守 $188.50 (15分K收盤)" in table


def test_below_put_wall_with_positive_gamma_support_keeps_escort_net() -> None:
    row = _mrna_row(
        atr_15m=3.0,
        gex_profile_data={
            "put_wall": 190.0,
            "call_wall": 210.0,
            "net_gex": 20_000_000.0,
            "gex_profile": {"185.0": 10_000_000.0, "190.0": 10_000_000.0},
        },
    )
    table, _ = _build([row])
    assert "護航網支撐" in table


def test_sto_strikes_from_radar_cache_are_formatted_and_deduped() -> None:
    """COIN/SOXL：radar_cache.sto_strikes 為 list[dict]，不得輸出原始 dict 字串。"""
    sto = [
        {"strike": 365.0, "type": "CALL", "oi": 1200, "volume": 9000, "expiry": e}
        for e in ("2026-10-17", "2026-11-21", "2026-12-18")
    ] + [{"strike": 300.0, "type": "PUT", "oi": 800, "volume": 5000}]
    row = _scan_row(
        symbol="COIN",
        quote={"c": 330.0, "dp": 0.0},
        max_pain={"max_pain": 330.0},
        radar_cache={"sto_strikes": sto},
    )
    table, _ = _build([row])
    assert "{'" not in table
    assert "'oi'" not in table
    # 價外鎖死皆優先，依距現價遠近：P300 (−30) 先於 C365 (+35)
    assert "P$300.0 / C$365.0" in table


def test_sto_uoa_items_deduped_across_expiries() -> None:
    """ARM：兩筆同履約價、不同到期日的 STO，只顯示一次。"""
    uoa = [
        {"strike": 300.0, "type": "CALL", "action": "STO", "expiry": e, "volume": 500}
        for e in ("2026-10-17", "2026-11-21")
    ]
    row = _scan_row(
        symbol="ARM",
        quote={"c": 280.0, "dp": 0.0},
        max_pain={"max_pain": 280.0},
        uoa=uoa,
    )
    table, _ = _build([row])
    assert "C$300.0 / C$300.0" not in table
    assert "| C$300.0 |" in table


def test_all_fields_within_limit_and_code_blocks_closed() -> None:
    long_sto = [
        {"strike": 100.0 + i, "type": "CALL", "oi": i, "expiry": "2026-12-18"}
        for i in range(60)
    ]
    rows = [
        _scan_row(symbol=f"S{i}", radar_cache={"sto_strikes": long_sto})
        for i in range(10)
    ]
    with patch(
        "market_analysis.insights_engine.InsightsEngine.generate_cro_insight",
        return_value=(None, None, None),
    ):
        embeds = build_radar_scan_embed(rows, "WATCHLIST", 12345)
    for embed in embeds:
        for field in embed.fields:
            value = str(field.value)
            assert len(value) <= 1024
            if value.startswith("```"):
                assert value.rstrip().endswith("```")
            if "核心 AI" in (field.name or ""):
                for line in value.split("\n")[1:-1]:
                    assert line.startswith("|") and line.endswith("|")


def test_format_sto_cell_rules() -> None:
    items = [
        {"strike": 90.0, "type": "CALL"},  # 價內 CALL，排後
        {"strike": 110.0, "type": "CALL"},
        {"strike": 105.0, "type": "CALL"},
        {"strike": 105.0, "type": "CALL", "expiry": "2026-12-18"},  # 重複
        {"strike": 95.0, "type": "PUT"},
        "not-a-dict",
        {"strike": None, "type": "PUT"},
    ]
    assert _format_sto_cell(items, 100.0) == "C$105.0 / P$95.0 +2"
    assert _format_sto_cell(items, 100.0, max_items=5) == (
        "C$105.0 / P$95.0 / C$110.0 / C$90.0"
    )
    assert _format_sto_cell([], 100.0) == ""
    assert _format_sto_cell("[{'strike': 1}]", 100.0) == ""
    assert _format_sto_cell(None, 100.0) == ""


def test_clip_cell() -> None:
    assert _clip_cell("abc", 5) == "abc"
    assert _clip_cell("abcdefgh", 5) == "abcd…"
    assert _clip_cell("a|b\nc", 10) == "a/b c"


def test_anti_washout_stop_uses_engine_track_one_multiplier() -> None:
    """雷達防守位 = PutWall − 0.5 × ATR_15m，與出場引擎軌道一同一常數。"""
    from market_analysis.dynamic_rollover.constants import (
        _MICROSTRUCTURE_SL_STRUCTURAL_ATR_MULT,
    )

    assert _MICROSTRUCTURE_SL_STRUCTURAL_ATR_MULT == 0.5
    table, _ = _build([_mrna_row(atr_15m=2.4)])  # 190 − 0.5 × 2.4 = 188.80
    assert "嚴守 $188.80 (15分K收盤)" in table


def test_call_wall_insight_follows_entry_gate() -> None:
    """AAPL 型：逼近 CallWall 但 AND-gate 未通過時，警示不得寫「現貨重砲攻擊」，
    須與表格戰術建議「保持觀察」一致。"""
    row = _scan_row(
        symbol="AAPL",
        quote={"c": 330.32, "dp": 0.5},
        max_pain={"max_pain": 330.0},
        gex_metrics={"put_wall": 300.0, "call_wall": 330.0, "net_gex": 5_000_000.0},
    )
    table, insights = _build([row])
    assert "保持觀察" in table
    assert "現貨重砲" not in insights
    assert "進場訊號未全數共振" in insights


def test_call_wall_insight_sto_veto_matches_tactical() -> None:
    row = _scan_row(
        symbol="AAPL",
        quote={"c": 330.32, "dp": 0.5},
        max_pain={"max_pain": 330.0},
        gex_metrics={"put_wall": 300.0, "call_wall": 330.0, "net_gex": 5_000_000.0},
        radar_cache={"physical_cap_above_spot": True},
    )
    table, insights = _build([row])
    assert "⛔ 禁止進場" in table
    assert "禁止進場" in insights
    assert "現貨重砲" not in insights
