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
    assert "⚠ 日內衝突: 65m 動能-8.67加速向下、15m 放量陰線2.82x破LVN $161.32" in text
    assert "（判定未改）" in text and "日線訊號落後" not in text
    assert "🟢 可進場" in text  # 判定不受影響
    assert "⚠ 超寬（5%倉≈0.80% NAV）" in text


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
    # 不再有獨立的「上檔」行，改於判定行尾附「⚠封頂」
    assert "⚠ 上檔: 距 CallWall" not in text
    verdict = next(ln for ln in text.splitlines() if "判定:" in ln and "可進場" in ln)
    assert verdict.rstrip().endswith("⚠封頂") or "⚠封頂" in verdict


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
    assert assess_intraday_conflict(m) == ["65m 動能-0.01加速向下"]
    # 負但減弱（Golden）不算
    assert assess_intraday_conflict({"65m": _tf(-5.0, "Golden")}) == []
    assert negative_momentum_tfs({"65m": _tf(-5.0, "Golden")}) == [("65m", -5.0)]
    assert negative_momentum_tfs(None) == []
    assert negative_momentum_tfs({"65m": SimpleNamespace()}) == []
    kw: dict[str, Any] = dict(bar_open=163.0, bar_close=160.9, rvol_eff=1.5, lvn=161.32)
    assert assess_intraday_conflict({}, **kw) == ["15m 放量陰線1.50x破LVN $161.32"]
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
    assert "未達背離門檻（Skew 93% 高避險／PCR 0.70）" in text
    assert "勿以 PCR 單獨判多" in text
    assert "狀態: \u001b[1;32m同步" not in text


def test_b3_pcr_high_only_label_no_action() -> None:
    text = _render(_skew(93.0, 1.00))
    assert "未達背離門檻" in text and "勿以 PCR 單獨判多" not in text


def test_b3_lower_skew_stays_sync() -> None:
    text = _render(_skew(80.0, 0.70))
    assert "同步" in text and "未達背離門檻" not in text


def test_b3_premarket_zero_pcr_excluded() -> None:
    text = _render(_skew(93.0, 0.0))
    assert "未達背離門檻" not in text


def test_b3_premarket_iv_suppresses_label() -> None:
    """盤前（iv_data.is_premarket）時 Target Lock 的 PCR 顯示為「--」，B3 也不得出現。"""
    from tests.unit.test_x_panel_embed_budget import _mrvl_data

    d = _mrvl_data()  # fixture 的 IV 為盤前
    d.update(skew=3.0, skew_percentile=93.0, pcr={"volume_pcr": 0.7, "oi_pcr": 0.9})
    assert "未達背離門檻" not in _render(d)
    object.__setattr__(d["iv_data"], "is_premarket", False)
    assert "未達背離門檻" in _render(d)


def test_b3_uses_named_pcr_constants() -> None:
    from cogs.embed_builders import portfolio_embeds as pe

    assert pe._PCR_DIVERGENCE_LOW == 0.40 and pe._PCR_BULLISH_NEUTRAL == 0.90
    # PCR 剛好 0.40：不屬 High Divergence（< 0.40），應落入 B3 標示
    assert "未達背離門檻" in _render(_skew(93.0, 0.40))
    # 0.39 為 High Divergence（既有判定），不得被 B3 取代
    text = _render(_skew(93.0, 0.39))
    assert "未達背離門檻" not in text and "結構性情緒背離" in text


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
    assert "重心轉移：10-10 $162.50 → 10-17 $150.00 (-7.8%)，結算後釘住失效" in text
    assert "結算轉移:" in text


def test_b4_small_gap_no_overlay() -> None:
    text = _render(_mp(_spcx(), 159.7), _fri_close_prior())  # -1.7%
    assert "重心轉移" not in text


def test_b4_front_far_no_overlay() -> None:
    from market_time import ny_tz

    text = _render(_mp(_spcx(), 150.0), datetime(2026, 10, 7, 12, 0, tzinfo=ny_tz))
    assert "重心轉移" not in text


