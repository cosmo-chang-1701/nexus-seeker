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


def test_snapshot_skip_reason_guards_schedule(tmp_path: Any) -> None:
    from zoneinfo import ZoneInfo

    from calibration.microstructure import snapshot_skip_reason

    ny = ZoneInfo("America/New_York")
    # 週六
    assert "不是交易日" in str(
        snapshot_skip_reason(tmp_path, datetime(2026, 9, 26, 18, 0, tzinfo=ny))
    )
    # 感恩節
    assert "不是交易日" in str(
        snapshot_skip_reason(tmp_path, datetime(2026, 11, 26, 18, 0, tzinfo=ny))
    )
    # 交易日盤中
    assert "尚未收盤" in str(
        snapshot_skip_reason(tmp_path, datetime(2026, 9, 23, 15, 0, tzinfo=ny))
    )
    # 半日市 13:00 收盤後可以執行
    assert (
        snapshot_skip_reason(tmp_path, datetime(2026, 11, 27, 13, 30, tzinfo=ny))
        is None
    )
    # 收盤後可以執行；當天快照已存在則略過
    after_close = datetime(2026, 9, 23, 18, 0, tzinfo=ny)
    assert snapshot_skip_reason(tmp_path, after_close) is None
    (tmp_path / "microstructure").mkdir()
    (tmp_path / "microstructure" / "snapshot_2026-09-23.jsonl").write_text("")
    assert "已存在" in str(snapshot_skip_reason(tmp_path, after_close))


@pytest.mark.asyncio
async def test_snapshot_symbol_fetches_full_chain_via_market_data_service() -> None:
    """快照經 market_data_service (edge 代理) 抓取，且期權鏈不裁減履約價。"""
    from calibration.microstructure import snapshot_symbol

    today = datetime(2026, 9, 21).date()
    hist = pd.DataFrame(
        {
            "Open": [100.0] * 30,
            "High": [101.0] * 30,
            "Low": [99.0] * 30,
            "Close": [100.0] * 30,
            "Volume": [1_000_000.0] * 30,
        }
    )
    chain = _Chain(3.0)
    chain.calls["openInterest"] = [500]
    chain.calls["impliedVolatility"] = [0.3]
    chain.puts["openInterest"] = [500]
    chain.puts["impliedVolatility"] = [0.3]
    get_chain = AsyncMock(return_value=chain)

    with (
        patch(
            "services.market_data_service.get_history_df",
            new=AsyncMock(return_value=hist),
        ),
        patch(
            "services.market_data_service.get_all_option_expiries",
            # 當天到期 (DTE=0) 的合約已結算，不得拿來算 GEX
            new=AsyncMock(return_value=["2026-09-21", "2026-09-28", "2026-10-30"]),
        ),
        patch("services.market_data_service.get_option_chain", new=get_chain),
        patch("calibration.microstructure.asyncio.sleep", new=AsyncMock()),
    ):
        rec = await snapshot_symbol("TEST", today)

    assert rec is not None
    assert rec["adv_dollar_20d"] == pytest.approx(1e8)
    assert rec["nearest_dte"] == 7
    assert [e["expiry"] for e in rec["em"]] == ["2026-09-28"]
    # 30 DTE 的到期日超出 EM 範圍且 GEX 已算出，不應再抓
    assert get_chain.await_count == 1
    assert get_chain.await_args is not None
    assert get_chain.await_args.kwargs.get("prune_pct", "missing") is None


@pytest.mark.asyncio
async def test_snapshot_symbol_drops_chain_with_zero_open_interest() -> None:
    """Yahoo 夜間重置時段的期權鏈 (未平倉量全為 0) 不寫入快照。"""
    from calibration.microstructure import snapshot_symbol

    hist = pd.DataFrame(
        {
            "Open": [100.0] * 30,
            "High": [101.0] * 30,
            "Low": [99.0] * 30,
            "Close": [100.0] * 30,
            "Volume": [1_000_000.0] * 30,
        }
    )
    chain = _Chain(3.0)
    for frame in (chain.calls, chain.puts):
        frame["openInterest"] = [0]
        frame["impliedVolatility"] = [1e-5]

    with (
        patch(
            "services.market_data_service.get_history_df",
            new=AsyncMock(return_value=hist),
        ),
        patch(
            "services.market_data_service.get_all_option_expiries",
            new=AsyncMock(return_value=["2026-09-28"]),
        ),
        patch(
            "services.market_data_service.get_option_chain",
            new=AsyncMock(return_value=chain),
        ),
        patch("calibration.microstructure.asyncio.sleep", new=AsyncMock()),
    ):
        assert await snapshot_symbol("TEST", datetime(2026, 9, 21).date()) is None


# ---------------------------------------------------------------------------
# PutWall 定義比較（docs/microstructure/02 §7）
# ---------------------------------------------------------------------------


