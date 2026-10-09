"""
tests/unit/test_uoa_semantics.py

UOA 戰略意圖語義修正（fixture 取自實盤回報）：
  #12 價內 STO CALL 不得被判為「物理封頂天花板」
  #13 垂直價差／多腿組合不得被拆成單腿誤讀，賣出腿不計入物理封頂閘門
  #14 距現價 < 2.5% 的末日合約是平價 (ATM) 博弈，不是深價內吸籌
  #15 價內外以首次偵測當下的現價判定，股價移動後不得事後改寫
"""

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from market_analysis.index_microstructure import detect_uoa_sto_call_physical_cap
from market_analysis.sentiment.uoa_detector import _process_uoa_candidate_rows
from market_analysis.uoa_telemetry import (
    UOATradeInput,
    annotate_spread_structures,
    check_uoa_moneyness,
    classify_uoa_trade,
)

BTO = "🟢 買入開倉 (BTO - Ask)"
STO = "🔴 賣出開倉 (STO - Bid)"


def _trade(strike: float, opt: str, side: str, volume: int = 5000) -> UOATradeInput:
    exp = (datetime.now().date() + timedelta(days=2)).isoformat()
    bid, ask = 10.0, 11.0
    price = ask if side == "BTO" else bid
    return UOATradeInput(
        expiry=exp,
        strike_price=strike,
        option_type=opt,
        trade_price=price,
        bid_price=bid,
        ask_price=ask,
        volume=volume,
        open_interest=1000,
        symbol="TEST",
    )


# ── #14 價內外分級 ────────────────────────────────────────────


def test_near_spot_contracts_are_atm_not_deep_itm() -> None:
    # MU #2 $1070 CALL @ 1073.7（0.35%）、RKLB $70 CALL @ 71（1.4%）、SNDK $1800 PUT @ 1789（0.59%）
    assert check_uoa_moneyness(True, 1070.0, 1073.7) == "ATM"
    assert check_uoa_moneyness(True, 70.0, 71.0) == "ATM"
    assert check_uoa_moneyness(False, 1800.0, 1789.42) == "ATM"
    # MRNA $175 CALL 偏離 2.4% 仍在 ATM 帶內
    assert check_uoa_moneyness(True, 175.0, 179.3) == "ATM"


def test_itm_requires_high_delta_for_whale_label() -> None:
    assert check_uoa_moneyness(True, 1000.0, 1073.7, delta=0.62) == "ITM_Directional"
    assert (
        check_uoa_moneyness(True, 1000.0, 1073.7, delta=0.90)
        == "ITM_Whale_Accumulation"
    )
    # 無 Delta 時沿用舊定義
    assert check_uoa_moneyness(True, 1000.0, 1073.7) == "ITM_Whale_Accumulation"


def test_atm_bto_intent_is_directional_gamble() -> None:
    res = classify_uoa_trade(_trade(70.0, "CALL", "BTO"), current_price=71.0, delta=0.6)
    assert "ATM 高槓桿方向性看漲博弈" in res.intent
    assert "吸籌" not in res.intent


# ── #12 價內 STO CALL ─────────────────────────────────────────


def test_itm_sto_call_is_not_a_ceiling() -> None:
    # SNDK 第 3 版：現價 1893 賣出 $1800 CALL（深價內）
    res = classify_uoa_trade(_trade(1800.0, "CALL", "STO"), current_price=1893.0)
    assert "備兌鎖利" in res.intent
    assert "天花板" in res.intent and "不構成上方天花板" in res.intent
    assert "物理封頂" not in res.intent


def test_otm_sto_call_is_still_a_ceiling() -> None:
    res = classify_uoa_trade(_trade(2000.0, "CALL", "STO"), current_price=1893.0)
    assert "鎖死上方天花板" in res.intent


def test_itm_sto_put_is_not_a_floor() -> None:
    res = classify_uoa_trade(_trade(1950.0, "PUT", "STO"), current_price=1893.0)
    assert "不構成下方支撐地板" in res.intent


