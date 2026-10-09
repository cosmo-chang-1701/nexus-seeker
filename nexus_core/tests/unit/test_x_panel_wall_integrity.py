"""/x 牆體真偽與盈虧比（PR-A）：助跌／紙牆／實牆、ADV 注入、貼牆、STO 封頂與跌破。

GEX 區塊整段包在 try/except，錯誤只會讓欄位消失，故每例都斷言欄位存在。
"""

from typing import Any

import pandas as pd

from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed
from cogs.unified_terminal.symbol_deep_dive import _compute_adv_dollar_20d

GEX_FIELD = "🧲 Gamma 曝險分布"


def _render(data: dict[str, Any]) -> str:
    embed = create_tactical_symbol_embed(data)
    assert any(GEX_FIELD in (f.name or "") for f in embed.fields), "GEX 欄位消失"
    return "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)


def _case(pw_net: float, key: str = "150.0", **extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "symbol": "SPCX",
        "price": 160.0,
        "atr_1d": 3.0,
        "atr_15m": 0.8,
        "gex_profile_data": {
            "spot": 160.0,
            "put_wall": 150.0,
            "call_wall": 175.0,
            "net_gex": 5_000_000.0,
            "gex_profile": {
                key: pw_net,
                "155.0": 90_000_000.0,
                "160.0": 1_000_000.0,
                "175.0": 3_000_000.0,
            },
        },
    }
    d.update(extra)
    return d


def test_negative_putwall_is_flagged_inline() -> None:
    text = _render(_case(-30_000_000.0))
    assert "PutWall: $150.00〔淨GEX" in text and "實為助跌區" in text
    assert "參考停損 (淨 GEX 支撐" in text
    assert "PutWall 為助跌區，閘門仍以 PutWall 為準" in text


def test_paper_wall_is_flagged_and_gets_alt_stop() -> None:
    text = _render(_case(200_000.0))  # +$2K < 門檻 $5K
    assert "〔紙牆：淨GEX" in text
    assert "參考停損 (淨 GEX 支撐" in text
    assert "PutWall 為紙牆" in text


def test_solid_wall_has_no_flag() -> None:
    text = _render(_case(80_000_000.0))
    assert "紙牆" not in text and "助跌區" not in text
    assert "參考停損 (淨 GEX 支撐" not in text


def test_integer_string_key_is_not_paper_wall() -> None:
    """鍵為 "150"（整數字串）時須正確讀值，不得被當成 +0K 紙牆。"""
    for key in ("150", "150.0"):
        text = _render(_case(80_000_000.0, key=key))
        assert "紙牆" not in text, key


def test_paper_wall_kelly_precondition_fails() -> None:
    from tests.unit.test_x_panel_gex_uoa_coherence import _mrvl_data

    d = _mrvl_data()
    d["gex_profile_data"]["gex_profile"]["270.0"] = 200_000  # 紙牆
    text = _render(d)
    assert "〔紙牆：淨GEX" in text
    assert "牆淨GEX❌" in text


def test_degraded_too_tight_marks_inflated_ratio() -> None:
    """TOO_TIGHT：盈虧比不得單獨印 ✅（旗標改由 put_too_tight 驅動）。

    降級分支（min_pct 為 None）要求 ATR₁₅ₘ 無效，此時停損行本身不輸出
    （atr_15m_val<=0），盈虧比區塊亦不會出現，故以有 min_pct 的路徑驗證旗標。
    """
    d = _case(80_000_000.0, price=150.4, atr_1d=3.0, atr_15m=0.5)
    d["gex_profile_data"].update({"spot": 150.4, "put_wall": 150.0, "call_wall": 200.0})
    d["gex_profile_data"]["gex_profile"]["200.0"] = 3_000_000.0
    text = _render(d)
    assert "短線盈虧比" in text
    assert ":1 ⚠ 停損過窄、比值虛高" in text
    primary = next(ln for ln in text.splitlines() if "短線盈虧比" in ln)
    assert primary.split("合格停損")[0].count("✅") == 0


def test_callwall_hug_caps_upside_and_skips_ratio() -> None:
    d = _case(80_000_000.0, price=372.48, atr_15m=1.2, atr_1d=8.0)
    d["gex_profile_data"].update(
        {
            "spot": 372.48,
            "put_wall": 360.0,
            "call_wall": 372.5,
            "gex_profile": {
                "360.0": 80_000_000.0,
                "370.0": -1_000_000.0,
                "372.5": 3_000_000.0,
            },
        }
    )
    text = _render(d)
    assert "📌 貼牆(<1×ATR₁₅ₘ)" in text
    assert "上檔已封頂" in text
    assert ":1 ✅" not in text


def test_credit_spread_sto_call_cap_line() -> None:
    d = _case(80_000_000.0)
    d["gex_profile_data"]["call_wall"] = 175.0
    d["sto_physical_cap_strikes"] = [
        {
            "strike": 172.0, "type": "CALL", "expiry": "2026-10-16", "volume": 1200,
            "notional_value": 1_200_000.0, "structure_leg": True, "spread_credit": True,
        }
    ]  # fmt: skip
    d["uoa"] = [
        {
            "expiry": "2026-10-16", "strike": 172.0, "type": "CALL",
            "action": "🔴 賣出開倉 (STO - Bid)", "volume": 1200,
            "spread_label": "熊市價差 (Bear Call Spread) $172/$180",
        }
    ]  # fmt: skip
    text = _render(d)
    assert "STO CALL 1,200口" in text and "封頂〔熊市Call價差 $172/$180〕" in text

    # 非貸方的價差腿不輸出
    d["sto_physical_cap_strikes"][0]["spread_credit"] = False
    assert "STO CALL 1,200口" not in _render(d)


def test_breached_sto_put_line() -> None:
    d = _case(80_000_000.0, price=160.9)
    d["gex_profile_data"]["spot"] = 160.9
    d["sto_physical_cap_strikes"] = [
        {"strike": 162.5, "type": "PUT", "expiry": "2026-10-10", "volume": 46000,
         "notional_value": 2_000_000.0}
    ]  # fmt: skip
    text = _render(d)
    assert "STO PUT $162.50" in text and "46,000口已跌破(價內)" in text

    d["sto_physical_cap_strikes"][0]["strike"] = 158.0
    assert "已跌破(價內)" not in _render(d)


def test_adv_injection_is_shallow_copy_and_guarded() -> None:
    df = pd.DataFrame({"Close": [10.0] * 25, "Volume": [1000.0] * 25})
    assert _compute_adv_dollar_20d(df, 20.0) == 20_000.0
    assert _compute_adv_dollar_20d(df.drop(columns=["Volume"]), 20.0) is None
    nan_df = pd.DataFrame({"Close": [10.0], "Volume": [float("nan")]})
    assert _compute_adv_dollar_20d(nan_df, 10.0) is None
    assert _compute_adv_dollar_20d(None, 10.0) is None
    zero = pd.DataFrame({"Close": [10.0], "Volume": [0.0]})
    assert _compute_adv_dollar_20d(zero, 10.0) is None