def _wall_snap(day: str = "2026-09-22") -> dict[str, Any]:
    # ADV $1B → 薄牆門檻 max(500k, 1e-5 × 1e9 × 100) = 1e6
    return {
        "date": day,
        "symbol": "MU",
        "spot": 1100.0,
        "adv_dollar_20d": 1e9,
        "atr_1d": 10.0,
        "gex": {
            "put_wall": 1050.0,
            "support_strike": 1000.0,
            "support_gex": 9e6,
            "net_profile": {
                "1090.0": 4e5,  # 最近但未過薄牆門檻
                "1070.0": 2e6,  # 最近強牆
                "1050.0": -3e6,  # PutWall 處淨 GEX 為負（助跌區）
                "1000.0": 9e6,  # 淨 GEX 最大
                "1150.0": 8e6,  # 現價上方不列入
            },
        },
    }


def test_support_definitions_separate_max_and_nearest_strong_wall() -> None:
    from calibration.microstructure import support_definitions

    defs = support_definitions(_wall_snap())
    assert defs["edge_PutWall"] == (1050.0, -3e6)
    assert defs["淨GEX最大"] == (1000.0, 9e6)
    assert defs["淨GEX最大_過薄牆門檻"] == (1000.0, 9e6)
    assert defs["淨GEX最近強牆"] == (1070.0, 2e6)


def test_support_definitions_without_profile_keeps_legacy_fields() -> None:
    from calibration.microstructure import support_definitions

    snap = _wall_snap()
    del snap["gex"]["net_profile"]
    snap["gex"]["support_gex"] = 6e5  # 低於門檻 1e6
    defs = support_definitions(snap)
    assert math.isnan(defs["edge_PutWall"][1])
    assert "淨GEX最大" in defs
    assert "淨GEX最大_過薄牆門檻" not in defs
    assert "淨GEX最近強牆" not in defs


def test_compute_gex_profile_exposes_net_profile() -> None:
    t = 7 / 365
    g = compute_gex_profile(
        [Contract(95.0, 1000, 0.3, t, False), Contract(95.0, 3000, 0.3, t, True)],
        100.0,
    )
    assert set(g["net_profile"]) == {"95.0"}
    assert g["net_profile"]["95.0"] == pytest.approx(g["support_gex"], rel=1e-6)


def test_label_symbol_scores_each_definition_on_same_window() -> None:
    from calibration.microstructure import _label_symbol, support_definitions

    snap = _wall_snap("2026-09-21")
    snap["_defs"] = support_definitions(snap)
    idx = pd.bdate_range("2026-09-21", periods=6)
    # 觀察期最低 1060（觸及 1070 帶、未到 1050/1000 帶），收盤最低 1065（跌破 1070）
    hist = pd.DataFrame(
        {
            "Low": [1095.0, 1080.0, 1060.0, 1075.0, 1090.0, 1092.0],
            "Close": [1098.0, 1085.0, 1065.0, 1080.0, 1095.0, 1096.0],
        },
        index=idx,
    )
    (rec,) = _label_symbol([snap], hist, horizon_days=5)
    w = rec["walls"]
    assert w["淨GEX最近強牆"]["tested"] and not w["淨GEX最近強牆"]["held"]
    assert not w["edge_PutWall"]["tested"]
    assert not w["淨GEX最大"]["tested"]
    assert rec["tested"] is False  # 頂層＝淨 GEX 最大，供深度四分位表
    assert w["淨GEX最近強牆"]["dist_pct"] == pytest.approx(30 / 1100 * 100)


def _labeled(
    day: str, put_held: bool, near_held: bool, put_net: float
) -> dict[str, Any]:
    walls = {
        name: {
            "strike": 1.0,
            "net_gex": 1.0,
            "dist_pct": 2.0,
            "tested": True,
            "held": True,
        }
        for name in ("淨GEX最大", "淨GEX最大_過薄牆門檻")
    }
    walls["edge_PutWall"] = {
        "strike": 1.0,
        "net_gex": put_net,
        "dist_pct": 5.0,
        "tested": True,
        "held": put_held,
    }
    walls["淨GEX最近強牆"] = {
        "strike": 1.0,
        "net_gex": 1.0,
        "dist_pct": 1.0,
        "tested": True,
        "held": near_held,
    }
    return {"symbol": "X", "date": day, "walls": walls}


def test_compare_support_definitions_requires_sample_floor() -> None:
    from calibration.microstructure import compare_support_definitions

    labeled = [_labeled(f"2026-09-{d:02d}", False, True, 1.0) for d in range(1, 11)]
    out = compare_support_definitions(labeled)
    assert out["n_labeled_dates"] == 10
    assert out["判讀"].startswith("樣本不足")
    assert out["定義比較"]["淨GEX最近強牆"]["hold_rate"] == 1.0
    assert out["定義比較"]["edge_PutWall"]["hold_rate"] == 0.0


def test_compare_support_definitions_needs_non_overlapping_ci() -> None:
    from calibration.microstructure import compare_support_definitions

    days = [f"d{i:02d}" for i in range(25)]
    better = [
        _labeled(d, i % 4 == 0, True, 1.0) for i, d in enumerate(days) for _ in (0, 1)
    ]
    assert "可提案統一定義" in compare_support_definitions(better)["判讀"]

    tied = [
        _labeled(d, i % 2 == 0, i % 2 == 1, 1.0)
        for i, d in enumerate(days)
        for _ in (0, 1)
    ]
    assert "維持 edge PutWall" in compare_support_definitions(tied)["判讀"]


