"""/x 個股面板的跨區塊一致性（2026-10-02 MU 實測案例）。

各區塊原本各自獨立判定：下行緩衝落在甜蜜點就印「✅ 進場甜蜜點」而不看上檔空間、
RVOL ≥ 1.5 就印「放量突破」而不看 Gamma 體制與時段、Skew/PCR 缺值時背離偵測
預設印「同步」。這裡以 MU 實測數值鎖定交叉判定後的呈現。
"""

from datetime import date, datetime, timedelta
from typing import Any

from cogs.embed_builders.portfolio_embeds import (
    _parse_poly_bullish_pct,
    create_tactical_symbol_embed,
)
from market_analysis.price_volume_alert import compute_time_of_day_avg_volume
from market_analysis.sentiment.max_pain import find_settlement_gravity
from market_time import ny_tz

import pandas as pd


def _embed_text(data: dict[str, Any]) -> str:
    embed = create_tactical_symbol_embed(data)
    parts = [str(embed.description or "")]
    for field in embed.fields:
        parts.append(str(field.name))
        parts.append(str(field.value))
    return "\n".join(parts)


def _mu_case(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "symbol": "MU",
        "price": 1097.39,
        "gex_profile_data": {
            "put_wall": 1050.0,
            "call_wall": 1100.0,
            "net_gex": 49_097_538_000,
            "gex_profile": {
                "1050.0": -138_988_000,
                "1070.0": 200_000_000,
                "1100.0": 8_850_000_000,
            },
        },
        # 1050 − 0.5 × 9.68 = 1045.16；停損距離 4.76%
        "atr_15m": 9.68,
        "atr_14": 40.0,
    }
    data.update(overrides)
    return data


def test_sweet_spot_downgraded_when_upside_room_insufficient() -> None:
    desc = _embed_text(_mu_case())
    assert "✅ 進場甜蜜點" not in desc
    assert "✅ 停損距離合格" in desc
    assert "❌ 上檔空間 0.24% 不足 10.47%，非進場點" in desc
    # (1100 − 1097.39) / (1097.39 − 1045.16) = 0.05；淨 GEX 錨 (1070 → 1065.16) = 0.08
    assert "短線盈虧比 (期權視角，至 CallWall $1100.00): 0.05:1 ❌" in desc
    assert "淨 GEX 支撐錨 $1070.00 0.08:1 ❌" in desc
    assert "(門檻 2.2:1)" in desc


def test_sweet_spot_kept_when_upside_room_sufficient() -> None:
    data = _mu_case()
    data["gex_profile_data"] = {
        "put_wall": 1050.0,
        "call_wall": 1250.0,
        "gex_profile": {"1050.0": 300_000_000, "1250.0": 900_000_000},
    }
    desc = _embed_text(data)
    assert "✅ 進場甜蜜點" in desc
    assert "非進場點" not in desc
    # (1250 − 1097.39) / (1097.39 − 1045.16) = 2.92
    assert "2.92:1 ✅" in desc


def test_net_gex_support_stop_is_primary_when_putwall_is_short_gamma() -> None:
    desc = _embed_text(_mu_case())
    assert "參考停損 (淨 GEX 支撐 $1070.00−0.5×ATR₁₅ₘ): $1065.16" in desc
    assert "引擎閘門停損 (PutWall−0.5×ATR₁₅ₘ): $1045.16 (↓4.76%)" in desc
    assert desc.index("參考停損 (淨 GEX 支撐") < desc.index("引擎閘門停損")


def test_pinning_note_when_long_gamma_and_hugging_callwall() -> None:
    desc = _embed_text(_mu_case())
    assert "LONG_GAMMA" in desc
    assert "📌 釘住效應：Long Gamma 且距 CallWall $1100.00 僅 0.24%" in desc


def test_no_pinning_note_when_short_gamma() -> None:
    data = _mu_case()
    data["gex_profile_data"]["net_gex"] = -5_000_000_000
    assert "釘住效應" not in _embed_text(data)


def _bar_fields(bar_time: datetime, **extra: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "bar_15m": {
            "open": 1088.57,
            "high": 1098.90,
            "low": 1088.0,
            "close": 1097.68,
            "volume": 2_942_149,
            "avg_volume": 1_202_233,
        },
        "bar_15m_time": bar_time,
        "rvol_15m": 2.45,
    }
    fields.update(extra)
    return fields


def test_rvol_label_is_volume_only_and_uses_time_of_day_baseline() -> None:
    bar_time = datetime(2026, 10, 1, 15, 45)
    desc = _embed_text(
        _mu_case(**_bar_fields(bar_time, rvol_15m_tod=1.86, tod_sample_count=4))
    )
    assert "放量突破" not in desc
    assert (
        "即時量比 (RVOL_15m): 2.45x｜同時段量比 1.86x (前 4 日中位數，樣本少)" in desc
    )
    assert "🟢 放量 >= 1.5x" in desc
    assert "@10-01 15:45 [前一交易日]" in desc


