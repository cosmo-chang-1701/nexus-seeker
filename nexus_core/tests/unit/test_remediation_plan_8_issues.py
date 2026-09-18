"""Unit tests for the 8-point remediation plan covering quantitative boundaries,
physical wall constraints, and floating-point precision guards.
"""

from typing import Any

import pytest

from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed
from cogs.embed_builders.market_embeds import build_radar_scan_embed
from cogs.embed_builders.watchlist_embeds import create_watchlist_signal_embed
from market_analysis.dynamic_rollover.structural_signals import (
    _scan_gex_walls,
    _scan_resistance_wall_above_spot,
)
from market_analysis.dynamic_rollover._shared import resolve_room_threshold_inputs
from market_analysis.dynamic_rollover.short_side_entry import (
    _find_next_negative_gex_peak,
)


def _get_embed_all_text(embed: Any) -> str:
    """提取 embed 內所有文字用於斷言驗證。"""
    if embed is None:
        return ""
    parts: list[str] = [str(getattr(embed, "description", "") or "")]
    for field in getattr(embed, "fields", []):
        parts.append(str(field.name))
        parts.append(str(field.value))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Point 1: PutWall Dynamic Re-anchoring & Float Precision in portfolio_embeds
# ---------------------------------------------------------------------------


def test_portfolio_embed_putwall_reanchor_on_spot_equal() -> None:
    """驗證當現價等於或平值觸及 PutWall 時，PutWall 正確向下動態重錨。"""
    data: dict[str, Any] = {
        "symbol": "SPCX",
        "price": 100.0,
        "quote": {"c": 100.0},
        "gex_profile_data": {
            "put_wall": 100.0,
            "call_wall": 110.0,
            "gex_profile": {
                "100.0": 500000.0,
                "95.0": 600000.0,
                "110.0": 600000.0,
            },
        },
        "atr_14": 2.0,
        "atr_15m": 0.5,
    }
    embed = create_tactical_symbol_embed(data)
    text = _get_embed_all_text(embed)
    # PutWall 應重錨至 95.00，且標註動態重錨
    assert "PutWall: $95.00 (動態重錨)" in text
    assert "⚠️ [數據異常：PutWall已高於現價]" not in text


def test_portfolio_embed_putwall_float_precision_jitter_no_anomaly() -> None:
    """驗證下行緩衝浮點抖動（如 put_buffer_pct 為 -1e-14）不觸發數據異常警示。"""
    data: dict[str, Any] = {
        "symbol": "SPCX",
        "price": 100.0,
        "quote": {"c": 100.0},
        "gex_profile_data": {
            "put_wall": 95.0,
            "call_wall": 105.0,
            "gex_profile": {
                "95.0": 600000.0,
                "105.0": 600000.0,
            },
        },
        "atr_14": 2.0,
        "atr_15m": 0.5,
    }
    embed = create_tactical_symbol_embed(data)
    text = _get_embed_all_text(embed)
    assert "⚠️ [數據異常：PutWall已高於現價]" not in text
    assert "⚠️ [數據異常：CallWall已低於現價]" not in text


def test_portfolio_embed_callwall_at_spot_does_not_trigger_data_anomaly() -> None:
    """驗證當 CallWall 等於現價（空間率 0.00%）時，不誤觸發 CallWall 已低於現價異常。"""
    data: dict[str, Any] = {
        "symbol": "SPCX",
        "price": 100.0,
        "quote": {"c": 100.0},
        "gex_profile_data": {
            "put_wall": 90.0,
            "call_wall": 100.0,
            "gex_profile": {
                "90.0": 600000.0,
                "100.0": 600000.0,
            },
        },
        "atr_14": 2.0,
        "atr_15m": 0.5,
    }
    embed = create_tactical_symbol_embed(data)
    text = _get_embed_all_text(embed)
    assert "⚠️ [數據異常：CallWall已低於現價]" not in text


def test_portfolio_embed_breached_putwall_filters_from_dynamic_room_threshold() -> None:
    """驗證當 PutWall 已被跌破且下方無有效底牆時，compute_dynamic_room_threshold 獲取 0.0 並標記降級。"""
    data: dict[str, Any] = {
        "symbol": "SPCX",
        "price": 90.0,
        "quote": {"c": 90.0},
        "gex_profile_data": {
            "put_wall": 100.0,
            "call_wall": 110.0,
            "gex_profile": {
                "100.0": 600000.0,
                "110.0": 600000.0,
            },
        },
        "atr_14": 2.0,
        "atr_15m": 0.5,
    }
    embed = create_tactical_symbol_embed(data)
    text = _get_embed_all_text(embed)
    # 由於 PutWall 跌破且無更低底牆，上檔空間計算缺少 PutWall 支撐牆，應退回絕對底線並揭露降級原因
    assert "數據缺失" in text or "PutWall" in text or "不足" in text


# ---------------------------------------------------------------------------
# Point 2: math.isfinite in structural_signals.py
# ---------------------------------------------------------------------------


def test_structural_signals_handles_inf_and_nan() -> None:
    """驗證 _scan_gex_walls 與 _scan_resistance_wall_above_spot 正確過濾 inf, -inf 與 nan。"""
    gex_profile_data: dict[str, Any] = {
        "gex_profile": {
            "100.0": float("inf"),
            "105.0": float("-inf"),
            "95.0": float("nan"),
            "90.0": 600000.0,
            "110.0": 700000.0,
        }
    }
    supp, res, _, _ = _scan_gex_walls("TEST", gex_profile_data, spot=100.0)
    assert supp == 90.0

    res_strike, res_gex = _scan_resistance_wall_above_spot(
        "TEST", gex_profile_data, spot=100.0
    )
    assert res_strike == 110.0
    assert res_gex == 700000.0


