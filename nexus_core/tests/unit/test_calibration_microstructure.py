"""D-03 週 EM 到期日選擇、D-04 成交額正規化薄牆門檻，以及其校準資料工具的測試。"""

import math
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from calibration.microstructure import Contract, compute_gex_profile, depth_ratio
from calibration.skew_proxy import rolling_midrank_percentile
from market_analysis.gex_wall_depth import (
    GEX_THIN_WALL_THRESHOLD,
    GEX_WALL_MIN_DEPTH_RATIO,
    thin_wall_threshold,
)

# ---------------------------------------------------------------------------
# D-03：週 EM 的跨式到期日選擇
# ---------------------------------------------------------------------------


class _Chain:
    def __init__(self, straddle_leg: float) -> None:
        self.calls = pd.DataFrame(
            [
                {
                    "strike": 100.0,
                    "bid": straddle_leg,
                    "ask": straddle_leg,
                    "lastPrice": 0,
                }
            ]
        )
        self.puts = pd.DataFrame(
            [
                {
                    "strike": 100.0,
                    "bid": straddle_leg,
                    "ask": straddle_leg,
                    "lastPrice": 0,
                }
            ]
        )


async def _em_for(dtes: list[int]) -> tuple[Any, list[str]]:
    from market_analysis.sentiment.iv_metrics import _calculate_straddle_implied_em

    today = datetime.now().date()
    expiries = [(today + timedelta(days=d)).strftime("%Y-%m-%d") for d in dtes]
    requested: list[str] = []

    async def _chain(symbol: str, expiry: str, force_live: bool = False) -> _Chain:
        requested.append(expiry)
        return _Chain(3.0)

    with (
        patch(
            "services.market_data_service.get_all_option_expiries",
            new_callable=AsyncMock,
            return_value=expiries,
        ),
        patch("services.market_data_service.get_option_chain", side_effect=_chain),
    ):
        em = await _calculate_straddle_implied_em("TEST", 100.0)
    return em, requested


@pytest.mark.asyncio
async def test_straddle_em_skips_0dte_and_prefers_7dte() -> None:
    em, requested = await _em_for([0, 1, 7, 14])
    today = datetime.now().date()
    assert requested == [(today + timedelta(days=7)).strftime("%Y-%m-%d")]
    assert em == pytest.approx(6.0 * math.sqrt(math.pi / 2.0), rel=1e-6)


@pytest.mark.asyncio
async def test_straddle_em_returns_none_when_only_expiring_contracts() -> None:
    """只剩 0/1-DTE 時回傳 None，由呼叫端退回 IV 公式，不做 √7 暴力外推。"""
    em, requested = await _em_for([0, 1])
    assert em is None
    assert requested == []


@pytest.mark.asyncio
async def test_straddle_em_picks_closest_to_week() -> None:
    _, requested = await _em_for([3, 10])
    today = datetime.now().date()
    assert requested == [(today + timedelta(days=10)).strftime("%Y-%m-%d")]


# ---------------------------------------------------------------------------
# D-04：成交額正規化薄牆門檻
# ---------------------------------------------------------------------------


def test_thin_wall_threshold_scales_with_adv_and_falls_back() -> None:
    assert thin_wall_threshold(None) == GEX_THIN_WALL_THRESHOLD
    assert thin_wall_threshold(0.0) == GEX_THIN_WALL_THRESHOLD
    assert thin_wall_threshold(float("nan")) == GEX_THIN_WALL_THRESHOLD
    assert thin_wall_threshold("bad") == GEX_THIN_WALL_THRESHOLD  # type: ignore[arg-type]
    # 原始 GEX 為每 100% 尺度：threshold_raw = ratio × ADV × 100，且不低於絕對下限
    assert thin_wall_threshold(1e9) == pytest.approx(GEX_WALL_MIN_DEPTH_RATIO * 1e11)
    # 小型股：正規化值低於下限時仍用 500k，不得比改版前寬鬆 (RCAT 62K 紙牆案例)
    assert thin_wall_threshold(5e7) == GEX_THIN_WALL_THRESHOLD


def test_support_wall_scan_uses_adv_normalized_threshold() -> None:
    from market_analysis.dynamic_rollover.structural_signals import _scan_gex_walls

    profile = {"gex_profile": {"90": 2_000_000.0, "110": -1_000_000.0}}
    # 無成交額：沿用 500k 絕對門檻，2M 視為有效支撐牆
    support, _, support_gex, _ = _scan_gex_walls("T", dict(profile), spot=100.0)
    assert (support, support_gex) == (90.0, 2_000_000.0)

    # 大型股 (ADV $10B → 門檻 1e7)：同一面 2M 的牆是薄牆，不得當成支撐
    big = {**profile, "adv_dollar_20d": 1e10}
    support, _, support_gex, _ = _scan_gex_walls("T", big, spot=100.0)
    assert (support, support_gex) == (0.0, 0.0)

    # 小型股 (ADV $50M → 正規化值 5e4 < 下限 500k)：門檻仍為 500k，2M 放行
    small = {**profile, "adv_dollar_20d": 5e7}
    support, _, _, _ = _scan_gex_walls("T", small, spot=100.0)
    assert support == 90.0
    thin_small = {"gex_profile": {"90": 100_000.0}, "adv_dollar_20d": 5e7}
    assert _scan_gex_walls("T", thin_small, spot=100.0)[0] == 0.0