# ── #13 價差配對 ─────────────────────────────────────────────


def _entry(
    strike: float, action: str, volume: int, opt: str = "CALL"
) -> dict[str, Any]:
    return {
        "expiry": "2026-09-25",
        "type": opt,
        "strike": strike,
        "volume": volume,
        "action": action,
        "ratio": 5.0,
        "intent": f"🛡️ 機構在 ${strike} 開倉賣出，物理封頂鎖死上方天花板"
        if "STO" in action
        else f"🔥 在 ${strike} 買入",
    }


def test_sndk_bull_call_spread_is_paired() -> None:
    """SNDK 第 4 版：買 $1800C 3.7k 口 + 賣 $1850C 4.1k 口。"""
    long_leg = _entry(1800.0, BTO, 3700)
    short_leg = _entry(1850.0, STO, 4100)
    entries = [long_leg, short_leg]
    annotate_spread_structures(entries)
    assert short_leg["spread_role"] == "SHORT_LEG"
    assert long_leg["spread_role"] == "LONG_LEG"
    assert "牛市價差 (Bull Call Spread) $1800/$1850" in short_leg["spread_label"]
    assert "物理封頂" not in short_leg["intent"]
    assert "價差獲利上限" in short_leg["intent"]


def test_mu_multi_leg_aggregation() -> None:
    """MU 第 3 版：賣 $1100C 5.4 萬口 vs 買 $1070/$1075/$1080C 合計 5.7 萬口。"""
    short_leg = _entry(1100.0, STO, 54000)
    longs = [
        _entry(1070.0, BTO, 15000),
        _entry(1075.0, BTO, 20000),
        _entry(1080.0, BTO, 22000),
    ]
    annotate_spread_structures([short_leg, *longs])
    assert short_leg["spread_role"] == "SHORT_LEG"
    assert "$1070+$1075+$1080/$1100" in short_leg["spread_label"]
    assert all(leg["spread_role"] == "LONG_LEG" for leg in longs)


def test_mu_two_sided_combo() -> None:
    """MU 第 2 版：賣 $1075C 1.2 萬口，上下各有 $1070C 1.27 萬、$1080C 1.4 萬口買單。"""
    short_leg = _entry(1075.0, STO, 12000)
    lo, hi = _entry(1070.0, BTO, 12700), _entry(1080.0, BTO, 14000)
    annotate_spread_structures([short_leg, lo, hi])
    assert short_leg["spread_role"] == "SHORT_LEG"
    assert "多腿組合" in short_leg["spread_label"]
    # 兩側各自 1:1 配對：買入合計 2.67 萬口 vs 賣出 1.2 萬口，須揭露為淨買入
    assert "買賣量 2.2:1" in short_leg["spread_label"]


def test_mu_put_combo_label_lists_strikes_and_keeps_basis_note() -> None:
    """MU 2026-10-02 盤中：賣 $1080P 1.14 萬口，下方買 $1070/$1075P、上方買 $1085/$1100P。

    舊標籤「$1085~$1100」會被讀成含中間的 $1090/$1095（實為賣出腿）；PUT 賣出腿
    不得用 CALL 的「封頂」語意；價內外判定基準註記不得被改寫截掉。
    """
    note = "（價內外判定基準：首次偵測 09:45 現價 $1097.39）"
    short_leg = _entry(1080.0, STO, 11418, opt="PUT")
    short_leg["intent"] = (
        f"🛡️ 在 $1080 賣出平價 PUT，短線承接現價附近賣壓，非遠端地板{note}"
    )
    short_leg["moneyness_basis_note"] = note
    longs = [
        _entry(1070.0, BTO, 5000, opt="PUT"),
        _entry(1075.0, BTO, 6000, opt="PUT"),
        _entry(1085.0, BTO, 7135, opt="PUT"),
        _entry(1100.0, BTO, 6216, opt="PUT"),
        _entry(1090.0, STO, 9484, opt="PUT"),
    ]
    annotate_spread_structures([short_leg, *longs])
    label = short_leg["spread_label"]
    assert "買 $1070+$1075、$1085+$1100" in label
    assert "~" not in label
    assert "封頂" not in short_leg["intent"]
    assert "非機構獨立承接地板" in short_leg["intent"]
    assert short_leg["intent"].endswith(note)


