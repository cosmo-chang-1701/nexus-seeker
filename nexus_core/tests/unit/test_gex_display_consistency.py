"""
tests/unit/test_gex_display_consistency.py

GEX 呈現層一致性（fixture 取自實盤回報的 DRAM / RKLB / MU / BE）：
  #7  CallWall 低於現價且上方全為負 GEX → 顯示負 Gamma 真空，不印舊牆
  #8  全鏈 LONG_GAMMA 但現價已落入負 Gamma 區 → 揭露局部體制與雙向翻轉線
  #10 PutWall 處淨 GEX 為負 → 揭露助跌區並列出淨 GEX 最大支撐
  #11 下行緩衝同時顯示牆距與停損距離，判定依停損距離
並確認 `estimate_symbol_gamma_flip()`（進場閘門共用）行為不變。
"""

from typing import Any

import pytest

from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed
from market_analysis.index_microstructure import (
    analyze_local_gamma_regime,
    estimate_symbol_gamma_flip,
)

# DRAM：現價 62.90，$62 以下正 GEX，$63、$64 負 GEX，全鏈總和為正
DRAM_PROFILE = {
    "60.0": 3_000_000,
    "61.0": 2_000_000,
    "62.0": 1_500_000,
    "63.0": -800_000,
    "64.0": -600_000,
}


def _text(data: dict[str, Any]) -> str:
    embed = create_tactical_symbol_embed(data)
    return "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)


def test_local_regime_detects_short_gamma_above_positive_chain() -> None:
    res = analyze_local_gamma_regime(DRAM_PROFILE, 62.90)
    assert res is not None
    assert res.is_short_gamma
    assert res.flip_side == "SHORT_ABOVE"
    assert 62.0 < res.flip_strike < 63.0
    # 閘門用的 flip 定義不變：此鏈型下仍回傳 0
    assert estimate_symbol_gamma_flip(DRAM_PROFILE, 62.90) == 0.0


def test_local_regime_invalid_inputs() -> None:
    assert analyze_local_gamma_regime({}, 62.9) is None
    assert analyze_local_gamma_regime(DRAM_PROFILE, 0.0) is None
    assert analyze_local_gamma_regime(DRAM_PROFILE, 80.0) is None  # 超出履約價範圍


def test_dram_embed_shows_vacuum_and_local_regime() -> None:
    text = _text(
        {
            "symbol": "DRAM",
            "price": 62.90,
            "gex_profile_data": {
                "put_wall": 60.0,
                "call_wall": 62.0,
                "net_gex": 5_100_000.0,
                "gex_profile": DRAM_PROFILE,
            },
        }
    )
    assert "LONG_GAMMA (自穩定壓制波動)" in text
    assert "局部體制 (現價處): 🔴 SHORT_GAMMA（與全鏈體制相反" in text
    assert "局部 Gamma 翻轉線 ≈ $62." in text
    assert "上方無正 Gamma 牆（負 Gamma 真空" in text
    assert "數據異常：CallWall已低於現價" not in text


def test_mu_putwall_on_negative_net_gex_is_disclosed() -> None:
    """MU 第 1 版：PutWall $1045 淨 GEX −39,414K，真實支撐 $1040 為正 GEX。"""
    text = _text(
        {
            "symbol": "MU",
            "price": 1050.0,
            "gex_profile_data": {
                "put_wall": 1045.0,
                "call_wall": 1060.0,
                "net_gex": 80_000_000.0,
                "gex_profile": {
                    "1035.0": 20_000_000,
                    "1040.0": 45_000_000,
                    "1045.0": -39_414_000,
                    "1050.0": 10_000_000,
                    "1060.0": 60_000_000,
                },
            },
        }
    )
    assert "PutWall: $1045.00" in text
    assert "淨 GEX -$394K 為負" in text
    assert "淨 GEX 最大支撐: $1040.00" in text


def test_healthy_putwall_has_no_extra_disclosure() -> None:
    text = _text(
        {
            "symbol": "NVDA",
            "price": 100.0,
            "gex_profile_data": {
                "put_wall": 95.0,
                "call_wall": 110.0,
                "net_gex": 5_000_000.0,
                "gex_profile": {
                    "95.0": 3_000_000,
                    "100.0": 500_000,
                    "110.0": 2_000_000,
                },
            },
        }
    )
    assert "為負（Put 端" not in text
    assert "淨 GEX 最大支撐" not in text


@pytest.mark.parametrize(
    "put_wall,expect",
    [
        (92.54, "↓7.46%｜停損距離 7.96%"),  # BE：牆距 7.46% 但停損距離接近上限
        (91.9, "停損距離過寬 (> 絕對上限 8.00%)"),
    ],
)
def test_be_buffer_shows_stop_distance(put_wall: float, expect: str) -> None:
    text = _text(
        {
            "symbol": "BE",
            "price": 100.0,
            "atr_15m": 1.0,
            "atr_14": 5.0,
            "gex_profile_data": {
                "put_wall": put_wall,
                "call_wall": 130.0,
                "net_gex": 5_000_000.0,
                "gex_profile": {
                    str(put_wall): 1_000_000,
                    "100.0": 2_000_000,
                    "130.0": 3_000_000,
                },
            },
        }
    )
    assert expect in text