def test_resistance_wall_scan_uses_adv_normalized_threshold() -> None:
    from market_analysis.dynamic_rollover.structural_signals import (
        _scan_resistance_wall_above_spot,
    )

    profile = {"gex_profile": {"110": 2_000_000.0}}
    assert _scan_resistance_wall_above_spot("T", dict(profile), 100.0) == (
        110.0,
        2_000_000.0,
    )
    assert _scan_resistance_wall_above_spot(
        "T", {**profile, "adv_dollar_20d": 1e10}, 100.0
    ) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# 校準工具
# ---------------------------------------------------------------------------


def test_compute_gex_profile_matches_edge_definitions() -> None:
    t = 7 / 365
    contracts = [
        Contract(95.0, 1000, 0.3, t, False),  # 下方 put
        Contract(90.0, 10, 0.3, t, False),  # |Δ|≈0.005 < 0.02，雜訊過濾剔除
        Contract(95.0, 3000, 0.3, t, True),  # 下方 call 使淨 GEX 為正
        Contract(105.0, 2000, 0.3, t, True),
        Contract(100.0, 0, 0.3, t, True),  # OI=0 應剔除
    ]
    g = compute_gex_profile(contracts, 100.0)
    assert g["support_strike"] == 95.0
    assert g["support_gex"] > 0
    assert g["put_wall"] == 95.0
    assert g["call_wall"] == 105.0
    assert g["n_contracts"] == 3


def test_depth_ratio_uses_per_one_percent_scale() -> None:
    assert depth_ratio(1e8, 1e9) == pytest.approx(1e-3)
    assert depth_ratio(0.0, 1e9) is None
    assert depth_ratio(1e8, 0.0) is None


def test_rolling_percentile_has_no_lookahead() -> None:
    values = [float(i) for i in range(100)]
    pct = rolling_midrank_percentile(values, window=252)
    # 前 60 天樣本不足
    assert pct[59] is None
    # 單調遞增序列：當天值永遠大於所有過去值 → 100%，若含當天會變成 < 100
    assert all(p == 100.0 for p in pct[60:])
    # 未來資料改變不影響過去的分位
    changed = values[:80] + [-1.0] * 20
    assert rolling_midrank_percentile(changed, window=252)[:80] == pct[:80]


def test_calibration_features_capture_wall_depth_and_skew() -> None:
    from market_analysis.evaluation_recorder import calibration_features

    feats = calibration_features(
        {
            "gex_profile": {"95": 3e6, "90": 1e6, "105": 5e6},
            "put_wall_gex": 2e6,
            "adv_dollar_20d": 4e8,
        },
        100.0,
        {"skew_percentile": 91.5, "skew_percentile_source": "CANONICAL"},
    )
    assert feats["support_wall"] == 95.0
    assert feats["support_gex"] == 3e6
    assert feats["adv_dollar_20d"] == 4e8
    assert feats["skew_percentile"] == 91.5
    assert feats["skew_percentile_source"] == "CANONICAL"


def test_forward_threshold_studies_group_by_depth_and_skew_source() -> None:
    import json

    from calibration.forward_log import FORWARD_MIN_ROWS, build_threshold_studies

    n = FORWARD_MIN_ROWS * 4
    rows = []
    for i in range(n):
        rows.append(
            {
                "features_json": json.dumps(
                    {
                        "support_gex": 1e5 * (i + 1),
                        "adv_dollar_20d": 1e9,
                        "skew_percentile": 99.0 if i % 2 else 50.0,
                        "skew_percentile_source": "CANONICAL",
                    }
                ),
                # 深度越深勝率越高：後半段全勝
                "win": 1 if i >= n // 2 else 0,
                "outcome": 1 if i >= n // 2 else -1,
            }
        )
    rows.append({"features_json": "not-json", "win": 0, "outcome": 0})
    entries = pd.DataFrame(rows)

    studies = build_threshold_studies(entries)
    depth = studies["wall_depth_ratio"]
    assert isinstance(depth, list) and len(depth) == 4
    assert depth[0]["win_rate"] == 0.0 and depth[-1]["win_rate"] == 1.0

    skew = studies["skew_percentile"]["CANONICAL"]
    buckets = {r["bucket"]: r["n"] for r in skew}
    assert buckets[">=98"] == n // 2
    assert buckets["15-85"] == n // 2


def test_forward_threshold_studies_without_features_reports_accumulating() -> None:
    from calibration.forward_log import build_threshold_studies

    studies = build_threshold_studies(
        pd.DataFrame({"features_json": [None], "win": [0], "outcome": [0]})
    )
    assert "資料累積中" in str(studies["wall_depth_ratio"])
    assert "資料累積中" in str(studies["skew_percentile"])