def test_unmatched_volume_is_not_paired() -> None:
    short_leg = _entry(1100.0, STO, 50000)
    long_leg = _entry(1070.0, BTO, 5000)
    annotate_spread_structures([short_leg, long_leg])
    assert "spread_role" not in short_leg


def test_different_expiry_is_not_paired() -> None:
    short_leg = _entry(1850.0, STO, 4000)
    long_leg = _entry(1800.0, BTO, 4000)
    long_leg["expiry"] = "2026-10-02"
    annotate_spread_structures([short_leg, long_leg])
    assert "spread_role" not in short_leg


def test_spread_short_leg_does_not_trigger_physical_cap() -> None:
    short_leg = _entry(1850.0, STO, 4100)
    annotate_spread_structures([_entry(1800.0, BTO, 3700), short_leg])
    assert detect_uoa_sto_call_physical_cap([short_leg], 1800.0) == (False, 0.0)
    lone = _entry(1850.0, STO, 4100)
    assert detect_uoa_sto_call_physical_cap([lone], 1800.0) == (True, 1850.0)


# ── #15 首次偵測現價錨點 ─────────────────────────────────────


def _chain_row() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "strike": 1800.0,
                "option_type": "CALL",
                "volume": 2000.0,
                "openInterest": 500.0,
                "lastPrice": 91.0,
                "bid": 89.0,
                "ask": 91.0,
                "impliedVolatility": 0.6,
            }
        ]
    )


def test_moneyness_is_anchored_to_first_detection_spot() -> None:
    """SNDK 第 1→2 版：$1800 CALL 在現價 1700 時為價外投機，漲到 1881 後不得改寫為價內吸籌。"""
    today = datetime.now().date()
    exp = (today + timedelta(days=7)).isoformat()

    first = _process_uoa_candidate_rows(
        "SNDK", exp, today, _chain_row(), 1e9, 1700.0, 5e8
    )
    assert "OTM 投機性看漲" in first[0]["intent"]

    later = _process_uoa_candidate_rows(
        "SNDK", exp, today, _chain_row(), 1e9, 1881.0, 5e8
    )
    assert "OTM 投機性看漲" in later[0]["intent"]
    assert "吸籌" not in later[0]["intent"]
    assert later[0]["classified_spot"] == 1700.0
    assert "首次偵測" in later[0]["intent"]


def test_pre_market_detection_does_not_anchor_moneyness() -> None:
    """MU 2026-10-02：08:46 ET 盤前偵測以前收 $1097.39 建錨，盤中成交全被以前收判價內外。

    非正規時段 (anchor_enabled=False) 不得寫入錨點；開盤後首次偵測才建錨。
    """
    today = datetime.now().date()
    exp = (today + timedelta(days=7)).isoformat()

    pre = _process_uoa_candidate_rows(
        "SNDK", exp, today, _chain_row(), 1e9, 1700.0, 5e8, anchor_enabled=False
    )
    assert pre[0]["classified_spot"] == 1700.0

    opened = _process_uoa_candidate_rows(
        "SNDK", exp, today, _chain_row(), 1e9, 1881.0, 5e8, anchor_enabled=True
    )
    assert opened[0]["classified_spot"] == 1881.0
    assert "首次偵測" not in opened[0]["intent"]

    # 收盤後沿用當日盤中錨點
    after = _process_uoa_candidate_rows(
        "SNDK", exp, today, _chain_row(), 1e9, 1850.0, 5e8, anchor_enabled=False
    )
    assert after[0]["classified_spot"] == 1881.0
    assert after[0]["moneyness_basis_note"] in after[0]["intent"]