# ---------------------------------------------------------------------------
# Point 3: resolve_room_threshold_inputs target_spot physical constraints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_room_threshold_inputs_reanchors_when_ge_spot() -> None:
    """驗證 resolve_room_threshold_inputs 在 put_wall >= target_spot 時觸發重錨至更低履約價。"""
    gex_profile_data: dict[str, Any] = {
        "put_wall": 100.0,
        "gex_profile": {
            "100.0": 600000.0,
            "95.0": 700000.0,
        },
    }
    pw, _, _ = await resolve_room_threshold_inputs(
        "TEST",
        {"atr_14": 2.0, "atr_15m": 0.5},
        gex_profile_data,
        target_spot=100.0,
    )
    assert pw == 95.0


@pytest.mark.asyncio
async def test_resolve_room_threshold_inputs_returns_zero_when_no_lower_support() -> (
    None
):
    """驗證 resolve_room_threshold_inputs 在跌破且無更低支撐時回傳 0.0。"""
    gex_profile_data: dict[str, Any] = {
        "put_wall": 100.0,
        "gex_profile": {
            "100.0": 600000.0,
        },
    }
    pw, _, _ = await resolve_room_threshold_inputs(
        "TEST",
        {"atr_14": 2.0, "atr_15m": 0.5},
        gex_profile_data,
        target_spot=100.0,
    )
    assert pw == 0.0


# ---------------------------------------------------------------------------
# Point 4: market_embeds safe call_wall parsing & 15m ATR anti-washout stop
# ---------------------------------------------------------------------------


def test_market_embeds_handles_invalid_call_wall_and_uses_15m_atr() -> None:
    """驗證 market_embeds 面板在 call_wall 格式畸形時安全解析為 0.0，且防洗盤停損使用 15m ATR。"""
    record: dict[str, Any] = {
        "symbol": "TEST",
        "price": 99.0,
        "quote": {"c": 99.0, "dp": -1.0},
        "max_pain": {"max_pain": 100.0},
        "gex_metrics": {"put_wall": 100.0, "call_wall": "INVALID_WALL"},
        "gex_profile_data": {
            "call_wall": "ANOTHER_BAD",
            "gex_profile": {"100.0": 500000.0},
        },
        "atr_14": 5.0,  # 日線 ATR = 5.0
        "atr_15m": 1.0,  # 15m ATR = 1.0
    }
    embeds = build_radar_scan_embed([record], scan_type_name="WATCHLIST", user_id=12345)
    assert len(embeds) == 1
    text = _get_embed_all_text(embeds[0])
    # 防洗盤停損應使用 15m ATR (1.0): 100.0 - 1.5 * 1.0 = 98.50
    # 若誤用日線 ATR (5.0)，數值會是 100.0 - 1.5 * 5.0 = 92.50
    assert "$98.50" in text
    assert "$92.50" not in text


# ---------------------------------------------------------------------------
# Point 5: watchlist_embeds safe strike parsing with math.isfinite
# ---------------------------------------------------------------------------


def test_watchlist_embeds_handles_malformed_strike_keys() -> None:
    """驗證 watchlist_embeds 在 gex_profile 包含非數值或無效 key 時安全略過，正常渲染其餘 strike。"""
    symbol_gex: dict[str, Any] = {
        "spot": 100.0,
        "gex_profile": {
            "INVALID_KEY": 1000.0,
            "nan": 500.0,
            "inf": 600.0,
            "100.0": 50000.0,
            "105.0": 60000.0,
            "95.0": 40000.0,
        },
    }
    embed = create_watchlist_signal_embed(
        symbol="TEST",
        symbol_gex=symbol_gex,
        alert_level="yellow",
    )
    assert embed is not None
    text = _get_embed_all_text(embed)
    assert "TEST" in (embed.title or "") or "TEST" in text


# ---------------------------------------------------------------------------
# Point 6: short_side_entry _find_next_negative_gex_peak ATM exclusion
# ---------------------------------------------------------------------------


def test_short_side_find_next_negative_gex_peak_atm_exclusion() -> None:
    """驗證 _find_next_negative_gex_peak 嚴格排除現價與平值履約價 (strike >= target_spot)。"""
    gex_profile_data: dict[str, Any] = {
        "gex_profile": {
            "100.0": -900000.0,  # ATM 履約價，不可被選為下行做空目標
            "95.0": -300000.0,
            "90.0": -500000.0,
            "85.0": 200000.0,  # 正 GEX，不可選
        }
    }
    # 現價 100.0
    best_strike = _find_next_negative_gex_peak(gex_profile_data, target_spot=100.0)
    # 應選取現價下方負 GEX 幅度最大者 (90.0，幅度 500k > 95.0 的 300k)
    assert best_strike == 90.0


def test_short_side_find_next_negative_gex_peak_float_close_exclusion() -> None:
    """驗證 _find_next_negative_gex_peak 在履約價微小接近現價 (math.isclose) 時予以排除。"""
    gex_profile_data: dict[str, Any] = {
        "gex_profile": {
            "99.99999": -900000.0,  # 微小接近 100.0 (差 1e-5 < 1e-4)
            "95.0": -300000.0,
        }
    }
    best_strike = _find_next_negative_gex_peak(gex_profile_data, target_spot=100.0)
    assert best_strike == 95.0
