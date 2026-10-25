"""舊快取（無 intent）SWEEP 文案：不歸因機構、不斷言意圖，且不得比舊文案更長。"""

from typing import Any

from cogs.embed_builders.scan_embeds.sentiment_scan import _format_uoa_field

DISCLAIMER = "舊快取無法判定意圖"

OLD_CALL = (
    "🔥 [MU] 機構在 $900.00 主動買入 5,187 口 CALL (OI=5,187)，Gamma 逼空火力集中"
)
OLD_PUT = "⚠️ [MU] 機構在 $900.00 急買 5,187 口 PUT (OI=5,187)，恐慌性避險避雷"


def _legacy_item(opt_type: str) -> dict[str, Any]:
    return {
        "symbol": "MU",
        "expiry": "2026-11-20",
        "strike": 900.0,
        "type": opt_type,
        "volume": 5187,
        "oi": 5187,
        "ratio": 1.0,
        "trade_type": "SWEEP",
    }


def _render(opt_type: str) -> str:
    # 表頭含「機構/OI」欄名，僅取資料列（意圖映射所在）
    return _format_uoa_field([_legacy_item(opt_type)]).splitlines()[-1]


def test_legacy_sweep_call_no_institution_claim() -> None:
    text = _render("CALL")
    assert "機構" not in text
    assert "逼空" not in text
    assert DISCLAIMER in text


def test_legacy_sweep_put_no_institution_claim() -> None:
    text = _render("PUT")
    assert "機構" not in text
    assert "避險避雷" not in text
    assert DISCLAIMER in text


def test_legacy_sweep_intent_not_longer_than_old() -> None:
    new_call = "🔥 [MU] 在 $900.00 主動買入 5,187 口 CALL (OI=5,187)，" + DISCLAIMER
    new_put = "⚠️ [MU] 在 $900.00 急買 5,187 口 PUT (OI=5,187)，" + DISCLAIMER
    # 實際渲染結果必須以預期意圖結尾（改動 sentiment_scan 文案會使此測試失敗）
    rendered_call = _render("CALL")
    rendered_put = _render("PUT")
    assert rendered_call.endswith(new_call)
    assert rendered_put.endswith(new_put)
    assert len(new_call) <= len(OLD_CALL)
    assert len(new_put) <= len(OLD_PUT)
