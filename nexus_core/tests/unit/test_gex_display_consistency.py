"""
tests/unit/test_gex_display_consistency.py

GEX 呈現層一致性（fixture 取自實盤回報的 DRAM / RKLB / MU / BE）：
  #7  CallWall 低於現價且上方全為負 GEX → 顯示負 Gamma 真空，不印舊牆
  #8  全鏈 LONG_GAMMA 但現價已落入負 Gamma 區 → 揭露局部體制與雙向翻轉線
  #10 PutWall 處淨 GEX 為負 → 揭露助跌區並列出最近淨 GEX 支撐
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
    assert "淨 GEX -39414K 為負" in text
    assert "最近淨 GEX 支撐: $1040.00" in text


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
    assert "最近淨 GEX 支撐" not in text


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
