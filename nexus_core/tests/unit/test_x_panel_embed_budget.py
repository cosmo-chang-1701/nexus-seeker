"""/x 面板字數預算回歸：MRVL 盤後完整資料不得觸發 5800 字靜默丟欄。

NexusEmbed.to_dict() 超過上限時會從尾端 pop 欄位（最先消失的是 UOA），而
validate_embed() 只看 embed.fields、看不到這個 pop；GEX 區塊又被 try/except 包住，
任何例外只會讓欄位消失。本測試同時守住兩者。
"""

from typing import Any
from unittest.mock import patch

from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed
from market_analysis.risk_engine import OptimizationResult
from models.quant import IVMetrics

_GEX_RAW = {
    250: 21590610, 252.5: -8221435, 255: -20233503, 257.5: -23101766,
    260: -73640973, 262.5: -62235230, 265: -65978673, 267.5: -7433355,
    270: 134385432, 272.5: 36280728, 275: 317887293, 277.5: 206894128,
    280: 557343510, 282.5: 689446112, 285: 522188798, 287.5: 118428987,
    290: 1029272950, 292.5: 81082831, 295: 403482758, 297.5: 80784634,
    300: 722026470,
}  # fmt: skip


def _uoa_row(exp: str, k: float, typ: str, act: str, vol: int, oi: int) -> dict:
    return {
        "expiry": exp, "strike": k, "type": typ, "volume": vol, "oi": oi,
        "ratio": round(vol / oi, 2), "ratio_str": f"{vol / oi:.2f}x",
        "trade_type": "SWEEP", "trade_type_inferred": True,
        "action": act, "trade_price": 1.0, "bid_price": 0.9, "ask_price": 1.1,
        "intent": f"🔗 屬牛市價差 (Bull Call Spread) ${k:.0f}/${k + 5:.0f}的買入腿",
        "notional_value": vol * 100 * 8.5,
    }  # fmt: skip


def _mrvl_data() -> dict[str, Any]:
    iv = IVMetrics(
        symbol="MRVL", current_iv=0.7356940799879191, iv_source="HV_PROXY",
        is_premarket=True, event_loading_applied=True, has_macro_event=True,
        straddle_implied_iv=0.6112018392634956, straddle_expiry="2026-10-16",
        straddle_dte=9, expected_move_weekly=24.0959579930421,
        reference_spot_price=284.68, hv_20=0.5271110245291736,
        term_structure_ratio=1.0327, iv_term_structure_status="Normal",
        iv_history_count=1,
    )  # fmt: skip
    return {
        "symbol": "MRVL",
        "price": 286.0802,
        "quote": {"c": 284.68, "d": -2.33, "dp": -0.8118, "pc": 287.01},
        "iv_data": iv,
        "expected_move_context": {
            "reference_price": 284.68,
            "reference_label": "最新收盤",
        },
        "gex_profile_data": {
            "spot": 284.68, "net_gex": 5416662985.4, "call_wall": 290.0,
            "put_wall": 270.0,
            "gex_profile": {str(float(k)): v for k, v in _GEX_RAW.items()},
        },  # fmt: skip
        "month_max_pains": [
            {"expiry": "2026-10-09", "max_pain": 270.0, "distance_pct": 5.44},
            {"expiry": "2026-10-16", "max_pain": 250.0, "distance_pct": 13.87},
            {"expiry": "2026-10-23", "max_pain": 280.0, "distance_pct": 1.67},
            {"expiry": "2026-10-30", "max_pain": 260.0, "distance_pct": 9.49},
        ],
        "max_pain": 270.0,
        "max_pain_expiry": "2026-10-09",
        "option_expiries": [
            "2026-10-09", "2026-10-16", "2026-10-23", "2026-10-30", "2026-11-06",
        ],  # fmt: skip
        "uoa": [
            _uoa_row("2026-10-09", 280.0, "CALL", "🟢 買入開倉 (BTO - Ask)", 3540, 3947),
            _uoa_row("2026-10-16", 300.0, "CALL", "🔴 賣出開倉 (STO - Bid)", 5249, 16928),
            _uoa_row("2026-10-09", 282.5, "CALL", "🔴 賣出開倉 (STO - Bid)", 3721, 3554),
            _uoa_row("2026-10-09", 290.0, "CALL", "🟢 買入開倉 (BTO - Ask)", 6994, 7016),
            _uoa_row("2026-10-09", 275.0, "CALL", "🔴 賣出開倉 (STO - Bid)", 1995, 3078),
        ],  # fmt: skip
        "sto_physical_cap_strikes": [
            {
                "strike": 257.5 + i * 0.5, "type": "PUT", "expiry": "2026-10-16",
                "volume": 4128 - i * 100, "oi": 4420, "ratio": 0.9,
                "notional_value": 833856.0 - i * 1000,
            }
            for i in range(16)
        ],  # fmt: skip
        "psq_result": {
            "is_squeezing": False, "momentum": 35.69, "squeeze_level": "Release",
            "direction": "Neutral", "vix_momentum_label": "NORMAL",
        },  # fmt: skip
        "kelly_sizing": OptimizationResult(
            suggested_contracts=1, exposure_pct=10.0, warnings=[]
        ),
        "atr_15m": 2.3655,
        "atr_1d": 14.1402,
        "skew": -1.1, "skew_percentile": 40.0, "oi_pcr": 0.9, "vol_pcr": 0.8,
        "spy_price": 777.31, "vix": 15.08, "beta": 3.03,
    }  # fmt: skip