def test_rvol_status_follows_time_of_day_ratio() -> None:
    bar_time = datetime(2026, 10, 1, 15, 45)
    desc = _embed_text(
        _mu_case(**_bar_fields(bar_time, rvol_15m_tod=1.2, tod_sample_count=4))
    )
    assert "❌ 缺乏放量代償 < 1.5x" in desc


def test_auction_bar_caveat_without_time_of_day_baseline() -> None:
    bar_time = datetime(2026, 10, 1, 15, 45)
    desc = _embed_text(_mu_case(**_bar_fields(bar_time)))
    assert "開盤／收盤競價時段，量比天然偏高" in desc


def test_atr_label_is_wilder_and_prior_session_vwap_is_marked() -> None:
    desc = _embed_text(
        _mu_case(
            **_bar_fields(datetime(2026, 10, 1, 15, 45)),
            session_vwap=1062.81,
            session_vwap_date=date(2026, 10, 1),
        )
    )
    assert "15m ATR (Wilder 14): $9.68" in desc
    assert "EMA14" not in desc
    assert "Session VWAP): $1062.81 [前一交易日 10-01]" in desc


def test_divergence_reports_missing_data_instead_of_in_sync() -> None:
    desc = _embed_text(_mu_case(skew_percentile=None, pcr={}))
    assert "資料不足（Skew 分位／PCR 缺失）" in desc
    assert "狀態: \u001b[1;30m資料不足" in desc
    assert "同步" not in desc.split("情緒背離偵測")[1].split("```")[0]


def test_reference_divergence_volume_vs_polymarket() -> None:
    desc = _embed_text(
        _mu_case(
            **_bar_fields(datetime(2026, 10, 1, 14, 0), rvol_15m_tod=1.9),
            polymarket_summary="🔴 20.2% 巨鯨偏空 (3檔加權 · 池量 $1.2M)",
        )
    )
    assert "量價／預測市場背離（參考）：量價偏多 vs Polymarket 偏空 (20.2%)" in desc
    assert "結構背離未判定" in desc
    # 參考級背離不得觸發結構性背離 overlay
    assert "結構背離:" not in desc


def test_parse_poly_bullish_pct() -> None:
    assert _parse_poly_bullish_pct("🔴 20.2% 巨鯨偏空 (3檔加權)") == 20.2
    assert _parse_poly_bullish_pct("🟢 61.0% 巨鯨看多") == 61.0
    assert _parse_poly_bullish_pct("N/A") is None
    assert _parse_poly_bullish_pct(None) is None


def test_settlement_gravity_overlay() -> None:
    tomorrow = (datetime.now(ny_tz).date() + timedelta(days=1)).isoformat()
    desc = _embed_text(
        _mu_case(
            month_max_pains=[
                {"expiry": tomorrow, "max_pain": 1040.0, "distance_pct": 5.5}
            ]
        )
    )
    assert (
        f"結算引力: \u001b[1;31m⚠️ 結算日引力：{tomorrow} (DTE 1) 痛點 $1040.00" in desc
    )
    assert "現價偏離 +5.5%" in desc


def test_find_settlement_gravity_rules() -> None:
    now = datetime(2026, 10, 2, 10, 0, tzinfo=ny_tz)
    items = [
        {"expiry": "2026-10-02", "max_pain": 1040.0, "distance_pct": 5.5},
        {"expiry": "2026-10-05", "max_pain": 1050.0, "distance_pct": 4.5},
    ]
    hit = find_settlement_gravity(items, now)
    assert hit is not None and hit["dte"] == 0 and hit["max_pain"] == 1040.0
    # 到期日 16:00 之後該檔已結算
    after_close = datetime(2026, 10, 2, 16, 30, tzinfo=ny_tz)
    assert find_settlement_gravity(items, after_close) is None
    # 偏離在 3% 內不觸發；>30% 屬斷路器範圍也不觸發
    assert (
        find_settlement_gravity(
            [{"expiry": "2026-10-02", "max_pain": 1090.0, "distance_pct": 0.7}], now
        )
        is None
    )
    assert (
        find_settlement_gravity(
            [{"expiry": "2026-10-02", "max_pain": 700.0, "distance_pct": 56.0}], now
        )
        is None
    )
    assert find_settlement_gravity(None, now) is None