# ---------------------------------------------------------------------------
# #12 Gamma Flip 重要性門檻（docs/microstructure/03 §5.6）：呈現與校準用，閘門不變
# ---------------------------------------------------------------------------

# MU 2026-10-02 11:15 ET：$1072.5 只有 −9.4M，夾在 $1070 +4.16B 與 $1075 +3.78B 之間
MU_NOISE_FLIP_PROFILE = {
    "1060.0": 1_200_000_000,
    "1065.0": 2_000_000_000,
    "1070.0": 4_160_000_000,
    "1072.5": -9_400_000,
    "1075.0": 3_780_000_000,
    "1080.0": 2_500_000_000,
}
MU_NOISE_SPOT = 1075.43


def test_mu_noise_flip_has_tiny_materiality_but_gate_unchanged() -> None:
    from market_analysis.index_microstructure import (
        estimate_material_gamma_flip,
        gamma_flip_materiality,
    )

    # 閘門定義不變：仍判為 Flip $1075
    assert estimate_symbol_gamma_flip(MU_NOISE_FLIP_PROFILE, MU_NOISE_SPOT) == 1075.0
    m = gamma_flip_materiality(MU_NOISE_FLIP_PROFILE, MU_NOISE_SPOT)
    assert m is not None
    assert m.flip_strike == 1075.0
    assert m.neg_peak == pytest.approx(9_400_000)
    assert m.window_max_abs == pytest.approx(4_160_000_000)
    assert m.ratio == pytest.approx(9.4e6 / 4.16e9)
    assert (
        estimate_material_gamma_flip(MU_NOISE_FLIP_PROFILE, MU_NOISE_SPOT, 0.05) == 0.0
    )


def test_materiality_uses_contiguous_negative_run_peak() -> None:
    """−5B、−10M、+4B：緊鄰一檔很小，但連續負值區段的峰值是 5B，屬重要交叉。"""
    from market_analysis.index_microstructure import (
        estimate_material_gamma_flip,
        gamma_flip_materiality,
    )

    profile = {"90": 1e9, "95": -5e9, "97.5": -1e7, "100": 4e9, "105": 1e9}
    m = gamma_flip_materiality(profile, 101.0)
    assert m is not None and m.flip_strike == 100.0
    assert m.neg_peak == pytest.approx(5e9)
    assert m.ratio == pytest.approx(1.0)
    assert estimate_material_gamma_flip(profile, 101.0, 0.05) == 100.0


def test_material_flip_reselects_next_significant_crossing() -> None:
    from market_analysis.index_microstructure import estimate_material_gamma_flip

    profile = {"90": -2e9, "92.5": 3e9, "95": 1e9, "97.5": -1e7, "100": 4e9}
    assert estimate_symbol_gamma_flip(profile, 101.0) == 100.0
    assert estimate_material_gamma_flip(profile, 101.0, 0.05) == 92.5


def test_materiality_none_when_gate_has_no_flip() -> None:
    from market_analysis.index_microstructure import gamma_flip_materiality

    assert gamma_flip_materiality(DRAM_PROFILE, 62.90) is None
    assert gamma_flip_materiality({}, 100.0) is None


def test_material_flip_with_zero_ratio_equals_gate_definition() -> None:
    """min_ratio = 0 時與閘門版逐值相同：將來切換閘門的安全前提。"""
    import random

    from market_analysis.index_microstructure import estimate_material_gamma_flip

    rng = random.Random(20261002)
    for _ in range(500):
        n = rng.randint(1, 12)
        strikes = sorted(rng.sample(range(50, 150), n))
        profile = {
            str(float(k)): rng.choice([-1, 1]) * rng.uniform(0, 5e9) for k in strikes
        }
        if rng.random() < 0.1:
            profile[str(float(strikes[0]))] = 0.0
        spot = rng.choice([0.0, rng.uniform(40, 160)])
        assert estimate_material_gamma_flip(profile, spot, 0.0) == (
            estimate_symbol_gamma_flip(profile, spot)
        ), (profile, spot)


def test_noise_note_helper() -> None:
    from cogs.embed_builders._embed_helpers import gamma_flip_noise_note
    from market_analysis.index_microstructure import GammaFlipMateriality

    noisy = GammaFlipMateriality(1075.0, 9_400_000.0, 4_160_000_000.0, 0.00226)
    note = gamma_flip_noise_note(noisy, 0.0)
    assert "-9400K" in note
    assert "0.2%" in note
    assert "排除後 Flip: 無" in note
    assert "$1070.00" in gamma_flip_noise_note(noisy, 1070.0)
    material = GammaFlipMateriality(100.0, 5e9, 5e9, 1.0)
    assert gamma_flip_noise_note(material, 100.0) == ""
    assert gamma_flip_noise_note(None, 0.0) == ""


def test_mu_embed_flags_noise_flip_and_drops_interpolated_zero() -> None:
    text = _text(
        {
            "symbol": "MU",
            "price": MU_NOISE_SPOT,
            "gex_profile_data": {
                "put_wall": 1065.0,
                "call_wall": 1080.0,
                "net_gex": 13_630_600_000.0,
                "gex_profile": MU_NOISE_FLIP_PROFILE,
            },
        }
    )
    assert "Gamma Flip (轉正履約價): $1075.00" in text
    assert "屬雜訊交叉" in text
    assert "排除後 Flip: 無" in text
    assert "相鄰履約價內插零軸" not in text