def test_mrvl_after_hours_embed_within_budget() -> None:
    from market_time import ny_tz
    from datetime import datetime

    now = datetime(2026, 10, 7, 21, 14, tzinfo=ny_tz)

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "_FakeDT":
            return now  # type: ignore[return-value]

    with patch("cogs.embed_builders.portfolio_embeds.datetime", _FakeDT):
        embed = create_tactical_symbol_embed(_mrvl_data())

    d = embed.to_dict()
    assert len(d["fields"]) == len(embed.fields), "欄位被 5800 字上限靜默 pop"
    total = len(embed.title or "") + len(embed.description or "")
    if embed.footer and embed.footer.text:
        total += len(embed.footer.text)
    total += sum(len(f.name or "") + len(f.value or "") for f in embed.fields)
    assert total <= 5600, f"總字數 {total} 超過 5600 緩衝線"
    names = [f.name or "" for f in embed.fields]
    assert any("🐋 異常活動" in n for n in names)
    assert any("🧲 Gamma 曝險分布" in n for n in names)


def test_mrvl_without_callwall_within_budget() -> None:
    """沒有 CallWall（R:R 分支不輸出）的一般路徑：同樣不得有欄位被 pop、總字數 ≤ 5600。"""
    from market_time import ny_tz
    from datetime import datetime

    now = datetime(2026, 10, 7, 21, 14, tzinfo=ny_tz)

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "_FakeDT":
            return now  # type: ignore[return-value]

    data = _mrvl_data()
    data["gex_profile_data"]["call_wall"] = 0.0
    with patch("cogs.embed_builders.portfolio_embeds.datetime", _FakeDT):
        embed = create_tactical_symbol_embed(data)

    assert len(embed.to_dict()["fields"]) == len(
        embed.fields
    ), "欄位被 5800 字上限靜默 pop"
    total = len(embed.title or "") + len(embed.description or "")
    if embed.footer and embed.footer.text:
        total += len(embed.footer.text)
    total += sum(len(f.name or "") + len(f.value or "") for f in embed.fields)
    assert total <= 5600, f"總字數 {total} 超過 5600 緩衝線"


def _total_chars(embed: Any) -> int:
    total = len(embed.title or "") + len(embed.description or "")
    if embed.footer and embed.footer.text:
        total += len(embed.footer.text)
    return total + sum(len(f.name or "") + len(f.value or "") for f in embed.fields)


def _render_at_night(data: dict[str, Any]) -> Any:
    from market_time import ny_tz
    from datetime import datetime

    now = datetime(2026, 10, 7, 21, 14, tzinfo=ny_tz)

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "_FakeDT":
            return now  # type: ignore[return-value]

    with patch("cogs.embed_builders.portfolio_embeds.datetime", _FakeDT):
        return create_tactical_symbol_embed(data)


# PR-A（牆體真偽）前的 MRVL fixture 總字數基準；淨增須 ≤ +120。
_MRVL_BASELINE_CHARS = 4100


def test_mrvl_net_increase_within_120_chars() -> None:
    total = _total_chars(_render_at_night(_mrvl_data()))
    assert total - _MRVL_BASELINE_CHARS <= 120, f"淨增 {total - _MRVL_BASELINE_CHARS}"


def test_wall_integrity_worst_case_within_budget() -> None:
    """紙牆＋貼牆＋STO CALL 封頂行＋STO PUT 跌破行同時觸發：不得被 5800 靜默丟欄。"""
    d = _mrvl_data()
    d["gex_profile_data"]["gex_profile"]["270.0"] = 200_000  # 紙牆
    d["gex_profile_data"]["call_wall"] = 284.9  # 貼牆
    d["gex_profile_data"]["gex_profile"]["285.0"] = 522188798
    d["sto_physical_cap_strikes"] = d["sto_physical_cap_strikes"] + [
        {
            "strike": 286.0, "type": "CALL", "expiry": "2026-10-16",
            "volume": 1200, "oi": 3000, "ratio": 0.4,
            "notional_value": 1_200_000.0, "structure_leg": True,
            "spread_credit": True,
        },
        {
            "strike": 287.0, "type": "PUT", "expiry": "2026-10-09",
            "volume": 46000, "oi": 50000, "ratio": 0.9,
            "notional_value": 9_000_000.0,
        },
    ]  # fmt: skip
    embed = _render_at_night(d)
    names = [f.name or "" for f in embed.fields]
    assert any("🧲 Gamma 曝險分布" in n for n in names)
    assert len(embed.to_dict()["fields"]) == len(embed.fields), "欄位被靜默 pop"
    text = "\n".join(f.value or "" for f in embed.fields)
    assert "〔紙牆" in text and "貼牆" in text
    assert "STO CALL 1,200口" in text and "已跌破(價內)" in text
    assert _total_chars(embed) <= 5600