def _leg(typ: str, action: str, vol: int, strike: float = 170.0) -> dict:
    return {
        "expiry": "2026-10-09",
        "strike": strike,
        "type": typ,
        "action": action,
        "volume": vol,
        "symbol": "SPCX",
        "intent": "x",
    }


def test_straddle_sto_pairs_and_marks_short_leg() -> None:
    from market_analysis.uoa_telemetry import annotate_straddle_structures

    entries = [
        _leg("CALL", "🔴 賣出開倉 (STO - Bid)", 24000),
        _leg("PUT", "🔴 賣出開倉 (STO - Bid)", 15000),
    ]
    annotate_straddle_structures(entries)
    assert all(e["structure"] == "STRADDLE" for e in entries)
    assert all(e["spread_role"] == "SHORT_LEG" for e in entries)
    assert "賣出跨式" in entries[0]["intent"]


def test_straddle_not_paired_when_volume_ratio_too_large() -> None:
    from market_analysis.uoa_telemetry import annotate_straddle_structures

    entries = [
        _leg("CALL", "🔴 賣出開倉 (STO - Bid)", 30000),
        _leg("PUT", "🔴 賣出開倉 (STO - Bid)", 10000),
    ]
    annotate_straddle_structures(entries)
    assert not any(e.get("structure") for e in entries)


def test_straddle_opposite_directions_not_paired() -> None:
    from market_analysis.uoa_telemetry import annotate_straddle_structures

    entries = [
        _leg("CALL", "🟢 買入開倉 (BTO - Ask)", 1000),
        _leg("PUT", "🔴 賣出開倉 (STO - Bid)", 1000),
    ]
    annotate_straddle_structures(entries)
    assert not any(e.get("structure") for e in entries)


def test_credit_spread_short_leg_semantics() -> None:
    """貸方價差（Bear Call／Bull Put）賣出腿標 spread_credit；借方與多腿不標。"""
    # Bear Call（貸方）：賣低買高 Call
    s, b = _entry(280.0, STO, 1000), _entry(290.0, BTO, 1000)
    annotate_spread_structures([s, b])
    assert s["spread_credit"] is True
    assert s["spread_role"] == "SHORT_LEG"
    assert "Bear Call Spread" in s["spread_label"]
    assert "賣方看跌上限（貸方價差，定義風險封頂）" in s["intent"]

    # Bull Put（貸方）：賣高買低 Put
    s, b = _entry(100.0, STO, 1000, opt="PUT"), _entry(95.0, BTO, 1000, opt="PUT")
    annotate_spread_structures([s, b])
    assert s["spread_credit"] is True
    assert "Bull Put Spread" in s["spread_label"]
    assert "賣方看守地板（貸方價差，定義風險承接）" in s["intent"]

    # Bull Call（借方）
    s, b = _entry(1850.0, STO, 1000), _entry(1800.0, BTO, 1000)
    annotate_spread_structures([s, b])
    assert s["spread_credit"] is False
    assert "價差獲利上限而非機構獨立封頂" in s["intent"]

    # Bear Put（借方）
    s, b = _entry(95.0, STO, 1000, opt="PUT"), _entry(100.0, BTO, 1000, opt="PUT")
    annotate_spread_structures([s, b])
    assert s["spread_credit"] is False
    assert "Bear Put Spread" in s["spread_label"]
    assert "屬價差下行目標，非機構獨立承接地板" in s["intent"]

    # 多腿組合：維持舊文案
    s = _entry(1075.0, STO, 12000)
    annotate_spread_structures(
        [s, _entry(1070.0, BTO, 12700), _entry(1080.0, BTO, 14000)]
    )
    assert s["spread_credit"] is False
    assert "多腿組合" in s["spread_label"]