def test_compute_time_of_day_avg_volume() -> None:
    idx = []
    vols = []
    for d in range(1, 6):
        for hh, mm, v in ((15, 30, 1_000_000), (15, 45, 2_000_000 + d * 100_000)):
            idx.append(pd.Timestamp(2026, 9, 24 + d, hh, mm))
            vols.append(v)
    df = pd.DataFrame({"Volume": vols}, index=pd.DatetimeIndex(idx))
    avg, n = compute_time_of_day_avg_volume(df)
    assert n == 4
    # 前 4 日 15:45：2.1M, 2.2M, 2.3M, 2.4M
    assert avg == 2_250_000.0
    avg_short, n_short = compute_time_of_day_avg_volume(df.iloc[-4:])
    assert avg_short is None and n_short == 1


def _mu_intraday_case(**overrides: Any) -> dict[str, Any]:
    """2026-10-02 11:15 ET 盤中實測：PutWall $1070 為淨 GEX 正支撐，停損距離過窄。"""
    data: dict[str, Any] = {
        "symbol": "MU",
        "price": 1075.47,
        "gex_profile_data": {
            "put_wall": 1070.0,
            "call_wall": 1100.0,
            "net_gex": 42_922_638_000,
            "gex_profile": {
                "1070.0": 4_162_210_000,
                "1075.0": 3_780_095_000,
                "1100.0": 12_756_674_000,
            },
        },
        # 1070 − 0.5 × 9.59 = 1065.205；停損距離 0.95% < 2.5×ATR₁₅ₘ = 2.23%
        "atr_15m": 9.59,
        "atr_14": 47.32,
    }
    data.update(overrides)
    return data


def test_rr_not_green_when_stop_too_tight() -> None:
    desc = _embed_text(_mu_intraday_case())
    assert "❌ 過窄 (< 2.5×ATR₁₅ₘ = 2.23%)" in desc
    # (1100 − 1075.47) / (1075.47 − 1065.205) = 2.39，但停損過窄，不得給 ✅
    assert "2.39:1 ⚠ 停損過窄、比值虛高" in desc
    assert "2.39:1 ✅" not in desc
    # 合格停損 2.23%：24.53 / 23.975 = 1.02
    assert "合格停損 ↓2.23% 1.02:1 ❌" in desc


def test_callwall_threshold_names_binding_term() -> None:
    desc = _embed_text(_mu_intraday_case())
    # max(2.2×0.95%, 1.5×4.40%, 3.5%) = 6.60%，由單日波幅項決定
    assert "❌ 不足 6.60% (動態門檻：1.5×ATR₁D)" in desc


def test_released_catalysts_are_not_shown_with_negative_days() -> None:
    from types import SimpleNamespace

    catalysts = [
        SimpleNamespace(
            time="2026-10-02T12:30:00Z", event="非農就業人數", tte_hours=-2.7
        ),
        SimpleNamespace(time="2026-10-02T12:30:00Z", event="失業率", tte_hours=-2.7),
        SimpleNamespace(
            time="2026-10-05T14:00:00Z", event="ISM 服務業 PMI", tte_hours=69.6
        ),
    ]
    desc = _embed_text(
        _mu_intraday_case(catalysts=catalysts, iv_data={"current_iv": 0.357})
    )
    assert "-0.1 天" not in desc
    assert "距離 ISM 服務業 PMI (10-05) 僅剩 2.9 天" in desc
    assert "✅ 已公布：非農就業人數、失業率（市場消化中）" in desc


def test_fmt_gex_notional_units() -> None:
    from cogs.embed_builders.portfolio_embeds import _fmt_gex_notional

    assert _fmt_gex_notional(1.162e9) == "+$11.6M"
    assert _fmt_gex_notional(-6.24e8) == "-$6.2M"
    assert _fmt_gex_notional(2.638e11) == "+$2.64B"
    assert _fmt_gex_notional(0.0) == " $0"


def test_uoa_field_labels_unknown_direction_and_inferred_type() -> None:
    from cogs.embed_builders.portfolio_embeds import _format_uoa_field

    base = {
        "expiry": "2026-10-09",
        "strike": 165.0,
        "type": "PUT",
        "volume": 5000,
        "oi": 4000,
        "ratio": 1.25,
        "ratio_str": "1.25x",
        "paced_ratio": 3.0,
        "intent": "x",
        "symbol": "SPCX",
    }
    unknown = _format_uoa_field(
        [
            {
                **base,
                "action": "⚖️ MIDPOINT (Cross)",
                "direction_note": "過時成交",
            }
        ]
    )
    assert "❔ 未定" in unknown
    assert "1.25x→3.00x" in unknown
    inferred = _format_uoa_field(
        [
            {
                **base,
                "action": "🔴 賣出開倉 (STO - Bid)",
                "trade_type": "SWEEP",
                "trade_type_inferred": True,
            }
        ]
    )
    assert "📊 日累積" in inferred
    assert "🔥 SWEEP" not in inferred
