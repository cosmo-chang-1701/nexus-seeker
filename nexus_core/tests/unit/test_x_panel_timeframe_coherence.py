"""/x 時間框架一致性與籌碼意圖（PR-B）：日內交叉檢核、熊旗、Skew 呈現、結算轉移、前向紀錄。

GEX 區塊整段包在 try/except，錯誤只會讓欄位消失，故每例都斷言欄位存在。
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed
from market_analysis.squeeze_entry.intraday_conflict import (
    assess_intraday_conflict,
    negative_momentum_tfs,
)

GEX_FIELD = "🧲 Gamma 曝險分布"


def _render_embed(data: dict[str, Any], now: datetime | None = None) -> Any:
    from market_time import ny_tz

    fixed = now or datetime(2026, 10, 7, 21, 14, tzinfo=ny_tz)

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "_FakeDT":
            return fixed  # type: ignore[return-value]

    with patch("cogs.embed_builders.portfolio_embeds.datetime", _FakeDT):
        return create_tactical_symbol_embed(data)


def _render(data: dict[str, Any], now: datetime | None = None) -> str:
    embed = _render_embed(data, now)
    assert any(GEX_FIELD in (f.name or "") for f in embed.fields), "GEX 欄位消失"
    return "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)


def _tf(mv: float, color: str) -> SimpleNamespace:
    return SimpleNamespace(momentum_value=mv, momentum_color=color)


def _spcx(**extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "symbol": "SPCX",
        "price": 160.9,
        "atr_1d": 3.0,
        "atr_15m": 0.8,
        "open_15m": 163.0,
        "close_15m": 160.9,
        "high_15m": 163.2,
        "low_15m": 160.8,
        "volume_15m": 2_820_000,
        "volume_15m_sma20": 1_000_000,
        "rvol_15m": 2.82,
        "volume_profile": {"hvn": 165.0, "lvn": 161.32},
        "gex_profile_data": {
            "spot": 160.9,
            "put_wall": 150.0,
            "call_wall": 175.0,
            "net_gex": 5_000_000.0,
            "gex_profile": {
                "150.0": 80_000_000.0,
                "155.0": 90_000_000.0,
                "160.0": 1_000_000.0,
                "175.0": 3_000_000.0,
            },
        },
        "squeeze_eval": SimpleNamespace(
            matrix={"65m": _tf(-8.67, "Red"), "15m": _tf(-2.0, "Golden")},
            result=SimpleNamespace(
                status="ENTRY",
                tier=3,
                size_pct=5,
                stop=135.2,  # -15.97%
                triggers=["D Green Dot"],
                reason="T3",
                resistance_warning=None,
            ),
        ),
    }
    d.update(extra)
    return d


# --- B1 ---------------------------------------------------------------------


def test_b1_spcx_intraday_conflict_and_wide_stop() -> None:
    text = _render(_spcx())
    assert (
        "⚠ 日內衝突: 65m 動能 -8.67 加速向下、15m 放量陰線 2.82x 跌穿 LVN $161.32"
        in text
    )
    assert "判定未改" in text
    assert "🟢 可進場" in text  # 判定不受影響
    assert "⚠ 超寬；5% 部位單筆風險≈0.80% NAV" in text


def test_b1_only_when_entry_status() -> None:
    d = _spcx()
    d["squeeze_eval"].result.status = "WATCH"
    text = _render(d)
    assert "日內衝突" not in text and "超寬" not in text


def test_b1_callwall_hug_line() -> None:
    d = _spcx()
    d["gex_profile_data"]["call_wall"] = 160.95
    d["gex_profile_data"]["gex_profile"]["160.95"] = 3_000_000.0
    text = _render(d)
    assert "⚠ 上檔: 距 CallWall" in text and "已封頂" in text


def test_b1_no_conflict_no_line() -> None:
    d = _spcx()
    d["squeeze_eval"].matrix = {
        "65m": _tf(1.0, "LightBlue"),
        "15m": _tf(1.0, "LightBlue"),
    }
    d["open_15m"], d["close_15m"] = 160.0, 160.9
    d["squeeze_eval"].result.stop = 155.0
    text = _render(d)
    assert "日內衝突" not in text and "超寬" not in text


def test_b1_turn_note_shares_helper_and_keeps_wording() -> None:
    d = _spcx()
    d["psq_result"] = {
        "is_squeezing": False, "momentum": 3.0, "squeeze_level": "Release",
        "direction": "Long", "vix_momentum_label": "NORMAL",
    }  # fmt: skip
    text = _render(d)
    assert "⚠ 日內轉弱: 65m/15m 動能<0" in text


def test_helper_boundaries() -> None:
    m = {"65m": _tf(-0.01, "Red"), "15m": _tf(0.0, "Red")}
    assert assess_intraday_conflict(m) == ["65m 動能 -0.01 加速向下"]
    # 負但減弱（Golden）不算
    assert assess_intraday_conflict({"65m": _tf(-5.0, "Golden")}) == []
    assert negative_momentum_tfs({"65m": _tf(-5.0, "Golden")}) == [("65m", -5.0)]
    assert negative_momentum_tfs(None) == []
    assert negative_momentum_tfs({"65m": SimpleNamespace()}) == []
    kw: dict[str, Any] = dict(bar_open=163.0, bar_close=160.9, rvol_eff=1.5, lvn=161.32)
    assert assess_intraday_conflict({}, **kw) == ["15m 放量陰線 1.50x 跌穿 LVN $161.32"]
    assert assess_intraday_conflict({}, **{**kw, "rvol_eff": 1.49}) == []  # 量比邊界
    assert (
        assess_intraday_conflict({}, **{**kw, "bar_open": 161.0}) == []
    )  # 開盤已在 LVN 下
    assert (
        assess_intraday_conflict({}, **{**kw, "bar_close": 161.32}) == []
    )  # 收盤未跌穿
    assert assess_intraday_conflict({}, **{**kw, "bar_close": 164.0}) == []  # 陽線
    assert assess_intraday_conflict({}, **{**kw, "lvn": None}) == []
    assert assess_intraday_conflict({}, **{**kw, "lvn": 0.0}) == []
    assert assess_intraday_conflict({}, bar_open=163.0, bar_close=160.9) == []


# --- B2 ---------------------------------------------------------------------


def _bounce(color: str, rvol: float = 0.55) -> dict[str, Any]:
    d = _spcx(
        open_15m=280.0, close_15m=282.0, rvol_15m=rvol, volume_15m=rvol * 1_000_000
    )
    d["squeeze_eval"].matrix = {"15m": _tf(-5.19, color)}
    return d


def test_b2_bear_flag() -> None:
    assert "無量反彈＋15m 空方動能加速（疑似熊旗）" in _render(_bounce("Red"))


def test_b2_decelerating_keeps_original_text() -> None:
    text = _render(_bounce("Golden"))
    assert "疑似熊旗" not in text and "缺乏放量代償" in text


def test_b2_rvol_above_threshold_keeps_original_text() -> None:
    text = _render(_bounce("Red", rvol=0.8))
    assert "疑似熊旗" not in text and "缺乏放量代償" in text


# --- B3 ---------------------------------------------------------------------


def _skew(pct: float, pcr: float, dp: float = -0.5) -> dict[str, Any]:
    return _spcx(
        skew=3.0, skew_percentile=pct,
        quote={"c": 160.9, "d": dp, "dp": dp, "pc": 161.4},
        pcr={"volume_pcr": pcr, "oi_pcr": 0.9},
    )  # fmt: skip


def test_b3_high_skew_not_labelled_sync() -> None:
    text = _render(_skew(93.0, 0.70))
    assert "未達結構背離門檻（Skew 93% 高避險 vs Vol PCR 0.70≥0.40）" in text
    assert "勿以 PCR 單獨判多" in text
    assert "狀態: \u001b[1;32m同步" not in text


def test_b3_pcr_high_only_label_no_action() -> None:
    text = _render(_skew(93.0, 1.00))
    assert "未達結構背離門檻" in text and "勿以 PCR 單獨判多" not in text


def test_b3_lower_skew_stays_sync() -> None:
    text = _render(_skew(80.0, 0.70))
    assert "同步" in text and "未達結構背離門檻" not in text


def test_b3_premarket_zero_pcr_excluded() -> None:
    text = _render(_skew(93.0, 0.0))
    assert "未達結構背離門檻" not in text


# --- B4 ---------------------------------------------------------------------


def _mp(d: dict[str, Any], nxt: float) -> dict[str, Any]:
    d["month_max_pains"] = [
        {"expiry": "2026-10-10", "max_pain": 162.5, "distance_pct": 1.0},
        {"expiry": "2026-10-17", "max_pain": nxt, "distance_pct": -6.0},
    ]
    return d


def _fri_close_prior() -> datetime:
    from market_time import ny_tz

    return datetime(2026, 10, 9, 12, 0, tzinfo=ny_tz)  # 10-10 為 DTE 1


def test_b4_shift_overlay() -> None:
    text = _render(_mp(_spcx(), 150.0), _fri_close_prior())
    assert "結算後重心轉移：10-10 痛點 $162.50 → 10-17 $150.00 (-7.8%)" in text
    assert "結算轉移:" in text


def test_b4_small_gap_no_overlay() -> None:
    text = _render(_mp(_spcx(), 159.7), _fri_close_prior())  # -1.7%
    assert "重心轉移" not in text


def test_b4_front_far_no_overlay() -> None:
    from market_time import ny_tz

    text = _render(_mp(_spcx(), 150.0), datetime(2026, 10, 7, 12, 0, tzinfo=ny_tz))
    assert "重心轉移" not in text


# --- B5 ---------------------------------------------------------------------


def test_b5_recorder_new_keys() -> None:
    from market_analysis import evaluation_recorder as rec

    captured: list[dict[str, Any]] = []
    with patch.object(rec, "_append", side_effect=captured.append):
        rec.record_squeeze_entry(
            "SPCX", 160.9, SimpleNamespace(status="ENTRY", tier=3),
            {"65m": _tf(-8.674, "Red"), "15m": _tf(1.234, "Red")},
        )  # fmt: skip
    f = captured[0]["features_json"]
    assert f["65m_mv"] == -8.67 and f["15m_mv"] == 1.23
    assert f["intraday_conflict"] is True


def test_b5_recorder_missing_tf_is_none() -> None:
    from market_analysis import evaluation_recorder as rec

    captured: list[dict[str, Any]] = []
    with patch.object(rec, "_append", side_effect=captured.append):
        rec.record_squeeze_entry("X", 1.0, SimpleNamespace(status="WATCH"), {})
    f = captured[0]["features_json"]
    assert f["65m_mv"] is None and f["intraday_conflict"] is False


def test_b5_default_flag_equivalence_and_shadow() -> None:
    from market_analysis.index_microstructure import detect_uoa_sto_call_physical_cap

    credit_leg: dict[str, Any] = {
        "type": "CALL", "action": "🔴 賣出開倉 (STO - Bid)", "strike": 290.0,
        "ratio": 2.0, "spread_role": "SHORT_LEG", "spread_credit": True,
    }  # fmt: skip
    debit_leg = {**credit_leg, "strike": 295.0, "spread_credit": False}
    plain = {**credit_leg, "strike": 300.0, "spread_role": None}
    cases: list[list[Any]] = [
        [],
        [credit_leg],
        [debit_leg],
        [plain],
        [credit_leg, plain],
    ]
    for uoa in cases:
        old = detect_uoa_sto_call_physical_cap(uoa, 280.0, 1.5, wall_reference=None)
        assert old == detect_uoa_sto_call_physical_cap(
            uoa, 280.0, 1.5, wall_reference=None, include_credit_short_legs=False
        )
    assert detect_uoa_sto_call_physical_cap([credit_leg], 280.0, 1.5) == (False, 0.0)
    assert detect_uoa_sto_call_physical_cap(
        [credit_leg], 280.0, 1.5, include_credit_short_legs=True
    ) == (True, 290.0)
    assert detect_uoa_sto_call_physical_cap(
        [debit_leg], 280.0, 1.5, include_credit_short_legs=True
    ) == (False, 0.0)


def test_b5_shadow_features_not_in_classification() -> None:
    from market_analysis.dynamic_rollover import regime_classifier as rc

    credit_leg = {
        "type": "CALL", "action": "🔴 賣出開倉 (STO - Bid)", "strike": 290.0,
        "ratio": 2.0, "spread_role": "SHORT_LEG", "spread_credit": True,
    }  # fmt: skip
    gex = {"call_wall": 285.0, "put_wall": 270.0, "gex_profile": {}}
    assert rc._sto_cap_shadow_features(280.0, gex, [credit_leg]) == {
        "sto_cap_base_strike": None,
        "sto_cap_shadow_strike": 290.0,
    }
    assert rc._sto_cap_shadow_features(0.0, gex, [credit_leg]) is None
    assert rc._sto_cap_shadow_features(280.0, None, [credit_leg]) is None

    captured: dict[str, Any] = {}

    def _fake_record(*a: Any, **k: Any) -> None:
        captured.update(k)

    async def _run() -> Any:
        with patch(
            "market_analysis.evaluation_recorder.record_regime_classification",
            _fake_record,
        ):
            return await rc.classify_dynamic_regime("X", 280.0, gex, [credit_leg])

    regime, _, _ = asyncio.run(_run())
    assert captured["extra_features"]["sto_cap_shadow_strike"] == 290.0

    # 影子值不得改變分類：與不含該腿時相同
    async def _run2() -> Any:
        with patch(
            "market_analysis.evaluation_recorder.record_regime_classification",
            _fake_record,
        ):
            return await rc.classify_dynamic_regime("X", 280.0, gex, [])

    regime2, _, _ = asyncio.run(_run2())
    assert regime == regime2


def test_b5_record_regime_extra_features_merged() -> None:
    from market_analysis import evaluation_recorder as rec

    captured: list[dict[str, Any]] = []
    with patch.object(rec, "_append", side_effect=captured.append):
        rec.record_regime_classification(
            "X", 10.0, "REGIME_II_CHAOS_STANDASIDE", "r", {},
            extra_features={"sto_cap_shadow_strike": 290.0},
        )  # fmt: skip
        rec.record_regime_classification(
            "X", 10.0, "REGIME_II_CHAOS_STANDASIDE", "r", {}
        )
    assert captured[0]["features_json"]["sto_cap_shadow_strike"] == 290.0
    assert "sto_cap_shadow_strike" not in captured[1]["features_json"]


# --- 字數：A、B 全部新增行同時觸發 ------------------------------------------------


def _total_chars(embed: Any) -> int:
    total = len(embed.title or "") + len(embed.description or "")
    if embed.footer and embed.footer.text:
        total += len(embed.footer.text)
    return total + sum(len(f.name or "") + len(f.value or "") for f in embed.fields)


# PR-A 之後的 MRVL fixture 總字數基準；PR-B 淨增須 ≤ +120。
_MRVL_BASELINE_CHARS_AFTER_A = 4100


def test_mrvl_net_increase_within_120_chars() -> None:
    from tests.unit.test_x_panel_embed_budget import _mrvl_data

    total = _total_chars(_render_embed(_mrvl_data()))
    assert total - _MRVL_BASELINE_CHARS_AFTER_A <= 120, total


def test_worst_case_all_new_lines_within_budget() -> None:
    from market_time import ny_tz
    from tests.unit.test_x_panel_embed_budget import _mrvl_data

    d = _mrvl_data()
    # --- PR-A：紙牆、貼牆、STO CALL 封頂行、STO PUT 跌破行
    d["gex_profile_data"]["gex_profile"]["270.0"] = 200_000
    d["gex_profile_data"]["call_wall"] = 284.9
    d["gex_profile_data"]["gex_profile"]["285.0"] = 522188798
    d["sto_physical_cap_strikes"] = d["sto_physical_cap_strikes"] + [
        {
            "strike": 286.0, "type": "CALL", "expiry": "2026-10-16", "volume": 1200,
            "oi": 3000, "ratio": 0.4, "notional_value": 1_200_000.0,
            "structure_leg": True, "spread_credit": True,
        },
        {
            "strike": 287.0, "type": "PUT", "expiry": "2026-10-09", "volume": 46000,
            "oi": 50000, "ratio": 0.9, "notional_value": 9_000_000.0,
        },
    ]  # fmt: skip
    # --- PR-B：B1 三行、B2 熊旗、B3 Skew 呈現、B4 轉移 overlay
    d["squeeze_eval"] = SimpleNamespace(
        matrix={"65m": _tf(-8.67, "Red"), "15m": _tf(-5.19, "Red")},
        result=SimpleNamespace(
            status="ENTRY", tier=3, size_pct=5, stop=240.0, triggers=["D Green Dot"],
            reason="T3", resistance_warning=None,
        ),
    )  # fmt: skip
    d.update(
        open_15m=288.0, close_15m=284.0, high_15m=288.5, low_15m=283.9,
        volume_15m=2_820_000, volume_15m_sma20=1_000_000, rvol_15m=2.82,
        volume_profile={"hvn": 290.0, "lvn": 285.5},
        skew=3.0, skew_percentile=93.0,
        pcr={"volume_pcr": 0.7, "oi_pcr": 0.9},
    )  # fmt: skip
    d["month_max_pains"] = [
        {"expiry": "2026-10-08", "max_pain": 285.0, "distance_pct": 0.1},
        {"expiry": "2026-10-16", "max_pain": 250.0, "distance_pct": 13.87},
        {"expiry": "2026-10-23", "max_pain": 280.0, "distance_pct": 1.67},
        {"expiry": "2026-10-30", "max_pain": 260.0, "distance_pct": 9.49},
    ]  # fmt: skip
    embed = _render_embed(d, datetime(2026, 10, 7, 12, 0, tzinfo=ny_tz))
    text = "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)
    assert any(GEX_FIELD in (f.name or "") for f in embed.fields)
    assert len(embed.to_dict()["fields"]) == len(embed.fields), "欄位被靜默 pop"
    for needle in (
        "〔紙牆", "貼牆", "STO CALL 1,200口", "已跌破(價內)", "日內衝突",
        "⚠ 上檔: 距 CallWall", "超寬", "結算後重心轉移", "未達結構背離門檻",
    ):  # fmt: skip
        assert needle in text, needle
    assert _total_chars(embed) <= 5600, _total_chars(embed)