def test_put_wall_net_gex_stats_counts_negative_zone() -> None:
    from calibration.microstructure import put_wall_net_gex_stats

    neg = _wall_snap("2026-09-21")
    pos = _wall_snap("2026-09-22")
    pos["gex"]["net_profile"]["1050.0"] = 5e5
    no_profile = _wall_snap("2026-09-23")
    del no_profile["gex"]["net_profile"]
    labeled = [
        _labeled("2026-09-21", False, True, -3e6),
        _labeled("2026-09-22", True, True, 5e5),
    ]
    out = put_wall_net_gex_stats([neg, pos, no_profile], labeled)
    assert out["n_put_wall_with_profile"] == 2
    assert out["淨GEX<0比例"] == 0.5
    by_sign = out["守住率_依PutWall處淨GEX正負"]
    assert by_sign["淨GEX<0"]["hold_rate"] == 0.0
    assert by_sign["淨GEX>=0"]["hold_rate"] == 1.0


def test_calibration_features_record_gamma_flip_materiality() -> None:
    from market_analysis.evaluation_recorder import calibration_features

    profile = {"1070.0": 4.16e9, "1072.5": -9.4e6, "1075.0": 3.78e9}
    feats = calibration_features({"gex_profile": profile}, 1075.43)
    assert feats["gamma_flip_raw"] == 1075.0
    assert feats["gamma_flip_ratio"] == pytest.approx(9.4e6 / 4.16e9)
    assert feats["gamma_flip_neg_peak"] == pytest.approx(9.4e6)
    # 沿用 `_num()` 慣例：0（排除後無 Flip）記為 None
    assert feats["gamma_flip_material_5pct"] is None
    # gex_profile 缺失或格式異常：不寫入欄位、也不拋例外
    assert "gamma_flip_raw" not in calibration_features({}, 100.0)
    bad = calibration_features({"gex_profile": {"x": "bad"}}, 100.0)
    assert bad["gamma_flip_raw"] is None
    assert bad["gamma_flip_ratio"] is None


def _flip_rows(n: int, ratio: float, outcome: int) -> list[dict[str, Any]]:
    import json

    return [
        {
            "features_json": json.dumps(
                {"gamma_flip_raw": 100.0, "gamma_flip_ratio": ratio}
            ),
            "date": f"2026-0{1 + i % 9}-{10 + i % 18}",
            "win": 1 if outcome == 1 else 0,
            "outcome": outcome,
        }
        for i in range(n)
    ]


def test_forward_gamma_flip_materiality_study_verdicts() -> None:
    from calibration.forward_log import FORWARD_MIN_ROWS, build_threshold_studies

    # 雜訊交叉全數逆向先觸及、重要交叉全勝 → 可提案
    entries = pd.DataFrame(
        _flip_rows(FORWARD_MIN_ROWS, 0.01, -1) + _flip_rows(FORWARD_MIN_ROWS, 0.5, 1)
    )
    study = build_threshold_studies(entries)["gamma_flip_materiality"]
    assert study["groups"]["雜訊交叉"]["n"] == FORWARD_MIN_ROWS
    assert study["groups"]["雜訊交叉"]["adverse_rate"] == 1.0
    assert study["groups"]["重要交叉"]["adverse_rate"] == 0.0
    assert "可提案" in study["判讀"]
    assert len(study["ratio_quartiles"]) >= 2

    # 樣本不足 → 維持
    small = pd.DataFrame(_flip_rows(10, 0.01, -1) + _flip_rows(10, 0.5, 1))
    study = build_threshold_studies(small)["gamma_flip_materiality"]
    assert "樣本不足" in study["判讀"]
    assert "ratio_quartiles" not in study

    # 尚無欄位
    studies = build_threshold_studies(
        pd.DataFrame({"features_json": [None], "win": [0], "outcome": [0]})
    )
    assert "資料累積中" in str(studies["gamma_flip_materiality"])


def test_micro_report_gamma_flip_materiality_stats() -> None:
    from calibration.microstructure import gamma_flip_materiality_stats

    noise = {"1070.0": 4.16e9, "1072.5": -9.4e6, "1075.0": 3.78e9}
    reselect = {"90.0": -2e9, "92.5": 3e9, "95.0": 1e9, "97.5": -1e7, "100.0": 4e9}
    material = {"95.0": -5e9, "100.0": 4e9, "105.0": 2e9}
    snaps = [
        {"spot": 1075.43, "gex": {"net_profile": noise}},
        {"spot": 101.0, "gex": {"net_profile": reselect}},
        {"spot": 101.0, "gex": {"net_profile": material}},
        {"spot": 101.0, "gex": {}},  # 舊版快照無 net_profile
    ]
    stats = gamma_flip_materiality_stats(snaps)
    assert stats["n_with_profile"] == 3
    assert stats["n_raw_flip"] == 3
    row = stats["依門檻剔除"]["5%"]
    assert row == {"剔除": 2, "改選": 1, "消失": 1, "剔除率": round(2 / 3, 3)}
