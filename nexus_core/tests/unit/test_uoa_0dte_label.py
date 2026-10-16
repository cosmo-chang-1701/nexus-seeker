"""UOA 意圖文案：0DTE 標示、DTE<=1 深價內不視為吸籌、fallback 不宣稱機構。"""

from typing import Optional

from market_analysis.uoa_telemetry import UOATradeInput, classify_uoa_trade

REF = "2026-10-16"

# base 文案（修改前）；0DTE 列字數必須 <= 這些常數
BASE_OTM_CALL_0DTE = (
    "🔥 [AMD] 在 $550.00 買入 5,187 口價外 CALL "
    "(DTE=0, OI=5,187)，屬 OTM 投機性看漲買盤 (OTM_Speculation)"
)
BASE_FALLBACK_CALL_0DTE = (
    "🔥 [MU] 機構在 $900.00 主動買入 5,187 口"
    " 末日 CALL (DTE=0, OI=5,187)，Gamma 逼空火力集中"
)
BASE_FALLBACK_PUT_0DTE = (
    "⚠️ [MU] 機構在 $900.00 急買 5,187 口" " 末日 PUT (DTE=0, OI=5,187)，恐慌性避險避雷"
)
BASE_WHALE_CALL_0DTE = (
    "🚀 [PLTR] 大額資金在 $150.00 買入 5,187 口深價內 CALL "
    "(DTE=0, OI=5,187, Δ=+0.95)，屬 ITM 機構主力吸籌建倉 (ITM_Whale_Accumulation)"
)
BASE_WHALE_PUT_0DTE = (
    "📉 [PLTR] 大額資金在 $250.00 買入 5,187 口深價內 PUT "
    "(DTE=0, OI=5,187, Δ=-0.95)，屬 ITM 機構主力避險建倉 (ITM_Whale_Accumulation)"
    "｜Whale_Hedge (巨鯨避險)"
)

FORBIDDEN = ("機構", "吸籌", "建倉")


def _trade(
    option_type: str, strike: float, expiry: str, symbol: str = "MU"
) -> UOATradeInput:
    return UOATradeInput(
        strike_price=strike,
        option_type=option_type,
        trade_price=1.0,
        bid_price=0.8,
        ask_price=1.0,
        volume=5187,
        open_interest=5187,
        expiry=expiry,
        symbol=symbol,
    )


def _intent(
    option_type: str,
    strike: float,
    expiry: str,
    symbol: str = "MU",
    current_price: Optional[float] = None,
    delta: Optional[float] = None,
) -> str:
    result = classify_uoa_trade(
        _trade(option_type, strike, expiry, symbol),
        reference_date=REF,
        current_price=current_price,
        delta=delta,
    )
    assert result.action == "🟢 買入開倉 (BTO - Ask)"
    return result.intent


def test_0dte_otm_call_uses_0dte_tag_and_not_longer() -> None:
    intent = _intent("CALL", 550.0, REF, "AMD", current_price=500.0)
    assert "(0DTE, OI=5,187)" in intent
    assert "DTE=0" not in intent
    assert not any(w in intent for w in FORBIDDEN)
    assert "OTM_Speculation" in intent
    assert len(intent) <= len(BASE_OTM_CALL_0DTE)


def test_0dte_fallback_call_has_no_institution_claim() -> None:
    intent = _intent("CALL", 900.0, REF)
    assert "0DTE" in intent
    assert not any(w in intent for w in FORBIDDEN)
    assert len(intent) <= len(BASE_FALLBACK_CALL_0DTE)


def test_0dte_fallback_put_has_no_institution_claim() -> None:
    intent = _intent("PUT", 900.0, REF)
    assert "0DTE" in intent
    assert not any(w in intent for w in FORBIDDEN)
    assert len(intent) <= len(BASE_FALLBACK_PUT_0DTE)


def test_fallback_within_3_days_drops_institution_prefix() -> None:
    intent = _intent("CALL", 900.0, "2026-10-19")  # DTE=3
    assert "DTE=3" in intent
    assert "機構" not in intent
    assert "末日 CALL" in intent


def test_0dte_deep_itm_call_not_accumulation() -> None:
    intent = _intent("CALL", 150.0, REF, "PLTR", current_price=200.0, delta=0.95)
    assert "0DTE" in intent
    assert "深價內 CALL" in intent
    assert not any(w in intent for w in FORBIDDEN)
    assert len(intent) <= len(BASE_WHALE_CALL_0DTE)


def test_0dte_deep_itm_put_not_accumulation() -> None:
    intent = _intent("PUT", 250.0, REF, "PLTR", current_price=200.0, delta=-0.95)
    assert "0DTE" in intent
    assert "深價內 PUT" in intent
    assert not any(w in intent for w in FORBIDDEN)
    assert len(intent) <= len(BASE_WHALE_PUT_0DTE)


def test_dte1_deep_itm_not_accumulation() -> None:
    call = _intent("CALL", 150.0, "2026-10-17", "PLTR", 200.0, 0.95)
    put = _intent("PUT", 250.0, "2026-10-17", "PLTR", 200.0, -0.95)
    for intent in (call, put):
        assert "DTE=1" in intent
        assert not any(w in intent for w in FORBIDDEN)


def test_dte7_deep_itm_unchanged() -> None:
    call = _intent("CALL", 150.0, "2026-10-23", "PLTR", 200.0, 0.95)
    put = _intent("PUT", 250.0, "2026-10-23", "PLTR", 200.0, -0.95)
    assert call == (
        "🚀 [PLTR] 大額資金在 $150.00 買入 5,187 口深價內 CALL "
        "(DTE=7, OI=5,187, Δ=+0.95)，屬 ITM 機構主力吸籌建倉 (ITM_Whale_Accumulation)"
    )
    assert put == (
        "📉 [PLTR] 大額資金在 $250.00 買入 5,187 口深價內 PUT "
        "(DTE=7, OI=5,187, Δ=-0.95)，屬 ITM 機構主力避險建倉 (ITM_Whale_Accumulation)"
        "｜Whale_Hedge (巨鯨避險)"
    )


def test_fallback_dte5_unchanged() -> None:
    call = _intent("CALL", 900.0, "2026-10-21")
    put = _intent("PUT", 900.0, "2026-10-21")
    assert call == (
        "🚀 [MU] 大額資金在 $900.00 買入 5,187 口"
        " 跨週 CALL (DTE=5, OI=5,187)，機構主力吸籌建倉"
    )
    assert put == (
        "📉 [MU] 空頭在 $900.00 主動買入 5,187 口"
        " PUT (DTE=5, OI=5,187)，加碼下行防護"
    )