def test_b4_invalid_middle_row_is_filtered_first() -> None:
    from cogs.embed_builders.portfolio_embeds import _settlement_shift_overlay
    from market_time import ny_tz

    now = _fri_close_prior()

    def _call(rows: list[Any]) -> Any:
        class _FakeDT(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> "_FakeDT":
                return now  # type: ignore[return-value]

        with patch("cogs.embed_builders.portfolio_embeds.datetime", _FakeDT):
            return _settlement_shift_overlay(rows, 160.9)

    assert ny_tz  # 與其他案例同一時區來源
    rows = [
        {"expiry": "2026-10-10", "max_pain": 285.0},
        {"expiry": "2026-10-12", "max_pain": None},
        {"expiry": "2026-10-17", "max_pain": 250.0},
    ]
    # 無效列先濾掉：次檔取 10-17，而非被 None 擋成 no-op
    assert _call(rows) is not None
    # NaN 同樣被濾掉
    nan_rows = [
        {"expiry": "2026-10-10", "max_pain": float("nan")},
        {"expiry": "2026-10-17", "max_pain": 250.0},
    ]
    assert _call(nan_rows) is None  # 只剩一檔，且第一檔 DTE 已超過上限不誤判
    # 無效的即期痛點被濾掉後，剩下的第一檔 DTE>1 → 不提示
    far = [
        {"expiry": "2026-10-10", "max_pain": None},
        {"expiry": "2026-10-17", "max_pain": 250.0},
        {"expiry": "2026-10-24", "max_pain": 200.0},
    ]
    assert _call(far) is None


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
    # 舊行為的具體案例（預設與明確傳 False 都必須相同）：SHORT_LEG 一律跳過
    expected: list[tuple[list[Any], tuple[bool, float]]] = [
        ([], (False, 0.0)),
        ([credit_leg], (False, 0.0)),
        ([debit_leg], (False, 0.0)),
        ([plain], (True, 300.0)),
        ([credit_leg, plain], (True, 300.0)),
    ]
    for uoa, want in expected:
        assert detect_uoa_sto_call_physical_cap(uoa, 280.0, 1.5) == want
        assert (
            detect_uoa_sto_call_physical_cap(
                uoa, 280.0, 1.5, include_credit_short_legs=False
            )
            == want
        )
    # 納入貸方腿時，最先命中的是列表順序中的第一筆
    assert detect_uoa_sto_call_physical_cap(
        [credit_leg, plain], 280.0, 1.5, include_credit_short_legs=True
    ) == (True, 290.0)
    assert detect_uoa_sto_call_physical_cap([credit_leg], 280.0, 1.5) == (False, 0.0)
    assert detect_uoa_sto_call_physical_cap(
        [credit_leg], 280.0, 1.5, include_credit_short_legs=True
    ) == (True, 290.0)
    assert detect_uoa_sto_call_physical_cap(
        [debit_leg], 280.0, 1.5, include_credit_short_legs=True
    ) == (False, 0.0)


_CREDIT_LEG: dict[str, Any] = {
    "type": "CALL", "action": "🔴 賣出開倉 (STO - Bid)", "strike": 330.0,
    "ratio": 2.0, "spread_role": "SHORT_LEG", "spread_credit": True,
}  # fmt: skip
_FAR_GEX: dict[str, Any] = {
    "call_wall": 320.0, "put_wall": 270.0, "net_gex": 1e9,
    "gex_profile": {"270.0": 5e7, "280.0": 1e7, "320.0": 4e7},
}  # fmt: skip


def _run_classifier(uoa: list[Any], recording: bool) -> tuple[Any, list[Any]]:
    """離線跑 classify_dynamic_regime：所有外部抓取皆 mock，並記錄 STO 封頂呼叫。"""
    import pandas as pd

    from market_analysis.dynamic_rollover import regime_classifier as rc
    from market_analysis.index_microstructure import (
        detect_uoa_sto_call_physical_cap as real_detect,
    )

    calls: list[dict[str, Any]] = []

    def _spy(*a: Any, **k: Any) -> Any:
        calls.append(k)
        return real_detect(*a, **k)

    async def _normal() -> str:
        return "NORMAL"

    async def _vts() -> dict[str, float]:
        return {"vts_ratio": 0.0}

    async def _atr(sym: str) -> float:
        return 5.0

    async def _high(sym: str) -> float:
        return 0.0

    async def _hist(*a: Any, **k: Any) -> Any:
        return pd.DataFrame()

    async def _go() -> Any:
        return await rc.classify_dynamic_regime(
            "X", 280.0, dict(_FAR_GEX), uoa, df_15m=pd.DataFrame()
        )

    with (
        patch("market_analysis.index_microstructure.get_market_regime", _normal),
        patch("services.market_data_service.get_vix_term_structure", _vts),
        patch("services.market_data_service.get_history_df", _hist),
        patch("market_analysis.atr_utils.fetch_atr_1d", _atr),
        patch("market_analysis.atr_utils.fetch_high_60d", _high),
        patch(
            "market_analysis.index_microstructure.detect_uoa_sto_call_physical_cap",
            _spy,
        ),
        patch(
            "market_analysis.evaluation_recorder.recording_active", lambda: recording
        ),
        patch("market_analysis.evaluation_recorder.record_regime_classification"),
    ):
        result = asyncio.run(_go())
    return result, calls


def test_b5_shadow_features_values() -> None:
    from market_analysis.dynamic_rollover import regime_classifier as rc

    gex = {"call_wall": 285.0}
    assert rc._sto_cap_shadow_features(280.0, gex, [_CREDIT_LEG]) == {
        "sto_cap_base_strike": None,
        "sto_cap_shadow_strike": 330.0,
        "sto_cap_shadow_changes_decision": True,
    }
    plain = {**_CREDIT_LEG, "spread_role": None, "strike": 300.0}
    assert rc._sto_cap_shadow_features(280.0, gex, [plain, _CREDIT_LEG]) == {
        "sto_cap_base_strike": 300.0,
        "sto_cap_shadow_strike": 300.0,
        "sto_cap_shadow_changes_decision": False,
    }
    assert rc._sto_cap_shadow_features(0.0, gex, [_CREDIT_LEG]) is None
    assert rc._sto_cap_shadow_features(280.0, None, [_CREDIT_LEG]) is None
    with patch(
        "market_analysis.index_microstructure.detect_uoa_sto_call_physical_cap",
        side_effect=RuntimeError("boom"),
    ):
        assert rc._sto_cap_shadow_features(280.0, gex, [_CREDIT_LEG]) == {
            "sto_cap_shadow_error": True
        }


def test_b5_classification_unchanged_by_shadow() -> None:
    """影子判定啟用時，分類結果與不含該貸方腿時完全相同，且 impl 不帶影子旗標。"""
    from market_analysis.dynamic_rollover.models import DynamicRegime

    (with_leg, _, _), calls_with = _run_classifier([_CREDIT_LEG], recording=True)
    (without, _, _), _ = _run_classifier([], recording=True)
    assert with_leg == without
    assert with_leg != DynamicRegime.REGIME_IV_STRUCTURAL_CAP_CRISIS
    # 依序為：impl（不帶旗標）、影子基準值（不帶旗標）、影子值（帶旗標）
    assert len(calls_with) == 3
    assert "include_credit_short_legs" not in calls_with[0]
    assert "include_credit_short_legs" not in calls_with[1]
    assert calls_with[2].get("include_credit_short_legs") is True


def test_b5_shadow_skipped_when_recorder_inactive() -> None:
    _, calls = _run_classifier([_CREDIT_LEG], recording=False)
    assert len(calls) == 1 and "include_credit_short_legs" not in calls[0]


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


def _worst_case_data(extreme: bool = False) -> dict[str, Any]:
    """A、B 全部新增行＋既有條件行同時成立。

    既有條件行：IV Rank 高、結算引力 overlay、0 口原因、時框建議、兩條 Kelly 風控
    警示、兩個 triggers、resistance_warning。extreme=True 再加財報與 3 筆總經事件、
    每口 Delta 行、較長的 UOA intent——此時總字數會超過 5800（見極端案例測試）。
    """
    from market_analysis.risk_engine import OptimizationResult
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
    # --- PR-B：B1 兩行、B2 熊旗、B3 Skew 呈現、B4 轉移 overlay
    d["squeeze_eval"] = SimpleNamespace(
        matrix={"65m": _tf(-8.67, "Red"), "15m": _tf(-5.19, "Red")},
        result=SimpleNamespace(
            status="ENTRY", tier=3, size_pct=5, stop=240.0,
            triggers=["D Green Dot", "W 擠壓釋放"],
            reason="T3",
            resistance_warning="⚠ 上方 $290–$295 為自動壓力區，距離 0.4%，進場空間不足",
        ),
    )  # fmt: skip
    d.update(
        open_15m=288.0, close_15m=284.0, high_15m=288.5, low_15m=283.9,
        volume_15m=2_820_000, volume_15m_sma20=1_000_000, rvol_15m=2.82,
        volume_profile={"hvn": 290.0, "lvn": 285.5},
        skew=3.0, skew_percentile=93.0,
        pcr={"volume_pcr": 0.7, "oi_pcr": 0.9},
        iv_rank=95.0,
    )  # fmt: skip
    if extreme:
        d.update(
            kelly_unit_weighted_delta=420.5, kelly_beta=3.03,
            catalysts=[
                SimpleNamespace(date="2026-10-14", days_to_earnings=6.0),
                SimpleNamespace(
                    time="2026-10-08 14:00", event="FOMC 利率決議", tte_hours=20.0
                ),
                SimpleNamespace(
                    time="2026-10-09 08:30", event="CPI 年增率", tte_hours=44.0
                ),
                SimpleNamespace(
                    time="2026-10-12 08:30", event="PPI", tte_hours=100.0
                ),
            ],
        )  # fmt: skip
        for row in d["uoa"]:
            row["intent"] = (
                "🔗 屬牛市價差 (Bull Call Spread) 買入腿；機構 BTO 跨週期佈局，"
                "疑似為財報前方向性押注而非單純避險"
            )
    iv = d["iv_data"]
    object.__setattr__(iv, "is_premarket", False)  # B3 在盤前不顯示
    object.__setattr__(iv, "iv_rank", 95.0)
    object.__setattr__(iv, "has_earnings_event", True)
    object.__setattr__(iv, "earnings_date", "2026-10-14")
    d["kelly_sizing"] = OptimizationResult(
        suggested_contracts=0, exposure_pct=10.0,
        warnings=["VIX 戰情階梯：賣方縮倉 50%", "單一標的集中度偏高，建議分散"],
    )  # fmt: skip
    # 結算引力：第一檔 DTE<=1 且偏離 3%~30%，與 B4 同時成立
    d["month_max_pains"] = [
        {"expiry": "2026-10-08", "max_pain": 270.0, "distance_pct": 5.2},
        {"expiry": "2026-10-16", "max_pain": 250.0, "distance_pct": 13.87},
        {"expiry": "2026-10-23", "max_pain": 280.0, "distance_pct": 1.67},
        {"expiry": "2026-10-30", "max_pain": 260.0, "distance_pct": 9.49},
    ]  # fmt: skip
    return d


def test_worst_case_all_new_lines_within_budget() -> None:
    from market_time import ny_tz

    d = _worst_case_data()
    embed = _render_embed(d, datetime(2026, 10, 7, 12, 0, tzinfo=ny_tz))
    text = "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)
    assert any(GEX_FIELD in (f.name or "") for f in embed.fields)
    assert len(embed.to_dict()["fields"]) == len(embed.fields), "欄位被靜默 pop"
    for needle in (
        "〔紙牆", "貼牆", "STO CALL 1,200口", "已跌破(價內)", "日內衝突",
        "⚠封頂", "超寬", "重心轉移", "未達背離門檻",
        "結算日引力", "IV Rank 極高", "0 口原因", "時框建議",
        "VIX 戰情階梯", "單一標的集中度", "W 擠壓釋放", "自動壓力區",
    ):  # fmt: skip
        assert needle in text, needle
    assert _total_chars(embed) <= 5600, _total_chars(embed)


def test_embed_overflow_logs_warning_with_dropped_field_names(
    caplog: Any,
) -> None:
    import logging

    from cogs.embed_builders._core import NexusEmbed

    e = NexusEmbed(title="t")
    for i in range(6):
        e.add_field(name=f"欄位{i}", value="字" * 1000, inline=False)
    with caplog.at_level(logging.WARNING, logger="cogs.embed_builders._core"):
        d = e.to_dict()
    assert len(d["fields"]) < 6
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "欄位5" in msg and "6" in msg

    caplog.clear()
    small = NexusEmbed(title="t")
    small.add_field(name="a", value="b")
    with caplog.at_level(logging.WARNING, logger="cogs.embed_builders._core"):
        small.to_dict()
    assert not caplog.records


def test_extreme_overflow_only_drops_trailing_uoa(caplog: Any) -> None:
    """再加上財報／總經事件、每口 Delta、長 UOA intent 會超過 5800：

    此時被丟的必須只有尾端的 UOA；GEX 與 Target Lock 欄位保留，且留下警告日誌。
    """
    import logging

    from market_time import ny_tz

    embed = _render_embed(
        _worst_case_data(extreme=True), datetime(2026, 10, 7, 12, 0, tzinfo=ny_tz)
    )
    assert _total_chars(embed) > 5800
    with caplog.at_level(logging.WARNING, logger="cogs.embed_builders._core"):
        kept = embed.to_dict()["fields"]
    names = [f["name"] for f in kept]
    assert any(GEX_FIELD in n for n in names)
    assert any("Target Lock" in n for n in names)
    assert not any("異常活動" in n for n in names)
    assert any("異常活動" in r.getMessage() for r in caplog.records)


def test_b1_verdict_line_gets_cap_suffix_only_for_entry() -> None:
    d = _spcx()
    d["gex_profile_data"]["call_wall"] = 160.95
    d["gex_profile_data"]["gex_profile"]["160.95"] = 3_000_000.0
    text = _render(d)
    assert "判定: 🟢 可進場 T3 → 建議部位 5% ⚠封頂" in text
    d["squeeze_eval"].result.status = "WATCH"
    assert "⚠封頂" not in _render(d)


def test_b5_recorder_inactive_without_source() -> None:
    from market_analysis import evaluation_recorder as rec

    assert rec.recording_active() is False
    with rec.evaluation_source("test"):
        with patch.object(rec, "_enabled", return_value=True):
            assert rec.recording_active() is True
        with patch.object(rec, "_enabled", return_value=False):
            assert rec.recording_active() is False


def test_momentum_value_2dp_helper() -> None:
    from market_analysis.squeeze_entry.intraday_conflict import momentum_value_2dp

    assert momentum_value_2dp(_tf(-8.674, "Red")) == -8.67
    assert momentum_value_2dp(_tf(0.0, "Red")) == 0.0
    assert momentum_value_2dp(_tf(float("nan"), "Red")) is None
    assert momentum_value_2dp(None) is None
    assert momentum_value_2dp(SimpleNamespace()) is None


def test_intraday_break_rvol_references_surge_multiplier() -> None:
    from market_analysis.dynamic_rollover.constants import (
        _ENTRY_VOLUME_SURGE_MULTIPLIER,
    )
    from market_analysis.squeeze_entry.intraday_conflict import INTRADAY_BREAK_RVOL

    assert INTRADAY_BREAK_RVOL == _ENTRY_VOLUME_SURGE_MULTIPLIER
